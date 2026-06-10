"""
Bulwark — Demo Mode Support (Phase 1, PLAN.md §Phase 1 & §Phase 2).

Provides synthetic Dynatrace & Arize baselines and request logs, and
manages isolated local `demo-app` subprocesses for GCP-free E2E runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from typing import Any

from shared.config import settings
from shared.models import (
    ArizeEvalResult,
    EnvironmentStatus,
    EphemeralEnvironment,
    ProductionRequest,
    ReplayResult,
    ServiceBaseline,
    ServiceDependency,
    ServiceTopology,
)

logger = logging.getLogger("bulwark.demo_mode")

# Dictionary to hold references to running demo-app subprocesses
# Key: simulation_id -> value: asyncio.subprocess.Process
_running_processes: dict[str, asyncio.subprocess.Process] = {}
_running_ports: dict[str, int] = {}


async def is_demo_mode_active() -> bool:
    """Check if Bulwark is running in E2E Demo Mode."""
    try:
        from shared.db import get_demo_mode
        return await get_demo_mode()
    except Exception:
        return settings.bulwark_demo_mode



def get_demo_baseline(service_name: str) -> ServiceBaseline:
    """Generate a realistic synthetic Dynatrace baseline for the demo service."""
    return ServiceBaseline(
        service_name=service_name,
        p50_latency_ms=110.0,
        p95_latency_ms=142.0,
        p99_latency_ms=180.0,
        error_rate_percent=0.12,
        throughput_rpm=3200.0,
        slo_latency_budget_p95_ms=200.0,
        slo_error_budget_percent=1.0,
    )


def get_demo_topology(service_name: str) -> ServiceTopology:
    """Generate a synthetic topology for the demo service."""
    return ServiceTopology(
        service_name=service_name,
        dependencies=[
            ServiceDependency(
                name="preferences-service",
                base_url=f"http://localhost:8082",  # Points to WireMock
                endpoints=["/api/preferences/{id}", "/api/preferences/batch"],
            )
        ],
    )


def get_demo_requests(service_name: str, limit: int = 200) -> list[ProductionRequest]:
    """Generate a list of 200 synthetic-but-realistic production requests."""
    requests = []
    
    # 70% GET /users
    # 20% GET /users/{id}
    # 5% GET /metrics
    # 5% GET /health
    for i in range(1, limit + 1):
        if i % 10 < 7:
            # GET /users
            requests.append(
                ProductionRequest(
                    method="GET",
                    path="/users",
                    headers={"Accept": "application/json"},
                    expected_status=200,
                )
            )
        elif i % 10 < 9:
            # GET /users/{id} (random user ID 1-25)
            user_id = (i % 25) + 1
            requests.append(
                ProductionRequest(
                    method="GET",
                    path=f"/users/{user_id}",
                    headers={"Accept": "application/json"},
                    expected_status=200,
                )
            )
        elif i % 20 == 18:
            # GET /metrics
            requests.append(
                ProductionRequest(
                    method="GET",
                    path="/metrics",
                    headers={"Accept": "application/json"},
                    expected_status=200,
                )
            )
        else:
            # GET /health
            requests.append(
                ProductionRequest(
                    method="GET",
                    path="/health",
                    headers={"Accept": "application/json"},
                    expected_status=200,
                )
            )
            
    return requests


def get_demo_arize_baseline(service_name: str) -> dict[str, Any]:
    """Generate synthetic Arize baseline dataset info."""
    return {
        "dataset_id": "ds_demo_baseline_987",
        "baseline_hallucination_score": 0.05,
        "baseline_relevance_score": 0.94,
        "baseline_groundedness_score": 0.96,
    }


def get_demo_arize_eval(
    simulation_id: str,
    has_llm_components: bool,
    is_regression: bool,
) -> ArizeEvalResult:
    """Generate synthetic Arize evaluation results for demo mode."""
    if not has_llm_components:
        return ArizeEvalResult(
            llm_detected=False,
            skip_reason="No LLM components detected via AST scan",
        )
        
    if is_regression:
        # Regression scenario: hallucination rises significantly, relevance drops
        return ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=0.15,  # Above 0.10 noise band
            relevance_delta=-0.12,     # Above 0.10 noise band
            groundedness_delta=0.03,   # Within noise band
            sim_hallucination_score=0.20,
            sim_relevance_score=0.82,
            sim_groundedness_score=0.93,
            baseline_hallucination_score=0.05,
            baseline_relevance_score=0.94,
            baseline_groundedness_score=0.96,
            eval_dataset_id=f"ds_eval_{simulation_id}",
            baseline_dataset_id="ds_demo_baseline_987",
            arize_span_logged=True,
            eval_duration_ms=1500,
        )
    else:
        # Fixed scenario: scores within noise band
        return ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=0.01,
            relevance_delta=-0.02,
            groundedness_delta=0.01,
            sim_hallucination_score=0.06,
            sim_relevance_score=0.92,
            sim_groundedness_score=0.97,
            baseline_hallucination_score=0.05,
            baseline_relevance_score=0.94,
            baseline_groundedness_score=0.96,
            eval_dataset_id=f"ds_eval_{simulation_id}",
            baseline_dataset_id="ds_demo_baseline_987",
            arize_span_logged=True,
            eval_duration_ms=1200,
        )


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def launch_demo_app_subprocess(
    simulation_id: str,
    demo_version: str,  # "regression" | "fixed" | "llm-demo"
) -> EphemeralEnvironment:
    """
    Launch the demo-app as a local subprocess on an available port.
    Returns the EphemeralEnvironment record pointing to it.
    """
    port = _find_free_port()
    
    # Root of the Bulwark project (parent of demo-app directory)
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # We will spawn the uvicorn server for the demo app using the current python executable
    env = os.environ.copy()
    env["DEMO_VERSION"] = demo_version
    env["PORT"] = str(port)
    env["WIREMOCK_BASE_URL"] = settings.wiremock_base_url or "http://localhost:8082"
    env["SIMULATED_DB_LATENCY_MS"] = "12"
    
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "demo-app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    
    logger.info("Launching demo app: %s on port %d in %s mode", " ".join(cmd), port, demo_version)
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        _running_processes[simulation_id] = proc
        _running_ports[simulation_id] = port
        
        # Wait a moment and check health to make sure it started successfully
        await asyncio.sleep(2.0)
        
        if proc.returncode is not None:
            # Subprocess failed to start or crashed immediately
            stdout, stderr = await proc.communicate()
            error_msg = f"Demo app failed to start. exit={proc.returncode}. stderr={stderr.decode()}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
            
        base_url = f"http://127.0.0.1:{port}"
        
        # Configure WireMock stubs if needed (already running in compose)
        # We can write stubs directly to WireMock via Admin API
        
        return EphemeralEnvironment(
            simulation_id=simulation_id,
            base_url=base_url,
            mock_base_url=env["WIREMOCK_BASE_URL"],
            build_duration_ms=500,  # Instant start
            deployed_at=datetime.now(timezone.utc),
            status=EnvironmentStatus.HEALTHY,
        )
    except Exception as e:
        logger.error("Failed to launch demo app subprocess: %s", e)
        return EphemeralEnvironment(
            simulation_id=simulation_id,
            status=EnvironmentStatus.BUILD_FAILED,
            startup_log_tail=str(e),
        )


async def terminate_demo_app_subprocess(simulation_id: str) -> None:
    """Terminate the running demo-app subprocess for the given simulation ID."""
    proc = _running_processes.pop(simulation_id, None)
    port = _running_ports.pop(simulation_id, None)
    
    if proc:
        logger.info("Terminating demo app subprocess on port %s for simulation %s", port, simulation_id)
        try:
            proc.terminate()
            # Wait with a short timeout
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Demo app subprocess did not terminate gracefully; killing it")
            try:
                proc.kill()
                await proc.wait()
            except Exception as e:
                logger.error("Failed to kill demo app subprocess: %s", e)
        except Exception as e:
            logger.error("Failed to terminate demo app subprocess: %s", e)
