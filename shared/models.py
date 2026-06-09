"""
Bulwark — Data models for all components (v3.0).

All Pydantic models used across the system: MR details, service baselines,
production requests, ephemeral environments, replay results, Arize eval
results, verdicts, and the top-level simulation record.

Key v3 changes:
- head_sha added to MREventMessage (idempotency + cancellation)
- ArizeEvalResult and ArizeData models added
- novel_error_classifications added to ReplayResult
- historical_note + uncertainty_acknowledgement added to VerdictResult
- SLO defaults corrected: 200ms / 1.0% (v1 had 20ms / 0.5% — below noise floor)
- EnvironmentStatus.BUILD_TIMEOUT and RUNTIME_CRASH added
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────


class SimulationStatus(str, Enum):
    """Lifecycle states for a simulation run."""

    QUEUED = "queued"
    INGESTING = "ingesting"
    FETCHING_BASELINE = "fetching_baseline"
    PROVISIONING = "provisioning"
    REPLAYING = "replaying"
    EVALUATING = "evaluating"
    REASONING = "reasoning"
    ENFORCING = "enforcing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BUILD_FAILED = "build_failed"
    BUILD_TIMEOUT = "build_timeout"
    HEALTH_CHECK_FAILED = "health_check_failed"
    RUNTIME_CRASH = "runtime_crash"
    TIMED_OUT = "timed_out"
    ERROR = "error"


class VerdictStatus(str, Enum):
    """The final verdict for a simulation."""

    APPROVED = "APPROVED"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"


class EnvironmentStatus(str, Enum):
    """Health states for an ephemeral environment."""

    HEALTHY = "healthy"
    BUILD_FAILED = "build_failed"
    BUILD_TIMEOUT = "build_timeout"
    HEALTH_CHECK_FAILED = "health_check_failed"
    RUNTIME_CRASH = "runtime_crash"


class NovelErrorClassification(str, Enum):
    """Gemini's classification of a novel error string."""

    CODE_REGRESSION = "code_regression"
    STUB_ARTEFACT = "stub_artefact"
    UNCERTAIN = "uncertain"


# ──────────────────────────────────────────────
# GitLab Models
# ──────────────────────────────────────────────


class FileDiff(BaseModel):
    """A single file change in a merge request diff."""

    old_path: str
    new_path: str
    diff: str
    new_file: bool = False
    renamed_file: bool = False
    deleted_file: bool = False


class MergeRequestDetails(BaseModel):
    """Full metadata for a GitLab merge request."""

    mr_id: int
    title: str
    description: str = ""
    source_branch: str
    target_branch: str
    author: str
    changed_files: list[str] = Field(default_factory=list)
    dockerfile_path: str | None = None
    ci_config_path: str = ".gitlab-ci.yml"
    has_llm_components: bool = False
    head_sha: str = ""


# ──────────────────────────────────────────────
# Dynatrace Models
# ──────────────────────────────────────────────


class ServiceBaseline(BaseModel):
    """Production performance baseline from Dynatrace.

    SLO defaults corrected in v3: p95 budget is 200ms (not 20ms which was
    below the ±35ms measurement noise floor for 200-sample p95 estimates).
    """

    service_name: str
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    error_rate_percent: float = 0.0
    throughput_rpm: float = 0.0
    # SLO targets — fetched from Dynatrace getSLOs; defaults per v3 §8
    slo_latency_budget_p95_ms: float = 200.0
    slo_error_budget_percent: float = 1.0
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProductionRequest(BaseModel):
    """A single anonymized production request for replay."""

    method: str  # GET, POST, PUT, DELETE, PATCH
    path: str  # e.g. /api/users/1001
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | str | None = None
    expected_status: int = 200
    expected_p95_ms: float = 0.0


class ServiceDependency(BaseModel):
    """A downstream dependency of a service (from Dynatrace topology)."""

    name: str
    base_url: str
    endpoints: list[str] = Field(default_factory=list)


class ServiceTopology(BaseModel):
    """The service's dependency graph from Dynatrace."""

    service_name: str
    dependencies: list[ServiceDependency] = Field(default_factory=list)


# ──────────────────────────────────────────────
# Arize Models (Phase 2)
# ──────────────────────────────────────────────


class ArizeEvalResult(BaseModel):
    """LLM quality evaluation results from Arize evaluators.

    Score deltas > 0.10 are above the evaluator noise band and are scored.
    Deltas <= 0.10 are informational only (per Arize documentation).
    """

    llm_detected: bool = False
    # Deltas vs production baseline (positive = regression)
    hallucination_delta: float | None = None
    relevance_delta: float | None = None
    groundedness_delta: float | None = None
    # Raw scores (simulation)
    sim_hallucination_score: float | None = None
    sim_relevance_score: float | None = None
    sim_groundedness_score: float | None = None
    # Raw scores (production baseline)
    baseline_hallucination_score: float | None = None
    baseline_relevance_score: float | None = None
    baseline_groundedness_score: float | None = None
    # Metadata
    eval_dataset_id: str | None = None
    baseline_dataset_id: str | None = None
    arize_span_logged: bool = False
    skip_reason: str | None = None  # e.g. "No LLM components detected via AST scan"
    eval_duration_ms: int = 0


# ──────────────────────────────────────────────
# Environment Builder Models
# ──────────────────────────────────────────────


class EphemeralEnvironment(BaseModel):
    """An ephemeral Cloud Run instance for simulation."""

    simulation_id: str
    base_url: str = ""  # https://bulwark-sim-<id>-<hash>-uc.a.run.app
    mock_base_url: str = ""  # WireMock instance URL
    build_duration_ms: int = 0
    deployed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: EnvironmentStatus = EnvironmentStatus.HEALTHY
    startup_log_tail: str = ""  # Last 20 lines of Cloud Run logs on failure


class WireMockStub(BaseModel):
    """A WireMock stub mapping for a dependency endpoint."""

    dependency_name: str
    request_method: str
    request_path: str
    response_status: int = 200
    response_body: dict[str, Any] | str = Field(default_factory=dict)
    response_headers: dict[str, str] = Field(
        default_factory=lambda: {"Content-Type": "application/json"}
    )


class EnvironmentConfig(BaseModel):
    """Per-service environment configuration (stored in MongoDB environment_configs)."""

    service_name: str
    project_id: str
    dockerfile_path: str = "Dockerfile"
    health_check_path: str = "/health"
    health_check_timeout_s: int = 90
    min_cpu: str = "1"
    min_memory: str = "512Mi"
    env_vars_to_mock: list[str] = Field(default_factory=list)
    llm_signature_paths: list[str] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# Traffic Replay Models
# ──────────────────────────────────────────────


class ReplayRequestResult(BaseModel):
    """Result from replaying a single production request."""

    method: str
    path: str
    expected_status: int
    actual_status: int
    response_time_ms: float
    response_body_hash: str = ""
    error_message: str | None = None
    status_match: bool = True


class ReplayResult(BaseModel):
    """Aggregated results from replaying all production requests."""

    simulation_id: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    actual_p50_ms: float = 0.0
    actual_p95_ms: float = 0.0
    actual_p99_ms: float = 0.0
    actual_error_rate_percent: float = 0.0
    latency_delta_p95_ms: float = 0.0
    error_rate_delta_percent: float = 0.0
    # Novel errors: string → classification by Gemini (or None if not yet classified)
    novel_error_messages: list[str] = Field(default_factory=list)
    novel_error_classifications: dict[str, str] = Field(default_factory=dict)
    response_shape_changes: list[str] = Field(default_factory=list)
    five_xx_count: int = 0
    replay_duration_ms: int = 0
    individual_results: list[ReplayRequestResult] = Field(default_factory=list)
    # Phase 4: set when replay is skipped due to insufficient real production samples
    skip_reason: str | None = None


# ──────────────────────────────────────────────
# Verdict Models
# ──────────────────────────────────────────────


class VerdictResult(BaseModel):
    """The structured verdict produced by Gemini reasoning (v3 schema)."""

    infrastructure_score: int = 0  # 0–100
    reliability_score: int = 0  # 0–100
    llm_quality_score: int | None = None  # 0–100, None if no LLM components
    overall_score: int = 0  # 0–100
    verdict: VerdictStatus = VerdictStatus.BLOCKED
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    # Novel error Gemini classification: error_string → {assessment, reasoning}
    novel_error_classification: dict[str, dict[str, str]] = Field(default_factory=dict)
    historical_note: str = ""  # Gemini's narration of historical context
    recommendation: str = ""
    # MANDATORY per PRD §8.7 — included verbatim in every MR comment
    uncertainty_acknowledgement: str = ""
    gemini_model: str = "gemini-2.0-flash"
    reasoning_duration_ms: int = 0


# ──────────────────────────────────────────────
# Top-Level Simulation Record (MongoDB document)
# ──────────────────────────────────────────────


class GitLabEnforcement(BaseModel):
    """Record of what was posted back to GitLab."""

    comment_id: int | None = None
    pipeline_status: str = ""  # success | failed
    override_applied: bool = False
    override_author: str | None = None
    override_timestamp: datetime | None = None


class DynatraceData(BaseModel):
    """All Dynatrace-sourced data for a simulation."""

    baseline: ServiceBaseline | None = None
    active_incident_at_start: bool = False
    requests_sampled: int = 0
    replay_result: ReplayResult | None = None


class SimulationRecord(BaseModel):
    """
    The top-level document stored in MongoDB's `simulations` collection.
    Contains all data for a single simulation run (90-day retention).
    """

    id: str = Field(..., alias="_id")  # sim_<uuid>
    mr_id: int
    project_id: str
    head_sha: str = ""  # Added v3: required for idempotency (FR-004) and cancellation (FR-023)
    source_branch: str
    target_branch: str
    service_name: str = ""
    status: SimulationStatus = SimulationStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    total_duration_ms: int = 0
    environment: EphemeralEnvironment | None = None
    dynatrace: DynatraceData = Field(default_factory=DynatraceData)
    arize: ArizeEvalResult = Field(default_factory=ArizeEvalResult)
    verdict: VerdictResult | None = None
    gitlab: GitLabEnforcement = Field(default_factory=GitLabEnforcement)
    error_log: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ──────────────────────────────────────────────
# Pub/Sub Message Schema
# ──────────────────────────────────────────────


class MREventMessage(BaseModel):
    """The Pub/Sub message published by the webhook receiver.

    v3: head_sha added for idempotency deduplication and in-progress
    simulation cancellation (FR-004, FR-023).
    """

    simulation_id: str
    mr_id: int
    project_id: str
    head_sha: str = ""  # NEW in v3
    source_branch: str
    target_branch: str
    mr_url: str = ""
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# LLM Detection Result
# ──────────────────────────────────────────────


class LLMDetectionResult(BaseModel):
    """Result of the two-stage LLM component detection scan (PRD §10)."""

    detected: bool = False
    detection_stage: str = ""  # "path_heuristic" | "ast_scan" | "none"
    matched_files: list[str] = Field(default_factory=list)
    matched_imports: list[str] = Field(default_factory=list)
    reason: str = ""
