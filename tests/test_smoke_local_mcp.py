"""
Phase 6: E2E Smoke Test against Local MCP Servers.

Simulates a complete Bulwark orchestration run using the real FastMCP local
servers.  It pushes an event through `run_simulation()` and ensures the entire
pipeline executes end-to-end:

  Step 1  INGEST         → real GitLab MCP (local server)
  Step 2  FETCH BASELINE → real Dynatrace MCP (local server)
  Step 4  PROVISION      → patched (no Docker / Cloud Run needed)
  Step 5  REPLAY         → patched (no WireMock needed)
  Step 8  REASONING      → patched (no Gemini API needed)
  Step 9  ENFORCE        → real GitLab MCP (local server)

All MCP calls travel through the real shared.mcp_client → local FastMCP servers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.agent import run_simulation
from shared.models import (
    EphemeralEnvironment,
    EnvironmentStatus,
    MREventMessage,
    ReplayResult,
    VerdictResult,
    VerdictStatus,
)


@pytest.mark.asyncio
async def test_full_pipeline_with_real_local_mcp_servers(
    local_mcp_servers,
    gitlab_server_state,
    dynatrace_server_state,
    arize_server_state,
):
    """
    Run the full Bulwark agent pipeline against local FastMCP servers.

    Verifies that all MCP integrations work together as orchestrated:
    - GitLab MCP: getMergeRequest, getMRDiff, createNote, createPipelineStatus
    - Dynatrace MCP: getProblems, getServiceMetrics, getLogs, getServiceTopology
    - All steps wired together through the real orchestrator

    Cloud-only steps (provision, replay, Gemini) are patched to avoid
    requiring Docker / Cloud Run / Gemini API in CI.
    """
    # ── Arrange: set GitLab server state to match the event ──
    gitlab_server_state["mr"]["iid"] = 42
    gitlab_server_state["mr"]["title"] = "E2E Integration Test"
    gitlab_server_state["mr"]["source_branch"] = "feature/test"
    gitlab_server_state["mr"]["target_branch"] = "main"

    # ── Arrange: set Dynatrace to no active incident ──
    dynatrace_server_state["problems"] = []

    event = MREventMessage(
        simulation_id="sim-smoke-42",
        project_id="group/demo-app",
        mr_id=42,
        head_sha="abcdef123456",
        source_branch="feature/test",
        target_branch="main",
    )

    # Build a healthy fake environment (no Docker needed)
    fake_environment = EphemeralEnvironment(
        simulation_id="sim-smoke-42",
        base_url="http://127.0.0.1:9999",
        mock_base_url="http://127.0.0.1:9998",
        status=EnvironmentStatus.HEALTHY,
    )

    # Build a clean replay result (no WireMock needed)
    fake_replay_result = ReplayResult(
        simulation_id="sim-smoke-42",
        total_requests=100,
        successful_requests=100,
        failed_requests=0,
        actual_p50_ms=110.0,
        actual_p95_ms=140.0,
        actual_p99_ms=180.0,
        actual_error_rate_percent=0.0,
        latency_delta_p95_ms=2.0,
        error_rate_delta_percent=0.0,
        five_xx_count=0,
        novel_error_messages=[],
        replay_duration_ms=1200,
    )

    # Build a deterministic APPROVED verdict (no Gemini needed)
    fake_verdict = VerdictResult(
        verdict=VerdictStatus.APPROVED,
        overall_score=91,
        infrastructure_score=100,
        reliability_score=82,
        llm_quality_score=None,
        recommendation="No regressions detected. Safe to merge.",
        uncertainty_acknowledgement="±35ms noise floor applied.",
        gemini_model="deterministic-fallback",
    )

    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        stack.enter_context(patch(
            "environment_builder.builder.provision_environment",
            new_callable=AsyncMock,
            return_value=fake_environment,
        ))
        stack.enter_context(patch(
            "traffic_replay.replayer.replay_traffic",
            new_callable=AsyncMock,
            return_value=fake_replay_result,
        ))
        stack.enter_context(patch(
            "orchestrator.reasoning.generate_verdict",
            new_callable=AsyncMock,
            return_value=fake_verdict,
        ))
        stack.enter_context(patch(
            "environment_builder.builder.teardown_environment",
            new_callable=AsyncMock,
        ))
        # Patch update_simulation everywhere it's used:
        # 1. In orchestrator.agent (module-level import)
        mock_update = stack.enter_context(patch(
            "orchestrator.agent.update_simulation",
            new_callable=AsyncMock,
        ))
        # 2. In verdict_writer.writer (local import from shared.db)
        stack.enter_context(patch(
            "shared.db.update_simulation",
            new_callable=AsyncMock,
        ))
        # 3. get_historical_context (module-level import in agent)
        stack.enter_context(patch(
            "orchestrator.agent.get_historical_context",
            new_callable=AsyncMock,
            return_value=[],
        ))
        # 4. get_database (called by _get_simulation for cancellation checks)
        mock_db = AsyncMock()
        mock_db.__getitem__ = MagicMock(return_value=AsyncMock())
        stack.enter_context(patch(
            "orchestrator.agent.get_database",
            new_callable=AsyncMock,
            return_value=mock_db,
        ))
        stack.enter_context(patch(
            "shared.db.get_database",
            new_callable=AsyncMock,
            return_value=mock_db,
        ))

        # Execute the full pipeline
        await run_simulation(event)

    # ── Assert: DB was updated at least once ──
    assert mock_update.call_count > 0, "update_simulation should have been called"

    # ── Assert: GitLab MCP side-effects ──
    # Pipeline status was set to success
    assert len(gitlab_server_state["pipeline_statuses"]) >= 1, (
        "Pipeline status should have been posted to GitLab"
    )
    pipeline_state = gitlab_server_state["pipeline_statuses"][-1]["state"]
    assert pipeline_state == "success", (
        f"Expected pipeline state 'success', got '{pipeline_state}'"
    )

    # Verdict comment was posted
    assert len(gitlab_server_state["comments"]) >= 1, (
        "Verdict comment should have been posted to GitLab MR"
    )
    comment_body = gitlab_server_state["comments"][-1]["body"]
    assert "APPROVED" in comment_body, (
        f"Expected 'APPROVED' in comment body, got: {comment_body[:200]}"
    )

    # ── Assert: Arize MCP — no spans logged (no LLM in this MR) ──
    assert len(arize_server_state["logged_spans"]) == 0, (
        "No spans should be logged when LLM components are absent"
    )
