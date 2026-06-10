"""
Bulwark Demo App — N+1 Regression Scenario
===========================================

This is a minimal FastAPI service used to demonstrate Bulwark's core value:
catching a performance regression *before* it reaches production.

PRD §18 scenario:
  - v1 (regression): GET /users makes one DB call per user (classic N+1)
  - v2 (fixed):      GET /users makes a single batched call

The N+1 pattern produces a +187ms p95 latency delta against a 142ms baseline
when replayed with 200 production requests — well above the ±35ms noise floor.

To run locally (regression version):
    DEMO_VERSION=regression uvicorn main:app --port 8080

To run locally (fixed version):
    DEMO_VERSION=fixed uvicorn main:app --port 8080

WireMock stub URL is controlled by the WIREMOCK_BASE_URL environment variable.
In Bulwark simulations this is automatically set to the ephemeral WireMock URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("demo_app")

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

# Set DEMO_VERSION=regression to enable the N+1 bug (for demo BLOCKED branch)
# Set DEMO_VERSION=fixed (default) for the APPROVED branch
DEMO_VERSION: str = os.getenv("DEMO_VERSION", "fixed")

# WireMock base URL — points to the ephemeral WireMock stub in Bulwark simulations
WIREMOCK_BASE_URL: str = os.getenv("WIREMOCK_BASE_URL", "http://localhost:8082")

# Simulated DB latency (ms) — represents realistic ORM round-trip
SIMULATED_DB_LATENCY_MS: float = float(os.getenv("SIMULATED_DB_LATENCY_MS", "12"))

# ──────────────────────────────────────────────
# Mock data (represents what would come from a real DB)
# ──────────────────────────────────────────────

MOCK_USERS = [
    {"id": i, "name": f"User {i}", "email": f"user{i}@example.com"}
    for i in range(1, 26)  # 25 users
]


# ──────────────────────────────────────────────
# App lifespan
# ──────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Demo app starting — version=%s, wiremock=%s",
        DEMO_VERSION,
        WIREMOCK_BASE_URL,
    )
    yield
    logger.info("Demo app shutting down")


# ──────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────

app = FastAPI(
    title="Bulwark Demo App",
    description=(
        "Minimal FastAPI service for demonstrating Bulwark's N+1 regression detection. "
        f"Running in **{DEMO_VERSION}** mode."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# Health check (required by Cloud Run + Bulwark)
# ──────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    """Health check endpoint — must return 2xx for Bulwark to proceed."""
    return {"status": "ok", "version": DEMO_VERSION}


# ──────────────────────────────────────────────
# GET /users — the regression hotspot
# ──────────────────────────────────────────────


@app.get("/users")
async def get_users(request: Request) -> JSONResponse:
    """
    Fetch all users with their preferences.

    REGRESSION version (DEMO_VERSION=regression):
        Makes one HTTP call per user to fetch preferences — O(n) calls.
        With 25 users × ~12ms DB latency = ~300ms total serial latency.
        This produces a +187ms p95 delta against the 142ms baseline.

    FIXED version (DEMO_VERSION=fixed):
        Makes a single batched call to fetch all preferences at once.
        Total latency: ~12ms + response overhead ≈ 3ms p95 delta.
    """
    start = time.perf_counter()

    if DEMO_VERSION == "regression":
        users_with_prefs = await _get_users_n_plus_one()
    else:
        users_with_prefs = await _get_users_batched()

    duration_ms = (time.perf_counter() - start) * 1000
    logger.info("GET /users completed in %.1fms (%s mode)", duration_ms, DEMO_VERSION)

    return JSONResponse(
        content={
            "users": users_with_prefs,
            "count": len(users_with_prefs),
            "mode": DEMO_VERSION,
            "duration_ms": round(duration_ms, 2),
        }
    )


async def _get_users_n_plus_one() -> list[dict]:
    """
    N+1 implementation: fetches preferences for each user individually.

    This is the regression introduced in the demo MR. Each call goes through
    WireMock (simulating a DB round-trip), producing serial per-user latency.

    In production this would be:
        for user in users:
            prefs = db.query(f"SELECT * FROM preferences WHERE user_id = {user.id}")
    """
    users_with_prefs = []
    async with httpx.AsyncClient(
        base_url=WIREMOCK_BASE_URL,
        timeout=httpx.Timeout(5.0),
    ) as client:
        for user in MOCK_USERS:
            try:
                # One HTTP call per user — simulates N+1 DB query pattern
                resp = await client.get(f"/api/preferences/{user['id']}")
                prefs = resp.json() if resp.status_code == 200 else {}
            except Exception as e:
                logger.warning("Failed to fetch prefs for user %d: %s", user["id"], e)
                prefs = {}

            users_with_prefs.append({**user, "preferences": prefs})

    return users_with_prefs


async def _get_users_batched() -> list[dict]:
    """
    Fixed implementation: fetches all preferences in a single batched call.

    This is the fix pushed in the follow-up MR. One call for all users,
    returning a dict keyed by user_id.

    In production this would be:
        prefs_map = db.query(
            "SELECT * FROM preferences WHERE user_id IN (:ids)",
            ids=[u.id for u in users]
        )
    """
    prefs_map: dict = {}
    async with httpx.AsyncClient(
        base_url=WIREMOCK_BASE_URL,
        timeout=httpx.Timeout(5.0),
    ) as client:
        try:
            # Single batch call — simulates a JOIN or IN-clause query
            resp = await client.get("/api/preferences/batch")
            if resp.status_code == 200:
                prefs_map = resp.json()
        except Exception as e:
            logger.warning("Batch preferences fetch failed: %s", e)

    return [
        {**user, "preferences": prefs_map.get(str(user["id"]), {})}
        for user in MOCK_USERS
    ]


# ──────────────────────────────────────────────
# Additional realistic endpoints for replay variety
# ──────────────────────────────────────────────


@app.get("/users/{user_id}")
async def get_user(user_id: int) -> dict:
    """Fetch a single user by ID."""
    user = next((u for u in MOCK_USERS if u["id"] == user_id), None)
    if not user:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return user


@app.get("/metrics")
async def get_metrics() -> dict:
    """Expose basic app metrics for Dynatrace."""
    return {
        "demo_version": DEMO_VERSION,
        "user_count": len(MOCK_USERS),
        "wiremock_url": WIREMOCK_BASE_URL,
    }


# ──────────────────────────────────────────────
# /traces — LLM Observability Endpoint (PRD §6.3)
# ──────────────────────────────────────────────


@app.get("/traces")
async def get_traces() -> dict:
    """
    Exposes LLM traces captured during the simulation.
    Bulwark's orchestrator calls this at the end of the replay phase
    to gather spans for Arize evaluation.
    
    In the real world, this would be backed by an in-memory buffer,
    Redis, or a trace collector sink within the ephemeral environment.
    """
    if DEMO_VERSION != "llm-demo":
        # Only return traces in the LLM demo scenario
        return {"traces": []}

    # Simulate traces captured during traffic replay
    return {
        "traces": [
            {
                "prompt": "Summarize this ticket: T-1234. Customer is asking for a refund.",
                "response": "The customer requested a refund for ticket T-1234.",
                "context": {"ticket_id": "T-1234", "user_tier": "premium"},
                "model": "gemini-1.5-flash",
                "latency_ms": 450,
            },
            {
                "prompt": "Write a polite rejection email for feature request F-99.",
                "response": "Thank you for the suggestion. Unfortunately, we cannot build F-99 right now.",
                "context": {"feature_id": "F-99"},
                "model": "gemini-1.5-flash",
                "latency_ms": 380,
            },
        ]
    }
