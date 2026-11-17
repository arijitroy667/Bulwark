"""
Checks for the simulation fixes: bounded Gemini retry, SLO gate, history-safe
prompt, signed LLM deltas, demo scenarios — plus a live end-to-end demo run
(regression → BLOCKED, fix → APPROVED) when the local WireMock is up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from environment_builder.mock_builder import _path_to_pattern
from orchestrator import reasoning
from orchestrator.tools import arize_tool
from shared.config import settings
from shared.demo_mode import build_demo_message, demo_version_for
from shared.models import ReplayResult, ServiceBaseline, VerdictStatus

BASELINE = ServiceBaseline(service_name="demo-app", p95_latency_ms=60.0)
REGRESSED = ReplayResult(
    simulation_id="t", total_requests=200, actual_p95_ms=600.0, latency_delta_p95_ms=540.0,
)


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-test")


async def test_gemini_failure_retries_once_then_falls_back(gemini_key):
    call = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(reasoning, "_call_gemini", call), patch.object(reasoning.asyncio, "sleep", AsyncMock()):
        verdict = await reasoning.generate_verdict("", REGRESSED, BASELINE, "demo-app")
    assert call.await_count == 2  # previously recursed until the 15-minute timeout
    assert verdict.gemini_model == "deterministic-fallback"
    assert verdict.verdict == VerdictStatus.BLOCKED


async def test_no_key_skips_gemini(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    call = AsyncMock()
    with patch.object(reasoning, "_call_gemini", call):
        verdict = await reasoning.generate_verdict("", REGRESSED, BASELINE, "demo-app")
    call.assert_not_awaited()
    assert verdict.gemini_model == "deterministic-fallback"


async def test_gemini_cannot_overrule_scores_or_slo_gate(gemini_key):
    """Gemini narrates; scores (incl. the SLO gate) are always recomputed locally."""
    replay = REGRESSED.model_copy(update={"novel_error_messages": ["HTTP 404"]})
    answer = {
        "verdict": "APPROVED", "overall_score": 99, "summary": "looks fine",
        "novel_error_classification": {"HTTP 404": {"assessment": "stub_artefact", "reasoning": "stub miss"}},
        "evidence": "not-a-list",
    }
    with patch.object(reasoning, "_call_gemini", AsyncMock(return_value=answer)):
        verdict = await reasoning.generate_verdict("", replay, BASELINE, "demo-app")
    assert verdict.verdict == VerdictStatus.BLOCKED and verdict.overall_score == 49
    assert verdict.reliability_score == 100  # stub artefact → no deduction
    assert verdict.summary == "looks fine"
    assert any("SLO breach" in e for e in verdict.evidence)


def test_prompt_handles_mongo_history_records():
    history = [
        {"created_at": datetime(2026, 11, 1, tzinfo=timezone.utc), "verdict": {"verdict": "BLOCKED", "overall_score": 49},
         "dynatrace": {"replay_result": {"latency_delta_p95_ms": 540.0}}, "gitlab": {"override_applied": True}},
        {"created_at": datetime(2026, 11, 2, tzinfo=timezone.utc), "verdict": None, "dynatrace": {}},
    ]
    pre = reasoning.compute_scores_locally(REGRESSED, BASELINE)
    prompt = reasoning._build_gemini_prompt("diff", REGRESSED, BASELINE, "demo-app", pre, None, history)
    assert "2026-11-01: BLOCKED (49/100)" in prompt and "OVERRIDE" in prompt


async def test_llm_deltas_positive_means_degradation():
    judged = {
        "prod": {"hallucination": 0.0, "relevance": 0.95, "groundedness": 1.0, "count": 5},
        "sim": {"hallucination": 1.0, "relevance": 0.40, "groundedness": 0.20, "count": 5},
    }

    async def judge(outputs):
        return judged[outputs[0]["tag"]]

    with patch.object(arize_tool, "judge_llm_outputs", judge):
        result = await arize_tool.evaluate_against_traces([{"tag": "sim"}], [{"tag": "prod"}])
    assert (result.hallucination_delta, result.relevance_delta, result.groundedness_delta) == (1.0, 0.55, 0.8)
    assert reasoning._compute_llm_quality_score(result) == 30  # -40 -15 -15


def test_demo_scenarios():
    msg = build_demo_message("fixed")
    assert msg.mr_id == build_demo_message("regression").mr_id  # same MR → FR-023 cancellation
    assert len(msg.head_sha) == 40 and demo_version_for(msg) == "fixed"
    assert demo_version_for(build_demo_message("llm-regression")) == "llm-regression"


def test_wiremock_pattern_from_path_template():
    assert _path_to_pattern("/api/preferences/{id}") == "/api/preferences/[^/]+(/.*)?$"


# ──────────────────────────────────────────────
# Live end-to-end demo run (needs `docker compose up -d wiremock`)
# ──────────────────────────────────────────────

WIREMOCK = "http://127.0.0.1:8082"


def _wiremock_up() -> bool:
    try:
        return httpx.get(f"{WIREMOCK}/__admin/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _wiremock_up(), reason="local WireMock not running")
async def test_demo_regression_blocked_then_fix_approved(monkeypatch):
    from orchestrator.agent import run_simulation

    monkeypatch.setattr(settings, "bulwark_demo_mode", True)
    monkeypatch.setattr(settings, "wiremock_base_url", WIREMOCK)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    saved: dict[str, dict] = {}

    async def save_verdict(sim_id, verdict):
        saved[sim_id] = verdict

    with patch("orchestrator.agent.update_simulation", AsyncMock()), \
         patch("shared.db.update_simulation", AsyncMock()), \
         patch("shared.db.save_verdict", save_verdict), \
         patch("orchestrator.agent.get_historical_context", AsyncMock(return_value=[])), \
         patch("orchestrator.agent._get_simulation", AsyncMock(return_value={"status": "queued"})):
        regression, fixed = build_demo_message("regression"), build_demo_message("fixed")
        await run_simulation(regression)
        await run_simulation(fixed)

    assert saved[regression.simulation_id]["verdict"] == "BLOCKED"
    assert saved[fixed.simulation_id]["verdict"] == "APPROVED"
