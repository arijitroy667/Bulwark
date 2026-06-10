"""
Bulwark — Centralized configuration via environment variables (v3.0).

All secrets and settings are loaded from environment variables (or .env file)
using Pydantic BaseSettings. In production, these are sourced from
Google Cloud Secret Manager.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class BulwarkSettings(BaseSettings):
    """Application-wide configuration. Values are read from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application Version ---
    bulwark_version: str = "3.0.0"

    # --- GitLab ---
    gitlab_url: str = "https://gitlab.com"
    gitlab_pat: str = ""  # GitLab Personal Access Token
    gitlab_webhook_token: str = ""  # Webhook signature validation token

    # --- Dynatrace ---
    dynatrace_env_id: str = ""
    dynatrace_api_token: str = ""

    # --- Arize (Phase 2) ---
    arize_api_key: str = ""
    arize_space_id: str = ""  # Arize space ID (not "space_key" — matches Arize MCP param name)

    # --- MongoDB ---
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "bulwark"

    # --- Google Cloud ---
    gcp_project_id: str = "bulwark-project"
    gcp_region: str = "us-central1"
    gcp_artifact_registry: str = "bulwark-images"

    # --- Gemini ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # --- Bulwark Settings ---
    bulwark_mode: str = "development"  # development | production
    bulwark_demo_mode: bool = False    # Run in demo mode (bypass real GCP/Dynatrace)
    simulation_timeout_seconds: int = 900  # 15 minutes hard ceiling (PRD §12.1)
    replay_concurrency: int = 10  # Default: 10× concurrent replay (FR-011)
    replay_request_limit: int = 200  # Max production request samples (FR-007)

    # --- v3 Scoring Thresholds (PRD §8) ---
    # Noise floors — signals within these bands are informational only
    p95_noise_floor_ms: float = 35.0  # ±35ms for 200-sample p95 (v1 had 20ms — WRONG)
    error_rate_noise_floor_percent: float = 1.0  # ±1.0% binomial noise at n=200
    llm_eval_noise_band: float = 0.10  # ±0.08–0.10 Arize evaluator noise

    # Verdict thresholds
    confidence_threshold_approved: int = 80  # ≥80 → APPROVED
    confidence_threshold_warning: int = 50   # 50–79 → WARNING; <50 → BLOCKED

    # ─────────────────────────────────────────────────────────
    # Phase 2: MCP Integration (PLAN.md §Phase 2)
    # ─────────────────────────────────────────────────────────
    # URLs of the remote MCP servers (Cloud Run-hosted).
    # Format: https://<service>-<hash>-<region>.a.run.app/sse  (SSE transport)
    gitlab_mcp_url: str = ""       # e.g. https://gitlab-mcp-xxx.run.app/sse
    dynatrace_mcp_url: str = ""    # e.g. https://dynatrace-mcp-xxx.run.app/sse
    arize_mcp_url: str = ""        # e.g. https://arize-mcp-xxx.run.app/sse

    # Shared bearer token sent as Authorization header for all MCP requests.
    # Leave empty if the MCP servers do not require authentication.
    mcp_auth_token: str = ""

    # ─────────────────────────────────────────────────────────
    # Phase 3: Real Environment Build (PLAN.md §Phase 3)
    # ─────────────────────────────────────────────────────────
    # GCS bucket used as Cloud Build StorageSource for GitLab archive uploads.
    gcp_source_archive_bucket: str = "bulwark-build-sources"

    @property
    def dynatrace_base_url(self) -> str:
        """Construct the Dynatrace API base URL from the environment ID."""
        return f"https://{self.dynatrace_env_id}.live.dynatrace.com/api/v2"

    @property
    def gitlab_api_url(self) -> str:
        """Construct the GitLab API base URL."""
        return f"{self.gitlab_url}/api/v4"

    @property
    def artifact_registry_host(self) -> str:
        """Construct the Artifact Registry Docker host."""
        return f"{self.gcp_region}-docker.pkg.dev"

    @property
    def artifact_registry_path(self) -> str:
        """Full Artifact Registry image path prefix."""
        return (
            f"{self.artifact_registry_host}/"
            f"{self.gcp_project_id}/"
            f"{self.gcp_artifact_registry}"
        )


# Singleton instance — import this across the application
settings = BulwarkSettings()
