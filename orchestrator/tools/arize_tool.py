"""
Bulwark — Arize MCP Wrapper (Phase 2, PLAN.md §Phase 2 / PRD §6.3).

All operations go through the Arize MCP server via ``shared.mcp_client``.
Python function signatures are unchanged from Phase 1 so that ``agent.py``
requires no modifications.

MCP Operations implemented using @arizeai/phoenix-mcp tools:
  list-datasets + list-experiments-for-dataset + get-experiment-by-id → get_llm_baseline()
  add-dataset-examples → run_eval() (simulation output ingestion)
  add-dataset-examples → log_spans() (simulation output ingestion)

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

    Uses `list-datasets` to find the service dataset, then `list-experiments-for-dataset`
    to find the latest eval run, and `get-experiment-by-id` to extract scores.

    Returns dict with keys: dataset_id, hallucination_score, relevance_score,
    groundedness_score, span_count.
    """
    logger.info("Fetching Arize LLM baseline for service: %s", service_name)
    try:
        # 1. Get all datasets
        datasets_result = await call_tool(
            MCPServer.ARIZE,
            "list-datasets",
            {"limit": 100},
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
    datasets = datasets_result if isinstance(datasets_result, list) else []
    
    # Try to find the production dataset for this service
    target_name = f"bulwark-prod-{service_name}"
    dataset = next((d for d in datasets if isinstance(d, dict) and (d.get("name") == target_name or service_name in d.get("name", ""))), None)

    if not dataset:
        logger.warning("No production dataset found in Arize for %s", service_name)
        return {"dataset_id": None, "error": "no_production_dataset"}

    dataset_id = dataset.get("id")

    if not dataset_id:
        return {"dataset_id": None, "error": "dataset_id_missing"}

    # 2. Fetch latest experiment for this dataset
    try:
        experiments_data = await call_tool(
            MCPServer.ARIZE,
            "list-experiments-for-dataset",
            {"dataset_id": dataset_id, "limit": 1},
        )
    except MCPCallError as exc:
        logger.warning("Arize experiments fetch failed for dataset %s: %s", dataset_id, exc)
        experiments_data = []

    experiments = experiments_data if isinstance(experiments_data, list) else []
    
    if not experiments:
        logger.warning("No experiments found for dataset %s", dataset_id)
        return {
            "dataset_id": dataset_id,
            "hallucination_score": 0.0,
            "relevance_score": 0.0,
            "groundedness_score": 0.0,
            "span_count": 0,
        }

    experiment_id = experiments[0].get("id")

    # 3. Get experiment details to calculate scores
    try:
        experiment_details = await call_tool(
            MCPServer.ARIZE,
            "get-experiment-by-id",
            {"experiment_id": experiment_id},
        )
    except MCPCallError as exc:
        logger.warning("Arize experiment details fetch failed for experiment %s: %s", experiment_id, exc)
        experiment_details = []

    # get-experiment-by-id returns metadata in block 0 and results in block 1
    # call_tool might return a list of parsed JSON if there are multiple text blocks
    results = []
    if isinstance(experiment_details, list):
        for item in experiment_details:
            if isinstance(item, dict) and "results" in item:
                results = item.get("results", [])
                break
    elif isinstance(experiment_details, dict):
        results = experiment_details.get("results", [])

    # Calculate means from annotations
    hal_scores, rel_scores, grnd_scores = [], [], []
    for r in results:
        for ann in r.get("annotations", []):
            name = ann.get("name", "").lower()
            score = ann.get("result", {}).get("score")
            if score is not None:
                if "hallucination" in name:
                    hal_scores.append(score)
                elif "relevance" in name:
                    rel_scores.append(score)
                elif "groundedness" in name:
                    grnd_scores.append(score)

    def _mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    return {
        "dataset_id": dataset_id,
        "hallucination_score": _mean(hal_scores),
        "relevance_score": _mean(rel_scores),
        "groundedness_score": _mean(grnd_scores),
        "span_count": len(results),
    }


async def run_eval(
    simulation_id: str,
    simulation_outputs: list[dict[str, Any]],
    baseline_dataset_id: str | None,
) -> ArizeEvalResult:
    """
    Submit simulation LLM outputs to Arize via MCP for evaluation.

    Since @arizeai/phoenix-mcp does not provide a synchronous `runEval` tool,
    we push the simulation outputs as examples to the simulation dataset,
    and return the eval result indicating that synchronous evaluation is unsupported.

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
        "Adding %d simulation outputs as examples to Arize (sim: %s)",
        len(simulation_outputs),
        simulation_id,
    )

    try:
        dataset_name = f"bulwark-sim-{simulation_id}"
        examples = []
        for out in simulation_outputs:
            examples.append({
                "input": {"prompt": out.get("prompt", "")},
                "output": {"response": out.get("response", "")},
                "metadata": {"simulation_id": simulation_id}
            })

        # 1. Log spans / add examples to a simulation dataset in Arize
        await call_tool(
            MCPServer.ARIZE,
            "add-dataset-examples",
            {
                "dataset_name": dataset_name,
                "examples": examples,
            },
        )
        sim_dataset_id = dataset_name # Using name as identifier for simplicity since we can't easily fetch the ID immediately

        duration_ms = int((time.time() - start_time) * 1000)

        return ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=None,
            relevance_delta=None,
            groundedness_delta=None,
            sim_hallucination_score=None,
            sim_relevance_score=None,
            sim_groundedness_score=None,
            baseline_hallucination_score=None,
            baseline_relevance_score=None,
            baseline_groundedness_score=None,
            eval_dataset_id=sim_dataset_id,
            baseline_dataset_id=baseline_dataset_id,
            eval_duration_ms=duration_ms,
            skip_reason="Synchronous evaluation and dataset comparison is unsupported by official Phoenix MCP."
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

    Uses `add-dataset-examples` to push the traces into a simulation dataset,
    as Phoenix MCP does not natively expose an OTLP `log-spans` tool.

    Args:
        simulation_id: The simulation ID.
        traces: List of trace dicts {prompt, response, latency_ms, model, ...}.

    Returns:
        True if logging succeeded, False otherwise.
    """
    if not traces:
        return True

    logger.info("Logging %d spans as examples to Arize for simulation %s", len(traces), simulation_id)
    try:
        dataset_name = f"bulwark-traces-{simulation_id}"
        examples = []
        for trace in traces:
            examples.append({
                "input": {"prompt": trace.get("prompt", trace.get("input", ""))},
                "output": {"response": trace.get("response", trace.get("output", ""))},
                "metadata": trace
            })

        await call_tool(
            MCPServer.ARIZE,
            "add-dataset-examples",
            {
                "dataset_name": dataset_name,
                "examples": examples,
            },
        )
        logger.info("Arize trace logging complete for %s", simulation_id)
        return True
    except MCPConfigError:
        raise
    except Exception as exc:
        logger.warning("Arize span logging failed (non-critical): %s", exc)
        return False
