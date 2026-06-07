"""
Bulwark — Dynatrace MCP Integration Tool.

Wrapper functions over the Dynatrace API for all operations defined in PRD Section 4.2:
  - Check for active production incidents
  - Fetch service performance baselines (p50/p95/p99 latency, error rate, throughput)
  - Fetch SLO targets
  - Retrieve production request logs for replay
  - Fetch service topology (dependencies)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from shared.anonymizer import (
    anonymize_production_request,
    reset_anonymization_state,
)
from shared.config import settings
from shared.models import (
    ProductionRequest,
    ServiceBaseline,
    ServiceDependency,
    ServiceTopology,
)

logger = logging.getLogger("bulwark.dynatrace")


# ──────────────────────────────────────────────
# HTTP client
# ──────────────────────────────────────────────


def _get_headers() -> dict[str, str]:
    """Build Dynatrace API request headers with authentication."""
    return {
        "Authorization": f"Api-Token {settings.dynatrace_api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api_url(path: str) -> str:
    """Build a full Dynatrace API URL."""
    return f"{settings.dynatrace_base_url}{path}"


# ──────────────────────────────────────────────
# Active Incident Check
# ──────────────────────────────────────────────


async def has_active_incident(service_name: str) -> bool:
    """
    Check if there is an active P1/P2 problem affecting the given service.
    If an active incident exists, Bulwark should defer evaluation.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                _api_url("/problems"),
                headers=_get_headers(),
                params={
                    "problemSelector": f'status("OPEN"),entityId("{service_name}")',
                    "fields": "evidenceDetails,impactAnalysis",
                },
            )
            response.raise_for_status()
            data = response.json()

        problems = data.get("problems", [])
        # Check for critical or high severity problems
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

    except httpx.HTTPStatusError as e:
        logger.error("Dynatrace API error checking incidents: %s", e)
        return False
    except Exception as e:
        logger.warning("Failed to check incidents (proceeding without): %s", e)
        return False


# ──────────────────────────────────────────────
# Service Baseline
# ──────────────────────────────────────────────


async def get_service_baseline(service_name: str) -> ServiceBaseline:
    """
    Fetch the production performance baseline for a service from Dynatrace.

    Retrieves p50/p95/p99 latency, error rate, and throughput for the last 24 hours.
    Also fetches SLO targets if available.
    """
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=24)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch response time metrics
            latency_response = await client.get(
                _api_url("/metrics/query"),
                headers=_get_headers(),
                params={
                    "metricSelector": (
                        f"builtin:service.response.time"
                        f":filter(eq(dt.entity.service,{service_name}))"
                        f":percentile(50):percentile(95):percentile(99)"
                    ),
                    "from": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "resolution": "1d",
                },
            )
            latency_response.raise_for_status()
            latency_data = latency_response.json()

            # Fetch error rate metrics
            error_response = await client.get(
                _api_url("/metrics/query"),
                headers=_get_headers(),
                params={
                    "metricSelector": (
                        f"builtin:service.errors.total.rate"
                        f":filter(eq(dt.entity.service,{service_name}))"
                    ),
                    "from": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "resolution": "1d",
                },
            )
            error_response.raise_for_status()
            error_data = error_response.json()

            # Fetch throughput metrics
            throughput_response = await client.get(
                _api_url("/metrics/query"),
                headers=_get_headers(),
                params={
                    "metricSelector": (
                        f"builtin:service.requestCount.total"
                        f":filter(eq(dt.entity.service,{service_name}))"
                    ),
                    "from": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "resolution": "1d",
                },
            )
            throughput_response.raise_for_status()
            throughput_data = throughput_response.json()

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
            slo_latency_budget_ms=slo_latency,
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

    except httpx.HTTPStatusError as e:
        logger.error("Dynatrace API error fetching baseline: %s", e)
        # Return a default baseline — the verdict will note the missing data
        return ServiceBaseline(service_name=service_name)
    except Exception as e:
        logger.warning("Failed to fetch Dynatrace baseline: %s", e)
        return ServiceBaseline(service_name=service_name)


# ──────────────────────────────────────────────
# Production Request Logs
# ──────────────────────────────────────────────


async def get_production_requests(
    service_name: str,
    hours: int = 24,
    limit: int = 200,
) -> list[ProductionRequest]:
    """
    Fetch production request logs from Dynatrace for traffic replay.

    Retrieves the last N hours of HTTP request logs, anonymizes all PII,
    and returns a list of ProductionRequest objects ready for replay.
    """
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=hours)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                _api_url("/logs/search"),
                headers=_get_headers(),
                params={
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
            response.raise_for_status()
            data = response.json()

        # Reset anonymization state for a fresh session
        reset_anonymization_state()

        # Parse and anonymize log entries
        requests = []
        for record in data.get("results", [])[:limit]:
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

    except httpx.HTTPStatusError as e:
        logger.error("Dynatrace API error fetching logs: %s", e)
        return []
    except Exception as e:
        logger.warning("Failed to fetch production requests: %s", e)
        return []


# ──────────────────────────────────────────────
# Service Topology
# ──────────────────────────────────────────────


async def get_service_topology(service_name: str) -> ServiceTopology:
    """
    Fetch the service dependency graph from Dynatrace.

    Returns the service's downstream dependencies — used to create WireMock stubs
    in the ephemeral environment.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch service entity with relationships
            response = await client.get(
                _api_url(f"/entities/{service_name}"),
                headers=_get_headers(),
                params={
                    "fields": (
                        "fromRelationships.calls,"
                        "toRelationships.calledBy,"
                        "properties"
                    ),
                },
            )
            response.raise_for_status()
            data = response.json()

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

    except httpx.HTTPStatusError as e:
        logger.error("Dynatrace API error fetching topology: %s", e)
        return ServiceTopology(service_name=service_name)
    except Exception as e:
        logger.warning("Failed to fetch service topology: %s", e)
        return ServiceTopology(service_name=service_name)


# ──────────────────────────────────────────────
# Internal parsers
# ──────────────────────────────────────────────


def _parse_latency_metrics(data: dict[str, Any]) -> tuple[float, float, float]:
    """Parse Dynatrace metrics response into p50/p95/p99 latency values in ms."""
    try:
        results = data.get("result", [])
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


def _parse_error_rate(data: dict[str, Any]) -> float:
    """Parse Dynatrace error rate metric response into a percentage."""
    try:
        results = data.get("result", [])
        if results:
            values = results[0].get("data", [{}])[0].get("values", [0])
            return float(values[0] or 0)
        return 0.0
    except (IndexError, KeyError, TypeError):
        return 0.0


def _parse_throughput(data: dict[str, Any]) -> float:
    """Parse Dynatrace throughput metric into requests per minute."""
    try:
        results = data.get("result", [])
        if results:
            values = results[0].get("data", [{}])[0].get("values", [0])
            # Convert from total count over 24h to RPM
            total_count = float(values[0] or 0)
            return total_count / (24 * 60)
        return 0.0
    except (IndexError, KeyError, TypeError):
        return 0.0


def _parse_log_to_request(record: dict[str, Any]) -> ProductionRequest | None:
    """
    Parse a Dynatrace log record into a ProductionRequest.
    Applies PII anonymization before returning.
    """
    try:
        content = record.get("content", "")
        # Parse HTTP method and path from log content
        # Typical format: "HTTP GET /api/users/123 200 45ms"
        parts = content.split()
        if len(parts) < 3:
            return None

        method = "GET"
        path = "/"
        expected_status = 200

        for i, part in enumerate(parts):
            if part.upper() in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                method = part.upper()
                if i + 1 < len(parts):
                    path = parts[i + 1]
                if i + 2 < len(parts) and parts[i + 2].isdigit():
                    expected_status = int(parts[i + 2])
                break

        # Anonymize the request
        anon_path, anon_headers, anon_body = anonymize_production_request(
            method=method,
            path=path,
            headers={},
            body=None,
        )

        return ProductionRequest(
            method=method,
            path=anon_path,
            headers=anon_headers or {},
            body=anon_body,
            expected_status=expected_status,
        )

    except Exception as e:
        logger.debug("Failed to parse log record: %s", e)
        return None


async def _fetch_slo_targets(service_name: str) -> tuple[float, float]:
    """
    Fetch SLO targets for a service from Dynatrace.
    Returns (latency_budget_ms, error_budget_percent).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                _api_url("/slo"),
                headers=_get_headers(),
                params={
                    "sloSelector": f'entityId("{service_name}")',
                    "pageSize": 10,
                },
            )
            if response.status_code == 404:
                return 20.0, 0.5  # Default budgets
            response.raise_for_status()
            data = response.json()

        slos = data.get("slo", [])
        latency_budget = 20.0  # default
        error_budget = 0.5  # default

        for slo in slos:
            slo_name = slo.get("name", "").lower()
            target = slo.get("target", 0)
            if "latency" in slo_name or "response" in slo_name:
                # Convert SLO target (e.g., 99.5% within 200ms) to budget
                latency_budget = 20.0  # Simplified: use 20ms default
            elif "error" in slo_name or "availability" in slo_name:
                error_budget = max(0, 100 - target)  # budget = 100 - target

        return latency_budget, error_budget

    except Exception as e:
        logger.debug("SLO fetch failed (using defaults): %s", e)
        return 20.0, 0.5
