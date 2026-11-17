"""
Local Arize MCP server for Phase 6 tests.

Implements real MCP tools over Streamable-HTTP using FastMCP.

Tools implemented:
  getDataset       → returns baseline dataset metadata + eval scores
  logSpans         → records spans, returns dataset_id
  runEval          → returns configurable eval results
  compareDatasets  → returns configurable score deltas
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

# ──────────────────────────────────────────────────────────────────────────────
# Shared state
# ──────────────────────────────────────────────────────────────────────────────

state: dict[str, Any] = {
    # Baseline dataset returned by getDataset
    "baseline_dataset": {
        "dataset_id": "baseline-dataset-001",
        "hallucination_score": 0.05,
        "relevance_score": 0.92,
        "groundedness_score": 0.88,
        "record_count": 150,
    },
    # What runEval returns
    "eval_results": {
        "eval_id": "eval-run-42",
        "hallucination_score": 0.07,
        "relevance_score": 0.91,
        "groundedness_score": 0.87,
        "evaluated_count": 45,
    },
    # What compareDatasets returns
    "comparison_results": {
        "hallucination_delta": 0.02,
        "relevance_delta": -0.01,
        "groundedness_delta": -0.01,
        "baseline_dataset_id": "baseline-dataset-001",
        "simulation_dataset_id": "sim-dataset-001",
    },
    # Spans logged by logSpans (for inspection)
    "logged_spans": [],
    # dataset_id returned by logSpans
    "logged_dataset_id": "sim-dataset-001",
    # Set to None to simulate missing baseline
    "baseline_missing": False,
}


def reset_state() -> None:
    """Reset mutable state between tests."""
    state["logged_spans"].clear()
    state["baseline_missing"] = False
    state["baseline_dataset"] = {
        "dataset_id": "baseline-dataset-001",
        "hallucination_score": 0.05,
        "relevance_score": 0.92,
        "groundedness_score": 0.88,
        "record_count": 150,
    }
    state["comparison_results"] = {
        "hallucination_delta": 0.02,
        "relevance_delta": -0.01,
        "groundedness_delta": -0.01,
        "baseline_dataset_id": "baseline-dataset-001",
        "simulation_dataset_id": "sim-dataset-001",
    }


# ──────────────────────────────────────────────────────────────────────────────
# FastMCP server
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("arize-local")


@mcp.tool()
def getDataset(
    spaceId: str = "",
    datasetName: str = "",
    datasetId: str = "",
) -> str:
    """Return baseline dataset with evaluation scores."""
    if state["baseline_missing"]:
        return json.dumps({"dataset_id": None, "error": "No baseline dataset found"})
    return json.dumps(state["baseline_dataset"])


@mcp.tool()
def logSpans(
    spaceId: str = "",
    datasetName: str = "",
    spans: list[dict[str, Any]] | None = None,
) -> str:
    """Record LLM spans. Returns the dataset_id for subsequent eval calls."""
    spans_list = spans or []
    state["logged_spans"].extend(spans_list)
    return json.dumps({
        "dataset_id": state["logged_dataset_id"],
        "logged_count": len(spans_list),
        "success": True,
    })


@mcp.tool()
def runEval(
    spaceId: str = "",
    datasetId: str = "",
    evalTypes: list[str] | None = None,
) -> str:
    """Run evaluation (hallucination/relevance/groundedness) on a dataset."""
    return json.dumps(state["eval_results"])


# ── Official Phoenix MCP tool names (what orchestrator.tools.arize_tool calls) ──


@mcp.tool(name="list-datasets")
def list_datasets(limit: int = 100) -> str:
    if state["baseline_missing"]:
        return json.dumps([])
    return json.dumps([{"id": state["baseline_dataset"]["dataset_id"], "name": "bulwark-prod-demo-app"}])


@mcp.tool(name="list-experiments-for-dataset")
def list_experiments_for_dataset(dataset_id: str, limit: int = 1) -> str:
    return json.dumps([{"id": "experiment-001"}])


@mcp.tool(name="get-experiment-by-id")
def get_experiment_by_id(experiment_id: str) -> str:
    b = state["baseline_dataset"]
    annotations = [
        {"name": "hallucination", "result": {"score": b["hallucination_score"]}},
        {"name": "relevance", "result": {"score": b["relevance_score"]}},
        {"name": "groundedness", "result": {"score": b["groundedness_score"]}},
    ]
    return json.dumps({"results": [{"annotations": annotations}] * 3})


@mcp.tool(name="add-dataset-examples")
def add_dataset_examples(dataset_name: str, examples: list[dict[str, Any]]) -> str:
    state["logged_spans"].extend(examples)
    return json.dumps({"dataset_name": dataset_name, "added": len(examples)})


@mcp.tool()
def compareDatasets(
    spaceId: str = "",
    baselineDatasetId: str = "",
    simulationDatasetId: str = "",
) -> str:
    """Compare simulation dataset against production baseline. Returns score deltas."""
    return json.dumps(state["comparison_results"])
