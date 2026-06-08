"""
Bulwark — Orchestrator Worker HTTP entrypoint.

Receives Pub/Sub push messages in production and delegates to the orchestrator
agent. Local development can still run `python -m orchestrator.agent` to poll
the Mongo-backed queue.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from orchestrator.agent import handle_pubsub_message
from shared.config import settings
from shared.db import close_database

logger = logging.getLogger("bulwark.orchestrator.worker")
_running_tasks: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bulwark Orchestrator Worker starting up")
    logger.info("Mode: %s", settings.bulwark_mode)
    yield
    for task in _running_tasks:
        task.cancel()
    if _running_tasks:
        await asyncio.gather(*_running_tasks, return_exceptions=True)
    await close_database()
    logger.info("Bulwark Orchestrator Worker shut down")


app = FastAPI(
    title="Bulwark Orchestrator Worker",
    description="Processes GitLab MR simulation events from Pub/Sub",
    version=settings.bulwark_version,
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = settings.bulwark_version


class PubSubAckResponse(BaseModel):
    status: str


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse()


@app.post("/pubsub/orchestrate", response_model=PubSubAckResponse)
async def receive_pubsub_push(request: Request) -> PubSubAckResponse:
    """
    Receive a Google Pub/Sub push envelope.

    Expected shape:
      {"message": {"data": "<base64 encoded MREventMessage JSON>", ...}}
    """
    try:
        envelope: dict[str, Any] = await request.json()
    except Exception as e:
        logger.warning("Invalid Pub/Sub push body: %s", e)
        raise HTTPException(status_code=400, detail="invalid pubsub body")

    message = envelope.get("message")
    if not isinstance(message, dict):
        raise HTTPException(status_code=400, detail="missing pubsub message")

    encoded_data = message.get("data")
    if not isinstance(encoded_data, str) or not encoded_data:
        raise HTTPException(status_code=400, detail="missing pubsub message data")

    try:
        message_data = base64.b64decode(encoded_data)
    except Exception as e:
        logger.warning("Invalid Pub/Sub data encoding: %s", e)
        raise HTTPException(status_code=400, detail="invalid pubsub data")

    task = asyncio.create_task(handle_pubsub_message(message_data))
    _running_tasks.add(task)

    def _cleanup(completed: asyncio.Task[None]) -> None:
        _running_tasks.discard(completed)
        try:
            completed.result()
        except Exception as e:
            logger.error("Background Pub/Sub simulation failed: %s", e)

    task.add_done_callback(_cleanup)
    return PubSubAckResponse(status="accepted")
