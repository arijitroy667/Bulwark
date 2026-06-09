"""
Bulwark — MongoDB async client (v3.0).

Provides CRUD operations for simulations, verdicts, and environment_configs
collections using Motor (async MongoDB driver). Creates required indexes on startup.

v3 additions:
- head_sha index for fast idempotency checks (FR-004)
- get_historical_context() — last 5 verdicts for same service (FR-017)
- cancel_in_progress_simulation() — cancels conflicting runs on new push (FR-023)
- simulation_exists_by_sha() — deduplication by head_sha
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

from shared.config import settings

logger = logging.getLogger("bulwark.db")

# Phase 5: bounded retries for MongoDB writes
def with_mongo_retry(max_retries: int = 3, initial_backoff: float = 1.0):
    """Decorator to retry MongoDB operations on transient errors (AutoReconnect, ServerSelectionTimeoutError)."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except (AutoReconnect, ServerSelectionTimeoutError) as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        backoff = initial_backoff * (2 ** attempt)
                        logger.warning(
                            "MongoDB error in %s: %s (attempt %d/%d). Retrying in %.1fs...",
                            func.__name__, exc, attempt + 1, max_retries + 1, backoff
                        )
                        await asyncio.sleep(backoff)
            logger.error(
                "MongoDB error in %s: %s (all retries exhausted)",
                func.__name__, last_exc
            )
            raise last_exc
        return wrapper
    return decorator

# ──────────────────────────────────────────────
# Client singleton
# ──────────────────────────────────────────────

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def get_database() -> AsyncIOMotorDatabase:
    """Return the MongoDB database instance, creating the client if needed."""
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(settings.mongodb_uri)
        _db = _client[settings.mongodb_db_name]
        await _ensure_indexes(_db)
        logger.info(
            "MongoDB connected: %s / %s",
            settings.mongodb_uri,
            settings.mongodb_db_name,
        )
    return _db


async def close_database() -> None:
    """Close the MongoDB client connection."""
    global _client, _db
    if _client is not None:
        _client.close()
        _client = None
        _db = None
        logger.info("MongoDB connection closed")


# ──────────────────────────────────────────────
# Index setup
# ──────────────────────────────────────────────


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create required indexes on all collections."""
    simulations = db["simulations"]
    await simulations.create_indexes([
        IndexModel([("mr_id", ASCENDING)]),
        IndexModel([("head_sha", ASCENDING)]),  # v3: for idempotency and cancellation
        IndexModel([("status", ASCENDING)]),
        IndexModel([("project_id", ASCENDING), ("created_at", DESCENDING)]),
        IndexModel([("service_name", ASCENDING), ("created_at", DESCENDING)]),
        IndexModel([("created_at", DESCENDING)]),  # For time-range queries
        # Phase 5: 90-day TTL — MongoDB automatically deletes documents after
        # 90 days (7776000 seconds). Requires ASCENDING order on the TTL field.
        IndexModel(
            [("created_at", ASCENDING)],
            expireAfterSeconds=7776000,
            name="simulations_ttl_90d",
        ),
    ])

    verdicts = db["verdicts"]
    await verdicts.create_indexes([
        IndexModel([("overall_score", ASCENDING)]),
        IndexModel([("verdict", ASCENDING)]),
        IndexModel([("simulation_id", ASCENDING)], unique=True),
    ])

    env_configs = db["environment_configs"]
    await env_configs.create_indexes([
        IndexModel([("project_id", ASCENDING)]),
    ])

    logger.info("MongoDB indexes ensured")


# ──────────────────────────────────────────────
# Simulation CRUD
# ──────────────────────────────────────────────


@with_mongo_retry()
async def create_simulation(simulation: dict[str, Any]) -> str:
    """Insert a new simulation record. Returns the simulation_id."""
    db = await get_database()
    result = await db["simulations"].insert_one(simulation)
    logger.info("Created simulation: %s", result.inserted_id)
    return str(result.inserted_id)


@with_mongo_retry()
async def update_simulation(simulation_id: str, update: dict[str, Any]) -> None:
    """Update fields on an existing simulation record."""
    db = await get_database()
    await db["simulations"].update_one(
        {"_id": simulation_id},
        {"$set": update},
    )
    logger.debug("Updated simulation %s: %s", simulation_id, list(update.keys()))


async def get_simulation(simulation_id: str) -> dict[str, Any] | None:
    """Fetch a simulation record by ID."""
    db = await get_database()
    return await db["simulations"].find_one({"_id": simulation_id})


async def simulation_exists(simulation_id: str) -> bool:
    """Check if a simulation with this ID already exists (idempotency check)."""
    db = await get_database()
    count = await db["simulations"].count_documents({"_id": simulation_id}, limit=1)
    return count > 0


async def simulation_exists_by_sha(mr_id: int, head_sha: str) -> bool:
    """
    Check if an active/completed simulation already exists for this exact
    mr_id + head_sha combination. Used for idempotency (FR-004).
    """
    db = await get_database()
    count = await db["simulations"].count_documents(
        {
            "mr_id": mr_id,
            "head_sha": head_sha,
            "status": {"$nin": ["cancelled", "error"]},
        },
        limit=1,
    )
    return count > 0


@with_mongo_retry()
async def cancel_in_progress_simulation(mr_id: int, new_head_sha: str) -> int:
    """
    Cancel any in-progress simulations for this MR that have a different head_sha.
    Called when a new push arrives on the same MR (FR-023).
    Returns the count of cancelled simulations.
    """
    db = await get_database()
    in_progress_statuses = [
        "queued", "ingesting", "fetching_baseline",
        "provisioning", "replaying", "evaluating", "reasoning", "enforcing",
    ]
    result = await db["simulations"].update_many(
        {
            "mr_id": mr_id,
            "head_sha": {"$ne": new_head_sha},
            "status": {"$in": in_progress_statuses},
        },
        {
            "$set": {
                "status": "cancelled",
                "completed_at": datetime.now(timezone.utc),
                "error_log": [
                    f"Cancelled: new push received for same MR (new SHA: {new_head_sha})"
                ],
            }
        },
    )
    if result.modified_count > 0:
        logger.info(
            "Cancelled %d in-progress simulation(s) for MR %d (new SHA: %s)",
            result.modified_count,
            mr_id,
            new_head_sha,
        )
    return result.modified_count


# ──────────────────────────────────────────────
# Historical context (Gemini reasoning — FR-017)
# ──────────────────────────────────────────────


async def get_historical_context(service_name: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    Fetch the last N completed simulations for a service.
    This data is injected into the Gemini reasoning prompt as historical context.

    Per PRD §7: Gemini has no memory — this is a DB query result injected
    into the context window. Works correctly only after the first 5 simulations
    for a given service have completed and been stored.
    """
    db = await get_database()
    cursor = (
        db["simulations"]
        .find(
            {"service_name": service_name, "status": "completed"},
            # Project only the fields Gemini needs — keep context window tight
            {
                "_id": 1,
                "created_at": 1,
                "verdict": 1,
                "dynatrace.replay_result.latency_delta_p95_ms": 1,
                "dynatrace.replay_result.error_rate_delta_percent": 1,
                "gitlab.override_applied": 1,
                "gitlab.override_author": 1,
            },
        )
        .sort("created_at", DESCENDING)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


@with_mongo_retry()
async def log_override_event(
    simulation_id: str,
    mr_id: int,
    override_author: str,
) -> None:
    """
    Record a bulwark-override label event in MongoDB (FR-021).
    Sets override_applied=True and logs author + timestamp.
    """
    db = await get_database()
    now = datetime.now(timezone.utc)
    await db["simulations"].update_one(
        {"_id": simulation_id},
        {
            "$set": {
                "gitlab.override_applied": True,
                "gitlab.override_author": override_author,
                "gitlab.override_timestamp": now,
                "gitlab.pipeline_status": "success",
            }
        },
    )
    logger.info(
        "Override logged for simulation %s (MR %d) by %s",
        simulation_id,
        mr_id,
        override_author,
    )


# ──────────────────────────────────────────────
# Verdict CRUD
# ──────────────────────────────────────────────


@with_mongo_retry()
async def save_verdict(simulation_id: str, verdict: dict[str, Any]) -> None:
    """Save a verdict document linked to a simulation."""
    db = await get_database()
    verdict["simulation_id"] = simulation_id
    await db["verdicts"].replace_one(
        {"simulation_id": simulation_id},
        verdict,
        upsert=True,
    )
    logger.info("Saved verdict for simulation %s: %s", simulation_id, verdict.get("verdict"))


async def get_verdict(simulation_id: str) -> dict[str, Any] | None:
    """Fetch the verdict for a simulation."""
    db = await get_database()
    return await db["verdicts"].find_one({"simulation_id": simulation_id})


# ──────────────────────────────────────────────
# Environment Config CRUD
# ──────────────────────────────────────────────


async def get_environment_config(service_name: str) -> dict[str, Any] | None:
    """Fetch the environment configuration for a service."""
    db = await get_database()
    return await db["environment_configs"].find_one({"_id": service_name})


@with_mongo_retry()
async def save_environment_config(config: dict[str, Any]) -> None:
    """Upsert an environment configuration for a service."""
    db = await get_database()
    await db["environment_configs"].replace_one(
        {"_id": config["_id"]},
        config,
        upsert=True,
    )


# ──────────────────────────────────────────────
# Legacy alias (kept for backwards compatibility)
# ──────────────────────────────────────────────


async def get_recent_simulations_for_service(
    service_name: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Alias for get_historical_context (kept for backwards compatibility)."""
    return await get_historical_context(service_name, limit)


async def list_simulations(
    filters: dict[str, Any] | None = None,
    limit: int = 20,
    skip: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """
    List simulations with optional filters.
    Returns (documents, total_count).
    """
    db = await get_database()
    collection = db["simulations"]
    query = filters or {}
    total = await collection.count_documents(query)
    cursor = collection.find(query).sort("created_at", DESCENDING).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    return docs, total


@with_mongo_retry()
async def enqueue_message(message_id: str, message_dict: dict[str, Any]) -> None:
    """Queue a message to MongoDB message_queue (development mode)."""
    db = await get_database()
    await db["message_queue"].insert_one({
        "_id": message_id,
        "message": message_dict,
        "processed": False,
        "queued_at": datetime.now(timezone.utc),
    })
