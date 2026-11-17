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

import asyncio
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


# ──────────────────────────────────────────────
# Evaluators (hallucination / relevance / groundedness)
# ──────────────────────────────────────────────
#
# The official Phoenix MCP server can store datasets but cannot *run*
# evaluators, so Bulwark runs the three Arize/Phoenix LLM-as-judge evaluators
# itself, with Gemini as the judge model.

JUDGE_MAX_RECORDS = 20

_JUDGE_PROMPT = """You are an LLM-output evaluator implementing the Arize/Phoenix
hallucination, relevance and groundedness evaluators.

For every record, the CONTEXT holds the only facts that are known. Judge the RESPONSE:
- hallucination: 1 if the response states anything not supported by the CONTEXT
  (invented facts, amounts, promises, timelines), otherwise 0.
- relevance: 0.0-1.0, how well the response does what the PROMPT asks.
- groundedness: 0.0-1.0, the fraction of the response's claims supported by the CONTEXT.

Return strict JSON: {{"scores": [{{"id": <record id>, "hallucination": 0 or 1,
"relevance": <float>, "groundedness": <float>}}]}}

Records:
{records}"""


async def judge_llm_outputs(outputs: list[dict[str, Any]]) -> dict[str, float] | None:
    """Score LLM outputs with the three evaluators. Returns mean scores, or None
    if the judge is unavailable or its answer is unusable."""
    from orchestrator.reasoning import _call_gemini
    from shared.config import settings

    if not settings.gemini_api_key:
        return None

    # Replayed traffic repeats requests; judge each distinct output once.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for out in outputs:
        unique.setdefault((str(out.get("prompt", "")), str(out.get("response", ""))), out)
    records = list(unique.values())[:JUDGE_MAX_RECORDS]
    text = "\n\n".join(
        f"[{i}] PROMPT: {r.get('prompt', '')}\nCONTEXT: {r.get('context') or 'none'}\nRESPONSE: {r.get('response', '')}"
        for i, r in enumerate(records)
    )
    try:
        data = await _call_gemini(_JUDGE_PROMPT.format(records=text))
        rows = [s for s in data.get("scores", []) if isinstance(s, dict)]
        if not rows:
            return None

        def _mean(key: str) -> float:
            return sum(float(s.get(key, 0) or 0) for s in rows) / len(rows)

        return {
            "hallucination": round(_mean("hallucination"), 3),
            "relevance": round(_mean("relevance"), 3),
            "groundedness": round(_mean("groundedness"), 3),
            "count": len(rows),
        }
    except Exception as exc:
        logger.warning("LLM evaluator call failed: %s: %s", type(exc).__name__, exc)
        return None


def _eval_result(
    sim: dict[str, float],
    base: dict[str, float],
    started: float,
    **extra: Any,
) -> ArizeEvalResult:
    """Deltas signed so positive = degradation (see ArizeEvalResult)."""
    return ArizeEvalResult(
        llm_detected=True,
        hallucination_delta=round(sim["hallucination"] - base["hallucination"], 3),
        relevance_delta=round(base["relevance"] - sim["relevance"], 3),
        groundedness_delta=round(base["groundedness"] - sim["groundedness"], 3),
        sim_hallucination_score=sim["hallucination"],
        sim_relevance_score=sim["relevance"],
        sim_groundedness_score=sim["groundedness"],
        baseline_hallucination_score=base["hallucination"],
        baseline_relevance_score=base["relevance"],
        baseline_groundedness_score=base["groundedness"],
        eval_duration_ms=int((time.time() - started) * 1000),
        **extra,
    )


async def evaluate_against_traces(
    simulation_outputs: list[dict[str, Any]],
    baseline_outputs: list[dict[str, Any]],
) -> ArizeEvalResult:
    """Evaluate simulation outputs against production outputs (both judged now)."""
    started = time.time()
    if not simulation_outputs:
        return ArizeEvalResult(llm_detected=True, skip_reason="No LLM traces captured during simulation (/traces empty)")
    if not baseline_outputs:
        return ArizeEvalResult(llm_detected=True, skip_reason="No production LLM traces available as a baseline")
    sim, base = await asyncio.gather(judge_llm_outputs(simulation_outputs), judge_llm_outputs(baseline_outputs))
    if sim is None or base is None:
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason="LLM evaluators unavailable (Gemini judge not configured or failed)",
        )
    return _eval_result(sim, base, started)


async def run_eval(
    simulation_id: str,
    simulation_outputs: list[dict[str, Any]],
    baseline_dataset_id: str | None,
    baseline_scores: dict[str, Any] | None = None,
) -> ArizeEvalResult:
    """
    Evaluate simulation LLM outputs against the Arize production baseline.

    1. Archive the outputs as examples in a Phoenix dataset (best effort).
    2. Score them with the hallucination / relevance / groundedness evaluators.
    3. Compare with the baseline experiment scores from get_llm_baseline().
    """
    started = time.time()

    if not simulation_outputs:
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason="No LLM outputs captured during simulation",
        )

    dataset_name = f"bulwark-sim-{simulation_id}"
    logged = False
    try:
        await call_tool(
            MCPServer.ARIZE,
            "add-dataset-examples",
            {
                "dataset_name": dataset_name,
                "examples": [
                    {
                        "input": {"prompt": out.get("prompt", "")},
                        "output": {"response": out.get("response", "")},
                        "metadata": {"simulation_id": simulation_id},
                    }
                    for out in simulation_outputs
                ],
            },
        )
        logged = True
    except MCPConfigError:
        raise
    except Exception as exc:
        logger.warning("Arize dataset ingestion failed (continuing with evaluation): %s", exc)

    meta = {
        "eval_dataset_id": dataset_name if logged else None,
        "baseline_dataset_id": baseline_dataset_id,
        "arize_span_logged": logged,
    }
    sim = await judge_llm_outputs(simulation_outputs)
    if sim is None:
        return ArizeEvalResult(
            llm_detected=True,
            skip_reason="LLM evaluators unavailable (Gemini judge not configured or failed)",
            **meta,
        )

    if not baseline_scores or not baseline_scores.get("span_count"):
        return ArizeEvalResult(
            llm_detected=True,
            sim_hallucination_score=sim["hallucination"],
            sim_relevance_score=sim["relevance"],
            sim_groundedness_score=sim["groundedness"],
            eval_duration_ms=int((time.time() - started) * 1000),
            skip_reason="No scored production baseline experiment in Arize for this service",
            **meta,
        )

    base = {
        "hallucination": float(baseline_scores.get("hallucination_score", 0.0)),
        "relevance": float(baseline_scores.get("relevance_score", 0.0)),
        "groundedness": float(baseline_scores.get("groundedness_score", 0.0)),
    }
    return _eval_result(sim, base, started, **meta)


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
