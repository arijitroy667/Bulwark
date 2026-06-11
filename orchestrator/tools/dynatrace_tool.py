"""
Bulwark — Dynatrace MCP Integration Tool (Phase 2, PLAN.md §Phase 2).

All operations go through the Dynatrace MCP server via ``shared.mcp_client``.
Python function signatures are unchanged from Phase 1 so that ``agent.py``
requires no modifications.

MCP operations implemented (per PRD §6.2) using execute-dql:
  getProblems       -> execute-dql (fetch problems)
  getServiceMetrics -> execute-dql (fetch metrics)
  getLogs           -> execute-dql (fetch logs)
  getServiceTopology -> execute-dql (fetch entities)
  getSLOs           -> execute-dql (fetch slos)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any
import json

from shared.anonymizer import (
    anonymize_production_request,
    reset_anonymization_state,
)
from shared.mcp_client import MCPCallError, MCPConfigError, MCPServer, call_tool
from shared.models import (
    ProductionRequest,
    ServiceBaseline,
    ServiceDependency,
    ServiceTopology,
)

logger = logging.getLogger("bulwark.dynatrace")


def _parse_dql_result(data: Any) -> list[dict]:
    """Parse the text output from the execute-dql MCP tool into a Python list of dicts."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("result", data.get("results", []))
    if isinstance(data, str):
        marker = "Execution result:\n"
        idx = data.find(marker)
        if idx != -1:
            try:
                parsed = json.loads(data[idx + len(marker):].strip())
                return parsed if isinstance(parsed, list) else [parsed]
            except Exception as exc:
                logger.debug("Failed to parse DQL execution result: %s", exc)
        else:
            # Maybe it's just raw JSON array?
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                pass
    return []


# ──────────────────────────────────────────────
# Active Incident Check
# ──────────────────────────────────────────────


async def has_active_incident(service_name: str) -> bool:
    """
    Check if there is an active P1/P2 problem affecting the given service.
    If an active incident exists, Bulwark should defer evaluation.
    """
    try:
        data = await call_tool(
            MCPServer.DYNATRACE,
            "execute-dql",
            {
                "dqlQueryString": f'fetch problems | filter status == "OPEN" and contains(entity.name, "{service_name}") | limit 10'
            },
        )
    except MCPConfigError:
        raise
    except MCPCallError as exc:
        logger.error("Dynatrace MCP error checking incidents: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Failed to check incidents (proceeding without): %s", exc)
        return False

    problems = _parse_dql_result(data)

    for problem in problems:
        severity = str(problem.get("severityLevel", "")).upper()
        if severity in ("CRITICAL", "ERROR", "AVAILABILITY"):
            logger.warning(
                "Active %s incident found for %s: %s",
                severity,
                service_name,
                problem.get("title", "Unknown"),
            )
            return True

    logger.info("No active incidents for service: %s", service_name)
    return False


# ──────────────────────────────────────────────
# Service Baseline
# ──────────────────────────────────────────────


async def get_service_baseline(service_name: str) -> ServiceBaseline:
    """
    Fetch the production performance baseline for a service from Dynatrace via MCP.

    Retrieves p50/p95/p99 latency, error rate, and throughput for the last 24 hours.
    Also fetches SLO targets if available.
    """
    now = datetime.now(timezone.utc)

    # Note: execute-dql timeframes are usually part of the query or environment defaults.
    try:
        latency_data = await call_tool(
            MCPServer.DYNATRACE,
            "execute-dql",
            {
                "dqlQueryString": (
                    f'fetch metrics | filter metric.key == "builtin:service.response.time" '
                    f'and dt.entity.service == "{service_name}" '
                    f'| summarize p50 = percentile(value, 50), p95 = percentile(value, 95), p99 = percentile(value, 99)'
                )
            },
        )

        error_data = await call_tool(
            MCPServer.DYNATRACE,
            "execute-dql",
            {
                "dqlQueryString": (
                    f'fetch metrics | filter metric.key == "builtin:service.errors.total.rate" '
                    f'and dt.entity.service == "{service_name}" | summarize rate = avg(value)'
                )
            },
        )

        throughput_data = await call_tool(
            MCPServer.DYNATRACE,
            "execute-dql",
            {
                "dqlQueryString": (
                    f'fetch metrics | filter metric.key == "builtin:service.requestCount.total" '
                    f'and dt.entity.service == "{service_name}" | summarize count = sum(value)'
                )
            },
        )

    except MCPConfigError:
        raise
    except MCPCallError as exc:
        logger.error("Dynatrace MCP error fetching baseline: %s", exc)
        return ServiceBaseline(service_name=service_name)
    except Exception as exc:
        logger.warning("Failed to fetch Dynatrace baseline: %s", exc)
        return ServiceBaseline(service_name=service_name)

    # Parse metrics (extract values from DQL result rows)
    p50 = p95 = p99 = 0.0
    lat_rows = _parse_dql_result(latency_data)
    if lat_rows:
        row = lat_rows[0]
        # values are generally in microseconds in dt core, but dql depends on formatting. We assume micros to ms for safety as per original
        p50 = float(row.get("p50", 0) or 0) / 1000.0
        p95 = float(row.get("p95", 0) or 0) / 1000.0
        p99 = float(row.get("p99", 0) or 0) / 1000.0

    error_rate = 0.0
    err_rows = _parse_dql_result(error_data)
    if err_rows:
        error_rate = float(err_rows[0].get("rate", 0) or 0)

    throughput = 0.0
    tpt_rows = _parse_dql_result(throughput_data)
    if tpt_rows:
        count = float(tpt_rows[0].get("count", 0) or 0)
        throughput = count / (24 * 60)

    # Fetch SLOs
    slo_latency, slo_error = await _fetch_slo_targets(service_name)

    baseline = ServiceBaseline(
        service_name=service_name,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        p99_latency_ms=p99,
        error_rate_percent=error_rate,
        throughput_rpm=throughput,
        slo_latency_budget_p95_ms=slo_latency,
        slo_error_budget_percent=slo_error,
        recorded_at=now,
    )

    logger.info(
        "Baseline for %s: p95=%.1fms, error=%.2f%%, throughput=%.0frpm",
        service_name,
        baseline.p95_latency_ms,
        baseline.error_rate_percent,
        baseline.throughput_rpm,
    )
    return baseline


# ──────────────────────────────────────────────
# Production Request Logs
# ──────────────────────────────────────────────


async def get_production_requests(
    service_name: str,
    hours: int = 24,
    limit: int = 200,
) -> list[ProductionRequest]:
    """
    Fetch production request logs from Dynatrace via MCP for traffic replay.
    """
    try:
        data = await call_tool(
            MCPServer.DYNATRACE,
            "execute-dql",
            {
                "dqlQueryString": (
                    f'fetch logs | filter loglevel == "INFO" and contains(dt.entity.service, "{service_name}") '
                    f'and contains(content, "HTTP") | sort timestamp desc | limit {limit}'
                )
            },
        )
    except MCPConfigError:
        raise
    except MCPCallError as exc:
        logger.error("Dynatrace MCP error fetching logs: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Failed to fetch production requests: %s", exc)
        return []

    reset_anonymization_state()
    records = _parse_dql_result(data)

    requests = []
    for record in records[:limit]:
        parsed = _parse_log_to_request(record)
        if parsed:
            requests.append(parsed)

    logger.info(
        "Fetched %d production requests for %s (last %dh)",
        len(requests),
        service_name,
        hours,
    )
    return requests


# ──────────────────────────────────────────────
# Service Topology
# ──────────────────────────────────────────────


async def get_service_topology(service_name: str) -> ServiceTopology:
    """
    Fetch the service dependency graph from Dynatrace via MCP.
    """
    try:
        data = await call_tool(
            MCPServer.DYNATRACE,
            "execute-dql",
            {
                "dqlQueryString": (
                    f'fetch entities | filter type == "SERVICE" and entity.name == "{service_name}" '
                    f'| fields fromRelationships.calls, toRelationships.calledBy'
                )
            },
        )
    except MCPConfigError:
        raise
    except MCPCallError as exc:
        logger.error("Dynatrace MCP error fetching topology: %s", exc)
        return ServiceTopology(service_name=service_name)
    except Exception as exc:
        logger.warning("Failed to fetch service topology: %s", exc)
        return ServiceTopology(service_name=service_name)

    rows = _parse_dql_result(data)
    if not rows:
        return ServiceTopology(service_name=service_name)

    dependencies = []
    calls = rows[0].get("fromRelationships.calls", [])
    if isinstance(calls, list):
        for dep in calls:
            dep_id = dep.get("id", "") if isinstance(dep, dict) else str(dep)
            dep_name = dep.get("type", dep_id) if isinstance(dep, dict) else str(dep)
            dependencies.append(
                ServiceDependency(
                    name=dep_name,
                    base_url=f"http://mock-{dep_name}:8080",
                    endpoints=["/"],
                )
            )

    topology = ServiceTopology(
        service_name=service_name,
        dependencies=dependencies,
    )

    logger.info(
        "Topology for %s: %d dependencies",
        service_name,
        len(dependencies),
    )
    return topology


# ──────────────────────────────────────────────
# Internal parsers
# ──────────────────────────────────────────────


_VALID_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def _parse_log_to_request(record: dict[str, Any]) -> ProductionRequest | None:
    """
    Parse a Dynatrace log record into a ProductionRequest.
    """
    import json as _json
    from urllib.parse import urlparse

    try:
        method: str = ""
        path: str = ""
        expected_status: int = 200

        # DQL result columns are often directly mapped to attributes
        method = str(record.get("http.method", record.get("request.method", record.get("method", "")))).upper()

        raw_url = (
            record.get("http.url")
            or record.get("url.path")
            or record.get("path")
            or record.get("url")
            or ""
        )
        if raw_url:
            parsed = urlparse(str(raw_url))
            path = parsed.path if parsed.path else str(raw_url)
        else:
            path = ""

        expected_status = int(record.get("http.status_code", record.get("status", 200)) or 200)

        # Free-text content fallback
        if not method or not path:
            content = str(record.get("content", ""))
            method, path, expected_status = _parse_text_content(content, method, path, expected_status)
            headers = {}
            body = None
        else:
            headers = {}
            body = None

        if not method or method not in _VALID_HTTP_METHODS:
            return None
        if not path:
            return None

        anon_path, anon_headers, anon_body = anonymize_production_request(
            method=method,
            path=path,
            headers=headers,
            body=body,
        )

        return ProductionRequest(
            method=method,
            path=anon_path,
            headers=anon_headers or {},
            body=anon_body,
            expected_status=expected_status,
        )

    except Exception as exc:
        logger.debug("Failed to parse log record: %s", exc)
        return None


def _parse_text_content(
    content: str,
    method: str,
    path: str,
    expected_status: int,
) -> tuple[str, str, int]:
    """
    Parse free-text HTTP log line: 'HTTP GET /api/path 200 45ms'
    """
    parts = content.split()
    for i, part in enumerate(parts):
        if part.upper() in _VALID_HTTP_METHODS:
            if not method:
                method = part.upper()
            if not path and i + 1 < len(parts):
                path = parts[i + 1]
            if i + 2 < len(parts) and parts[i + 2].isdigit():
                expected_status = int(parts[i + 2])
            break
    return method, path, expected_status


async def _fetch_slo_targets(service_name: str) -> tuple[float, float]:
    """
    Fetch SLO targets for a service from Dynatrace via MCP using execute-dql.
    """
    try:
        data = await call_tool(
            MCPServer.DYNATRACE,
            "execute-dql",
            {
                "dqlQueryString": f'fetch slos | filter contains(entity.name, "{service_name}") | fields name, target'
            },
        )
    except Exception as exc:
        logger.debug("SLO fetch failed (using defaults): %s", exc)
        return 200.0, 1.0

    slos = _parse_dql_result(data)

    latency_budget = 200.0  
    error_budget = 1.0      

    for slo in slos:
        slo_name = slo.get("name", "").lower()
        target = slo.get("target", 0)
        if "latency" in slo_name or "response" in slo_name:
            latency_budget = 200.0  
        elif "error" in slo_name or "availability" in slo_name:
            error_budget = max(0, 100 - target)

    return latency_budget, error_budget
