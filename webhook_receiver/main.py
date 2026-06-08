"""
Bulwark — Webhook Receiver (v3.0).

FastAPI application that receives GitLab webhook events, validates signatures,
filters relevant MR events, and publishes them to Google Cloud Pub/Sub for
asynchronous processing by the orchestrator.

v3 changes:
- head_sha added to MREventMessage (idempotency + cancellation)
- Idempotency check uses mr_id + head_sha (not simulation_id alone)
- Cancel in-progress simulations when new push arrives on same MR (FR-023)
- Version bumped to 3.0.0
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from shared.config import settings
from shared.db import (
    cancel_in_progress_simulation,
    close_database,
    get_database,
    simulation_exists_by_sha,
)
from shared.models import MREventMessage, SimulationStatus

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bulwark.webhook")

# ──────────────────────────────────────────────
# Documentation-only file extensions (skip these MRs)
# ──────────────────────────────────────────────

DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc"}
DOC_DIRECTORIES = {"docs/", "doc/", "documentation/"}


def _is_doc_only_mr(changed_files: list[str]) -> bool:
    """Return True if all changed files are documentation files."""
    if not changed_files:
        return False
    for filepath in changed_files:
        filepath_lower = filepath.lower()
        # Check if it's in a docs directory
        if any(filepath_lower.startswith(d) for d in DOC_DIRECTORIES):
            continue
        # Check file extension
        ext = Path(filepath_lower).suffix
        if ext in DOC_EXTENSIONS:
            continue
        # Non-doc file found
        return False
    return True


def _generate_simulation_id(mr_id: int, source_branch: str, sha: str) -> str:
    """
    Generate a deterministic simulation ID from MR metadata.
    This ensures idempotency — the same MR update won't create duplicates.
    """
    raw = f"{mr_id}:{source_branch}:{sha}"
    short_hash = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"sim_{short_hash}"


def _extract_label_names(labels: Any) -> set[str]:
    """Extract label names from GitLab's string or object label shapes."""
    names: set[str] = set()
    if not isinstance(labels, list):
        return names
    for label in labels:
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, dict):
            name = label.get("title") or label.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _has_override_label(body: dict[str, Any], object_attrs: dict[str, Any]) -> bool:
    """Return True when the MR update includes the bulwark-override label."""
    labels = set()
    labels.update(_extract_label_names(object_attrs.get("labels")))
    labels.update(_extract_label_names(body.get("labels")))

    changes = body.get("changes", {})
    if isinstance(changes, dict):
        label_change = changes.get("labels")
        if isinstance(label_change, dict):
            labels.update(_extract_label_names(label_change.get("current")))
            labels.update(_extract_label_names(label_change.get("previous")))

    return "bulwark-override" in labels


async def _handle_override_label(
    project_id: str,
    mr_id: int,
    head_sha: str,
    override_author: str,
) -> WebhookResponse:
    """Apply a human override to the latest BLOCKED simulation for this MR."""
    from orchestrator.tools.gitlab_tool import post_mr_comment, set_pipeline_status
    from shared.db import log_override_event

    db = await get_database()
    simulation = await db["simulations"].find_one(
        {
            "mr_id": mr_id,
            "verdict.verdict": "BLOCKED",
            "gitlab.override_applied": {"$ne": True},
        },
        sort=[("created_at", -1)],
    )

    if not simulation:
        logger.info("Override label ignored: no blocked simulation found for MR !%d", mr_id)
        return WebhookResponse(
            status="skipped",
            message="No blocked Bulwark simulation found to override",
        )

    simulation_id = simulation["_id"]
    await log_override_event(simulation_id, mr_id, override_author)

    await set_pipeline_status(
        project_id=project_id,
        sha=head_sha,
        state="success",
        description=f"OVERRIDDEN — human override applied by {override_author}",
    )
    await post_mr_comment(
        project_id,
        mr_id,
        (
            "## Bulwark Override Applied\n\n"
            f"`bulwark-override` was applied by `{override_author}`. "
            "The blocked verdict has been overridden and logged for audit.\n\n"
            f"*Simulation ID: `{simulation_id}`*"
        ),
    )

    return WebhookResponse(
        status="overridden",
        simulation_id=simulation_id,
        message="Blocked verdict overridden",
    )


# ──────────────────────────────────────────────
# Pub/Sub publisher (abstracted for testability)
# ──────────────────────────────────────────────

_pubsub_publisher = None


async def _get_publisher():
    """Lazy-load the Pub/Sub publisher client."""
    global _pubsub_publisher
    if _pubsub_publisher is None:
        if settings.bulwark_mode == "development":
            # In dev mode, we'll store messages in MongoDB instead of Pub/Sub
            logger.info("Development mode: using MongoDB as message queue")
            return None
        else:
            from google.cloud import pubsub_v1
            _pubsub_publisher = pubsub_v1.PublisherClient()
    return _pubsub_publisher


async def _publish_to_queue(message: MREventMessage) -> None:
    """
    Publish an MR event message to the processing queue.
    In development mode, writes to MongoDB. In production, publishes to Pub/Sub.
    """
    message_dict = message.model_dump(mode="json")
    message_json = json.dumps(message_dict).encode("utf-8")

    if settings.bulwark_mode == "development":
        # Store in MongoDB for local development
        db = await get_database()
        await db["message_queue"].insert_one({
            "_id": message.simulation_id,
            "message": message_dict,
            "processed": False,
            "queued_at": datetime.now(timezone.utc),
        })
        logger.info("Queued message to MongoDB: %s", message.simulation_id)
    else:
        publisher = await _get_publisher()
        topic_path = publisher.topic_path(
            settings.gcp_project_id,
            "bulwark-mr-events",
        )
        future = publisher.publish(topic_path, message_json)
        result = future.result(timeout=10)
        logger.info("Published to Pub/Sub: %s (message_id=%s)", message.simulation_id, result)


# ──────────────────────────────────────────────
# FastAPI application
# ──────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler — setup and teardown."""
    logger.info("Bulwark Webhook Receiver starting up")
    logger.info("Mode: %s", settings.bulwark_mode)
    yield
    await close_database()
    logger.info("Bulwark Webhook Receiver shut down")


app = FastAPI(
    title="Bulwark Webhook Receiver",
    description="Receives GitLab MR webhook events for production simulation",
    version="1.0.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "3.0.0"


class WebhookResponse(BaseModel):
    status: str
    simulation_id: str = ""
    message: str = ""


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint for Cloud Run and load balancers."""
    return HealthResponse()


@app.post("/webhook/gitlab", response_model=WebhookResponse)
async def receive_gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(None),
) -> WebhookResponse:
    """
    Receive a GitLab webhook event for merge request changes.

    Validates the webhook token, filters for relevant MR events,
    and enqueues them for processing by the orchestrator.
    """
    # ── Step 1: Validate webhook signature ──
    if not x_gitlab_token or x_gitlab_token != settings.gitlab_webhook_token:
        logger.warning("Invalid webhook token received")
        raise HTTPException(status_code=400, detail="invalid signature")

    # ── Step 2: Parse the event body ──
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        logger.error("Failed to parse webhook body")
        raise HTTPException(status_code=400, detail="invalid request body")

    # ── Step 3: Filter — only merge_request events ──
    object_kind = body.get("object_kind", "")
    if object_kind != "merge_request":
        logger.info("Skipping non-MR event: %s", object_kind)
        raise HTTPException(status_code=422, detail="unsupported event type")

    # ── Step 4: Filter — only relevant MR actions ──
    object_attrs = body.get("object_attributes", {})
    action = object_attrs.get("action", "")
    valid_actions = {"open", "update", "reopen"}

    if action not in valid_actions:
        logger.info("Skipping MR action: %s", action)
        return WebhookResponse(
            status="skipped",
            message=f"MR action '{action}' is not tracked",
        )

    # ── Step 5: Extract MR metadata ──
    mr_id = object_attrs.get("iid", 0)
    source_branch = object_attrs.get("source_branch", "")
    target_branch = object_attrs.get("target_branch", "")
    last_commit = object_attrs.get("last_commit", {})
    head_sha = last_commit.get("id", str(uuid.uuid4()))
    mr_url = object_attrs.get("url", "")

    project = body.get("project", {})
    project_id = project.get("path_with_namespace", "")

    user = body.get("user", {})
    override_author = user.get("username") or user.get("name") or "unknown"

    # ── Override handling: label updates should not enqueue a fresh simulation ──
    if action == "update" and _has_override_label(body, object_attrs):
        return await _handle_override_label(
            project_id=project_id,
            mr_id=mr_id,
            head_sha=head_sha,
            override_author=override_author,
        )

    # ── Step 6: Filter — skip documentation-only MRs ──
    changes = body.get("changes", {})
    # GitLab sends changed files in the webhook payload under various formats
    changed_files: list[str] = []
    if "diff_refs" in object_attrs:
        # Try to extract from the changes object
        pass
    # If we have explicit file changes in the payload
    if "changes" in body and isinstance(body["changes"], dict):
        for change_key, change_val in body["changes"].items():
            if isinstance(change_val, dict) and "previous" in change_val:
                continue  # These are attribute changes, not file changes

    # We'll do the full doc-only check in the orchestrator using the GitLab API
    # since webhook payloads may not always include the full file list

    # ── Step 7: Generate deterministic simulation ID ──
    # Deterministic from mr_id + source_branch + head_sha (ensures idempotency)
    simulation_id = _generate_simulation_id(mr_id, source_branch, head_sha)

    # ── Step 8: Cancel in-progress simulations for this MR (FR-023) ──
    # If a new push arrives on the same MR, cancel any running simulation
    try:
        cancelled = await cancel_in_progress_simulation(mr_id, head_sha)
        if cancelled > 0:
            logger.info(
                "Cancelled %d in-progress simulation(s) for MR %d — new SHA: %s",
                cancelled, mr_id, head_sha,
            )
    except Exception as e:
        logger.warning("Cancellation check failed (proceeding): %s", e)

    # ── Step 9: Idempotency check by mr_id + head_sha (v3) ──
    try:
        if await simulation_exists_by_sha(mr_id, head_sha):
            logger.info("Duplicate simulation skipped: MR %d SHA %s", mr_id, head_sha)
            return WebhookResponse(
                status="duplicate",
                simulation_id=simulation_id,
                message="Simulation already exists for this MR state",
            )
    except Exception as e:
        # If DB is unreachable, proceed anyway (don't block the webhook)
        logger.warning("Idempotency check failed (proceeding): %s", e)

    # ── Step 10: Create initial simulation record ──
    try:
        db = await get_database()
        await db["simulations"].insert_one({
            "_id": simulation_id,
            "mr_id": mr_id,
            "project_id": project_id,
            "head_sha": head_sha,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "status": SimulationStatus.QUEUED.value,
            "created_at": datetime.now(timezone.utc),
            "completed_at": None,
            "total_duration_ms": 0,
            "environment": None,
            "dynatrace": {},
            "arize": {},
            "verdict": None,
            "gitlab": {},
            "error_log": [],
        })
    except Exception as e:
        logger.warning("Failed to create simulation record (proceeding): %s", e)

    # ── Step 11: Build and enqueue the message ──
    message = MREventMessage(
        simulation_id=simulation_id,
        mr_id=mr_id,
        project_id=project_id,
        head_sha=head_sha,
        source_branch=source_branch,
        target_branch=target_branch,
        mr_url=mr_url,
        received_at=datetime.now(timezone.utc),
    )

    try:
        await _publish_to_queue(message)
    except Exception as e:
        logger.error("Failed to publish message: %s", e)
        raise HTTPException(status_code=500, detail="failed to enqueue simulation")

    logger.info(
        "Webhook processed: MR !%d (%s → %s) → simulation %s",
        mr_id,
        source_branch,
        target_branch,
        simulation_id,
    )

    return WebhookResponse(
        status="queued",
        simulation_id=simulation_id,
    )


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=settings.bulwark_mode == "development",
    )
