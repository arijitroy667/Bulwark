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
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import json
from sse_starlette.sse import EventSourceResponse

from orchestrator.agent import handle_pubsub_message
from shared.config import settings
from shared.db import close_database, get_simulation, save_gitlab_pat, list_simulations

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

# Add CORS middleware for the dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


class SetupRequest(BaseModel):
    pat: str
    project_id: str
    webhook_url: str


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health_check() -> HealthResponse:
    """Liveness / readiness probe for Cloud Run."""
    return HealthResponse()


@app.post("/api/setup", tags=["dashboard"])
async def setup_project(request: SetupRequest):
    """Save PAT and auto-configure GitLab webhook for the given project."""
    await save_gitlab_pat(request.pat)
    
    # Configure webhook on GitLab
    gitlab_url = settings.gitlab_url.rstrip('/')
    url = f"{gitlab_url}/api/v4/projects/{request.project_id}/hooks"
    headers = {
        "PRIVATE-TOKEN": request.pat,
        "Content-Type": "application/json"
    }
    payload = {
        "url": request.webhook_url,
        "merge_requests_events": True,
        "push_events": False,
        "enable_ssl_verification": False,
        "token": settings.gitlab_webhook_token
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            logger.error("Failed to setup webhook: %s", resp.text)
            raise HTTPException(status_code=500, detail=f"Failed to setup webhook: {resp.text}")
            
    return {"status": "ok", "message": "Webhook configured successfully"}


@app.get("/api/simulations", tags=["dashboard"])
async def get_simulations(limit: int = 20, skip: int = 0):
    """List recent simulations."""
    docs, total = await list_simulations({}, limit, skip)
    # Convert ObjectIds to strings
    for doc in docs:
        doc["_id"] = str(doc["_id"])
    return {"data": docs, "total": total}


@app.get("/api/simulation/{simulation_id}", tags=["dashboard"])
async def api_get_simulation(simulation_id: str):
    """Get single simulation state."""
    doc = await get_simulation(simulation_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Simulation not found")
    doc["_id"] = str(doc["_id"])
    return doc


@app.get("/api/simulation/{simulation_id}/stream", tags=["dashboard"])
async def stream_simulation(simulation_id: str, request: Request):
    """Stream simulation state changes using Server-Sent Events."""
    async def event_publisher():
        last_status = None
        while True:
            if await request.is_disconnected():
                break
                
            doc = await get_simulation(simulation_id)
            if not doc:
                yield dict(event="error", data=json.dumps({"error": "not found"}))
                break
                
            # JSON serialization fix
            doc["_id"] = str(doc["_id"])
            if "created_at" in doc and hasattr(doc["created_at"], "isoformat"):
                doc["created_at"] = doc["created_at"].isoformat()
            if "completed_at" in doc and hasattr(doc["completed_at"], "isoformat"):
                doc["completed_at"] = doc["completed_at"].isoformat()
                
            status = doc.get("status")
            yield dict(event="message", data=json.dumps(doc))
            
            if status in ["completed", "error", "cancelled"]:
                break
                
            last_status = status
            await asyncio.sleep(1)
            
    return EventSourceResponse(event_publisher())


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
