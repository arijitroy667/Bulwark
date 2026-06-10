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


@mcp.tool()
def compareDatasets(
    spaceId: str = "",
    baselineDatasetId: str = "",
    simulationDatasetId: str = "",
) -> str:
    """Compare simulation dataset against production baseline. Returns score deltas."""
    return json.dumps(state["comparison_results"])
