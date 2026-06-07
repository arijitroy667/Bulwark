"""
Bulwark — Verdict Writer (v3.0, PRD §6.1 + §8.7 + §18).

Formats the simulation verdict as a GitLab MR comment and sets the pipeline
status gate. Implements the full v3 comment format:

  - Verdict header (emoji + verdict + score)
  - Metric delta table (baseline vs simulation, delta, status emoji)
  - Novel error section (classified by Gemini)
  - Gemini reasoning paragraph
  - Arize eval section (conditionally rendered when LLM detected)
  - Historical context note
  - MANDATORY uncertainty disclosure block (verbatim, PRD §8.7)
  - Override instructions when BLOCKED

Pipeline gate:
  APPROVED → Bulwark/production-ready = success
  WARNING  → Bulwark/production-ready = success (with caveat)
  BLOCKED  → Bulwark/production-ready = failed (MR cannot merge)
"""

from __future__ import annotations

import logging

from shared.config import settings
from shared.models import ArizeEvalResult, ReplayResult, ServiceBaseline, VerdictResult, VerdictStatus

logger = logging.getLogger("bulwark.verdict_writer")

# Mandatory uncertainty disclosure block — PRD §8.7
# Must appear verbatim in every MR comment
UNCERTAINTY_DISCLOSURE = """\
ℹ️ **Measurement context:** This verdict is based on {total_requests} production request samples.
p95 latency measurement uncertainty: ±35 ms. LLM evaluator noise band: ±0.08.
Signals within these bands are informational observations, not scored regressions.
Bulwark flags probable regressions — it does not certify safety. Human judgement is the final gate."""


def format_mr_comment(
    simulation_id: str,
    service_name: str,
    verdict: VerdictResult,
    replay_result: ReplayResult,
    baseline: ServiceBaseline,
    arize_result: ArizeEvalResult | None = None,
) -> str:
    """
    Format the full v3 MR comment with metric table, Gemini reasoning,
    Arize section, historical note, and mandatory uncertainty disclosure.
    """
    # Determine verdict emoji and pipeline action text
    if verdict.verdict == VerdictStatus.APPROVED:
        header_emoji = "✅"
        pipeline_action = "`Bulwark/production-ready` set to **success** — MR is mergeable."
    elif verdict.verdict == VerdictStatus.WARNING:
        header_emoji = "⚠️"
        pipeline_action = "`Bulwark/production-ready` set to **success** (with caveats) — MR is mergeable."
    else:
        header_emoji = "🚫"
        pipeline_action = "`Bulwark/production-ready` set to **failed** — MR cannot be merged until regression is resolved."

    lines: list[str] = []

    # ── Header ──
    lines.append(f"## {header_emoji} Bulwark — {verdict.verdict.value} — {verdict.overall_score}/100 confidence")
    lines.append("")
    lines.append(f"**Service:** `{service_name}` &nbsp;|&nbsp; **Simulation:** `{simulation_id}`")
    lines.append("")

    # ── Summary ──
    if verdict.summary:
        lines.append(verdict.summary)
        lines.append("")

    # ── Metric delta table ──
    lines.append("### Infrastructure Metrics")
    lines.append("")
    lines.append("| Metric | Baseline | Simulation | Delta | Status |")
    lines.append("|--------|----------|------------|-------|--------|")

    # p95 latency
    p95_delta = replay_result.latency_delta_p95_ms
    p95_status = _latency_status(p95_delta, baseline.slo_latency_budget_p95_ms)
    lines.append(
        f"| p95 Latency | {baseline.p95_latency_ms:.0f}ms | "
        f"{replay_result.actual_p95_ms:.0f}ms | "
        f"{p95_delta:+.0f}ms | {p95_status} |"
    )

    # p50 latency
    p50_delta = replay_result.actual_p50_ms - baseline.p50_latency_ms
    lines.append(
        f"| p50 Latency | {baseline.p50_latency_ms:.0f}ms | "
        f"{replay_result.actual_p50_ms:.0f}ms | "
        f"{p50_delta:+.0f}ms | {_latency_status(p50_delta, baseline.slo_latency_budget_p95_ms)} |"
    )

    # Error rate
    err_delta = replay_result.error_rate_delta_percent
    err_status = _error_rate_status(err_delta)
    lines.append(
        f"| Error Rate | {baseline.error_rate_percent:.2f}% | "
        f"{replay_result.actual_error_rate_percent:.2f}% | "
        f"{err_delta:+.2f}% | {err_status} |"
    )

    # 5xx responses
    fivexx_status = "✅ None" if replay_result.five_xx_count == 0 else f"🚫 {replay_result.five_xx_count} responses"
    lines.append(
        f"| 5xx Responses | 0 | {replay_result.five_xx_count} | "
        f"{'—' if replay_result.five_xx_count == 0 else f'+{replay_result.five_xx_count}'} | "
        f"{fivexx_status} |"
    )

    # Novel errors
    novel_count = len(replay_result.novel_error_messages)
    novel_status = "✅ None" if novel_count == 0 else _novel_error_verdict_status(verdict)
    lines.append(
        f"| Novel Errors | 0 | {novel_count} | "
        f"{'—' if novel_count == 0 else f'+{novel_count}'} | "
        f"{novel_status} |"
    )
    lines.append("")

    # ── Score breakdown ──
    lines.append("### Score Breakdown")
    lines.append("")
    lines.append(f"- **Infrastructure score:** {verdict.infrastructure_score}/100")
    lines.append(f"- **Reliability score:** {verdict.reliability_score}/100")
    if verdict.llm_quality_score is not None:
        lines.append(f"- **LLM quality score:** {verdict.llm_quality_score}/100")
    lines.append(f"- **Overall score:** **{verdict.overall_score}/100**")
    lines.append("")

    # ── Novel error classification ──
    if replay_result.novel_error_messages:
        lines.append("### Novel Error Analysis")
        lines.append("")
        for error_msg in replay_result.novel_error_messages:
            classification_data = verdict.novel_error_classification.get(error_msg, {})
            assessment = classification_data.get("assessment", "uncertain")
            reasoning = classification_data.get("reasoning", "")
            assessment_emoji = {
                "code_regression": "🚫",
                "stub_artefact": "ℹ️",
                "uncertain": "⚠️",
            }.get(assessment, "⚠️")
            lines.append(f"**{assessment_emoji} `{error_msg}`**")
            lines.append(f"- Classification: `{assessment}`")
            if reasoning:
                lines.append(f"- Reasoning: {reasoning}")
            lines.append("")

    # ── Gemini reasoning ──
    if verdict.recommendation:
        lines.append("### Gemini Analysis")
        lines.append("")
        lines.append(f"**Recommendation:** {verdict.recommendation}")
        lines.append("")

    # ── Arize LLM evaluation (Phase 2 — conditionally rendered) ──
    if arize_result and arize_result.llm_detected:
        lines.append("### LLM Quality Evaluation (Arize)")
        lines.append("")
        if arize_result.skip_reason:
            lines.append(f"⚠️ LLM evaluation skipped: {arize_result.skip_reason}")
        else:
            lines.append("| Evaluator | Baseline | Simulation | Delta | Status |")
            lines.append("|-----------|----------|------------|-------|--------|")

            def _arize_row(name: str, baseline_score: float | None, sim_score: float | None, delta: float | None) -> str:
                b = f"{baseline_score:.3f}" if baseline_score is not None else "—"
                s = f"{sim_score:.3f}" if sim_score is not None else "—"
                d = f"{delta:+.3f}" if delta is not None else "—"
                status = "✅ Within noise band" if (delta is None or abs(delta) <= 0.10) else "🚫 Regression"
                return f"| {name} | {b} | {s} | {d} | {status} |"

            lines.append(_arize_row(
                "Hallucination",
                arize_result.baseline_hallucination_score,
                arize_result.sim_hallucination_score,
                arize_result.hallucination_delta,
            ))
            lines.append(_arize_row(
                "Relevance",
                arize_result.baseline_relevance_score,
                arize_result.sim_relevance_score,
                arize_result.relevance_delta,
            ))
            lines.append(_arize_row(
                "Groundedness",
                arize_result.baseline_groundedness_score,
                arize_result.sim_groundedness_score,
                arize_result.groundedness_delta,
            ))
        lines.append("")
    elif arize_result and not arize_result.llm_detected:
        lines.append("_Arize LLM evaluation: skipped — no LLM components detected via two-stage AST scan._")
        lines.append("")

    # ── Historical note ──
    if verdict.historical_note:
        lines.append("### Historical Context")
        lines.append("")
        lines.append(f"_{verdict.historical_note}_")
        lines.append("")

    # ── Pipeline gate ──
    lines.append(f"**Pipeline gate:** {pipeline_action}")
    lines.append("")

    # ── Override instructions (BLOCKED only) ──
    if verdict.verdict == VerdictStatus.BLOCKED:
        lines.append(
            "> **To override:** Add the `bulwark-override` label to this MR. "
            "The override will be logged in the audit trail and included in future simulation context."
        )
        lines.append("")

    # ── MANDATORY uncertainty disclosure block (PRD §8.7) ──
    lines.append("---")
    lines.append("")
    lines.append(UNCERTAINTY_DISCLOSURE.format(total_requests=replay_result.total_requests))
    lines.append("")

    # ── Simulation metadata ──
    lines.append(f"<sub>Simulation ID: `{simulation_id}` · Gemini model: `{verdict.gemini_model}` · "
                 f"Reasoning: {verdict.reasoning_duration_ms}ms · Bulwark v{settings.bulwark_version}</sub>")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Status emoji helpers
# ──────────────────────────────────────────────


def _latency_status(delta_ms: float, slo_budget_ms: float) -> str:
    """Return status emoji and text for a latency delta."""
    NOISE_FLOOR = 35.0
    if delta_ms <= NOISE_FLOOR:
        return "✅ Within budget"
    noise_multiple = delta_ms / NOISE_FLOOR if NOISE_FLOOR > 0 else 0
    slo_multiple = delta_ms / slo_budget_ms if slo_budget_ms > 0 else 0
    return f"🚫 {slo_multiple:.1f}x SLO budget ({noise_multiple:.1f}x noise floor)"


def _error_rate_status(delta_percent: float) -> str:
    """Return status emoji and text for an error rate delta."""
    NOISE_FLOOR = 1.0
    if delta_percent <= NOISE_FLOOR:
        return "✅ Within noise band"
    return f"🚫 {delta_percent:+.2f}% above noise floor"


def _novel_error_verdict_status(verdict: VerdictResult) -> str:
    """Return status text for the novel error row based on Gemini classification."""
    has_regression = any(
        v.get("assessment") == "code_regression"
        for v in verdict.novel_error_classification.values()
    )
    has_uncertain = any(
        v.get("assessment") == "uncertain"
        for v in verdict.novel_error_classification.values()
    )
    if has_regression:
        return "🚫 Code regression"
    elif has_uncertain:
        return "⚠️ Uncertain"
    return "ℹ️ Stub artefact"


# ──────────────────────────────────────────────
# Verdict enforcement (GitLab + MongoDB)
# ──────────────────────────────────────────────


async def enforce_verdict(
    project_id: str,
    mr_id: int,
    head_sha: str,
    simulation_id: str,
    service_name: str,
    verdict: VerdictResult,
    replay_result: ReplayResult | None,
    baseline: ServiceBaseline,
    arize_result: ArizeEvalResult | None = None,
) -> None:
    """
    Post verdict to GitLab and set pipeline status gate.

    Steps:
    1. Format the MR comment
    2. Post the comment via GitLab API
    3. Set Bulwark/production-ready pipeline status (success/failed)
    4. Approve or request changes on the MR (if PAT has approval rights)
    5. Persist results to MongoDB
    """
    from orchestrator.tools.gitlab_tool import post_mr_comment, set_pipeline_status, update_mr_approval
    from shared.db import update_simulation

    if replay_result is None:
        from shared.models import ReplayResult as RR
        replay_result = RR(simulation_id=simulation_id)

    comment_body = format_mr_comment(
        simulation_id=simulation_id,
        service_name=service_name,
        verdict=verdict,
        replay_result=replay_result,
        baseline=baseline,
        arize_result=arize_result,
    )

    # Step 1+2: Post MR comment
    comment_id = None
    try:
        comment_id = await post_mr_comment(project_id, mr_id, comment_body)
        logger.info("Posted verdict comment on MR !%d: note_id=%s", mr_id, comment_id)
    except Exception as e:
        logger.error("Failed to post MR comment: %s", e)

    # Step 3: Set pipeline status
    pipeline_state = "success" if verdict.verdict in (VerdictStatus.APPROVED, VerdictStatus.WARNING) else "failed"
    pipeline_description = {
        VerdictStatus.APPROVED: f"APPROVED — {verdict.overall_score}/100 confidence",
        VerdictStatus.WARNING: f"WARNING — {verdict.overall_score}/100 confidence (review recommended)",
        VerdictStatus.BLOCKED: f"BLOCKED — {verdict.overall_score}/100 confidence (regression detected)",
    }[verdict.verdict]

    try:
        await set_pipeline_status(
            project_id=project_id,
            sha=head_sha,
            state=pipeline_state,
            description=pipeline_description,
        )
        logger.info(
            "Pipeline status: %s (%s) for sha %s",
            pipeline_state,
            pipeline_description,
            head_sha[:8],
        )
    except Exception as e:
        logger.error("Failed to set pipeline status: %s", e)

    # Step 4: Approve or request changes (best-effort)
    try:
        if verdict.verdict == VerdictStatus.APPROVED:
            await update_mr_approval(project_id, mr_id, approve=True)
        elif verdict.verdict == VerdictStatus.BLOCKED:
            await update_mr_approval(project_id, mr_id, approve=False)
    except Exception as e:
        logger.warning("MR approval update failed (non-critical): %s", e)

    # Step 5: Persist to MongoDB
    try:
        await update_simulation(simulation_id, {
            "verdict": verdict.model_dump(mode="json"),
            "gitlab.comment_id": comment_id,
            "gitlab.pipeline_status": pipeline_state,
        })
    except Exception as e:
        logger.error("Failed to persist verdict to MongoDB: %s", e)
