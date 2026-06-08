"""
Tests for Bulwark Environment Builder — Phase 3.

Tests cover:
  - Parallel deployment (asyncio.gather behaviour)
  - Health check retry logic
  - Teardown error tolerance
  - WireMock stub generation
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from environment_builder.builder import (
    HEALTH_CHECK_RETRIES,
    _deploy_service_and_mock_parallel,
    _health_check,
    teardown_environment,
)
from environment_builder.mock_builder import generate_stubs, stubs_to_wiremock_json
from shared.models import (
    EnvironmentStatus,
    ServiceDependency,
    ServiceTopology,
)


# ──────────────────────────────────────────────
# Parallel deployment helper
# ──────────────────────────────────────────────


class TestParallelDeploy:
    """Tests for _deploy_service_and_mock_parallel concurrency behaviour."""

    @pytest.mark.asyncio
    async def test_parallel_deploy_returns_both_urls(self):
        """Both service_url and mock_url should be returned."""
        topology = ServiceTopology(
            service_name="test-svc",
            dependencies=[
                ServiceDependency(name="test-db", base_url="http://db", endpoints=["/query"])
            ],
        )

        with (
            patch(
                "environment_builder.builder._deploy_to_cloud_run",
                new=AsyncMock(return_value="https://sim.run.app"),
            ),
            patch(
                "environment_builder.builder._deploy_wiremock",
                new=AsyncMock(return_value="https://mock.run.app"),
            ),
        ):
            service_url, mock_url = await _deploy_service_and_mock_parallel(
                service_name="bulwark-sim-abc",
                image_tag="gcr.io/test/image:latest",
                simulation_id="sim-parallel-001",
                mock_service_name="bulwark-mock-abc",
                topology=topology,
            )

        assert service_url == "https://sim.run.app"
        assert mock_url == "https://mock.run.app"

    @pytest.mark.asyncio
    async def test_parallel_deploy_no_deps_skips_wiremock(self):
        """With no topology dependencies, WireMock deploy should be skipped."""
        topology = ServiceTopology(service_name="test-svc", dependencies=[])

        with patch(
            "environment_builder.builder._deploy_to_cloud_run",
            new=AsyncMock(return_value="https://sim.run.app"),
        ) as mock_deploy:
            service_url, mock_url = await _deploy_service_and_mock_parallel(
                service_name="bulwark-sim-abc",
                image_tag="gcr.io/test/image:latest",
                simulation_id="sim-nodeps-001",
                mock_service_name=None,
                topology=topology,
            )

        assert service_url == "https://sim.run.app"
        assert mock_url == ""  # No WireMock
        mock_deploy.assert_called_once()

    @pytest.mark.asyncio
    async def test_parallel_deploy_runs_concurrently(self):
        """Both deploy calls should complete without one blocking the other."""
        call_order: list[str] = []

        async def slow_service_deploy(*args, **kwargs) -> str:
            call_order.append("service_start")
            await asyncio.sleep(0.05)
            call_order.append("service_end")
            return "https://sim.run.app"

        async def slow_mock_deploy(*args, **kwargs) -> str:
            call_order.append("mock_start")
            await asyncio.sleep(0.05)
            call_order.append("mock_end")
            return "https://mock.run.app"

        topology = ServiceTopology(
            service_name="test-svc",
            dependencies=[ServiceDependency(name="dep", base_url="http://dep")],
        )

        with (
            patch("environment_builder.builder._deploy_to_cloud_run", side_effect=slow_service_deploy),
            patch("environment_builder.builder._deploy_wiremock", side_effect=slow_mock_deploy),
        ):
            start = time.perf_counter()
            await _deploy_service_and_mock_parallel(
                service_name="bulwark-sim-concurrent",
                image_tag="gcr.io/test/img:latest",
                simulation_id="sim-concurrent-001",
                mock_service_name="bulwark-mock-concurrent",
                topology=topology,
            )
            elapsed = time.perf_counter() - start

        # Both tasks sleep 0.05s concurrently — total should be ~0.05s, not ~0.10s
        # We use 0.08s as threshold to be safe against CI timing jitter
        assert elapsed < 0.08, (
            f"Parallel deploy took {elapsed:.3f}s — expected < 0.08s. "
            "Calls may not be running concurrently."
        )

    @pytest.mark.asyncio
    async def test_parallel_deploy_none_topology_skips_wiremock(self):
        """None topology should skip WireMock entirely."""
        with patch(
            "environment_builder.builder._deploy_to_cloud_run",
            new=AsyncMock(return_value="https://sim.run.app"),
        ):
            service_url, mock_url = await _deploy_service_and_mock_parallel(
                service_name="bulwark-sim-notopo",
                image_tag="gcr.io/test/img:latest",
                simulation_id="sim-notopo-001",
                mock_service_name=None,
                topology=None,
            )

        assert service_url == "https://sim.run.app"
        assert mock_url == ""


# ──────────────────────────────────────────────
# Health check retry logic
# ──────────────────────────────────────────────


class TestHealthCheck:
    """Tests for _health_check retry behaviour."""

    @pytest.mark.asyncio
    async def test_health_check_passes_on_first_try(self):
        """2xx response on first attempt → HEALTHY."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            status = await _health_check("http://localhost:8080")

        assert status == EnvironmentStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_health_check_fails_after_all_retries(self):
        """All retries fail → HEALTH_CHECK_FAILED."""
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            # Patch sleep to speed up tests
            with patch("asyncio.sleep", new=AsyncMock()):
                status = await _health_check("http://localhost:8080")

        assert status == EnvironmentStatus.HEALTH_CHECK_FAILED

    @pytest.mark.asyncio
    async def test_health_check_passes_on_second_retry(self):
        """First attempt fails, second succeeds → HEALTHY."""
        fail_response = MagicMock()
        fail_response.status_code = 503

        pass_response = MagicMock()
        pass_response.status_code = 200

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fail_response
            return pass_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch("asyncio.sleep", new=AsyncMock()):
                status = await _health_check("http://localhost:8080")

        assert status == EnvironmentStatus.HEALTHY
        assert call_count == 2

    def test_health_check_retry_constant(self):
        """HEALTH_CHECK_RETRIES should be 3 (per PRD §9.4)."""
        assert HEALTH_CHECK_RETRIES == 3


# ──────────────────────────────────────────────
# Teardown error tolerance
# ──────────────────────────────────────────────


class TestTeardown:
    """Tests that teardown handles partial failures gracefully."""

    @pytest.mark.asyncio
    async def test_teardown_uses_real_cleanup_in_development_mode(self, monkeypatch):
        """Development mode should not skip Cloud Run or Artifact Registry cleanup."""
        monkeypatch.setattr("environment_builder.builder.settings.bulwark_mode", "development")

        with (
            patch(
                "environment_builder.builder._delete_cloud_run_service",
                new=AsyncMock(),
            ) as mock_service_delete,
            patch(
                "environment_builder.builder._delete_artifact_image",
                new=AsyncMock(),
            ) as mock_image_delete,
        ):
            await teardown_environment("sim-dev-teardown-001")

        assert mock_service_delete.await_count == 2
        mock_image_delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_teardown_continues_on_service_deletion_error(self, monkeypatch):
        """
        If Cloud Run service deletion fails, teardown should still attempt
        image deletion (no exception propagated).
        """
        monkeypatch.setattr("environment_builder.builder.settings.bulwark_mode", "production")

        with (
            patch(
                "environment_builder.builder._delete_cloud_run_service",
                new=AsyncMock(side_effect=Exception("Cloud Run delete failed")),
            ),
            patch(
                "environment_builder.builder._delete_artifact_image",
                new=AsyncMock(),
            ) as mock_image_delete,
        ):
            # Should not raise
            await teardown_environment("sim-teardown-fail-001")

        # Image deletion was still attempted
        mock_image_delete.assert_called_once()


# ──────────────────────────────────────────────
# WireMock stub generation
# ──────────────────────────────────────────────


class TestMockBuilder:
    def test_generate_stubs_for_single_dependency(self):
        """Single dependency → stubs for all 4 HTTP methods per endpoint."""
        topology = ServiceTopology(
            service_name="my-service",
            dependencies=[
                ServiceDependency(
                    name="user-service",
                    base_url="http://user-svc",
                    endpoints=["/users", "/users/{id}"],
                )
            ],
        )
        stubs = generate_stubs(topology)
        # 2 endpoints × 4 methods = 8 stubs
        assert len(stubs) == 8

    def test_generate_stubs_empty_topology(self):
        """No dependencies → no stubs."""
        topology = ServiceTopology(service_name="my-service", dependencies=[])
        assert generate_stubs(topology) == []

    def test_stubs_have_correct_response_status(self):
        """All generated stubs should return 200."""
        topology = ServiceTopology(
            service_name="my-service",
            dependencies=[
                ServiceDependency(name="dep", base_url="http://dep", endpoints=["/api"])
            ],
        )
        stubs = generate_stubs(topology)
        assert all(s.response_status == 200 for s in stubs)

    def test_auth_dependency_gets_auth_response(self):
        """Dependency named 'auth-service' should get the auth response template."""
        topology = ServiceTopology(
            service_name="my-service",
            dependencies=[
                ServiceDependency(name="auth-service", base_url="http://auth", endpoints=["/verify"])
            ],
        )
        stubs = generate_stubs(topology)
        # Auth stub body should have 'valid' field
        get_stub = next(s for s in stubs if s.request_method == "GET")
        assert "valid" in get_stub.response_body

    def test_stubs_to_wiremock_json_format(self):
        """stubs_to_wiremock_json should produce WireMock mapping objects."""
        topology = ServiceTopology(
            service_name="svc",
            dependencies=[
                ServiceDependency(name="dep", base_url="http://dep", endpoints=["/api"])
            ],
        )
        stubs = generate_stubs(topology)
        mappings = stubs_to_wiremock_json(stubs)

        assert len(mappings) > 0
        for m in mappings:
            assert "request" in m
            assert "response" in m
            assert "method" in m["request"]
            assert "status" in m["response"]
            assert m["response"]["status"] == 200
