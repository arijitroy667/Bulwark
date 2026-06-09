"""
Bulwark — Gemini Reasoning Step (v3.0).

Implements the two-phase verdict generation:
  1. Deterministic pre-processing (PRD §8) — computes scores using tiered
     deduction tables. Runs before Gemini, structures the inputs.
  2. Gemini reasoning call — stateless LLM call that receives the structured
     context window and produces a JSON verdict with novel error classification,
     historical narration, and uncertainty acknowledgement.

Key v3 corrections from v1:
  - Noise floor: ±35ms p95 (was ±20ms — below measurement noise)
  - Noise floor: ±1.0% error rate (was not correctly applied)
  - Tiered deduction tables (was linear/flat)
  - LLM quality score formula with evaluator noise band (±0.10)
  - Weighted formula: 40/60 (no LLM) or 30/40/30 (with LLM)
  - Novel error classification delegated to Gemini (not scored blindly)
  - Mandatory uncertainty_acknowledgement in every verdict
  - historical_note field populated from injected context
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from shared.config import settings
from shared.models import ArizeEvalResult, ReplayResult, ServiceBaseline, VerdictResult, VerdictStatus

logger = logging.getLogger("bulwark.reasoning")

# ──────────────────────────────────────────────
# Constants — PRD §8 noise floors
# ──────────────────────────────────────────────

P95_NOISE_FLOOR_MS = 35.0       # ±35ms (v1 had ±20ms — below measurement noise)
ERROR_RATE_NOISE_FLOOR = 1.0    # ±1.0% binomial noise at n=200
LLM_EVAL_NOISE_BAND = 0.10      # ±0.08–0.10 Arize evaluator noise


# ──────────────────────────────────────────────
# Infrastructure Score (PRD §8.2)
# ──────────────────────────────────────────────


def _compute_infrastructure_score(replay: ReplayResult, baseline: ServiceBaseline) -> int:
    """
    Compute infrastructure score (0–100) using v3 tiered deduction tables.

    Signals within the noise floor are NOT deducted — they are informational.
    This prevents false positives from measurement noise.

    Scoring table (per PRD §8.2):
      p95 delta within ±35ms: no deduction
      p95 delta 36–100ms:    -15 per 20ms above noise floor (capped at -50)
      p95 delta >100ms:       -30 per 20ms above noise floor (capped at -80)
      error rate within ±1%: no deduction
      error rate 1–3%:       -20 per 1% above noise floor (capped at -40)
      error rate >3%:         -40 per 1% above noise floor (capped at -60)
      5xx responses >0:       -40 (binary, sample-size independent)
    """
    score = 100

    # ── p95 latency delta ──
    delta_p95 = replay.latency_delta_p95_ms
    if delta_p95 > 100.0:
        # High-confidence regression: -30 per 20ms above noise floor
        excess_above_floor = delta_p95 - P95_NOISE_FLOOR_MS
        deduction = int((excess_above_floor / 20.0) * 30)
        score -= min(deduction, 80)  # Cap at 80 to leave floor at 20
    elif delta_p95 > P95_NOISE_FLOOR_MS:
        # Probable regression: -15 per 20ms above noise floor
        excess_above_floor = delta_p95 - P95_NOISE_FLOOR_MS
        deduction = int((excess_above_floor / 20.0) * 15)
        score -= min(deduction, 50)
    # else: within noise floor — no deduction, informational only

    # ── Error rate delta ──
    delta_err = replay.error_rate_delta_percent
    if delta_err > 3.0:
        # High-confidence regression: -40 per 1% above noise floor
        excess_above_floor = delta_err - ERROR_RATE_NOISE_FLOOR
        deduction = int(excess_above_floor * 40)
        score -= min(deduction, 60)
    elif delta_err > ERROR_RATE_NOISE_FLOOR:
        # Probable regression: -20 per 1% above noise floor
        excess_above_floor = delta_err - ERROR_RATE_NOISE_FLOOR
        deduction = int(excess_above_floor * 20)
        score -= min(deduction, 40)
    # else: within noise floor — no deduction

    # ── 5xx responses ── (binary signal, sample-size independent)
    if replay.five_xx_count > 0:
        score -= 40

    return max(0, score)


# ──────────────────────────────────────────────
# Reliability Score (PRD §8.3)
# ──────────────────────────────────────────────


def _compute_reliability_score(
    replay: ReplayResult,
    novel_error_classifications: dict[str, str] | None = None,
) -> int:
    """
    Compute reliability score (0–100) using Gemini's novel error classifications.

    Novel errors classified as stub artefacts = 0 deduction (environment noise).
    Novel errors classified as code regressions = -25 each (real regression).
    Novel errors with uncertain classification = -10 each (partial deduction).

    Without Gemini classifications (pre-Gemini pass), all novel errors score -10
    (will be recalculated after Gemini classifies them).
    """
    score = 100
    classifications = novel_error_classifications or {}

    for error_msg in replay.novel_error_messages:
        classification = classifications.get(error_msg, "uncertain")
        if classification == "code_regression":
            score -= 25
        elif classification == "uncertain":
            score -= 10
        # stub_artefact → 0 deduction

    return max(0, score)


# ──────────────────────────────────────────────
# LLM Quality Score (PRD §8.4)
# ──────────────────────────────────────────────


def _compute_llm_quality_score(arize_result: ArizeEvalResult) -> int | None:
    """
    Compute LLM quality score (0–100) from Arize evaluation deltas.

    Returns None if Arize evaluation was skipped (no LLM detected or unavailable).
    Deltas <= EVAL_NOISE_BAND are not scored (informational only).
    """
    if not arize_result.llm_detected or arize_result.skip_reason:
        return None

    score = 100

    # Hallucination delta
    h_delta = arize_result.hallucination_delta or 0.0
    if h_delta > 0.25:
        score -= 40
    elif h_delta > LLM_EVAL_NOISE_BAND:
        score -= 20
    # else: within noise band

    # Relevance delta
    r_delta = arize_result.relevance_delta or 0.0
    if r_delta > LLM_EVAL_NOISE_BAND:
        score -= 15  # Only one tier for relevance per PRD §8.4

    # Groundedness delta
    g_delta = arize_result.groundedness_delta or 0.0
    if g_delta > LLM_EVAL_NOISE_BAND:
        score -= 15

    return max(0, score)


# ──────────────────────────────────────────────
# Overall Score and Verdict (PRD §8.5, §8.6)
# ──────────────────────────────────────────────


def _compute_overall_score(
    infra_score: int,
    reliability_score: int,
    llm_quality_score: int | None,
) -> int:
    """
    Compute weighted overall score (PRD §8.5).

    No LLM:   overall = infra × 0.40 + reliability × 0.60
    With LLM: overall = infra × 0.30 + reliability × 0.40 + llm × 0.30
    """
    if llm_quality_score is None:
        overall = infra_score * 0.40 + reliability_score * 0.60
    else:
        overall = (
            infra_score * 0.30
            + reliability_score * 0.40
            + llm_quality_score * 0.30
        )
    return max(0, min(100, int(round(overall))))


def _score_to_verdict(overall_score: int) -> VerdictStatus:
    """Map overall score to verdict (PRD §8.6)."""
    if overall_score >= settings.confidence_threshold_approved:
        return VerdictStatus.APPROVED
    elif overall_score >= settings.confidence_threshold_warning:
        return VerdictStatus.WARNING
    else:
        return VerdictStatus.BLOCKED


def _build_uncertainty_acknowledgement(replay: ReplayResult, baseline: ServiceBaseline) -> str:
    """
    Build the mandatory uncertainty disclosure block (PRD §8.7).
    Must appear verbatim in every MR comment.
    """
    if replay.skip_reason:
        return f"Replay skipped: {replay.skip_reason}"

    delta_multiple = ""
    if replay.latency_delta_p95_ms > P95_NOISE_FLOOR_MS and P95_NOISE_FLOOR_MS > 0:
        multiple = replay.latency_delta_p95_ms / P95_NOISE_FLOOR_MS
        delta_multiple = f" Delta of +{replay.latency_delta_p95_ms:.0f}ms is {multiple:.1f}x the noise floor — high-confidence regression." if multiple >= 3 else ""

    return (
        f"p95 computed from {replay.total_requests} samples; "
        f"measurement uncertainty ±{P95_NOISE_FLOOR_MS:.0f}ms."
        f"{delta_multiple}"
    )


# ──────────────────────────────────────────────
# Local scoring (deterministic fallback)
# ──────────────────────────────────────────────


def compute_scores_locally(
    replay_result: ReplayResult | None,
    baseline: ServiceBaseline,
    arize_result: ArizeEvalResult | None = None,
    novel_error_classifications: dict[str, str] | None = None,
) -> VerdictResult:
    """
    Compute a verdict using purely deterministic threshold logic.

    Used as:
    1. Pre-processing step before Gemini (to structure inputs)
    2. Fallback if Gemini API is unavailable (FR: Gemini timeout handling)

    Does NOT include novel error narration or historical context narration —
    those require Gemini.
    """
    if replay_result is None:
        replay_result = ReplayResult(simulation_id="unknown")

    infra_score = _compute_infrastructure_score(replay_result, baseline)
    reliability_score = _compute_reliability_score(replay_result, novel_error_classifications)
    llm_score = _compute_llm_quality_score(arize_result) if arize_result else None
    overall_score = _compute_overall_score(infra_score, reliability_score, llm_score)
    verdict_status = _score_to_verdict(overall_score)

    # Build evidence list
    evidence: list[str] = []
    if replay_result.skip_reason:
        evidence.append(replay_result.skip_reason)
    else:
        delta_p95 = replay_result.latency_delta_p95_ms
        if delta_p95 > P95_NOISE_FLOOR_MS:
            evidence.append(
                f"p95 latency increased from {baseline.p95_latency_ms:.0f}ms to "
                f"{replay_result.actual_p95_ms:.0f}ms (+{delta_p95:.0f}ms — "
                f"{delta_p95/P95_NOISE_FLOOR_MS:.1f}x measurement noise floor of ±{P95_NOISE_FLOOR_MS:.0f}ms)"
            )
        else:
            evidence.append(
                f"p95 latency delta +{delta_p95:.1f}ms — within ±{P95_NOISE_FLOOR_MS:.0f}ms noise floor, not scored"
            )

        delta_err = replay_result.error_rate_delta_percent
        if delta_err > ERROR_RATE_NOISE_FLOOR:
            evidence.append(
                f"Error rate increased {delta_err:+.2f}% above noise floor — regression signal"
            )
        else:
            evidence.append(
                f"Error rate delta {delta_err:+.2f}% — within ±{ERROR_RATE_NOISE_FLOOR:.1f}% noise floor, not scored"
            )

        if replay_result.five_xx_count > 0:
            evidence.append(f"5xx responses: {replay_result.five_xx_count} (binary signal — scored)")

        for error_msg in replay_result.novel_error_messages:
            classification = (novel_error_classifications or {}).get(error_msg, "uncertain")
            evidence.append(f"Novel error: \"{error_msg}\" — classified as {classification}")

    summary_parts = []
    if verdict_status == VerdictStatus.BLOCKED:
        summary_parts.append(f"Regression(s) detected. Confidence score {overall_score}/100 below BLOCKED threshold.")
    elif verdict_status == VerdictStatus.WARNING:
        summary_parts.append(f"Minor signals detected. Confidence score {overall_score}/100 — WARNING.")
    else:
        summary_parts.append(f"No significant regressions detected. Confidence score {overall_score}/100.")

    uncertainty = _build_uncertainty_acknowledgement(replay_result, baseline)

    return VerdictResult(
        infrastructure_score=infra_score,
        reliability_score=reliability_score,
        llm_quality_score=llm_score,
        overall_score=overall_score,
        verdict=verdict_status,
        summary=" ".join(summary_parts),
        evidence=evidence,
        uncertainty_acknowledgement=uncertainty,
        novel_error_classification={},
        historical_note="",
        recommendation=(
            "Review the changes that introduced the regression. "
            "Gemini reasoning unavailable — verdict based on threshold logic only."
            if verdict_status != VerdictStatus.APPROVED else ""
        ),
    )


# ──────────────────────────────────────────────
# Gemini reasoning call
# ──────────────────────────────────────────────


async def generate_verdict(
    diff_summary: str,
    replay_result: ReplayResult | None,
    baseline: ServiceBaseline,
    service_name: str,
    arize_result: ArizeEvalResult | None = None,
    historical_context: list[dict[str, Any]] | None = None,
) -> VerdictResult:
    """
    Call Gemini to generate the final verdict with reasoning.

    Assembles a structured context window containing:
    1. Code diff summary
    2. Infrastructure metric deltas (pre-scored by compute_scores_locally)
    3. Arize LLM quality deltas (if applicable)
    4. SLO targets from Dynatrace
    5. Historical context (last 5 simulation verdicts for same service)

    Gemini outputs a JSON verdict with novel error classification,
    historical narration, and uncertainty acknowledgement.

    Falls back to compute_scores_locally if Gemini is unavailable.
    """
    if replay_result is None:
        replay_result = ReplayResult(simulation_id="unknown")

    # Pre-compute deterministic scores (Gemini refines the narrative, not the raw scores)
    pre_scores = compute_scores_locally(replay_result, baseline, arize_result)

    start_time = time.time()

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)

        prompt = _build_gemini_prompt(
            diff_summary=diff_summary,
            replay_result=replay_result,
            baseline=baseline,
            service_name=service_name,
            pre_scores=pre_scores,
            arize_result=arize_result,
            historical_context=historical_context or [],
        )

        logger.info("Calling Gemini %s for verdict reasoning...", settings.gemini_model)
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,  # Low temperature for consistent structured output
            ),
        )

        raw_json = response.text
        verdict_data = json.loads(raw_json)

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info("Gemini verdict: %s (%d/100) in %dms", verdict_data.get("verdict"), verdict_data.get("overall_score"), duration_ms)

        # Merge Gemini's novel error classifications back into pre_scores reliability score
        novel_classifications = {
            k: v.get("assessment", "uncertain")
            for k, v in verdict_data.get("novel_error_classification", {}).items()
        }
        # Recompute reliability with Gemini's classifications
        reliability_score = _compute_reliability_score(replay_result, novel_classifications)
        llm_score = pre_scores.llm_quality_score
        overall_score = _compute_overall_score(pre_scores.infrastructure_score, reliability_score, llm_score)
        final_verdict = _score_to_verdict(overall_score)

        return VerdictResult(
            infrastructure_score=pre_scores.infrastructure_score,
            reliability_score=reliability_score,
            llm_quality_score=llm_score,
            overall_score=overall_score,
            verdict=final_verdict,
            summary=verdict_data.get("summary", pre_scores.summary),
            evidence=verdict_data.get("evidence", pre_scores.evidence),
            novel_error_classification=verdict_data.get("novel_error_classification", {}),
            historical_note=verdict_data.get("historical_note", ""),
            recommendation=verdict_data.get("recommendation", ""),
            uncertainty_acknowledgement=verdict_data.get(
                "uncertainty_acknowledgement",
                _build_uncertainty_acknowledgement(replay_result, baseline),
            ),
            gemini_model=settings.gemini_model,
            reasoning_duration_ms=duration_ms,
        )

    except json.JSONDecodeError as e:
        logger.error("Gemini returned invalid JSON: %s", e)
        # Fall back to deterministic scoring
        return pre_scores

    except Exception as e:
        logger.error("Gemini reasoning failed: %s — falling back to threshold logic", e)
        # Retry once after 5 seconds (per PRD §16)
        if "retry" not in str(type(e).__name__).lower():
            try:
                import asyncio
                await asyncio.sleep(5)
                return await generate_verdict(
                    diff_summary, replay_result, baseline, service_name,
                    arize_result, historical_context,
                )
            except Exception as retry_e:
                logger.error("Gemini retry also failed: %s", retry_e)

        fallback = compute_scores_locally(replay_result, baseline, arize_result)
        fallback.recommendation = (
            "Gemini reasoning unavailable — verdict based on threshold logic only. "
            + fallback.recommendation
        )
        return fallback


# ──────────────────────────────────────────────
# Prompt assembly
# ──────────────────────────────────────────────


def _build_gemini_prompt(
    diff_summary: str,
    replay_result: ReplayResult,
    baseline: ServiceBaseline,
    service_name: str,
    pre_scores: VerdictResult,
    arize_result: ArizeEvalResult | None,
    historical_context: list[dict[str, Any]],
) -> str:
    """
    Assemble the structured context window for Gemini.

    Per PRD §7: Gemini is a stateless reasoner. It has no memory between
    calls. All context must be injected into this prompt.
    """
    # Format historical context
    history_text = "No prior simulation records for this service."
    if historical_context:
        history_parts = []
        for i, sim in enumerate(historical_context):
            verdict_info = sim.get("verdict", {})
            v = verdict_info.get("verdict", "UNKNOWN")
            score = verdict_info.get("overall_score", 0)
            p95_delta = sim.get("dynatrace", {}).get("replay_result", {}).get("latency_delta_p95_ms", 0)
            err_delta = sim.get("dynatrace", {}).get("replay_result", {}).get("error_rate_delta_percent", 0)
            override = sim.get("gitlab", {}).get("override_applied", False)
            created = sim.get("created_at", "")
            override_note = " [OVERRIDE APPLIED by engineer]" if override else ""
            history_parts.append(
                f"  {i+1}. {created[:10]}: {v} ({score}/100) | "
                f"p95Δ={p95_delta:+.0f}ms | errΔ={err_delta:+.2f}%{override_note}"
            )
        history_text = "Last {} simulation(s) for {}:\n".format(
            len(historical_context), service_name
        ) + "\n".join(history_parts)

    # Format Arize section
    arize_text = "Arize evaluation: Not applicable (no LLM components detected via AST scan)."
    if arize_result and arize_result.llm_detected:
        if arize_result.skip_reason:
            arize_text = f"Arize evaluation: Skipped — {arize_result.skip_reason}"
        else:
            arize_text = (
                f"Arize LLM evaluation:\n"
                f"  Hallucination delta: {arize_result.hallucination_delta:+.3f} "
                f"({'ABOVE' if (arize_result.hallucination_delta or 0) > 0.10 else 'within'} noise band)\n"
                f"  Relevance delta: {arize_result.relevance_delta:+.3f} "
                f"({'ABOVE' if (arize_result.relevance_delta or 0) > 0.10 else 'within'} noise band)\n"
                f"  Groundedness delta: {arize_result.groundedness_delta:+.3f} "
                f"({'ABOVE' if (arize_result.groundedness_delta or 0) > 0.10 else 'within'} noise band)"
            )

    novel_errors_text = "None."
    if replay_result.novel_error_messages:
        novel_errors_text = "\n".join(
            f'  - "{e}"' for e in replay_result.novel_error_messages
        )

    if replay_result.skip_reason:
        simulation_results_text = f"Replay skipped: {replay_result.skip_reason}"
    else:
        simulation_results_text = f"""## Simulation Results ({replay_result.total_requests} requests replayed)
- Actual p95 latency: {replay_result.actual_p95_ms:.0f}ms (delta: {replay_result.latency_delta_p95_ms:+.0f}ms)
- Actual p50 latency: {replay_result.actual_p50_ms:.0f}ms
- Actual error rate: {replay_result.actual_error_rate_percent:.2f}% (delta: {replay_result.error_rate_delta_percent:+.2f}%)
- 5xx responses: {replay_result.five_xx_count}
- Novel error messages (not in production baseline):
{novel_errors_text}"""

    prompt = f"""You are Bulwark, a pre-deployment production simulation agent. Your task is to analyze the evidence below and produce a structured JSON verdict.

## Code Changes (diff summary)
{diff_summary or "No diff summary available."}

## Production Baseline (from Dynatrace)
Service: {service_name}
- p50 latency: {baseline.p50_latency_ms:.0f}ms
- p95 latency: {baseline.p95_latency_ms:.0f}ms
- p99 latency: {baseline.p99_latency_ms:.0f}ms
- Error rate: {baseline.error_rate_percent:.2f}%
- Throughput: {baseline.throughput_rpm:.0f} RPM
- SLO p95 budget: {baseline.slo_latency_budget_p95_ms:.0f}ms
- SLO error budget: {baseline.slo_error_budget_percent:.1f}%

{simulation_results_text}

## Pre-computed Scores (deterministic threshold logic, PRD §8)
- Infrastructure score: {pre_scores.infrastructure_score}/100
- Reliability score (pre-Gemini, unclassified novel errors scored as uncertain): {pre_scores.reliability_score}/100
- LLM quality score: {pre_scores.llm_quality_score if pre_scores.llm_quality_score is not None else 'N/A (no LLM detected)'}
- Overall score: {pre_scores.overall_score}/100
- Noise floors applied: ±35ms p95 latency, ±1.0% error rate

## {arize_text}

## Historical Context (MongoDB — injected into this prompt, not Gemini memory)
{history_text}

## Your Tasks
1. Classify each novel error string as one of: `code_regression`, `stub_artefact`, or `uncertain`
   - `code_regression`: the error pattern aligns with code changes in the diff and does not appear in production baseline
   - `stub_artefact`: the error is a 404 or timeout from a WireMock stub miss (environment artefact, not a real regression)
   - `uncertain`: cannot determine from the evidence
2. Write a `historical_note` narrating whether the historical data provides context for this verdict (or note if there is no history)
3. Write a concise `summary` and `recommendation`
4. Write the `uncertainty_acknowledgement` noting measurement limitations

## Output Format (strict JSON, no markdown)
{{
  "infrastructure_score": {pre_scores.infrastructure_score},
  "reliability_score": <integer 0-100, recomputed after your novel error classifications>,
  "llm_quality_score": {pre_scores.llm_quality_score if pre_scores.llm_quality_score is not None else 'null'},
  "overall_score": <integer 0-100, recomputed after your classifications>,
  "verdict": "<APPROVED|WARNING|BLOCKED>",
  "summary": "<one sentence summary of findings>",
  "evidence": ["<evidence item 1>", "<evidence item 2>"],
  "novel_error_classification": {{
    "<error string>": {{
      "assessment": "<code_regression|stub_artefact|uncertain>",
      "reasoning": "<one sentence explanation>"
    }}
  }},
  "historical_note": "<narration of historical context or 'No prior simulation records for this service.'>",
  "recommendation": "<actionable recommendation for the engineer>",
  "uncertainty_acknowledgement": "p95 computed from {replay_result.total_requests} samples; measurement uncertainty ±35ms. LLM evaluator noise band: ±0.08. Signals within these bands are informational observations, not scored regressions. Bulwark flags probable regressions — it does not certify safety. Human judgement is the final gate."
}}

Important: infrastructure_score is fixed at {pre_scores.infrastructure_score}. Recompute reliability_score based on your novel error classifications (code_regression=-25, uncertain=-10, stub_artefact=0 per novel error). Then recompute overall_score using the formula: infra×0.{30 if pre_scores.llm_quality_score is not None else 40} + reliability×0.{40 if pre_scores.llm_quality_score is not None else 60}{' + llm×0.30' if pre_scores.llm_quality_score is not None else ''}. APPROVED≥80, WARNING≥50, BLOCKED<50."""

    return prompt
