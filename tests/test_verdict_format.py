"""
Phase 6: Verdict Comment Format Tests.

Tests for format_mr_comment() and the pipeline gate logic in enforce_verdict().

Verifies:
  - Metric table headers are present and correct
  - All required sections rendered (score breakdown, uncertainty disclosure)
  - BLOCKED verdict includes override instructions
  - Pipeline gate status text correct for APPROVED/WARNING/BLOCKED
  - Arize section rendered when llm_detected=True, skipped otherwise
  - Historical note rendered when verdict.historical_note is set
  - Uncertainty disclosure appears verbatim with total_requests count
  - enforce_verdict calls post_mr_comment and set_pipeline_status correctly
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.models import (
    ArizeEvalResult,
    ReplayResult,
    ServiceBaseline,
    VerdictResult,
    VerdictStatus,
)
from verdict_writer.writer import (
    UNCERTAINTY_DISCLOSURE,
    format_mr_comment,
    enforce_verdict,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


def _make_verdict(
    verdict: VerdictStatus = VerdictStatus.APPROVED,
    score: int = 91,
    infrastructure_score: int = 100,
    reliability_score: int = 100,
    llm_quality_score: int | None = None,
    recommendation: str = "No regressions detected. Approve.",
    historical_note: str = "",
    novel_error_classification: dict | None = None,
) -> VerdictResult:
    return VerdictResult(
        verdict=verdict,
        overall_score=score,
        infrastructure_score=infrastructure_score,
        reliability_score=reliability_score,
        llm_quality_score=llm_quality_score,
        evidence=["200 production requests replayed"],
        uncertainty_acknowledgement="±35ms noise floor applied to all latency measurements",
        recommendation=recommendation,
        historical_note=historical_note,
        novel_error_classification=novel_error_classification or {},
        gemini_model="gemini-2.5-flash",
        reasoning_duration_ms=850,
    )


def _make_replay(
    total_requests: int = 200,
    five_xx_count: int = 0,
    novel_errors: list | None = None,
    latency_delta_p95: float = 3.0,
    error_rate_delta: float = 0.01,
    actual_p95_ms: float = 145.0,
    actual_p50_ms: float = 112.0,
    actual_error_rate: float = 0.13,
) -> ReplayResult:
    return ReplayResult(
        simulation_id="sim-test-42",
        total_requests=total_requests,
        successful_requests=total_requests - five_xx_count,
        failed_requests=0,
        actual_p50_ms=actual_p50_ms,
        actual_p95_ms=actual_p95_ms,
        actual_p99_ms=actual_p95_ms + 20,
        actual_error_rate_percent=actual_error_rate,
        latency_delta_p95_ms=latency_delta_p95,
        error_rate_delta_percent=error_rate_delta,
        five_xx_count=five_xx_count,
        novel_error_messages=novel_errors or [],
        replay_duration_ms=8500,
    )


MOCK_BASELINE = ServiceBaseline(
    service_name="demo-app",
    p50_latency_ms=110.0,
    p95_latency_ms=142.0,
    p99_latency_ms=180.0,
    error_rate_percent=0.12,
    throughput_rpm=3200.0,
    slo_latency_budget_p95_ms=200.0,
)


# ──────────────────────────────────────────────────────────────────────────────
# format_mr_comment — required sections
# ──────────────────────────────────────────────────────────────────────────────


class TestCommentRequiredSections:
    """Verify all mandatory sections appear in every formatted comment."""

    def test_metric_table_header_present(self):
        """The metric delta table header must be present."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "| Metric | Baseline | Simulation | Delta | Status |" in comment

    def test_metric_table_separator_present(self):
        """Table separator row is present."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "|--------|----------|------------|-------|--------|" in comment

    def test_p95_latency_row_present(self):
        """p95 Latency row is present in the table."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "p95 Latency" in comment

    def test_error_rate_row_present(self):
        """Error Rate row is present in the table."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "Error Rate" in comment

    def test_five_xx_row_present(self):
        """5xx Responses row is present in the table."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "5xx Responses" in comment

    def test_score_breakdown_section_present(self):
        """Score breakdown section with infrastructure/reliability/overall present."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "Infrastructure score:" in comment
        assert "Reliability score:" in comment
        assert "Overall score:" in comment

    def test_uncertainty_disclosure_present(self):
        """Mandatory uncertainty disclosure block is present in every comment."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "±35 ms" in comment
        assert "Bulwark flags probable regressions" in comment

    def test_uncertainty_disclosure_has_total_requests(self):
        """Uncertainty disclosure contains the total_requests count."""
        replay = _make_replay(total_requests=175)
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), replay, MOCK_BASELINE
        )
        assert "175 production request samples" in comment

    def test_simulation_id_present(self):
        """Simulation ID appears in the metadata footer."""
        comment = format_mr_comment(
            "sim-test-abc", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "sim-test-abc" in comment

    def test_service_name_present(self):
        """Service name appears in the header."""
        comment = format_mr_comment(
            "sim-42", "payments-service", _make_verdict(), _make_replay(), MOCK_BASELINE
        )
        assert "payments-service" in comment


# ──────────────────────────────────────────────────────────────────────────────
# Verdict-specific formatting
# ──────────────────────────────────────────────────────────────────────────────


class TestVerdictSpecificFormat:
    """Verify verdict-specific sections are rendered correctly."""

    def test_approved_verdict_header(self):
        """APPROVED verdict shows ✅ emoji in header."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.APPROVED, score=91),
            _make_replay(), MOCK_BASELINE,
        )
        assert "✅" in comment
        assert "APPROVED" in comment
        assert "91/100" in comment

    def test_warning_verdict_header(self):
        """WARNING verdict shows ⚠️ emoji."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.WARNING, score=62),
            _make_replay(), MOCK_BASELINE,
        )
        assert "⚠️" in comment
        assert "WARNING" in comment

    def test_blocked_verdict_header(self):
        """BLOCKED verdict shows 🚫 emoji."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.BLOCKED, score=43),
            _make_replay(), MOCK_BASELINE,
        )
        assert "🚫" in comment
        assert "BLOCKED" in comment

    def test_blocked_includes_override_instructions(self):
        """BLOCKED verdict must include bulwark-override label instructions."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.BLOCKED),
            _make_replay(), MOCK_BASELINE,
        )
        assert "bulwark-override" in comment
        assert "To override" in comment

    def test_approved_does_not_include_override_instructions(self):
        """APPROVED verdict must NOT include override instructions."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.APPROVED),
            _make_replay(), MOCK_BASELINE,
        )
        assert "bulwark-override" not in comment

    def test_pipeline_gate_success_for_approved(self):
        """APPROVED → pipeline gate text says 'success'."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.APPROVED),
            _make_replay(), MOCK_BASELINE,
        )
        assert "success" in comment
        assert "mergeable" in comment

    def test_pipeline_gate_failed_for_blocked(self):
        """BLOCKED → pipeline gate text says 'failed'."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.BLOCKED),
            _make_replay(), MOCK_BASELINE,
        )
        assert "failed" in comment
        assert "cannot be merged" in comment

    def test_pipeline_gate_success_for_warning(self):
        """WARNING → pipeline gate text says 'success with caveats'."""
        comment = format_mr_comment(
            "sim-42", "demo-app",
            _make_verdict(verdict=VerdictStatus.WARNING),
            _make_replay(), MOCK_BASELINE,
        )
        assert "success" in comment


# ──────────────────────────────────────────────────────────────────────────────
# Arize section
# ──────────────────────────────────────────────────────────────────────────────


class TestArizeSection:
    """Verify Arize LLM evaluation section is rendered correctly."""

    def test_arize_section_rendered_when_llm_detected(self):
        """Arize section appears when llm_detected=True."""
        arize = ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=0.02,
            relevance_delta=-0.01,
            groundedness_delta=0.00,
        )
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE,
            arize_result=arize,
        )
        assert "LLM Quality Evaluation" in comment
        assert "Hallucination" in comment
        assert "Relevance" in comment
        assert "Groundedness" in comment

    def test_arize_section_skipped_when_no_llm(self):
        """When arize_result is None, Arize section is not rendered."""
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE,
            arize_result=None,
        )
        assert "LLM Quality Evaluation" not in comment

    def test_arize_skip_reason_shown(self):
        """When Arize eval was skipped, skip_reason appears in the comment."""
        arize = ArizeEvalResult(
            llm_detected=True,
            skip_reason="No /traces endpoint on simulation service",
        )
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE,
            arize_result=arize,
        )
        assert "No /traces endpoint" in comment

    def test_arize_hallucination_regression_shown(self):
        """Hallucination delta > noise band → '🚫 Regression' in the table."""
        arize = ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=0.15,  # > 0.10 noise band
        )
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE,
            arize_result=arize,
        )
        assert "Regression" in comment

    def test_arize_within_noise_band_shown(self):
        """Hallucination delta within noise band → '✅ Within noise band'."""
        arize = ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=0.02,  # within 0.10 band
        )
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), _make_replay(), MOCK_BASELINE,
            arize_result=arize,
        )
        assert "Within noise band" in comment

    def test_llm_score_shown_in_breakdown_when_present(self):
        """LLM quality score appears in score breakdown when llm_quality_score is set."""
        verdict = _make_verdict(llm_quality_score=80)
        comment = format_mr_comment(
            "sim-42", "demo-app", verdict, _make_replay(), MOCK_BASELINE,
        )
        # writer.py formats as "**LLM quality score:** 80/100"
        assert "LLM quality score:" in comment
        assert "80/100" in comment

    def test_llm_score_omitted_when_none(self):
        """LLM quality score line is omitted when llm_quality_score=None."""
        verdict = _make_verdict(llm_quality_score=None)
        comment = format_mr_comment(
            "sim-42", "demo-app", verdict, _make_replay(), MOCK_BASELINE,
        )
        assert "LLM quality score" not in comment


# ──────────────────────────────────────────────────────────────────────────────
# Historical note
# ──────────────────────────────────────────────────────────────────────────────


class TestHistoricalNote:
    def test_historical_note_rendered_when_set(self):
        """historical_note text appears in Historical Context section."""
        verdict = _make_verdict(
            historical_note="3 of the last 5 simulations detected latency regressions."
        )
        comment = format_mr_comment(
            "sim-42", "demo-app", verdict, _make_replay(), MOCK_BASELINE
        )
        assert "Historical Context" in comment
        assert "3 of the last 5 simulations" in comment

    def test_historical_note_omitted_when_empty(self):
        """Historical Context section not rendered when historical_note is empty."""
        verdict = _make_verdict(historical_note="")
        comment = format_mr_comment(
            "sim-42", "demo-app", verdict, _make_replay(), MOCK_BASELINE
        )
        assert "Historical Context" not in comment


# ──────────────────────────────────────────────────────────────────────────────
# Novel error classification
# ──────────────────────────────────────────────────────────────────────────────


class TestNovelErrorSection:
    def test_novel_error_section_rendered(self):
        """Novel error section appears when replay has novel errors."""
        replay = _make_replay(novel_errors=["ConnectionPool timeout after 500ms"])
        verdict = _make_verdict(
            novel_error_classification={
                "ConnectionPool timeout after 500ms": {
                    "assessment": "code_regression",
                    "reasoning": "Connection pool exhaustion under load",
                }
            }
        )
        comment = format_mr_comment(
            "sim-42", "demo-app", verdict, replay, MOCK_BASELINE
        )
        assert "Novel Error Analysis" in comment
        assert "code_regression" in comment
        assert "Connection pool exhaustion" in comment

    def test_novel_error_section_skipped_when_no_errors(self):
        """No novel errors → Novel Error Analysis section not rendered."""
        replay = _make_replay(novel_errors=[])
        comment = format_mr_comment(
            "sim-42", "demo-app", _make_verdict(), replay, MOCK_BASELINE
        )
        assert "Novel Error Analysis" not in comment


# ──────────────────────────────────────────────────────────────────────────────
# enforce_verdict — GitLab MCP calls
# ──────────────────────────────────────────────────────────────────────────────


class TestEnforceVerdictGitlabCalls:
    """
    Test enforce_verdict posts comment and sets pipeline status via MCP.
    Uses real local GitLab server.
    """

    @pytest.mark.asyncio
    async def test_approved_verdict_posts_comment_and_sets_success(
        self, gitlab_server_state
    ):
        """APPROVED verdict → comment posted and pipeline status = success."""
        with patch("shared.db.update_simulation", new=AsyncMock()):
            await enforce_verdict(
                project_id="group/demo-app",
                mr_id=42,
                head_sha="abc123def456",
                simulation_id="sim-test-42",
                service_name="demo-app",
                verdict=_make_verdict(verdict=VerdictStatus.APPROVED, score=91),
                replay_result=_make_replay(),
                baseline=MOCK_BASELINE,
            )

        # Comment should have been posted
        assert len(gitlab_server_state["comments"]) == 1
        assert "APPROVED" in gitlab_server_state["comments"][0]["body"]

        # Pipeline status should be success
        assert len(gitlab_server_state["pipeline_statuses"]) == 1
        assert gitlab_server_state["pipeline_statuses"][0]["state"] == "success"

    @pytest.mark.asyncio
    async def test_blocked_verdict_sets_failed_status(self, gitlab_server_state):
        """BLOCKED verdict → pipeline status = failed."""
        with patch("shared.db.update_simulation", new=AsyncMock()):
            await enforce_verdict(
                project_id="group/demo-app",
                mr_id=42,
                head_sha="abc123def456",
                simulation_id="sim-test-42",
                service_name="demo-app",
                verdict=_make_verdict(verdict=VerdictStatus.BLOCKED, score=43),
                replay_result=_make_replay(),
                baseline=MOCK_BASELINE,
            )

        assert gitlab_server_state["pipeline_statuses"][0]["state"] == "failed"

    @pytest.mark.asyncio
    async def test_approved_verdict_sends_approval(self, gitlab_server_state):
        """APPROVED verdict → MR approval action sent."""
        with patch("shared.db.update_simulation", new=AsyncMock()):
            await enforce_verdict(
                project_id="group/demo-app",
                mr_id=42,
                head_sha="abc123def456",
                simulation_id="sim-test-42",
                service_name="demo-app",
                verdict=_make_verdict(verdict=VerdictStatus.APPROVED),
                replay_result=_make_replay(),
                baseline=MOCK_BASELINE,
            )

        # updateMergeRequest with approve was called
        approvals = gitlab_server_state["approvals"]
        assert len(approvals) >= 1
        assert any(a["action"] == "approve" for a in approvals)
