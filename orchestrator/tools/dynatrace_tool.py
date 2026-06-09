"""
Bulwark — Dynatrace MCP Integration Tool (Phase 2, PLAN.md §Phase 2).

All operations go through the Dynatrace MCP server via ``shared.mcp_client``.
Python function signatures are unchanged from Phase 1 so that ``agent.py``
requires no modifications.

MCP operations implemented (per PRD §6.2):
  getProblems       → has_active_incident()
  getServiceMetrics → get_service_baseline()
  getLogs           → get_production_requests()
  getServiceTopology → get_service_topology()
  getSLOs           → _fetch_slo_targets() (internal, called from baseline)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

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
            "getProblems",
            {
                "problemSelector": f'status("OPEN"),entityId("{service_name}")',
                "fields": "evidenceDetails,impactAnalysis",
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

    problems = []
    if isinstance(data, dict):
        problems = data.get("problems", [])
    elif isinstance(data, list):
        problems = data

    for problem in problems:
        severity = problem.get("severityLevel", "").upper()
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
    start_time = now - timedelta(hours=24)
    from_ts = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        latency_data = await call_tool(
            MCPServer.DYNATRACE,
            "getServiceMetrics",
            {
                "metricSelector": (
                    f"builtin:service.response.time"
                    f":filter(eq(dt.entity.service,{service_name}))"
                    f":percentile(50):percentile(95):percentile(99)"
                ),
                "from": from_ts,
                "to": to_ts,
                "resolution": "1d",
            },
        )

        error_data = await call_tool(
            MCPServer.DYNATRACE,
            "getServiceMetrics",
            {
                "metricSelector": (
                    f"builtin:service.errors.total.rate"
                    f":filter(eq(dt.entity.service,{service_name}))"
                ),
                "from": from_ts,
                "to": to_ts,
                "resolution": "1d",
            },
        )

        throughput_data = await call_tool(
            MCPServer.DYNATRACE,
            "getServiceMetrics",
            {
                "metricSelector": (
                    f"builtin:service.requestCount.total"
                    f":filter(eq(dt.entity.service,{service_name}))"
                ),
                "from": from_ts,
                "to": to_ts,
                "resolution": "1d",
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

    # Parse latency values (Dynatrace returns microseconds → convert to ms)
    p50, p95, p99 = _parse_latency_metrics(latency_data)
    error_rate = _parse_error_rate(error_data)
    throughput = _parse_throughput(throughput_data)

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

    Retrieves the last N hours of HTTP request logs, anonymizes all PII,
    and returns a list of ProductionRequest objects ready for replay.
    """
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=hours)

    try:
        data = await call_tool(
            MCPServer.DYNATRACE,
            "getLogs",
            {
                "query": (
                    f'loglevel="INFO" AND '
                    f'dt.entity.service="{service_name}" AND '
                    f'content="HTTP"'
                ),
                "from": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": limit,
                "sort": "-timestamp",
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

    # Reset anonymization state for a fresh session
    reset_anonymization_state()

    records = []
    if isinstance(data, dict):
        records = data.get("results", [])
    elif isinstance(data, list):
        records = data

    # Parse and anonymize log entries
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

    Returns the service's downstream dependencies — used to create WireMock stubs
    in the ephemeral environment.
    """
    try:
        data = await call_tool(
            MCPServer.DYNATRACE,
            "getServiceTopology",
            {
                "entityId": service_name,
                "fields": (
                    "fromRelationships.calls,"
                    "toRelationships.calledBy,"
                    "properties"
                ),
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

    if not isinstance(data, dict):
        return ServiceTopology(service_name=service_name)

    # Parse downstream dependencies
    dependencies = []
    calls = data.get("fromRelationships", {}).get("calls", [])
    for dep in calls:
        dep_id = dep.get("id", "")
        dep_name = dep.get("type", dep_id)
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


def _parse_latency_metrics(data: Any) -> tuple[float, float, float]:
    """Parse Dynatrace metrics response into p50/p95/p99 latency values in ms."""
    try:
        results = data.get("result", []) if isinstance(data, dict) else []
        p50 = p95 = p99 = 0.0
        for result in results:
            metric_id = result.get("metricId", "")
            values = result.get("data", [{}])[0].get("values", [0])
            value = values[0] if values else 0
            # Dynatrace returns microseconds — convert to ms
            value_ms = (value or 0) / 1000.0
            if "percentile(50)" in metric_id:
                p50 = value_ms
            elif "percentile(95)" in metric_id:
                p95 = value_ms
            elif "percentile(99)" in metric_id:
                p99 = value_ms
        return p50, p95, p99
    except (IndexError, KeyError, TypeError):
        return 0.0, 0.0, 0.0

_VALID_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def _parse_log_to_request(record: dict[str, Any]) -> ProductionRequest | None:
    """
    Parse a Dynatrace log record into a ProductionRequest.
    Applies PII anonymization before returning.

    Phase 4: Handles four real Dynatrace / OpenTelemetry record shapes:

    1. **Structured direct fields** (e.g., Dynatrace Log v1):
       ``{"method": "GET", "path": "/api/users", "status": 200}``

    2. **OpenTelemetry span attributes dict** (Dynatrace Distributed Tracing):
       ``{"attributes": {"http.method": "POST", "http.url": "https://svc/api", "http.status_code": 201}}``

    3. **Dynatrace Logs API v2 log.body wrapper**:
       ``{"log.body": {"content": "HTTP GET /api/users 200 45ms"}}``

    4. **Free-text content fallback** (Dynatrace Log v1 text):
       ``{"content": "HTTP GET /api/users/123 200 45ms"}``

    Records with invalid or absent HTTP methods are discarded (returns None).
    """
    import json as _json
    from urllib.parse import urlparse

    try:
        method: str = ""
        path: str = ""
        expected_status: int = 200

        # ── Path 1: OpenTelemetry span attributes dict ──
        attributes = record.get("attributes")
        if isinstance(attributes, dict):
            method = str(attributes.get("http.method", "")).upper()
            raw_url = attributes.get("http.url", "") or attributes.get("http.target", "")
            if raw_url:
                parsed = urlparse(str(raw_url))
                path = parsed.path or raw_url
            expected_status = int(attributes.get("http.status_code", 200) or 200)
            # Also pull headers/body from OTel attributes
            raw_headers = attributes.get("http.request.headers", {})
            headers: dict[str, str] = raw_headers if isinstance(raw_headers, dict) else {}
            body_raw = attributes.get("http.request.body")
            if isinstance(body_raw, str):
                try:
                    body: Any = _json.loads(body_raw)
                except _json.JSONDecodeError:
                    body = body_raw
            else:
                body = body_raw

        else:
            # ── Path 2: Structured direct fields ──
            method = str(record.get("method") or record.get("http.method", "")).upper()

            # Path/URL resolution — full URL → extract path component
            raw_url = (
                record.get("url")
                or record.get("http.url")
                or record.get("url.path")
                or record.get("path")
                or ""
            )
            if raw_url:
                parsed = urlparse(str(raw_url))
                path = parsed.path if parsed.path else str(raw_url)
            else:
                path = ""

            expected_status = int(record.get("status") or record.get("http.status_code", 200) or 200)
            raw_headers = record.get("http.request.headers", record.get("headers", {}))
            headers = raw_headers if isinstance(raw_headers, dict) else {}
            body_raw = record.get("http.request.body", record.get("body"))
            if isinstance(body_raw, str):
                try:
                    body = _json.loads(body_raw)
                except _json.JSONDecodeError:
                    body = body_raw
            else:
                body = body_raw

        # ── Path 3: Dynatrace Logs API v2 log.body wrapper ──
        if (not method or not path) and "log.body" in record:
            log_body = record["log.body"]
            content = (
                log_body.get("content", "") if isinstance(log_body, dict) else str(log_body)
            )
            method, path, expected_status = _parse_text_content(content, method, path, expected_status)
            headers = {}
            body = None

        # ── Path 4: Free-text content fallback ──
        if not method or not path:
            content = record.get("content", "")
            method, path, expected_status = _parse_text_content(content, method, path, expected_status)
            if not isinstance(headers, dict):
                headers = {}
            if "body" not in dir():
                body = None

        # Validate: discard records with absent or invalid HTTP methods
        if not method or method not in _VALID_HTTP_METHODS:
            logger.debug(
                "Discarding log record with invalid HTTP method '%s'", method
            )
            return None

        if not path:
            return None

        anon_path, anon_headers, anon_body = anonymize_production_request(
            method=method,
            path=path,
            headers=headers if isinstance(headers, dict) else {},
            body=body if isinstance(body, (dict, str)) or body is None else None,
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


def _parse_error_rate(data: Any) -> float:
    """Parse Dynatrace error rate metric response into a percentage."""
    try:
        results = data.get("result", []) if isinstance(data, dict) else []
        if results:
            values = results[0].get("data", [{}])[0].get("values", [0])
            return float(values[0] or 0)
        return 0.0
    except (IndexError, KeyError, TypeError):
        return 0.0


def _parse_throughput(data: Any) -> float:
    """Parse Dynatrace throughput metric into requests per minute."""
    try:
        results = data.get("result", []) if isinstance(data, dict) else []
        if results:
            values = results[0].get("data", [{}])[0].get("values", [0])
            # Convert from total count over 24h to RPM
            total_count = float(values[0] or 0)
            return total_count / (24 * 60)
        return 0.0
    except (IndexError, KeyError, TypeError):
        return 0.0


def _parse_text_content(
    content: str,
    method: str,
    path: str,
    expected_status: int,
) -> tuple[str, str, int]:
    """
    Parse free-text HTTP log line: 'HTTP GET /api/path 200 45ms' or
    'GET /api/path 200'.
    Returns (method, path, status) updating only fields that are currently unset.
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
    Fetch SLO targets for a service from Dynatrace via MCP.
    Returns (latency_budget_ms, error_budget_percent).
    """
    try:
        data = await call_tool(
            MCPServer.DYNATRACE,
            "getSLOs",
            {
                "sloSelector": f'entityId("{service_name}")',
                "pageSize": 10,
            },
        )
    except Exception as exc:
        logger.debug("SLO fetch failed (using defaults): %s", exc)
        return 200.0, 1.0  # v3 corrected defaults (not 20ms/0.5% from v1)

    slos = []
    if isinstance(data, dict):
        slos = data.get("slo", [])
    elif isinstance(data, list):
        slos = data

    latency_budget = 200.0  # v3 corrected default (20ms was below noise floor)
    error_budget = 1.0      # v3 corrected default

    for slo in slos:
        slo_name = slo.get("name", "").lower()
        target = slo.get("target", 0)
        if "latency" in slo_name or "response" in slo_name:
            latency_budget = 200.0  # Simplified: use corrected default
        elif "error" in slo_name or "availability" in slo_name:
            error_budget = max(0, 100 - target)  # budget = 100 - target

    return latency_budget, error_budget
