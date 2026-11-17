"""
Bulwark — Traffic Replay Engine.

Replays anonymized production requests against the ephemeral environment
and collects metrics for comparison against the production baseline.

Features:
  - Async HTTP replay with configurable concurrency (semaphore-based)
  - Per-request timing, status, and body hash recording
  - Aggregate metrics: p50/p95/p99 latency, error rate, 5xx count
  - Delta computation vs production baseline
  - Novel error detection (patterns not seen in production)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import httpx

from shared.config import settings
from shared.models import (
    EphemeralEnvironment,
    ProductionRequest,
    ReplayRequestResult,
    ReplayResult,
    ServiceBaseline,
)

logger = logging.getLogger("bulwark.replayer")


# ──────────────────────────────────────────────
# Single request replay
# ──────────────────────────────────────────────


async def _replay_single_request(
    client: httpx.AsyncClient,
    base_url: str,
    request: ProductionRequest,
    semaphore: asyncio.Semaphore,
) -> ReplayRequestResult:
    """
    Replay a single production request against the ephemeral environment.

    Records: response time, status code, response body hash, and any errors.
    """
    async with semaphore:
        url = f"{base_url.rstrip('/')}{request.path}"

        try:
            start_time = time.perf_counter()

            # Build request kwargs
            kwargs: dict[str, Any] = {
                "method": request.method,
                "url": url,
                "headers": request.headers,
            }

            # Add body for methods that support it
            if request.method in ("POST", "PUT", "PATCH") and request.body:
                if isinstance(request.body, dict):
                    kwargs["json"] = request.body
                else:
                    kwargs["content"] = str(request.body)

            response = await client.request(**kwargs)

            elapsed_ms = (time.perf_counter() - start_time) * 1000

            # Hash the response body for shape comparison
            body_bytes = response.content
            body_hash = hashlib.md5(body_bytes).hexdigest()

            # Determine if there's an error
            error_message = None
            if response.status_code >= 500:
                error_message = f"HTTP {response.status_code}: {response.text[:200]}"
            elif response.status_code >= 400:
                error_message = f"HTTP {response.status_code}"

            return ReplayRequestResult(
                method=request.method,
                path=request.path,
                expected_status=request.expected_status,
                actual_status=response.status_code,
                response_time_ms=elapsed_ms,
                response_body_hash=body_hash,
                error_message=error_message,
                status_match=(response.status_code == request.expected_status),
            )

        except httpx.TimeoutException as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            return ReplayRequestResult(
                method=request.method,
                path=request.path,
                expected_status=request.expected_status,
                actual_status=0,
                response_time_ms=elapsed_ms,
                error_message=f"Timeout: {str(e)[:200]}",
                status_match=False,
            )

        except httpx.ConnectError as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            return ReplayRequestResult(
                method=request.method,
                path=request.path,
                expected_status=request.expected_status,
                actual_status=0,
                response_time_ms=elapsed_ms,
                error_message=f"Connection error: {str(e)[:200]}",
                status_match=False,
            )

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            return ReplayRequestResult(
                method=request.method,
                path=request.path,
                expected_status=request.expected_status,
                actual_status=0,
                response_time_ms=elapsed_ms,
                error_message=f"Unexpected error: {str(e)[:200]}",
                status_match=False,
            )


# ──────────────────────────────────────────────
# Percentile calculation
# ──────────────────────────────────────────────


def _percentile(data: list[float], pct: float) -> float:
    """Calculate the given percentile of a sorted list of values."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    idx = (pct / 100.0) * (n - 1)
    lower = int(idx)
    upper = min(lower + 1, n - 1)
    fraction = idx - lower
    return sorted_data[lower] + fraction * (sorted_data[upper] - sorted_data[lower])


# ──────────────────────────────────────────────
# Novel error detection
# ──────────────────────────────────────────────


def _detect_novel_errors(
    results: list[ReplayRequestResult],
    known_error_patterns: list[str] | None = None,
) -> list[str]:
    """
    Identify error messages that are not seen in the production baseline.

    Novel errors are strong signals of regression — the new code is producing
    failure modes that production has never seen.
    """
    known_patterns = set(known_error_patterns or [])
    novel_errors: list[str] = []
    seen_errors: set[str] = set()

    for result in results:
        if result.error_message and result.error_message not in seen_errors:
            seen_errors.add(result.error_message)
            # Check if this error pattern is known
            is_known = any(
                known in result.error_message for known in known_patterns
            )
            if not is_known:
                novel_errors.append(result.error_message)

    return novel_errors


# ──────────────────────────────────────────────
# Response shape change detection
# ──────────────────────────────────────────────


def _detect_response_shape_changes(
    results: list[ReplayRequestResult],
) -> list[str]:
    """
    Detect cases where the response body structure has changed.

    This is a simplified check — we look for status code mismatches
    which often indicate a structural change in the response.
    """
    changes = []
    for result in results:
        if not result.status_match:
            changes.append(
                f"{result.method} {result.path}: "
                f"expected {result.expected_status}, "
                f"got {result.actual_status}"
            )
    return changes[:20]  # Cap at 20 to avoid noise


# ──────────────────────────────────────────────
# Main replay function
# ──────────────────────────────────────────────


async def replay_traffic(
    environment: EphemeralEnvironment,
    requests: list[ProductionRequest],
    baseline: ServiceBaseline,
    concurrency: int | None = None,
) -> ReplayResult:
    """
    Replay all production requests against the ephemeral environment.

    Sends requests with configurable concurrency, collects per-request metrics,
    and computes aggregate statistics + deltas vs the production baseline.

    Args:
        environment: The ephemeral Cloud Run environment to target
        requests: List of anonymized production requests to replay
        baseline: The production performance baseline from Dynatrace
        concurrency: Max concurrent requests (default: from settings)

    Returns:
        ReplayResult with full metrics and delta analysis
    """
    concurrency = concurrency or settings.replay_concurrency
    semaphore = asyncio.Semaphore(concurrency)
    total_requests = len(requests)

    logger.info(
        "Starting traffic replay: %d requests at %dx concurrency → %s",
        total_requests,
        concurrency,
        environment.base_url,
    )

    replay_start = time.time()

    # Execute all requests with bounded concurrency
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=concurrency * 2,
            max_keepalive_connections=concurrency,
        ),
    ) as client:
        # Warm-up burst (discarded): a fresh instance pays cold start, lazy imports
        # and connection setup on its first requests. At n=200 the first burst of
        # `concurrency` requests *is* the p95, so measuring it would report a
        # regression on unchanged code.
        warmup = requests[:concurrency]
        await asyncio.gather(*[
            _replay_single_request(client, environment.base_url, req, semaphore)
            for req in warmup
        ])
        replay_start = time.time()

        tasks = [
            _replay_single_request(client, environment.base_url, req, semaphore)
            for req in requests
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    replay_duration_ms = int((time.time() - replay_start) * 1000)

    # ── Aggregate metrics ──
    response_times = [r.response_time_ms for r in results if r.actual_status > 0]
    successful = sum(1 for r in results if 200 <= r.actual_status < 400)
    failed = sum(1 for r in results if r.actual_status >= 400 or r.actual_status == 0)
    five_xx = sum(1 for r in results if 500 <= r.actual_status < 600)

    actual_p50 = _percentile(response_times, 50) if response_times else 0.0
    actual_p95 = _percentile(response_times, 95) if response_times else 0.0
    actual_p99 = _percentile(response_times, 99) if response_times else 0.0

    error_rate = (
        (five_xx / total_requests * 100) if total_requests > 0 else 0.0
    )

    # ── Compute deltas ──
    latency_delta = actual_p95 - baseline.p95_latency_ms
    error_rate_delta = error_rate - baseline.error_rate_percent

    # ── Detect novel errors ──
    novel_errors = _detect_novel_errors(results)

    # ── Detect response shape changes ──
    shape_changes = _detect_response_shape_changes(results)

    replay_result = ReplayResult(
        simulation_id=environment.simulation_id,
        total_requests=total_requests,
        successful_requests=successful,
        failed_requests=failed,
        actual_p50_ms=round(actual_p50, 2),
        actual_p95_ms=round(actual_p95, 2),
        actual_p99_ms=round(actual_p99, 2),
        actual_error_rate_percent=round(error_rate, 4),
        latency_delta_p95_ms=round(latency_delta, 2),
        error_rate_delta_percent=round(error_rate_delta, 4),
        novel_error_messages=novel_errors,
        response_shape_changes=shape_changes,
        five_xx_count=five_xx,
        replay_duration_ms=replay_duration_ms,
        individual_results=results,
    )

    logger.info(
        "Replay complete: %d/%d successful | p95=%.1fms (Δ%+.1fms) | "
        "error=%.2f%% (Δ%+.2f%%) | 5xx=%d | novel_errors=%d | %dms",
        successful,
        total_requests,
        actual_p95,
        latency_delta,
        error_rate,
        error_rate_delta,
        five_xx,
        len(novel_errors),
        replay_duration_ms,
    )

    return replay_result
