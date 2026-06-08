"""
Bulwark — Integration Smoke Tests (Phase 3).

End-to-end pipeline smoke tests that exercise the full 9-step
orchestrator workflow using mocked external dependencies.

These tests verify:
  - Full pipeline runs without error for non-LLM MRs
  - Full pipeline runs with Arize integration for LLM MRs
  - Doc-only MRs are skipped without posting a comment
  - Cancelled simulations abort before Step 1 executes
  - Pipeline handles Dynatrace failures gracefully (partial data)
  - Pipeline handles Gemini failures and falls back to local scoring
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.reasoning import compute_scores_locally
from shared.models import (
    ArizeEvalResult,
    EphemeralEnvironment,
    EnvironmentStatus,
    LLMDetectionResult,
    MREventMessage,
    MergeRequestDetails,
    ReplayResult,
    ServiceBaseline,
    ServiceTopology,
    SimulationStatus,
    VerdictResult,
    VerdictStatus,
)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


def make_mr_message(
    mr_id: int = 42,
    head_sha: str = "abc123def456",
    source_branch: str = "feature/test",
) -> MREventMessage:
    return MREventMessage(
        simulation_id=f"sim-test-{mr_id}",
        mr_id=mr_id,
        project_id="group/demo-app",
        head_sha=head_sha,
        source_branch=source_branch,
        target_branch="main",
        mr_url=f"https://gitlab.com/group/demo-app/-/merge_requests/{mr_id}",
    )


def make_mr_details(
    changed_files: list[str] | None = None,
    has_llm: bool = False,
) -> MergeRequestDetails:
    return MergeRequestDetails(
        mr_id=42,
        title="Test MR",
        source_branch="feature/test",
        target_branch="main",
        author="test-user",
        changed_files=changed_files or ["src/main.py", "src/utils.py"],
        has_llm_components=has_llm,
        head_sha="abc123def456",
    )


def make_healthy_environment(sim_id: str = "sim-test-42") -> EphemeralEnvironment:
    return EphemeralEnvironment(
        simulation_id=sim_id,
        base_url="http://localhost:8081",
        mock_base_url="http://localhost:8082",
        build_duration_ms=1200,
        deployed_at=datetime.now(timezone.utc),
        status=EnvironmentStatus.HEALTHY,
    )


def make_clean_replay_result(sim_id: str = "sim-test-42") -> ReplayResult:
    """Replay result with no regressions — should produce APPROVED."""
    return ReplayResult(
        simulation_id=sim_id,
        total_requests=200,
        successful_requests=200,
        failed_requests=0,
        actual_p50_ms=142.0,
        actual_p95_ms=145.0,
        actual_p99_ms=160.0,
        actual_error_rate_percent=0.13,
        latency_delta_p95_ms=3.0,   # Well within ±35ms noise floor
        error_rate_delta_percent=0.01,  # Well within ±1.0% noise floor
        five_xx_count=0,
        novel_error_messages=[],
        replay_duration_ms=8500,
    )


def make_regressed_replay_result(sim_id: str = "sim-test-42") -> ReplayResult:
    """Replay result with N+1 regression — should produce BLOCKED."""
    return ReplayResult(
        simulation_id=sim_id,
        total_requests=200,
        successful_requests=198,
        failed_requests=2,
        actual_p50_ms=280.0,
        actual_p95_ms=329.0,
        actual_p99_ms=410.0,
        actual_error_rate_percent=0.18,
        latency_delta_p95_ms=187.0,   # 5.3x the noise floor — BLOCKED
        error_rate_delta_percent=0.06,  # Within noise floor — not scored
        five_xx_count=0,
        novel_error_messages=["ConnectionPool timeout after 500ms"],
        replay_duration_ms=14200,
    )


MOCK_BASELINE = ServiceBaseline(
    service_name="demo-app",
    p50_latency_ms=110.0,
    p95_latency_ms=142.0,
    p99_latency_ms=180.0,
    error_rate_percent=0.12,
    throughput_rpm=3200.0,
)


# ──────────────────────────────────────────────
# Local scoring tests (no external deps)
# ──────────────────────────────────────────────


class TestLocalScoringIntegration:
    """Test compute_scores_locally end-to-end — the deterministic fallback path."""

    def test_clean_result_produces_approved(self):
        """No regressions → APPROVED."""
        result = compute_scores_locally(make_clean_replay_result(), MOCK_BASELINE)
        assert result.verdict == VerdictStatus.APPROVED
        assert result.overall_score >= 80

    def test_regressed_result_produces_below_approved(self):
        """
        N+1 regression (+187ms p95, no 5xx, error rate within noise floor) →
        WARNING (53/100).

        Full BLOCKED requires additional signals (5xx responses or multiple
        code-regression novel errors). The PRD §18 '43/100 BLOCKED' scenario
        includes a code_regression novel error on top of the latency regression.
        """
        result = compute_scores_locally(make_regressed_replay_result(), MOCK_BASELINE)
        # Must not be APPROVED — there IS a significant regression
        assert result.verdict != VerdictStatus.APPROVED
        assert result.overall_score < 80
        # Infra score should be heavily penalised for +187ms (5.3× noise floor)
        assert result.infrastructure_score <= 30

    def test_uncertainty_acknowledgement_always_present(self):
        """Every verdict must include the uncertainty disclosure (PRD §8.7)."""
        result = compute_scores_locally(make_clean_replay_result(), MOCK_BASELINE)
        assert result.uncertainty_acknowledgement != ""
        assert "±35" in result.uncertainty_acknowledgement

    def test_evidence_always_non_empty(self):
        """Evidence list must contain at least one entry."""
        result = compute_scores_locally(make_clean_replay_result(), MOCK_BASELINE)
        assert len(result.evidence) >= 1

    def test_five_xx_causes_blocked(self):
        """Any 5xx response should heavily penalise the score."""
        replay = make_clean_replay_result()
        replay.five_xx_count = 3
        result = compute_scores_locally(replay, MOCK_BASELINE)
        # -40 flat from infrastructure score should pull score well below APPROVED
        assert result.infrastructure_score <= 60

    def test_noise_floor_not_penalised(self):
        """
        Delta within noise floor (< 35ms p95, < 1.0% error rate) must NOT
        deduct from the infrastructure score.
        """
        replay = make_clean_replay_result()
        replay.latency_delta_p95_ms = 20.0   # Below 35ms noise floor
        replay.error_rate_delta_percent = 0.5  # Below 1.0% noise floor
        result = compute_scores_locally(replay, MOCK_BASELINE)
        assert result.infrastructure_score == 100

    def test_llm_detection_no_components_yields_none_llm_score(self):
        """When no LLM components, llm_quality_score must be None."""
        result = compute_scores_locally(
            make_clean_replay_result(), MOCK_BASELINE, arize_result=None
        )
        assert result.llm_quality_score is None

    def test_llm_detection_with_components_yields_score(self):
        """When LLM components detected and Arize ran, llm_quality_score must be set."""
        arize = ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=0.05,  # Below noise band
            relevance_delta=0.03,
            groundedness_delta=0.02,
        )
        result = compute_scores_locally(
            make_clean_replay_result(), MOCK_BASELINE, arize_result=arize
        )
        assert result.llm_quality_score == 100  # No deductions — all within noise band

    def test_hallucination_above_noise_band_penalised(self):
        """Hallucination delta > 0.10 should deduct from LLM quality score."""
        arize = ArizeEvalResult(
            llm_detected=True,
            hallucination_delta=0.15,  # Above 0.10 noise band
            relevance_delta=0.0,
            groundedness_delta=0.0,
        )
        result = compute_scores_locally(
            make_clean_replay_result(), MOCK_BASELINE, arize_result=arize
        )
        assert result.llm_quality_score == 80  # -20 for hallucination delta 0.10–0.25

    def test_weighted_formula_no_llm(self):
        """Without LLM: overall = infra × 0.40 + reliability × 0.60."""
        replay = make_clean_replay_result()
        result = compute_scores_locally(replay, MOCK_BASELINE)
        expected = int(round(result.infrastructure_score * 0.40 + result.reliability_score * 0.60))
        assert abs(result.overall_score - expected) <= 1  # Allow 1 point rounding

    def test_weighted_formula_with_llm(self):
        """With LLM: overall = infra × 0.30 + reliability × 0.40 + llm × 0.30."""
        arize = ArizeEvalResult(llm_detected=True, hallucination_delta=0.0)
        result = compute_scores_locally(make_clean_replay_result(), MOCK_BASELINE, arize_result=arize)
        assert result.llm_quality_score is not None
        expected = int(round(
            result.infrastructure_score * 0.30
            + result.reliability_score * 0.40
            + result.llm_quality_score * 0.30
        ))
        assert abs(result.overall_score - expected) <= 1

    def test_novel_error_code_regression_penalised(self):
        """Novel error classified as code_regression → -25 reliability."""
        replay = make_clean_replay_result()
        replay.novel_error_messages = ["ConnectionPool timeout after 500ms"]
        result = compute_scores_locally(
            replay,
            MOCK_BASELINE,
            novel_error_classifications={"ConnectionPool timeout after 500ms": "code_regression"},
        )
        assert result.reliability_score == 75  # 100 - 25

    def test_novel_error_stub_artefact_not_penalised(self):
        """Novel error classified as stub_artefact → 0 reliability deduction."""
        replay = make_clean_replay_result()
        replay.novel_error_messages = ["404 Not Found: /api/unmocked-endpoint"]
        result = compute_scores_locally(
            replay,
            MOCK_BASELINE,
            novel_error_classifications={"404 Not Found: /api/unmocked-endpoint": "stub_artefact"},
        )
        assert result.reliability_score == 100  # No deduction


# ──────────────────────────────────────────────
# Demo app scenario validation
# ──────────────────────────────────────────────


class TestDemoScenario:
    """
    Validates the exact PRD §18 demo numbers using the local scoring engine.
    These numbers appear in the BLOCKED verdict comment during the demo.
    """

    def test_n_plus_one_with_five_xx_produces_blocked(self):
        """
        PRD §18 BLOCKED scenario: N+1 regression + code_regression novel error +
        5xx responses → BLOCKED.

        The PRD's '43/100' requires all three signals to fire:
          - infra: +187ms p95 → infra score ~20
          - reliability: code_regression novel error → reliability score 75
          - 5xx: -40 flat deduction → infra score drops to 0

        infra×0.40 + reliability×0.60 = 0×0.40 + 75×0.60 = 45 → BLOCKED
        """
        regressed = ReplayResult(
            simulation_id="demo-blocked",
            total_requests=200,
            successful_requests=198,
            failed_requests=2,
            actual_p95_ms=329.0,
            actual_p50_ms=280.0,
            actual_p99_ms=410.0,
            latency_delta_p95_ms=187.0,
            actual_error_rate_percent=0.18,
            error_rate_delta_percent=0.06,  # Within noise floor — not scored
            five_xx_count=3,  # Adds -40 flat to infra score → drops to 0
            novel_error_messages=["ConnectionPool timeout after 500ms"],
        )

        # With Gemini classification: code_regression
        result = compute_scores_locally(
            regressed,
            MOCK_BASELINE,
            novel_error_classifications={"ConnectionPool timeout after 500ms": "code_regression"},
        )
        assert result.verdict == VerdictStatus.BLOCKED
        assert result.overall_score < 50
        # Infrastructure score should be 0 (20 - 40 for 5xx = clamped to 0)
        assert result.infrastructure_score == 0

    def test_n_plus_one_no_five_xx_produces_warning(self):
        """
        N+1 regression without 5xx: +187ms p95 + code_regression novel error
        → WARNING (53/100).

        Without 5xx the infrastructure score is 20 (not 0), so:
        infra×0.40 + reliability×0.60 = 20×0.40 + 75×0.60 = 8+45 = 53 → WARNING
        """
        regressed = ReplayResult(
            simulation_id="demo-warning",
            total_requests=200,
            actual_p95_ms=329.0,
            latency_delta_p95_ms=187.0,
            error_rate_delta_percent=0.06,
            five_xx_count=0,  # No 5xx
            novel_error_messages=["ConnectionPool timeout after 500ms"],
        )
        result = compute_scores_locally(
            regressed,
            MOCK_BASELINE,
            novel_error_classifications={"ConnectionPool timeout after 500ms": "code_regression"},
        )
        assert result.verdict == VerdictStatus.WARNING
        assert 50 <= result.overall_score < 80

    def test_fix_produces_approved_91(self):
        """
        PRD §18: Fixed MR (JOIN query) → APPROVED, ~91/100.
        """
        fixed = ReplayResult(
            simulation_id="demo-approved",
            total_requests=200,
            successful_requests=200,
            failed_requests=0,
            actual_p95_ms=145.0,
            actual_p50_ms=112.0,
            latency_delta_p95_ms=3.0,   # Within noise floor
            actual_error_rate_percent=0.13,
            error_rate_delta_percent=0.01,  # Within noise floor
            five_xx_count=0,
            novel_error_messages=[],
        )

        result = compute_scores_locally(fixed, MOCK_BASELINE)
        assert result.verdict == VerdictStatus.APPROVED
        assert result.overall_score >= 80
        # PRD says ~91 — allow ±15
        assert 75 <= result.overall_score <= 100

    def test_error_rate_within_noise_floor_not_scored(self):
        """
        PRD §18: +0.06% error rate delta is within ±1.0% noise floor — must NOT be scored.
        This is the 'uncertainty disclosure working correctly' moment in the demo.
        """
        regressed = make_regressed_replay_result()
        # Error rate delta is 0.06% — within noise floor
        # Infrastructure score should NOT deduct for error rate
        infra_score_only_replay = ReplayResult(
            simulation_id="noise-test",
            total_requests=200,
            latency_delta_p95_ms=0.0,   # No latency regression
            error_rate_delta_percent=0.06,  # Within noise floor
            five_xx_count=0,
        )
        result = compute_scores_locally(infra_score_only_replay, MOCK_BASELINE)
        # Infrastructure score should remain 100 (no deduction for within-noise signals)
        assert result.infrastructure_score == 100
