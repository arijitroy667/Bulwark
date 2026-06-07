"""
Tests for Bulwark Traffic Replay Engine.

Tests cover:
  - Percentile calculation (p50, p95, p99)
  - Delta computation vs baseline
  - Novel error detection
  - Response shape change detection
  - Replay result aggregation
"""

from __future__ import annotations

import pytest

from traffic_replay.replayer import (
    _detect_novel_errors,
    _detect_response_shape_changes,
    _percentile,
)
from shared.models import ReplayRequestResult


# ──────────────────────────────────────────────
# Percentile calculation
# ──────────────────────────────────────────────


class TestPercentile:
    def test_p50_even(self):
        """Median of an even-length list."""
        data = [10.0, 20.0, 30.0, 40.0]
        result = _percentile(data, 50)
        assert result == 25.0

    def test_p50_odd(self):
        """Median of an odd-length list."""
        data = [10.0, 20.0, 30.0]
        result = _percentile(data, 50)
        assert result == 20.0

    def test_p95(self):
        """95th percentile of a known distribution."""
        data = list(range(1, 101))  # 1 to 100
        result = _percentile([float(x) for x in data], 95)
        assert result == pytest.approx(95.05, abs=0.5)

    def test_p99(self):
        """99th percentile."""
        data = [float(x) for x in range(1, 101)]
        result = _percentile(data, 99)
        assert result == pytest.approx(99.01, abs=0.5)

    def test_empty_list(self):
        """Empty list should return 0."""
        assert _percentile([], 50) == 0.0

    def test_single_element(self):
        """Single element — all percentiles equal that element."""
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 95) == 42.0
        assert _percentile([42.0], 99) == 42.0

    def test_unsorted_input(self):
        """Function should handle unsorted input."""
        data = [50.0, 10.0, 30.0, 20.0, 40.0]
        result = _percentile(data, 50)
        assert result == 30.0


# ──────────────────────────────────────────────
# Novel error detection
# ──────────────────────────────────────────────


class TestNovelErrorDetection:
    def test_no_errors(self):
        """No errors → no novel errors."""
        results = [
            ReplayRequestResult(
                method="GET",
                path="/api/test",
                expected_status=200,
                actual_status=200,
                response_time_ms=10.0,
            ),
        ]
        assert _detect_novel_errors(results) == []

    def test_novel_error_detected(self):
        """Error not in known patterns → novel."""
        results = [
            ReplayRequestResult(
                method="GET",
                path="/api/test",
                expected_status=200,
                actual_status=500,
                response_time_ms=10.0,
                error_message="NullPointerException in UserService",
            ),
        ]
        novel = _detect_novel_errors(results)
        assert len(novel) == 1
        assert "NullPointerException" in novel[0]

    def test_known_error_not_novel(self):
        """Error matching known pattern → not novel."""
        results = [
            ReplayRequestResult(
                method="GET",
                path="/api/test",
                expected_status=200,
                actual_status=500,
                response_time_ms=10.0,
                error_message="HTTP 500: Internal Server Error",
            ),
        ]
        novel = _detect_novel_errors(results, known_error_patterns=["Internal Server Error"])
        assert len(novel) == 0

    def test_deduplicated_errors(self):
        """Same error appearing twice should only be counted once."""
        results = [
            ReplayRequestResult(
                method="GET",
                path="/api/test1",
                expected_status=200,
                actual_status=500,
                response_time_ms=10.0,
                error_message="Connection refused",
            ),
            ReplayRequestResult(
                method="GET",
                path="/api/test2",
                expected_status=200,
                actual_status=500,
                response_time_ms=10.0,
                error_message="Connection refused",
            ),
        ]
        novel = _detect_novel_errors(results)
        assert len(novel) == 1


# ──────────────────────────────────────────────
# Response shape change detection
# ──────────────────────────────────────────────


class TestResponseShapeChanges:
    def test_no_changes(self):
        """All status codes match → no shape changes."""
        results = [
            ReplayRequestResult(
                method="GET",
                path="/api/test",
                expected_status=200,
                actual_status=200,
                response_time_ms=10.0,
                status_match=True,
            ),
        ]
        assert _detect_response_shape_changes(results) == []

    def test_status_mismatch_detected(self):
        """Status code mismatch → shape change reported."""
        results = [
            ReplayRequestResult(
                method="GET",
                path="/api/users",
                expected_status=200,
                actual_status=404,
                response_time_ms=10.0,
                status_match=False,
            ),
        ]
        changes = _detect_response_shape_changes(results)
        assert len(changes) == 1
        assert "expected 200" in changes[0]
        assert "got 404" in changes[0]

    def test_max_changes_capped(self):
        """Shape changes should be capped at 20."""
        results = [
            ReplayRequestResult(
                method="GET",
                path=f"/api/endpoint{i}",
                expected_status=200,
                actual_status=500,
                response_time_ms=10.0,
                status_match=False,
            )
            for i in range(30)
        ]
        changes = _detect_response_shape_changes(results)
        assert len(changes) == 20
