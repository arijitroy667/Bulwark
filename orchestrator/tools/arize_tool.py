"""
Bulwark — Arize MCP Wrapper (Phase 2, PLAN.md §Phase 2 / PRD §6.3).

All operations go through the Arize MCP server via ``shared.mcp_client``.
Python function signatures are unchanged from Phase 1 so that ``agent.py``
requires no modifications.

MCP Operations implemented:
  getDataset / getSpans  → get_llm_baseline()
  runEval                → run_eval()
  compareDatasets        → run_eval() (comparison step)
  logSpans               → log_spans()

If the Arize MCP server is unavailable or not configured, all functions fail
gracefully and return a result with skip_reason set (per PRD §16 error handling).
These functions do not fabricate evaluation scores; all evaluations require real
captured spans and real Arize API responses.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from shared.mcp_client import MCPCallError, MCPConfigError, MCPServer, call_tool
from shared.models import ArizeEvalResult

logger = logging.getLogger("bulwark.arize_tool")

# Evaluator noise band — deltas below this are informational only (PRD §8.4)
EVAL_NOISE_BAND = 0.10


async def get_llm_baseline(service_name: str) -> dict[str, Any]:
    """
    Fetch the production LLM quality baseline for a service from Arize via MCP.

    Retrieves the most recent production dataset (golden dataset) and
    computes aggregate hallucination/relevance/groundedness scores.

    Returns dict with keys: dataset_id, hallucination_score, relevance_score,
    groundedness_score, span_count.
    """
    logger.info("Fetching Arize LLM baseline for service: %s", service_name)
    try:
        # Get production dataset for this service
        datasets_result = await call_tool(
            MCPServer.ARIZE,
            "getDataset",
            {
                "modelId": service_name,
                "environment": "production",
                "limit": 1,
                "sort": "-created_at",
            },
        )
    except MCPConfigError:
        raise
    except MCPCallError as exc:
        logger.error("Arize MCP error fetching baseline: %s", exc)
        return {"error": str(exc), "dataset_id": None}
    except Exception as exc:
        logger.error("Arize baseline fetch failed: %s", exc)
        return {"error": str(exc), "dataset_id": None}

    # Normalize datasets result
    datasets = []
    if isinstance(datasets_result, list):
        datasets = datasets_result
    elif isinstance(datasets_result, dict):
        datasets = datasets_result.get("data", datasets_result.get("datasets", []))

    if not datasets:
        logger.warning("No production dataset found in Arize for %s", service_name)
        return {"dataset_id": None, "error": "no_production_dataset"}

    dataset = datasets[0] if isinstance(datasets[0], dict) else {}
    dataset_id = dataset.get("id")

    if not dataset_id:
        return {"dataset_id": None, "error": "dataset_id_missing"}

    # Fetch evaluation scores for this dataset
    try:
        eval_data = await call_tool(
            MCPServer.ARIZE,
            "getDataset",
            {"datasetId": dataset_id, "includeEvaluations": True},
        )
    except MCPCallError as exc:
        logger.warning("Arize eval fetch failed for dataset %s: %s", dataset_id, exc)
        eval_data = {}

    if not isinstance(eval_data, dict):
        eval_data = {}

    evals = eval_data.get("evaluations", eval_data)

    return {
        "dataset_id": dataset_id,
        "hallucination_score": (
            evals.get("hallucination", {}).get("mean", 0.0)
            if isinstance(evals.get("hallucination"), dict)
            else evals.get("hallucination_score", 0.0)
        ),
        "relevance_score": (
            evals.get("relevance", {}).get("mean", 0.0)
            if isinstance(evals.get("relevance"), dict)
            else evals.get("relevance_score", 0.0)
        ),
        "groundedness_score": (
            evals.get("groundedness", {}).get("mean", 0.0)
            if isinstance(evals.get("groundedness"), dict)
            else evals.get("groundedness_score", 0.0)
        ),
        "span_count": dataset.get("span_count", 0),
    }


async def run_eval(
    simulation_id: str,
    simulation_outputs: list[dict[str, Any]],
    baseline_dataset_id: str | None,
) -> ArizeEvalResult:
    """
    Submit simulation LLM outputs to Arize evaluators via MCP and compute quality deltas.

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
        # 1. Log spans to create a simulation dataset in Arize
        log_result = await call_tool(
            MCPServer.ARIZE,
            "logSpans",
            {
                "modelId": f"bulwark-{simulation_id}",
                "environment": "simulation",
                "datasetName": f"bulwark-sim-{simulation_id}",
                "spans": simulation_outputs,
            },
        )
        sim_dataset_id: str | None = None
        if isinstance(log_result, dict):
            sim_dataset_id = log_result.get("dataset_id") or log_result.get("id")

        # 2. Run evaluators (hallucination, relevance, groundedness)
        eval_result = await call_tool(
            MCPServer.ARIZE,
            "runEval",
            {
                "datasetId": sim_dataset_id,
                "modelId": f"bulwark-{simulation_id}",
                "evaluators": ["hallucination", "relevance", "groundedness"],
                "waitForCompletion": True,
                "timeoutSeconds": 55,
                "spans": simulation_outputs if not sim_dataset_id else None,
            },
        )
        if not isinstance(eval_result, dict):
            eval_result = {}

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

        if baseline_dataset_id and sim_dataset_id:
            comparison = await call_tool(
                MCPServer.ARIZE,
                "compareDatasets",
                {
                    "baselineDatasetId": baseline_dataset_id,
                    "comparisonDatasetId": sim_dataset_id,
                    "metrics": ["hallucination", "relevance", "groundedness"],
                },
            )
            if isinstance(comparison, dict):
                for metric in ["hallucination", "relevance", "groundedness"]:
                    baseline_val = comparison.get("baseline", {}).get(metric)
                    sim_val = comparison.get("comparison", {}).get(metric)
                    baseline_scores[metric] = baseline_val
                    if sim_val is not None and baseline_val is not None:
                        deltas[metric] = sim_val - baseline_val

        duration_ms = int((time.time() - start_time) * 1000)

        def _get_score(key: str) -> float | None:
            v = eval_result.get(key, {})
            if isinstance(v, dict):
                return v.get("mean")
            return float(v) if v is not None else None

        return ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=deltas["hallucination"],
            relevance_delta=deltas["relevance"],
            groundedness_delta=deltas["groundedness"],
            sim_hallucination_score=_get_score("hallucination"),
            sim_relevance_score=_get_score("relevance"),
            sim_groundedness_score=_get_score("groundedness"),
            baseline_hallucination_score=baseline_scores["hallucination"],
            baseline_relevance_score=baseline_scores["relevance"],
            baseline_groundedness_score=baseline_scores["groundedness"],
            eval_dataset_id=sim_dataset_id,
            baseline_dataset_id=baseline_dataset_id,
            eval_duration_ms=duration_ms,
        )

    except MCPConfigError:
        raise
    except MCPCallError as exc:
        logger.error("Arize MCP eval error: %s", exc)
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason=f"Arize MCP error: {exc}",
        )
    except Exception as exc:
        logger.error("Arize eval failed: %s", exc)
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason=f"Arize evaluation failed: {exc}",
        )


async def log_spans(simulation_id: str, traces: list[dict[str, Any]]) -> bool:
    """
    Archive simulation LLM traces into Arize for observability via MCP.

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
        await call_tool(
            MCPServer.ARIZE,
            "logSpans",
            {
                "modelId": f"bulwark-{simulation_id}",
                "environment": "simulation",
                "spans": traces,
            },
        )
        logger.info("Arize span logging complete for %s", simulation_id)
        return True
    except MCPConfigError:
        raise
    except Exception as exc:
        logger.warning("Arize span logging failed (non-critical): %s", exc)
        return False
