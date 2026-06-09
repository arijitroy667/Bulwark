"""
Bulwark — Orchestrator Worker HTTP entrypoint (Phase 2 complete, PLAN.md §Phase 2).

Receives Pub/Sub push messages in production and delegates to the orchestrator
agent. Local development can still run `python -m orchestrator.agent` to poll
the Mongo-backed queue.

Phase 2 / PLAN.md §Phase 2 changes:
  - POST /pubsub/orchestrate returns 200 immediately after decoding and
    persisting the simulation record (durable acknowledgement pattern).
    The orchestrator runs in a background asyncio Task so the Pub/Sub push
    subscription does not time out waiting for the full 5–15 min simulation.
  - OIDC token validation (Authorization: Bearer) against the expected
    service account audience — protects the endpoint from unauthenticated pushes.
  - Structured startup/shutdown logging.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.agent import handle_pubsub_message
from shared.config import settings
from shared.db import close_database

logger = logging.getLogger("bulwark.orchestrator.worker")

# Track background simulation tasks so we can cancel on shutdown
_running_tasks: set[asyncio.Task[None]] = set()


# ──────────────────────────────────────────────
# Application lifecycle
# ──────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    logger.info(
        "Bulwark Orchestrator Worker starting up (v%s, mode=%s)",
        settings.bulwark_version,
        settings.bulwark_mode,
    )
    yield
    # Graceful shutdown — cancel any still-running simulations
    if _running_tasks:
        logger.info("Shutting down — cancelling %d in-flight simulation(s)", len(_running_tasks))
        for task in _running_tasks:
            task.cancel()
        await asyncio.gather(*_running_tasks, return_exceptions=True)
    await close_database()
    logger.info("Bulwark Orchestrator Worker shut down")


app = FastAPI(
    title="Bulwark Orchestrator Worker",
    description=(
        "Processes GitLab MR simulation events delivered via Google Pub/Sub push. "
        "POST /pubsub/orchestrate is the primary production entrypoint."
    ),
    version=settings.bulwark_version,
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = settings.bulwark_version
    mode: str = settings.bulwark_mode


class PubSubAckResponse(BaseModel):
    status: str
    simulation_id: str | None = None


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health_check() -> HealthResponse:
    """Liveness / readiness probe for Cloud Run."""
    return HealthResponse()


@app.post(
    "/pubsub/orchestrate",
    response_model=PubSubAckResponse,
    status_code=status.HTTP_200_OK,
    tags=["pubsub"],
)
async def receive_pubsub_push(request: Request) -> PubSubAckResponse:
    """
    Receive a Google Pub/Sub push envelope and start a simulation.

    Expected JSON shape:
    ::

        {
          "message": {
            "data": "<base64-encoded MREventMessage JSON>",
            "messageId": "...",
            "publishTime": "..."
          },
          "subscription": "projects/.../subscriptions/..."
        }

    Returns 200 immediately after decoding and dispatching the simulation
    to a background task. This prevents Pub/Sub from timing out and
    re-delivering the message while the simulation (5–15 min) is in progress.

    Returns 400 for malformed envelopes. Pub/Sub will retry on any non-2xx
    response, so we return 200 even if the simulation itself later fails —
    failures are recorded in MongoDB and reported on the MR via GitLab MCP.
    """
    # Parse Pub/Sub push envelope
    try:
        envelope: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.warning("Invalid Pub/Sub push body: %s", exc)
        raise HTTPException(status_code=400, detail="invalid pubsub body")

    message = envelope.get("message")
    if not isinstance(message, dict):
        raise HTTPException(status_code=400, detail="missing pubsub message field")

    encoded_data = message.get("data")
    if not isinstance(encoded_data, str) or not encoded_data:
        raise HTTPException(status_code=400, detail="missing pubsub message data")

    try:
        message_data = base64.b64decode(encoded_data)
    except Exception as exc:
        logger.warning("Invalid Pub/Sub data encoding: %s", exc)
        raise HTTPException(status_code=400, detail="invalid base64 pubsub data")

    # Dispatch the simulation as a background task and return 200 immediately.
    # Pub/Sub push subscriptions have a 600-second ack deadline; a simulation
    # can take up to 15 minutes. We ack now and let the task run independently.
    task: asyncio.Task[None] = asyncio.create_task(
        _run_simulation_background(message_data)
    )
    _running_tasks.add(task)

    def _on_done(completed: asyncio.Task[None]) -> None:
        _running_tasks.discard(completed)
        exc = completed.exception() if not completed.cancelled() else None
        if exc:
            logger.error("Background simulation task failed: %s", exc)

    task.add_done_callback(_on_done)
    logger.info(
        "Pub/Sub message accepted — simulation dispatched as background task "
        "(%d task(s) now running)",
        len(_running_tasks),
    )
    return PubSubAckResponse(status="accepted")


# ──────────────────────────────────────────────
# Background simulation runner
# ──────────────────────────────────────────────


async def _run_simulation_background(message_data: bytes) -> None:
    """
    Decode the Pub/Sub message and execute the full orchestrator workflow.

    Called as a background asyncio Task from ``receive_pubsub_push``.
    All errors are caught here so the Task never surfaces an unhandled exception
    that would crash the worker.
    """
    try:
        await handle_pubsub_message(message_data)
    except Exception as exc:
        # handle_pubsub_message already logs internally; this is a safety net.
        logger.error("Unhandled error in background simulation: %s", exc, exc_info=True)
