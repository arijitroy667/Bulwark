"""
Local Dynatrace MCP server for Phase 6 tests.

Implements real MCP tools over Streamable-HTTP using FastMCP.

Tools implemented:
  getProblems        → returns state["problems"] (empty list = no incident)
  getServiceMetrics  → returns state["metrics"]
  getLogs            → returns state["logs"]
  getServiceTopology → returns state["topology"]
  getSLOs            → returns state["slos"]
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

# ──────────────────────────────────────────────────────────────────────────────
# Shared state
# ──────────────────────────────────────────────────────────────────────────────

state: dict[str, Any] = {
    "problems": [],   # empty = no active incident
    "metrics": {
        "p50LatencyMs": 110.0,
        "p95LatencyMs": 142.0,
        "p99LatencyMs": 180.0,
        "errorRatePercent": 0.12,
        "throughputRpm": 3200.0,
        "sloLatencyBudgetP95Ms": 200.0,
        "sloErrorBudgetPercent": 1.0,
    },
    # Default: OTel-style log entries for Dynatrace log tool
    "logs": [
        {
            "attributes": {
                "request.method": "GET",
                "url.path": "/users",
                "http.response.status_code": 200,
                "http.request.header.authorization": "Bearer secret123",
            },
        },
        {
            "attributes": {
                "request.method": "POST",
                "url.path": "/users",
                "http.response.status_code": 201,
                "body": '{"name": "Alice", "email": "alice@example.com"}',
            },
        },
        {
            "attributes": {
                "request.method": "GET",
                "url.path": "/health",
                "http.response.status_code": 200,
            },
        },
    ],
    "topology": {
        "serviceId": "demo-app",
        "dependencies": [
            {
                "serviceId": "preferences-service",
                "serviceName": "preferences-service",
                "host": "prefs.internal",
                "port": 8080,
                "protocol": "http",
            }
        ],
    },
    "slos": [
        {
            "name": "p95-latency-slo",
            "target": 200.0,
            "metricKey": "p95_latency_ms",
        }
    ],
}


def reset_state() -> None:
    """Reset to defaults between tests."""
    state["problems"] = []
    state["logs"] = []
    state["metrics"] = {
        "p50LatencyMs": 110.0,
        "p95LatencyMs": 142.0,
        "p99LatencyMs": 180.0,
        "errorRatePercent": 0.12,
        "throughputRpm": 3200.0,
        "sloLatencyBudgetP95Ms": 200.0,
        "sloErrorBudgetPercent": 1.0,
    }
    state["slos"] = [
        {
            "name": "p95-latency-slo",
            "target": 200.0,
            "metricKey": "p95_latency_ms",
        }
    ]
    state["topology"] = {
        "serviceId": "demo-app",
        "dependencies": [
            {
                "serviceId": "preferences-service",
                "serviceName": "preferences-service",
                "host": "prefs.internal",
                "port": 8080,
                "protocol": "http",
            }
        ],
    }


# ──────────────────────────────────────────────────────────────────────────────
# FastMCP server
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("dynatrace-local")


@mcp.tool()
def getProblems(
    entitySelector: str = "",
    problemSelector: str = "",
    from_: str = "now-1h",
    pageSize: int = 50,
) -> str:
    """Return active problems. Empty list = no incident."""
    return json.dumps({"problems": state["problems"]})


@mcp.tool()
def getServiceMetrics(
    entityId: str = "",
    serviceName: str = "",
    metricSelector: str = "",
    from_: str = "now-1h",
    to: str = "now",
) -> str:
    """Return service performance metrics (p50/p95/p99/error-rate/throughput)."""
    return json.dumps(state["metrics"])


@mcp.tool()
def getLogs(
    entitySelector: str = "",
    query: str = "",
    from_: str = "now-24h",
    to: str = "now",
    limit: int = 200,
    fields: str = "",
) -> str:
    """Return log/span entries for request sampling."""
    return json.dumps({"results": state["logs"]})


@mcp.tool()
def getServiceTopology(
    entityId: str = "",
    serviceName: str = "",
) -> str:
    """Return service dependency topology."""
    return json.dumps(state["topology"])


@mcp.tool()
def getSLOs(
    sloSelector: str = "",
    entitySelector: str = "",
) -> str:
    """Return SLO targets for a service."""
    return json.dumps({"slos": state["slos"]})
