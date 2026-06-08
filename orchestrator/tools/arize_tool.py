"""
Bulwark — Arize MCP Wrapper (Phase 2, PRD §6.3).

Provides async functions that interface with the Arize platform for LLM
quality evaluation. Called only when LLM components are detected in the MR.

MCP Operations implemented:
  - get_llm_baseline()     → getSpans / getDataset
  - run_eval()             → runEval (hallucination, relevance, groundedness)
  - compare_datasets()     → compareDatasets
  - log_spans()            → logSpans

If the Arize API is unavailable, all functions fail gracefully and return
a result with skip_reason set (per PRD §16 error handling). These functions
do not fabricate development-mode Arize scores; all evaluations require real
captured spans and real Arize API responses.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from shared.config import settings
from shared.models import ArizeEvalResult

logger = logging.getLogger("bulwark.arize_tool")

# Arize API base URL
ARIZE_API_BASE = "https://api.arize.com/v1"

# Evaluator noise band — deltas below this are informational only (PRD §8.4)
EVAL_NOISE_BAND = 0.10


def _arize_headers() -> dict[str, str]:
    """Build Arize API request headers."""
    return {
        "Authorization": f"Bearer {settings.arize_api_key}",
        "Arize-Space-Id": settings.arize_space_id,
        "Content-Type": "application/json",
    }


async def get_llm_baseline(service_name: str) -> dict[str, Any]:
    """
    Fetch the production LLM quality baseline for a service from Arize.

    Retrieves the most recent production dataset (golden dataset) and
    computes aggregate hallucination/relevance/groundedness scores.

    Returns dict with keys: dataset_id, hallucination_score, relevance_score,
    groundedness_score, span_count.
    """
    logger.info("Fetching Arize LLM baseline for service: %s", service_name)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get production dataset for this service
            response = await client.get(
                f"{ARIZE_API_BASE}/datasets",
                headers=_arize_headers(),
                params={
                    "model_id": service_name,
                    "environment": "production",
                    "limit": 1,
                    "sort": "-created_at",
                },
            )
            response.raise_for_status()
            datasets = response.json().get("data", [])

            if not datasets:
                logger.warning("No production dataset found in Arize for %s", service_name)
                return {"dataset_id": None, "error": "no_production_dataset"}

            dataset = datasets[0]
            dataset_id = dataset["id"]

            # Fetch evaluation scores for this dataset
            eval_response = await client.get(
                f"{ARIZE_API_BASE}/datasets/{dataset_id}/evaluations",
                headers=_arize_headers(),
            )
            eval_response.raise_for_status()
            eval_data = eval_response.json()

            return {
                "dataset_id": dataset_id,
                "hallucination_score": eval_data.get("hallucination", {}).get("mean", 0.0),
                "relevance_score": eval_data.get("relevance", {}).get("mean", 0.0),
                "groundedness_score": eval_data.get("groundedness", {}).get("mean", 0.0),
                "span_count": dataset.get("span_count", 0),
            }

    except httpx.HTTPStatusError as e:
        logger.error("Arize API error fetching baseline: %s %s", e.response.status_code, e)
        return {"error": f"http_{e.response.status_code}", "dataset_id": None}
    except Exception as e:
        logger.error("Arize baseline fetch failed: %s", e)
        return {"error": str(e), "dataset_id": None}


async def run_eval(
    simulation_id: str,
    simulation_outputs: list[dict[str, Any]],
    baseline_dataset_id: str | None,
) -> ArizeEvalResult:
    """
    Submit simulation LLM outputs to Arize evaluators and compute quality deltas.

    Runs hallucination, relevance, and groundedness evaluators on simulation
    outputs, then compares against the production baseline dataset.

    Args:
        simulation_id: The simulation identifier (used for Arize dataset naming).
        simulation_outputs: List of {prompt, response, context} dicts from simulation.
        baseline_dataset_id: Arize dataset ID of the production baseline.

    Returns:
        ArizeEvalResult with score deltas and metadata.
    """
    start_time = time.time()

    if not simulation_outputs:
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason="No LLM outputs captured during simulation",
        )

    logger.info(
        "Running Arize evaluators on %d simulation outputs (sim: %s)",
        len(simulation_outputs),
        simulation_id,
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1. Create a simulation dataset in Arize
            create_resp = await client.post(
                f"{ARIZE_API_BASE}/datasets",
                headers=_arize_headers(),
                json={
                    "name": f"bulwark-sim-{simulation_id}",
                    "environment": "simulation",
                    "model_id": simulation_id,
                    "records": simulation_outputs,
                },
            )
            create_resp.raise_for_status()
            sim_dataset_id = create_resp.json()["id"]

            # 2. Run evaluators (hallucination, relevance, groundedness)
            eval_resp = await client.post(
                f"{ARIZE_API_BASE}/evaluations",
                headers=_arize_headers(),
                json={
                    "dataset_id": sim_dataset_id,
                    "evaluators": ["hallucination", "relevance", "groundedness"],
                    "wait_for_completion": True,
                    "timeout_s": 55,
                },
            )
            eval_resp.raise_for_status()
            sim_scores = eval_resp.json()

            # 3. Compare against baseline dataset if available
            deltas: dict[str, float | None] = {
                "hallucination": None,
                "relevance": None,
                "groundedness": None,
            }
            baseline_scores: dict[str, float | None] = {
                "hallucination": None,
                "relevance": None,
                "groundedness": None,
            }

            if baseline_dataset_id:
                compare_resp = await client.post(
                    f"{ARIZE_API_BASE}/compare",
                    headers=_arize_headers(),
                    json={
                        "baseline_dataset_id": baseline_dataset_id,
                        "comparison_dataset_id": sim_dataset_id,
                        "metrics": ["hallucination", "relevance", "groundedness"],
                    },
                )
                compare_resp.raise_for_status()
                comparison = compare_resp.json()

                for metric in ["hallucination", "relevance", "groundedness"]:
                    baseline_scores[metric] = comparison.get("baseline", {}).get(metric)
                    sim_val = comparison.get("comparison", {}).get(metric)
                    base_val = baseline_scores[metric]
                    if sim_val is not None and base_val is not None:
                        deltas[metric] = sim_val - base_val

            duration_ms = int((time.time() - start_time) * 1000)

            return ArizeEvalResult(
                llm_detected=True,
                hallucination_delta=deltas["hallucination"],
                relevance_delta=deltas["relevance"],
                groundedness_delta=deltas["groundedness"],
                sim_hallucination_score=sim_scores.get("hallucination", {}).get("mean"),
                sim_relevance_score=sim_scores.get("relevance", {}).get("mean"),
                sim_groundedness_score=sim_scores.get("groundedness", {}).get("mean"),
                baseline_hallucination_score=baseline_scores["hallucination"],
                baseline_relevance_score=baseline_scores["relevance"],
                baseline_groundedness_score=baseline_scores["groundedness"],
                eval_dataset_id=sim_dataset_id,
                baseline_dataset_id=baseline_dataset_id,
                eval_duration_ms=duration_ms,
            )

    except httpx.HTTPStatusError as e:
        logger.error("Arize eval API error: %s %s", e.response.status_code, e)
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason=f"Arize API error: HTTP {e.response.status_code}",
        )
    except Exception as e:
        logger.error("Arize eval failed: %s", e)
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason=f"Arize evaluation failed: {e}",
        )


async def log_spans(simulation_id: str, traces: list[dict[str, Any]]) -> bool:
    """
    Archive simulation LLM traces into Arize for observability.

    Args:
        simulation_id: The simulation ID (used as model_id in Arize).
        traces: List of trace dicts {prompt, response, latency_ms, model, ...}.

    Returns:
        True if logging succeeded, False otherwise.
    """
    if not traces:
        return True

    logger.info("Logging %d spans to Arize for simulation %s", len(traces), simulation_id)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ARIZE_API_BASE}/spans/batch",
                headers=_arize_headers(),
                json={
                    "model_id": f"bulwark-{simulation_id}",
                    "environment": "simulation",
                    "spans": traces,
                },
            )
            response.raise_for_status()
            logger.info("Arize span logging complete for %s", simulation_id)
            return True
    except Exception as e:
        logger.warning("Arize span logging failed (non-critical): %s", e)
        return False
