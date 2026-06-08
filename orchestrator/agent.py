"""
Bulwark — Orchestrator Agent (v3.0).

The central brain that manages the entire 9-step simulation workflow.
Consumes MR event messages from the queue and executes each step sequentially:

  1. INGEST         — Fetch MR metadata from GitLab; run two-stage LLM detection
  2. FETCH BASELINE — Get production metrics + SLO targets from Dynatrace
     ↕ (parallel with Step 3)
  3. LLM BASELINE   — Fetch Arize LLM baseline (if LLM detected)
  3.5. HISTORY      — Fetch last 5 simulation verdicts for same service
  4. PROVISION      — Build ephemeral environment on Cloud Run
  5. REPLAY         — Replay 200 production requests against new code
  6. LLM EVALUATION — Run Arize evaluators (if LLM detected)
  7. TEARDOWN       — Destroy ephemeral environment (always runs)
  8. REASONING      — Gemini analyzes evidence and produces JSON verdict
  9. ENFORCE        — Post verdict to GitLab, set pipeline status

v3 changes:
- Steps 2 + 3 run in parallel (asyncio.gather)
- Step 3 implemented: Arize baseline fetch
- Step 6 implemented: Arize eval run
- Two-stage LLM detection via orchestrator.llm_detector
- Historical context fetched and injected into Gemini prompt (Step 3.5)
- head_sha cancellation check at Step 1 entry
- Novel error classification by Gemini (not scored blindly in Step 8)
- arize_result passed through to verdict writer
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from shared.config import settings
from shared.db import (
    get_database,
    get_historical_context,
    update_simulation,
)
from shared.models import (
    ArizeEvalResult,
    DynatraceData,
    MREventMessage,
    ReplayResult,
    ServiceBaseline,
    SimulationStatus,
    VerdictResult,
)

logger = logging.getLogger("bulwark.orchestrator")


# ──────────────────────────────────────────────
# Main orchestration loop
# ──────────────────────────────────────────────


async def run_simulation(message: MREventMessage) -> None:
    """
    Execute the full Bulwark simulation workflow for a merge request.

    This is the entry point called when a Pub/Sub message is consumed.
    Runs all 9 steps of the agentic loop from PRD §7.
    """
    simulation_id = message.simulation_id
    start_time = time.time()
    error_log: list[str] = []

    logger.info(
        "═══ Starting simulation %s for MR !%d (%s → %s) [SHA: %s] ═══",
        simulation_id,
        message.mr_id,
        message.source_branch,
        message.target_branch,
        message.head_sha[:8] if message.head_sha else "unknown",
    )

    # Track state across steps
    mr_details = None
    diffs = None
    diff_summary = ""
    baseline = ServiceBaseline(service_name="unknown")
    topology = None
    production_requests = []
    environment = None
    replay_result = None
    arize_result = ArizeEvalResult()
    verdict = None
    service_name = "default"
    has_llm_components = False
    historical_context: list = []

    try:
        # ════════════════════════════════════════════
        # STEP 1: INGEST [GitLab MCP] + LLM Detection
        # ════════════════════════════════════════════
        logger.info("── Step 1: INGEST ──")
        await update_simulation(simulation_id, {
            "status": SimulationStatus.INGESTING.value,
        })

        try:
            from orchestrator.tools.gitlab_tool import (
                generate_diff_summary,
                get_file_contents_batch,
                get_mr_details,
                get_mr_diff,
                identify_services_from_diff,
                is_doc_only_mr,
            )
            from orchestrator.llm_detector import detect_llm_components

            # ── Check if simulation was cancelled since it was queued ──
            # (A new push may have arrived while we were starting)
            sim_doc = await _get_simulation(simulation_id)
            if sim_doc and sim_doc.get("status") == SimulationStatus.CANCELLED.value:
                logger.info("Simulation %s was cancelled before Step 1 could run — aborting", simulation_id)
                return

            mr_details = await get_mr_details(message.project_id, message.mr_id)
            diffs = await get_mr_diff(message.project_id, message.mr_id)
            diff_summary = generate_diff_summary(diffs)

            # Skip doc-only MRs (FR-005)
            if is_doc_only_mr(mr_details.changed_files):
                logger.info("Skipping doc-only MR !%d", message.mr_id)
                await update_simulation(simulation_id, {
                    "status": SimulationStatus.COMPLETED.value,
                    "completed_at": datetime.now(timezone.utc),
                    "error_log": ["Skipped: documentation-only MR"],
                })
                return

            # Identify affected services
            services = identify_services_from_diff(mr_details.changed_files)
            service_name = services[0] if services else "default"

            # ── Stage 2 AST scan for LLM detection (PRD §10) ──
            # Fetch source files for AST analysis (capped at 50 by llm_detector)
            from orchestrator.llm_detector import AST_SCAN_FILE_CAP
            scannable = [
                f for f in mr_details.changed_files
                if f.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".mjs"))
            ][:AST_SCAN_FILE_CAP]

            file_contents: dict[str, str] = {}
            if scannable:
                try:
                    file_contents = await get_file_contents_batch(
                        message.project_id,
                        mr_details.source_branch,
                        scannable,
                    )
                except Exception as fe:
                    logger.warning("File contents fetch failed for AST scan: %s", fe)

            llm_detection = detect_llm_components(mr_details.changed_files, file_contents)
            has_llm_components = llm_detection.detected
            mr_details.has_llm_components = has_llm_components

            logger.info(
                "MR !%d: '%s' — %d files — services: %s — LLM: %s (%s)",
                message.mr_id,
                mr_details.title,
                len(mr_details.changed_files),
                services,
                has_llm_components,
                llm_detection.detection_stage,
            )

            await update_simulation(simulation_id, {
                "service_name": service_name,
            })

        except Exception as e:
            error_msg = f"Step 1 (INGEST) failed: {e}"
            logger.error(error_msg)
            error_log.append(error_msg)
            # MR metadata is critical — cannot continue without it
            await _fail_simulation(simulation_id, error_log, start_time)
            return

        # ════════════════════════════════════════════
        # STEPS 2 + 3: PARALLEL — Dynatrace + Arize baselines
        # ════════════════════════════════════════════
        logger.info("── Steps 2 + 3: FETCH BASELINE (parallel Dynatrace + Arize) ──")
        await update_simulation(simulation_id, {
            "status": SimulationStatus.FETCHING_BASELINE.value,
        })

        has_incident = False
        arize_baseline: dict = {}

        async def _fetch_dynatrace_baseline() -> None:
            nonlocal baseline, topology, production_requests, has_incident
            try:
                from orchestrator.tools.dynatrace_tool import (
                    get_production_requests,
                    get_service_baseline,
                    get_service_topology,
                    has_active_incident,
                )
                has_incident = await has_active_incident(service_name)
                if has_incident:
                    return
                baseline = await get_service_baseline(service_name)
                topology = await get_service_topology(service_name)
                production_requests = await get_production_requests(
                    service_name, hours=24, limit=settings.replay_request_limit,
                )
                logger.info(
                    "Baseline: p95=%.1fms, error=%.2f%%, requests=%d, deps=%d",
                    baseline.p95_latency_ms,
                    baseline.error_rate_percent,
                    len(production_requests),
                    len(topology.dependencies) if topology else 0,
                )
            except Exception as e:
                error_msg = f"Step 2 (DYNATRACE BASELINE) failed: {e}"
                logger.error(error_msg)
                error_log.append(error_msg)

        async def _fetch_arize_baseline() -> None:
            nonlocal arize_baseline
            if not has_llm_components:
                logger.debug("Step 3: Arize baseline skipped — no LLM components")
                return
            try:
                from orchestrator.tools.arize_tool import get_llm_baseline
                arize_baseline = await get_llm_baseline(service_name)
                logger.info("Arize baseline fetched: dataset_id=%s", arize_baseline.get("dataset_id"))
            except Exception as e:
                error_msg = f"Step 3 (ARIZE BASELINE) failed: {e}"
                logger.warning(error_msg)
                error_log.append(error_msg)

        # Run both in parallel
        await asyncio.gather(_fetch_dynatrace_baseline(), _fetch_arize_baseline())

        # Check for active incident (defer evaluation if found)
        if has_incident:
            logger.warning("Active incident detected for %s — deferring", service_name)
            from orchestrator.tools.gitlab_tool import post_mr_comment
            await post_mr_comment(
                message.project_id,
                message.mr_id,
                (
                    "## 🛡️ Bulwark — Evaluation Deferred\n\n"
                    f"An active production incident was detected on service `{service_name}`. "
                    "Bulwark will re-evaluate when you push a new commit.\n\n"
                    f"*Simulation ID: `{simulation_id}`*"
                ),
            )
            await update_simulation(simulation_id, {
                "status": SimulationStatus.COMPLETED.value,
                "completed_at": datetime.now(timezone.utc),
                "dynatrace": {"active_incident_at_start": True},
                "error_log": ["Deferred: active production incident"],
            })
            return

        # ════════════════════════════════════════════
        # STEP 3.5: FETCH HISTORICAL CONTEXT
        # ════════════════════════════════════════════
        logger.info("── Step 3.5: HISTORICAL CONTEXT ──")
        try:
            historical_context = await get_historical_context(service_name, limit=5)
            logger.info(
                "Historical context: %d prior simulation(s) found for %s",
                len(historical_context),
                service_name,
            )
        except Exception as e:
            logger.warning("Historical context fetch failed (non-critical): %s", e)
            historical_context = []

        # ════════════════════════════════════════════
        # STEP 4: PROVISION EPHEMERAL ENVIRONMENT
        # ════════════════════════════════════════════
        logger.info("── Step 4: PROVISION ──")
        await update_simulation(simulation_id, {
            "status": SimulationStatus.PROVISIONING.value,
        })

        try:
            from environment_builder.builder import provision_environment

            environment = await provision_environment(
                simulation_id=simulation_id,
                source_branch=message.source_branch,
                project_id=message.project_id,
                dockerfile_path=mr_details.dockerfile_path or "Dockerfile",
                topology=topology,
            )

            if environment.status.value not in ("healthy",):
                error_msg = f"Environment unhealthy: {environment.status.value}"
                logger.error(error_msg)
                error_log.append(error_msg)

                comment_body = (
                    f"## 🛡️ Bulwark — Build Failed\n\n"
                    f"Environment status: `{environment.status.value}`\n\n"
                )
                if environment.startup_log_tail:
                    comment_body += f"Last startup logs:\n```\n{environment.startup_log_tail}\n```\n\n"
                else:
                    comment_body += "Check the Dockerfile for syntax errors.\n\n"
                comment_body += f"*Simulation ID: `{simulation_id}`*"

                from orchestrator.tools.gitlab_tool import post_mr_comment
                await post_mr_comment(message.project_id, message.mr_id, comment_body)
                await _fail_simulation(
                    simulation_id, error_log, start_time,
                    status=SimulationStatus.BUILD_FAILED,
                )
                return

            await update_simulation(simulation_id, {
                "environment": environment.model_dump(mode="json"),
            })
            logger.info("Environment healthy: %s", environment.base_url)

        except Exception as e:
            error_msg = f"Step 4 (PROVISION) failed: {e}"
            logger.error(error_msg)
            error_log.append(error_msg)
            await _fail_simulation(
                simulation_id, error_log, start_time,
                status=SimulationStatus.BUILD_FAILED,
            )
            return

        # ════════════════════════════════════════════
        # STEP 5: REPLAY PRODUCTION TRAFFIC
        # ════════════════════════════════════════════
        logger.info("── Step 5: REPLAY ──")
        await update_simulation(simulation_id, {
            "status": SimulationStatus.REPLAYING.value,
        })

        try:
            from traffic_replay.replayer import replay_traffic

            if not production_requests:
                logger.warning("No production requests returned by Dynatrace — skipping replay")
                replay_result = ReplayResult(simulation_id=simulation_id)
            else:
                replay_result = await replay_traffic(
                    environment=environment,
                    requests=production_requests,
                    baseline=baseline,
                    concurrency=settings.replay_concurrency,
                )

                logger.info(
                    "Replay: %d/%d ok | p95=%.1fms (Δ%+.1fms) | errors=%.2f%% | novel_errors=%d",
                    replay_result.successful_requests,
                    replay_result.total_requests,
                    replay_result.actual_p95_ms,
                    replay_result.latency_delta_p95_ms,
                    replay_result.actual_error_rate_percent,
                    len(replay_result.novel_error_messages),
                )

        except Exception as e:
            error_msg = f"Step 5 (REPLAY) failed: {e}"
            logger.error(error_msg)
            error_log.append(error_msg)
            replay_result = ReplayResult(simulation_id=simulation_id)

        # ════════════════════════════════════════════
        # STEP 6: LLM EVALUATION [Arize MCP]
        # ════════════════════════════════════════════
        logger.info("── Step 6: LLM EVALUATION ──")

        if has_llm_components:
            await update_simulation(simulation_id, {
                "status": SimulationStatus.EVALUATING.value,
            })
            try:
                from orchestrator.tools.arize_tool import log_spans, run_eval

                llm_outputs = await _extract_llm_outputs_from_replay(environment)

                arize_result = await run_eval(
                    simulation_id=simulation_id,
                    simulation_outputs=llm_outputs,
                    baseline_dataset_id=arize_baseline.get("dataset_id"),
                )
                arize_result.llm_detected = True

                # Log spans (non-blocking)
                if llm_outputs:
                    await log_spans(simulation_id, llm_outputs)
                    arize_result.arize_span_logged = True

                logger.info(
                    "Arize eval: hallucination_Δ=%.3f, relevance_Δ=%.3f, groundedness_Δ=%.3f",
                    arize_result.hallucination_delta or 0,
                    arize_result.relevance_delta or 0,
                    arize_result.groundedness_delta or 0,
                )

            except Exception as e:
                error_msg = f"Step 6 (ARIZE EVAL) failed: {e}"
                logger.warning(error_msg)
                error_log.append(error_msg)
                arize_result = ArizeEvalResult(
                    llm_detected=True,
                    skip_reason=f"Arize API unavailable at simulation time: {e}",
                )
        else:
            arize_result = ArizeEvalResult(
                llm_detected=False,
                skip_reason="No LLM components detected via AST scan",
            )
            logger.info("Step 6: LLM evaluation skipped — no LLM components detected")

        # ════════════════════════════════════════════
        # STEP 7: TEARDOWN
        # ════════════════════════════════════════════
        logger.info("── Step 7: TEARDOWN ──")
        try:
            from environment_builder.builder import teardown_environment
            await teardown_environment(simulation_id)
        except Exception as e:
            error_msg = f"Step 7 (TEARDOWN) failed: {e}"
            logger.error(error_msg)
            error_log.append(error_msg)
            # Teardown failure is logged but does not stop the verdict

        # ════════════════════════════════════════════
        # STEP 8: GEMINI REASONING
        # ════════════════════════════════════════════
        logger.info("── Step 8: REASONING ──")
        await update_simulation(simulation_id, {
            "status": SimulationStatus.REASONING.value,
        })

        try:
            from orchestrator.reasoning import generate_verdict

            verdict = await generate_verdict(
                diff_summary=diff_summary,
                replay_result=replay_result,
                baseline=baseline,
                service_name=service_name,
                arize_result=arize_result if has_llm_components else None,
                historical_context=historical_context,
            )

            logger.info(
                "Verdict: %s (%d/100) — infra=%d, reliability=%d, llm=%s",
                verdict.verdict.value,
                verdict.overall_score,
                verdict.infrastructure_score,
                verdict.reliability_score,
                str(verdict.llm_quality_score),
            )

        except Exception as e:
            error_msg = f"Step 8 (REASONING) failed: {e}"
            logger.error(error_msg)
            error_log.append(error_msg)

            # Fallback to local scoring
            from orchestrator.reasoning import compute_scores_locally
            verdict = compute_scores_locally(replay_result, baseline, arize_result if has_llm_components else None)
            logger.info("Fallback verdict: %s (%d/100)", verdict.verdict.value, verdict.overall_score)

        # ════════════════════════════════════════════
        # STEP 9: ENFORCE [GitLab MCP]
        # ════════════════════════════════════════════
        logger.info("── Step 9: ENFORCE ──")
        await update_simulation(simulation_id, {
            "status": SimulationStatus.ENFORCING.value,
        })

        try:
            from verdict_writer.writer import enforce_verdict

            await enforce_verdict(
                project_id=message.project_id,
                mr_id=message.mr_id,
                head_sha=mr_details.head_sha if mr_details else message.head_sha,
                simulation_id=simulation_id,
                service_name=service_name,
                verdict=verdict,
                replay_result=replay_result,
                baseline=baseline,
                arize_result=arize_result,
            )

        except Exception as e:
            error_msg = f"Step 9 (ENFORCE) failed: {e}"
            logger.error(error_msg)
            error_log.append(error_msg)

        # ── Final update ──
        total_duration = int((time.time() - start_time) * 1000)
        await update_simulation(simulation_id, {
            "status": SimulationStatus.COMPLETED.value,
            "completed_at": datetime.now(timezone.utc),
            "total_duration_ms": total_duration,
            "dynatrace": DynatraceData(
                baseline=baseline,
                active_incident_at_start=has_incident,
                requests_sampled=len(production_requests),
                replay_result=replay_result,
            ).model_dump(mode="json"),
            "arize": arize_result.model_dump(mode="json"),
            "error_log": error_log,
        })

        logger.info(
            "═══ Simulation %s complete: %s (%d/100) in %dms ═══",
            simulation_id,
            verdict.verdict.value if verdict else "NO_VERDICT",
            verdict.overall_score if verdict else 0,
            total_duration,
        )

    except Exception as e:
        # Catch-all for unexpected errors
        logger.critical("Unexpected error in simulation %s: %s", simulation_id, e, exc_info=True)
        error_log.append(f"Critical error: {e}")
        await _fail_simulation(simulation_id, error_log, start_time)

        # Always attempt teardown even on unexpected errors
        try:
            from environment_builder.builder import teardown_environment
            await teardown_environment(simulation_id)
        except Exception:
            pass


async def _fail_simulation(
    simulation_id: str,
    error_log: list[str],
    start_time: float,
    status: SimulationStatus = SimulationStatus.ERROR,
) -> None:
    """Mark a simulation as failed with error details."""
    total_duration = int((time.time() - start_time) * 1000)
    try:
        await update_simulation(simulation_id, {
            "status": status.value,
            "completed_at": datetime.now(timezone.utc),
            "total_duration_ms": total_duration,
            "error_log": error_log,
        })
    except Exception as e:
        logger.error("Failed to update simulation as failed: %s", e)


async def _get_simulation(simulation_id: str) -> dict | None:
    """Fetch simulation document (for cancellation check)."""
    try:
        db = await get_database()
        return await db["simulations"].find_one({"_id": simulation_id})
    except Exception:
        return None


async def _extract_llm_outputs_from_replay(environment: Any | None) -> list[dict[str, Any]]:
    """
    Fetch LLM-specific outputs from the ephemeral service for Arize evaluation.

    The service must expose a real /traces endpoint populated during traffic
    replay. Supported response shapes are either a list of trace objects or an
    object containing "traces" or "spans". Each returned trace must contain at
    least "prompt" and "response"; "context" is optional.
    """
    if environment is None or not getattr(environment, "base_url", ""):
        logger.warning("LLM trace capture skipped: ephemeral environment URL is unavailable")
        return []

    traces_url = f"{environment.base_url.rstrip('/')}/traces"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.get(traces_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as e:
        logger.warning(
            "LLM trace capture skipped: /traces returned HTTP %s",
            e.response.status_code,
        )
        return []
    except Exception as e:
        logger.warning("LLM trace capture skipped: failed to fetch /traces: %s", e)
        return []

    raw_traces: Any
    if isinstance(payload, list):
        raw_traces = payload
    elif isinstance(payload, dict):
        raw_traces = payload.get("traces") or payload.get("spans") or []
    else:
        logger.warning("LLM trace capture skipped: /traces returned unsupported JSON shape")
        return []

    if not isinstance(raw_traces, list):
        logger.warning("LLM trace capture skipped: /traces trace collection is not a list")
        return []

    llm_outputs: list[dict[str, Any]] = []
    for trace in raw_traces:
        if not isinstance(trace, dict):
            continue

        prompt = trace.get("prompt")
        response_text = trace.get("response")
        if not isinstance(prompt, str) or not isinstance(response_text, str):
            continue

        normalized = dict(trace)
        normalized["prompt"] = prompt
        normalized["response"] = response_text
        normalized.setdefault("context", trace.get("context"))
        llm_outputs.append(normalized)

    logger.info("Captured %d LLM trace(s) from %s", len(llm_outputs), traces_url)
    return llm_outputs


# ──────────────────────────────────────────────
# Message consumer (development mode)
# ──────────────────────────────────────────────


async def consume_dev_queue() -> None:
    """
    Poll the MongoDB message queue for unprocessed messages (development mode).
    In production, Pub/Sub push subscription triggers the agent directly.
    """
    logger.info("Starting development queue consumer")

    while True:
        try:
            db = await get_database()
            doc = await db["message_queue"].find_one_and_update(
                {"processed": False},
                {"$set": {"processed": True, "processing_started_at": datetime.now(timezone.utc)}},
            )

            if doc:
                message = MREventMessage(**doc["message"])
                logger.info("Consuming message: %s", message.simulation_id)
                await run_simulation(message)
            else:
                await asyncio.sleep(5)

        except Exception as e:
            logger.error("Queue consumer error: %s", e)
            await asyncio.sleep(10)


# ──────────────────────────────────────────────
# Pub/Sub handler (production mode)
# ──────────────────────────────────────────────


async def handle_pubsub_message(message_data: bytes) -> None:
    """
    Handle a Pub/Sub message containing an MR event.
    Called by the Cloud Run Pub/Sub push handler.
    """
    try:
        data = json.loads(message_data.decode("utf-8"))
        event = MREventMessage(**data)
        await run_simulation(event)
    except Exception as e:
        logger.error("Failed to process Pub/Sub message: %s", e)
        raise


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if settings.bulwark_mode == "development":
        logger.info("Running in development mode — consuming from MongoDB queue")
        asyncio.run(consume_dev_queue())
    else:
        logger.info("Production mode — waiting for Pub/Sub messages")
        # In production, Cloud Run invokes handle_pubsub_message via HTTP push
