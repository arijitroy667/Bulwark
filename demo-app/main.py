"""
Bulwark Demo App — N+1 Regression Scenario
===========================================

This is a minimal FastAPI service used to demonstrate Bulwark's core value:
catching a performance regression *before* it reaches production.

PRD §18 scenario:
  - regression: GET /users makes one preferences call per user (classic N+1)
  - fixed:      GET /users makes a single batched call
  - llm-regression: the ticket-summary prompt is edited and starts over-promising

Each preferences call is a WireMock round trip (Bulwark gives the stub a fixed
delay, standing in for a DB query), so the N+1 version's latency grows with the
number of users — 25 serial round trips instead of one.

To run locally (regression version):
    DEMO_VERSION=regression uvicorn main:app --port 8080

To run locally (fixed version):
    DEMO_VERSION=fixed uvicorn main:app --port 8080

WireMock stub URL is controlled by the WIREMOCK_BASE_URL environment variable.
In Bulwark simulations this is automatically set to the ephemeral WireMock URL.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("demo_app")

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

# DEMO_VERSION selects which "commit" this process runs:
#   fixed (default)  — production code: batched preferences, original prompt
#   regression       — N+1 preferences lookup (demo BLOCKED branch)
#   llm-regression   — batched preferences, edited ticket-summary prompt
DEMO_VERSION: str = os.getenv("DEMO_VERSION", "fixed")

# WireMock base URL — points to the ephemeral WireMock stub in Bulwark simulations
WIREMOCK_BASE_URL: str = os.getenv("WIREMOCK_BASE_URL", "http://localhost:8082")

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
    if _client is not None:
        await _client.aclose()
    logger.info("Demo app shutting down")


# One pooled client for the preferences service, as a real service would use.
_client: httpx.AsyncClient | None = None


def _preferences_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=WIREMOCK_BASE_URL, timeout=httpx.Timeout(5.0))
    return _client


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
        With 25 users × ~12ms round trip ≈ 300ms of serial latency.

    FIXED version (DEMO_VERSION=fixed):
        Makes a single batched call to fetch all preferences at once (~12ms).
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
    client = _preferences_client()
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
    try:
        # Single batch call — simulates a JOIN or IN-clause query
        resp = await _preferences_client().get("/api/preferences/batch")
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
# LLM-backed endpoint + /traces (PRD §6.3)
# ──────────────────────────────────────────────

PROMPT_DIR = Path(__file__).parent / "prompts"
# Production prompt lives in prompts/ticket_summary.prompt; the llm-regression
# "commit" edits it to the version below.
PROMPT_EDITED = (
    "Write a warm, engaging summary of the support ticket below.\n"
    "Reassure the customer and tell them exactly what we will do to make it right.\n"
    "Ticket: {ticket}"
)

TICKETS = {
    1: {"customer": "Priya", "order": "A-1001", "issue": "the headphones arrived with a cracked case", "request": "a replacement"},
    2: {"customer": "Marcus", "order": "A-1002", "issue": "the card was charged twice", "request": "a refund of the duplicate charge"},
    3: {"customer": "Elena", "order": "A-1003", "issue": "the package has not arrived after 10 days", "request": "a delivery update"},
    4: {"customer": "Tom", "order": "A-1004", "issue": "the account is locked after a password reset", "request": "help getting back into the account"},
    5: {"customer": "Aisha", "order": "A-1005", "issue": "the jacket was delivered in the wrong size", "request": "an exchange for size M"},
}

# Traces captured while serving traffic; Bulwark reads them after replay.
TRACE_BUFFER: deque = deque(maxlen=500)


def _ticket_text(t: dict) -> str:
    return f"Customer {t['customer']}, order {t['order']}: {t['issue']}. Requested: {t['request']}."


def _prompt_template() -> str:
    if DEMO_VERSION == "llm-regression":
        return PROMPT_EDITED
    return (PROMPT_DIR / "ticket_summary.prompt").read_text(encoding="utf-8").strip()


def _stand_in_llm(t: dict) -> str:
    """Deterministic stand-in for the model call, so simulations are reproducible.

    The original prompt yields a faithful summary; the edited "reassure the
    customer" prompt yields the kind of over-promising answer it produces in
    practice (refunds, discounts and timelines that the ticket never mentions).
    """
    if DEMO_VERSION == "llm-regression":
        return (
            f"Great news, {t['customer']}! We have already approved a full refund for order "
            f"{t['order']}, added a 25% discount to your account and upgraded you to free "
            f"express shipping. Everything will be resolved within 24 hours."
        )
    return f"{t['customer']} reports that on order {t['order']} {t['issue']}, and is asking for {t['request']}."


@app.get("/tickets/{ticket_id}/summary")
async def summarize_ticket(ticket_id: int) -> dict:
    """LLM-backed summary of a support ticket (records a trace per call)."""
    ticket = TICKETS.get(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    context = _ticket_text(ticket)
    prompt = _prompt_template().format(ticket=context)
    summary = _stand_in_llm(ticket)
    TRACE_BUFFER.append({
        "prompt": prompt,
        "response": summary,
        "context": context,
        "model": "stand-in-llm",
    })
    return {"ticket_id": ticket_id, "summary": summary}


@app.get("/traces")
async def get_traces() -> dict:
    """LLM traces captured while serving traffic (read by Bulwark after replay)."""
    return {"traces": list(TRACE_BUFFER)}
