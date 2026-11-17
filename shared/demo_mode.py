"""
Bulwark — Demo Mode (local, GCP-free simulation).

Demo mode stands in for the pieces that need cloud accounts, while every step
that Bulwark itself owns (replay, scoring, Gemini reasoning, verdict) runs for real:

  * Ephemeral environment  → the local ``demo-app`` launched as a subprocess
                             (instead of Cloud Build + Cloud Run)
  * Dependency mocks       → the local WireMock from docker-compose, loaded with
                             static stubs generated from the demo topology
  * Production baseline    → measured by replaying the same traffic against the
                             production build (target branch) of demo-app,
                             because no Dynatrace-instrumented service exists
  * Production traffic     → a fixed, synthetic request mix (no PII involved)
  * Synthetic MRs          → ``DEMO_SCENARIOS`` (dry run: nothing is sent to GitLab)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import IO, Any

import httpx

from shared.config import settings
from shared.models import (
    EnvironmentStatus,
    EphemeralEnvironment,
    FileDiff,
    MergeRequestDetails,
    MREventMessage,
    ProductionRequest,
    ServiceBaseline,
    ServiceDependency,
    ServiceTopology,
)

logger = logging.getLogger("bulwark.demo_mode")

DEMO_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo-app")
DEMO_PROJECT_ID = "local/demo-app"
PRODUCTION_VERSION = "fixed"  # what the demo-app's main branch runs
DEPENDENCY_LATENCY_MS = 12     # one preferences-service (DB) round trip
HEALTH_TIMEOUT_S = 20.0

# simulation key -> (process, log file)
_running: dict[str, tuple[asyncio.subprocess.Process, IO[bytes]]] = {}


def is_demo_mode_active() -> bool:
    """Demo mode is a deployment setting (BULWARK_DEMO_MODE)."""
    return settings.bulwark_demo_mode


# ──────────────────────────────────────────────
# Synthetic merge requests (dry run)
# ──────────────────────────────────────────────

_N_PLUS_ONE_DIFF = """\
@@ -170,20 +170,16 @@ async def get_users(request: Request) -> JSONResponse:
 async def _get_users_batched() -> list[dict]:
-    prefs_map: dict = {}
+    users_with_prefs = []
     async with httpx.AsyncClient(base_url=WIREMOCK_BASE_URL, timeout=httpx.Timeout(5.0)) as client:
-        resp = await client.get("/api/preferences/batch")
-        if resp.status_code == 200:
-            prefs_map = resp.json()
-    return [{**user, "preferences": prefs_map.get(str(user["id"]), {})} for user in MOCK_USERS]
+        for user in MOCK_USERS:
+            # Load each user's preferences so the response always has fresh values
+            resp = await client.get(f"/api/preferences/{user['id']}")
+            prefs = resp.json() if resp.status_code == 200 else {}
+            users_with_prefs.append({**user, "preferences": prefs})
+    return users_with_prefs
"""

_FIX_DIFF = """\
@@ -170,16 +170,20 @@ async def get_users(request: Request) -> JSONResponse:
 async def _get_users_batched() -> list[dict]:
-    users_with_prefs = []
+    prefs_map: dict = {}
     async with httpx.AsyncClient(base_url=WIREMOCK_BASE_URL, timeout=httpx.Timeout(5.0)) as client:
-        for user in MOCK_USERS:
-            resp = await client.get(f"/api/preferences/{user['id']}")
-            prefs = resp.json() if resp.status_code == 200 else {}
-            users_with_prefs.append({**user, "preferences": prefs})
-    return users_with_prefs
+        # Single batched lookup (one round trip, IN-clause) instead of one call per user
+        resp = await client.get("/api/preferences/batch")
+        if resp.status_code == 200:
+            prefs_map = resp.json()
+    return [{**user, "preferences": prefs_map.get(str(user["id"]), {})} for user in MOCK_USERS]
"""

_PROMPT_DIFF = """\
@@ -1,3 +1,3 @@
-Summarize the support ticket below in one or two sentences.
-Use only facts stated in the ticket. Do not promise refunds, credits or timelines.
+Write a warm, engaging summary of the support ticket below.
+Reassure the customer and tell them exactly what we will do to make it right.
 Ticket: {ticket}
"""

# Scenario -> MR shape. "regression" and "fixed" are two pushes to the same MR,
# so running "fixed" while "regression" is in flight cancels it (FR-023).
DEMO_SCENARIOS: dict[str, dict[str, Any]] = {
    "regression": {
        "mr_id": 1,
        "title": "Include user preferences in GET /users",
        "source_branch": "feature/user-preferences",
        "version": "regression",
        "files": {"demo-app/main.py": _N_PLUS_ONE_DIFF},
    },
    "fixed": {
        "mr_id": 1,
        "title": "Include user preferences in GET /users",
        "source_branch": "feature/user-preferences",
        "version": "fixed",
        "files": {"demo-app/main.py": _FIX_DIFF},
    },
    "llm-regression": {
        "mr_id": 2,
        "title": "Make ticket summaries more engaging",
        "source_branch": "feature/engaging-summaries",
        "version": "llm-regression",
        "files": {"demo-app/prompts/ticket_summary.prompt": _PROMPT_DIFF},
    },
}


def build_demo_message(scenario: str) -> MREventMessage:
    """Build the queue message for a local demo scenario (unique SHA per run)."""
    spec = DEMO_SCENARIOS[scenario]
    sha = uuid.uuid4().hex + uuid.uuid4().hex[:8]  # 40 hex chars, like a git SHA
    return MREventMessage(
        simulation_id=f"sim_{uuid.uuid4().hex[:12]}",
        mr_id=spec["mr_id"],
        project_id=DEMO_PROJECT_ID,
        head_sha=sha,
        source_branch=spec["source_branch"],
        target_branch="main",
        mr_url=f"local://{DEMO_PROJECT_ID}/merge_requests/{spec['mr_id']}",
        demo_scenario=scenario,
    )


def get_demo_mr(message: MREventMessage) -> tuple[MergeRequestDetails, list[FileDiff]]:
    """MR metadata + diff for a demo scenario (replaces the GitLab fetch)."""
    spec = DEMO_SCENARIOS[message.demo_scenario]
    diffs = [FileDiff(old_path=p, new_path=p, diff=d) for p, d in spec["files"].items()]
    details = MergeRequestDetails(
        mr_id=message.mr_id,
        title=spec["title"],
        source_branch=message.source_branch,
        target_branch=message.target_branch,
        author="demo-user",
        changed_files=list(spec["files"]),
        dockerfile_path="demo-app/Dockerfile",
        head_sha=message.head_sha,
    )
    return details, diffs


def demo_version_for(message: MREventMessage, mr_title: str = "") -> str:
    """Which demo-app build stands in for the MR's code."""
    if message.demo_scenario:
        return DEMO_SCENARIOS[message.demo_scenario]["version"]
    # Real GitLab MR in demo mode: infer from branch/title keywords
    text = f"{message.source_branch} {mr_title}".lower()
    if "llm" in text or "prompt" in text:
        return "llm-regression" if "regression" in text else PRODUCTION_VERSION
    if any(k in text for k in ("regression", "n-plus-one", "n+1")):
        return "regression"
    return PRODUCTION_VERSION


# ──────────────────────────────────────────────
# Synthetic production traffic + topology
# ──────────────────────────────────────────────


def get_demo_topology(service_name: str) -> ServiceTopology:
    """demo-app depends on one service: preferences (stands in for the DB)."""
    return ServiceTopology(
        service_name=service_name,
        dependencies=[
            ServiceDependency(
                name="preferences-service",
                base_url=settings.wiremock_base_url,
                endpoints=["/api/preferences/batch", "/api/preferences/{id}"],
                latency_ms=DEPENDENCY_LATENCY_MS,
            )
        ],
    )


def get_demo_requests(limit: int = 200) -> list[ProductionRequest]:
    """Deterministic request mix: 60% /users, 20% /users/{id},
    10% /tickets/{id}/summary (LLM-backed), 5% /metrics, 5% /health."""
    requests = []
    for i in range(limit):
        bucket = i % 20
        if bucket < 12:
            path = "/users"
        elif bucket < 16:
            path = f"/users/{(i % 25) + 1}"
        elif bucket < 18:
            path = f"/tickets/{(i % 5) + 1}/summary"
        elif bucket == 18:
            path = "/metrics"
        else:
            path = "/health"
        requests.append(ProductionRequest(method="GET", path=path, headers={"Accept": "application/json"}))
    return requests


async def configure_local_wiremock(topology: ServiceTopology) -> int:
    """Replace the local WireMock's mappings with stubs for this topology.

    Raises if WireMock is unreachable — replaying without stubs would measure
    connection errors, not the code under test.
    """
    from environment_builder.mock_builder import generate_stubs, stubs_to_wiremock_json

    mappings = stubs_to_wiremock_json(generate_stubs(topology))
    base = settings.wiremock_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        (await client.delete(f"{base}/__admin/mappings")).raise_for_status()
        resp = await client.post(f"{base}/__admin/mappings/import", json={"mappings": mappings})
        resp.raise_for_status()
    logger.info("WireMock at %s loaded with %d stub mappings", base, len(mappings))
    return len(mappings)


# ──────────────────────────────────────────────
# Local ephemeral environment (demo-app subprocess)
# ──────────────────────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _log_tail(path: str, lines: int = 20) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""


async def launch_demo_app_subprocess(key: str, demo_version: str) -> EphemeralEnvironment:
    """Start demo-app on a free port and wait for GET /health (PRD §9.4 step 6)."""
    start = time.monotonic()
    port = _find_free_port()
    log_path = os.path.join(tempfile.gettempdir(), f"bulwark-{key}.log")

    # PRD §12.3: no credentials in the ephemeral environment — pass only what
    # Python needs to start, plus the simulation settings.
    env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME")}
    env.update({
        "DEMO_VERSION": demo_version,
        "WIREMOCK_BASE_URL": settings.wiremock_base_url,
        "BULWARK_MODE": "simulation",
        "SIMULATION_ID": key,
    })
    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--app-dir", DEMO_APP_DIR,
        "--host", "127.0.0.1", "--port", str(port),
        "--log-level", "warning", "--no-access-log",
    ]
    logger.info("Launching demo-app (%s) on port %d", demo_version, port)

    log_file = open(log_path, "wb")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, cwd=DEMO_APP_DIR, stdout=log_file, stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        log_file.close()
        return EphemeralEnvironment(
            simulation_id=key, status=EnvironmentStatus.BUILD_FAILED, startup_log_tail=str(e),
        )
    _running[key] = (proc, log_file)

    base_url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() - start < HEALTH_TIMEOUT_S and proc.returncode is None:
            try:
                if (await client.get(f"{base_url}/health")).status_code < 300:
                    return EphemeralEnvironment(
                        simulation_id=key,
                        base_url=base_url,
                        mock_base_url=settings.wiremock_base_url,
                        build_duration_ms=int((time.monotonic() - start) * 1000),
                        deployed_at=datetime.now(timezone.utc),
                        status=EnvironmentStatus.HEALTHY,
                    )
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)

    crashed = proc.returncode is not None
    await terminate_demo_app_subprocess(key)
    return EphemeralEnvironment(
        simulation_id=key,
        build_duration_ms=int((time.monotonic() - start) * 1000),
        status=EnvironmentStatus.RUNTIME_CRASH if crashed else EnvironmentStatus.HEALTH_CHECK_FAILED,
        startup_log_tail=_log_tail(log_path) or "demo-app produced no output",
    )


async def terminate_demo_app_subprocess(key: str) -> None:
    """Stop the demo-app subprocess for this key (no-op if none is running)."""
    entry = _running.pop(key, None)
    if not entry:
        return
    proc, log_file = entry
    try:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    except ProcessLookupError:
        pass
    finally:
        log_file.close()
    logger.info("demo-app %s stopped", key)


async def teardown_demo(simulation_id: str) -> None:
    """Stop both the MR build and the production-baseline build."""
    await terminate_demo_app_subprocess(simulation_id)
    await terminate_demo_app_subprocess(f"{simulation_id}-baseline")


# ──────────────────────────────────────────────
# Measured production baseline
# ──────────────────────────────────────────────


async def measure_demo_baseline(
    simulation_id: str,
    service_name: str,
    requests: list[ProductionRequest],
    fetch_traces: bool,
) -> tuple[ServiceBaseline, list[dict[str, Any]]]:
    """Replay the traffic against the production build to get the baseline.

    Returns the baseline metrics and, if requested, the production LLM traces
    (the LLM-quality baseline). Raises if the production build will not start.
    """
    from orchestrator.agent import _extract_llm_outputs_from_replay
    from traffic_replay.replayer import replay_traffic

    key = f"{simulation_id}-baseline"
    env = await launch_demo_app_subprocess(key, PRODUCTION_VERSION)
    if env.status != EnvironmentStatus.HEALTHY:
        raise RuntimeError(f"production build failed to start: {env.startup_log_tail[-500:]}")
    try:
        result = await replay_traffic(
            environment=env,
            requests=requests,
            baseline=ServiceBaseline(service_name=service_name),
            concurrency=settings.replay_concurrency,
        )
        traces = await _extract_llm_outputs_from_replay(env) if fetch_traces else []
    finally:
        await terminate_demo_app_subprocess(key)

    duration_min = max(result.replay_duration_ms, 1) / 60000
    baseline = ServiceBaseline(
        service_name=service_name,
        p50_latency_ms=result.actual_p50_ms,
        p95_latency_ms=result.actual_p95_ms,
        p99_latency_ms=result.actual_p99_ms,
        error_rate_percent=result.actual_error_rate_percent,
        throughput_rpm=round(result.total_requests / duration_min, 1),
    )
    logger.info(
        "Demo baseline (production build): p50=%.1fms p95=%.1fms errors=%.2f%% traces=%d",
        baseline.p50_latency_ms, baseline.p95_latency_ms, baseline.error_rate_percent, len(traces),
    )
    return baseline, traces
