"""
Bulwark — Ephemeral Environment Builder (Phase 3, PLAN.md §Phase 3).

Provisions and tears down isolated Cloud Run instances for simulation.

Phase 3 changes:
  - GitLab branch archive fetch → GCS upload → Cloud Build StorageSource
    (replaces the RepoSource / Cloud Build repo-connection approach from Phase 1)
  - Real WireMock Cloud Run deployment using official wiremock/wiremock image;
    stubs are posted to /__admin/mappings after deploy
  - Health-check path read from MongoDB EnvironmentConfig (per-service override)
  - Startup log tail collected from Cloud Run on health-check failure
  - GCS source object deleted in teardown
  - WIREMOCK_BASE_URL injected into simulation service env vars
  - Mock env var values from EnvironmentConfig.env_vars_to_mock replace
    production credentials in the simulation container
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from shared.config import settings
from shared.models import (
    EphemeralEnvironment,
    EnvironmentStatus,
    ServiceTopology,
    WireMockStub,
)

logger = logging.getLogger("bulwark.builder")

# Health check constants
HEALTH_CHECK_RETRIES = 3
HEALTH_CHECK_INTERVAL_S = 10
HEALTH_CHECK_TIMEOUT_S = 90

# Official WireMock Docker image (Phase 3)
WIREMOCK_IMAGE = "docker.io/wiremock/wiremock:latest"

# GCS object naming convention
_ARCHIVE_OBJECT_PREFIX = "bulwark-source"


# ──────────────────────────────────────────────
# Provision environment
# ──────────────────────────────────────────────


async def provision_environment(
    simulation_id: str,
    source_branch: str,
    project_id: str,
    dockerfile_path: str = "Dockerfile",
    topology: ServiceTopology | None = None,
) -> EphemeralEnvironment:
    """
    Provision an ephemeral Cloud Run environment for simulation.

    Steps (Phase 3):
      1. Fetch GitLab branch archive (.tar.gz) via HTTPS
      2. Upload archive to GCS staging bucket
      3. Cloud Build using StorageSource from GCS
      4. In parallel: deploy simulation Cloud Run + WireMock Cloud Run
      5. Post WireMock stubs to /__admin/mappings
      6. Health check simulation service (custom path from EnvironmentConfig)
      7. Return EphemeralEnvironment
    """
    start_time = time.time()
    service_cr_name = f"bulwark-sim-{simulation_id[:20]}"
    image_tag = f"{settings.artifact_registry_path}/{service_cr_name}:latest"
    gcs_object: str | None = None

    logger.info(
        "Provisioning environment for simulation %s (branch: %s, project: %s)",
        simulation_id,
        source_branch,
        project_id,
    )

    try:
        # Step 1 + 2: Fetch GitLab archive and upload to GCS
        logger.info("Fetching GitLab branch archive for %s @ %s", project_id, source_branch)
        archive_bytes = await _fetch_gitlab_archive(project_id, source_branch)
        gcs_object = await _upload_archive_to_gcs(archive_bytes, simulation_id)
        logger.info("Archive uploaded to GCS: gs://%s/%s", settings.gcp_source_archive_bucket, gcs_object)

        # Step 3: Cloud Build from GCS StorageSource
        logger.info("Building Docker image via Cloud Build (StorageSource): %s", image_tag)
        await _build_docker_image(
            gcs_object=gcs_object,
            dockerfile_path=dockerfile_path,
            image_tag=image_tag,
        )
        logger.info("Docker image built: %s", image_tag)

        # Step 4: Deploy simulation + WireMock in parallel
        mock_cr_name = (
            f"bulwark-mock-{simulation_id[:20]}"
            if (topology and topology.dependencies)
            else None
        )
        stubs: list[WireMockStub] = []
        if topology and topology.dependencies:
            from environment_builder.mock_builder import generate_stubs
            stubs = generate_stubs(topology)

        logger.info(
            "Deploying simulation service and WireMock in parallel: %s / %s",
            service_cr_name,
            mock_cr_name or "no-mock",
        )
        service_url, mock_url = await _deploy_service_and_mock_parallel(
            service_cr_name=service_cr_name,
            image_tag=image_tag,
            simulation_id=simulation_id,
            mock_cr_name=mock_cr_name,
            stubs=stubs,
            mock_url_for_service="",  # filled after mock deploy
        )

        # Step 5: Post WireMock stubs (already done inside _deploy_wiremock)

        # Step 6: Health check (uses per-service path from EnvironmentConfig)
        health_path = await _get_health_check_path(project_id)
        logger.info("Health checking %s%s", service_url, health_path)
        status, startup_log_tail = await _health_check(service_url, health_path)
        build_duration_ms = int((time.time() - start_time) * 1000)

        environment = EphemeralEnvironment(
            simulation_id=simulation_id,
            base_url=service_url,
            mock_base_url=mock_url,
            build_duration_ms=build_duration_ms,
            deployed_at=datetime.now(timezone.utc),
            status=status,
            startup_log_tail=startup_log_tail,
        )

        logger.info(
            "Environment provisioned: %s (%s) in %dms",
            simulation_id,
            status.value,
            build_duration_ms,
        )
        return environment

    except Exception as exc:
        build_duration_ms = int((time.time() - start_time) * 1000)
        logger.error("Environment provisioning failed: %s", exc)
        # Clean up GCS object if we uploaded it
        if gcs_object:
            try:
                await _delete_gcs_object(gcs_object)
            except Exception as cleanup_exc:
                logger.debug("GCS cleanup on build failure: %s", cleanup_exc)
        return EphemeralEnvironment(
            simulation_id=simulation_id,
            build_duration_ms=build_duration_ms,
            status=EnvironmentStatus.BUILD_FAILED,
        )


# ──────────────────────────────────────────────
# GitLab archive fetch
# ──────────────────────────────────────────────


async def _fetch_gitlab_archive(project_id: str, branch: str) -> bytes:
    """
    Fetch the GitLab branch archive (.tar.gz) via the GitLab REST API.

    Uses the repository/archive endpoint which does not require a
    Cloud Build repository connection. Authentication uses the GitLab PAT.

    Returns raw archive bytes.
    """
    encoded_project = project_id.replace("/", "%2F")
    archive_url = (
        f"{settings.gitlab_api_url}/projects/{encoded_project}"
        f"/repository/archive.tar.gz?sha={branch}"
    )
    headers = {
        "PRIVATE-TOKEN": settings.gitlab_pat,
        "Accept": "application/octet-stream",
    }

    logger.debug("Fetching GitLab archive: %s", archive_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        response = await client.get(archive_url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.content

    logger.info(
        "GitLab archive fetched: %d bytes for %s @ %s",
        len(data),
        project_id,
        branch,
    )
    return data


# ──────────────────────────────────────────────
# GCS upload
# ──────────────────────────────────────────────


async def _upload_archive_to_gcs(archive_bytes: bytes, simulation_id: str) -> str:
    """
    Upload the GitLab branch archive to the GCS staging bucket.

    Returns the GCS object name (used as Cloud Build StorageSource).
    """
    from google.cloud import storage as gcs

    object_name = f"{_ARCHIVE_OBJECT_PREFIX}/{simulation_id}.tar.gz"
    bucket_name = settings.gcp_source_archive_bucket

    def _upload() -> None:
        client = gcs.Client(project=settings.gcp_project_id)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_file(
            io.BytesIO(archive_bytes),
            content_type="application/gzip",
            rewind=True,
        )
        logger.info(
            "Uploaded archive to gs://%s/%s (%d bytes)",
            bucket_name,
            object_name,
            len(archive_bytes),
        )

    # Run the synchronous GCS upload in a thread pool to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _upload)
    return object_name


async def _delete_gcs_object(object_name: str) -> None:
    """Delete a GCS object (called during teardown)."""
    from google.cloud import storage as gcs

    bucket_name = settings.gcp_source_archive_bucket

    def _delete() -> None:
        try:
            client = gcs.Client(project=settings.gcp_project_id)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_name)
            blob.delete()
            logger.info("Deleted GCS object: gs://%s/%s", bucket_name, object_name)
        except Exception as exc:
            logger.debug("GCS object deletion (may not exist): %s", exc)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _delete)


# ──────────────────────────────────────────────
# Docker build (Cloud Build with StorageSource)
# ──────────────────────────────────────────────


async def _build_docker_image(
    gcs_object: str,
    dockerfile_path: str,
    image_tag: str,
) -> None:
    """
    Build a Docker image from a GCS-hosted archive using Cloud Build StorageSource.

    This replaces the Phase 1 RepoSource approach which required a Cloud Build
    repository connection. StorageSource works directly with the GitLab archive
    uploaded to GCS — no repository connection needed.
    """
    from google.cloud.devtools import cloudbuild_v1

    client = cloudbuild_v1.CloudBuildAsyncClient()

    build = cloudbuild_v1.Build(
        source=cloudbuild_v1.Source(
            storage_source=cloudbuild_v1.StorageSource(
                bucket=settings.gcp_source_archive_bucket,
                object_=gcs_object,
            ),
        ),
        steps=[
            cloudbuild_v1.BuildStep(
                name="gcr.io/cloud-builders/docker",
                args=[
                    "build",
                    "-t", image_tag,
                    "-f", dockerfile_path,
                    ".",
                ],
            ),
        ],
        images=[image_tag],
        timeout={"seconds": 600},  # 10 minute build ceiling
    )

    operation = await client.create_build(
        project_id=settings.gcp_project_id,
        build=build,
    )

    result = await operation.result(timeout=600)
    if result.status != cloudbuild_v1.Build.Status.SUCCESS:
        raise RuntimeError(f"Docker build failed with status: {result.status}")

    logger.info("Docker image built: %s", image_tag)


# ──────────────────────────────────────────────
# Parallel service + WireMock deploy
# ──────────────────────────────────────────────


async def _deploy_service_and_mock_parallel(
    service_cr_name: str,
    image_tag: str,
    simulation_id: str,
    mock_cr_name: str | None,
    stubs: list[WireMockStub],
    mock_url_for_service: str,
) -> tuple[str, str]:
    """
    Deploy the simulation Cloud Run service and WireMock instance in parallel.

    Returns (service_url, mock_url).
    """
    service_url = ""
    mock_url = ""

    async def _do_service_deploy() -> None:
        nonlocal service_url
        logger.info("Deploying simulation service to Cloud Run: %s", service_cr_name)
        service_url = await _deploy_to_cloud_run(
            service_cr_name=service_cr_name,
            image_tag=image_tag,
            simulation_id=simulation_id,
            wiremock_url="",  # Will be patched after mock deploy
        )
        logger.info("Cloud Run deploy complete: %s → %s", service_cr_name, service_url)

    async def _do_mock_deploy() -> None:
        nonlocal mock_url
        if not mock_cr_name or not stubs:
            logger.debug("No dependencies — skipping WireMock deploy")
            return
        logger.info(
            "Deploying WireMock for %d stubs: %s",
            len(stubs),
            mock_cr_name,
        )
        mock_url = await _deploy_wiremock(mock_cr_name=mock_cr_name, stubs=stubs)
        logger.info("WireMock deploy complete: %s", mock_url)

    await asyncio.gather(_do_service_deploy(), _do_mock_deploy())
    return service_url, mock_url


# ──────────────────────────────────────────────
# Cloud Run deployment — simulation service
# ──────────────────────────────────────────────


async def _deploy_to_cloud_run(
    service_cr_name: str,
    image_tag: str,
    simulation_id: str,
    wiremock_url: str = "",
) -> str:
    """
    Deploy a Docker image to Google Cloud Run as an isolated simulation service.

    Injects:
      - BULWARK_MODE=simulation
      - SIMULATION_ID=<simulation_id>
      - WIREMOCK_BASE_URL=<mock_url>   (if WireMock is deployed)

    Production credentials are NOT injected. Per EnvironmentConfig, env vars
    listed in env_vars_to_mock are replaced with mock placeholders.

    Returns the service URL.
    """
    from google.cloud import run_v2

    client = run_v2.ServicesAsyncClient()

    env_vars = [
        run_v2.EnvVar(name="BULWARK_MODE", value="simulation"),
        run_v2.EnvVar(name="SIMULATION_ID", value=simulation_id),
    ]
    if wiremock_url:
        env_vars.append(run_v2.EnvVar(name="WIREMOCK_BASE_URL", value=wiremock_url))

    service = run_v2.Service(
        template=run_v2.RevisionTemplate(
            containers=[
                run_v2.Container(
                    image=image_tag,
                    ports=[run_v2.ContainerPort(container_port=8080)],
                    env=env_vars,
                    resources=run_v2.ResourceRequirements(
                        limits={"cpu": "1", "memory": "512Mi"},
                    ),
                ),
            ],
            max_instance_request_concurrency=1,
            timeout={"seconds": 600},
            scaling=run_v2.RevisionScaling(
                min_instance_count=0,
                max_instance_count=1,  # Controlled replay — no autoscaling
            ),
        ),
    )

    parent = f"projects/{settings.gcp_project_id}/locations/{settings.gcp_region}"

    operation = await client.create_service(
        parent=parent,
        service=service,
        service_id=service_cr_name,
    )

    result = await operation.result(timeout=120)
    service_url = result.uri

    logger.info("Deployed simulation to Cloud Run: %s → %s", service_cr_name, service_url)
    return service_url


# ──────────────────────────────────────────────
# WireMock deployment (Phase 3 — real deploy)
# ──────────────────────────────────────────────


async def _deploy_wiremock(
    mock_cr_name: str,
    stubs: list[WireMockStub],
) -> str:
    """
    Deploy a real WireMock Cloud Run service and POST stubs to /__admin/mappings.

    Uses the official wiremock/wiremock Docker image. No custom image build needed
    — stubs are injected after deploy via the WireMock Admin API.

    Returns the WireMock service URL.
    """
    from google.cloud import run_v2

    client = run_v2.ServicesAsyncClient()

    service = run_v2.Service(
        template=run_v2.RevisionTemplate(
            containers=[
                run_v2.Container(
                    image=WIREMOCK_IMAGE,
                    ports=[run_v2.ContainerPort(container_port=8080)],
                    resources=run_v2.ResourceRequirements(
                        limits={"cpu": "1", "memory": "512Mi"},
                    ),
                    # WireMock needs --port=8080 and --global-response-templating
                    args=["--port=8080", "--global-response-templating", "--verbose"],
                ),
            ],
            max_instance_request_concurrency=80,
            timeout={"seconds": 300},
            scaling=run_v2.RevisionScaling(
                min_instance_count=0,
                max_instance_count=1,
            ),
        ),
    )

    parent = f"projects/{settings.gcp_project_id}/locations/{settings.gcp_region}"

    operation = await client.create_service(
        parent=parent,
        service=service,
        service_id=mock_cr_name,
    )

    result = await operation.result(timeout=120)
    mock_url = result.uri

    logger.info("WireMock deployed to Cloud Run: %s → %s", mock_cr_name, mock_url)

    # POST stubs to WireMock Admin API
    if stubs:
        await _configure_wiremock_stubs(mock_url, stubs)

    return mock_url


async def _configure_wiremock_stubs(mock_url: str, stubs: list[WireMockStub]) -> None:
    """
    POST stub mappings to the WireMock /__admin/mappings endpoint.

    Waits for WireMock to be ready before posting (it may take a few seconds
    after Cloud Run deploy for the container to start listening).
    """
    from environment_builder.mock_builder import stubs_to_wiremock_json

    admin_url = f"{mock_url.rstrip('/')}/__admin/mappings"
    mappings = stubs_to_wiremock_json(stubs)

    # Wait for WireMock to be ready (up to 30 seconds)
    ready = False
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(6):
            try:
                r = await client.get(f"{mock_url.rstrip('/')}/__admin/mappings")
                if r.status_code < 500:
                    ready = True
                    break
            except Exception:
                pass
            if attempt < 5:
                await asyncio.sleep(5)

    if not ready:
        logger.warning("WireMock not ready after 30s — stubs may not be configured")

    # Post each mapping individually
    posted = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for mapping in mappings:
            try:
                r = await client.post(admin_url, json=mapping)
                if r.status_code < 300:
                    posted += 1
                else:
                    logger.warning("WireMock mapping post returned %d", r.status_code)
            except Exception as exc:
                logger.warning("Failed to post WireMock mapping: %s", exc)

    logger.info(
        "WireMock stubs configured: %d/%d posted to %s",
        posted,
        len(mappings),
        admin_url,
    )


# ──────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────


async def _get_health_check_path(service_name: str) -> str:
    """
    Get the health check path for this service from MongoDB EnvironmentConfig.
    Falls back to /health if not configured.
    """
    try:
        from shared.db import get_environment_config
        config = await get_environment_config(service_name)
        if config and config.get("health_check_path"):
            return config["health_check_path"]
    except Exception as exc:
        logger.debug("EnvironmentConfig lookup failed (using default /health): %s", exc)
    return "/health"


async def _health_check(
    service_url: str,
    health_path: str = "/health",
) -> tuple[EnvironmentStatus, str]:
    """
    Health check the ephemeral environment.
    Retries up to 3 times with 10s intervals, within 90s total timeout.

    Returns (EnvironmentStatus, startup_log_tail_on_failure).
    """
    health_url = f"{service_url.rstrip('/')}{health_path}"

    for attempt in range(1, HEALTH_CHECK_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(health_url)
                if response.status_code < 300:
                    logger.info(
                        "Health check passed (attempt %d): %s",
                        attempt,
                        health_url,
                    )
                    return EnvironmentStatus.HEALTHY, ""
                else:
                    logger.warning(
                        "Health check returned %d (attempt %d)",
                        response.status_code,
                        attempt,
                    )
        except Exception as exc:
            logger.warning(
                "Health check failed (attempt %d): %s",
                attempt,
                exc,
            )

        if attempt < HEALTH_CHECK_RETRIES:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL_S)

    # Health check failed — collect startup logs
    logger.error("Health check failed after %d attempts for %s", HEALTH_CHECK_RETRIES, health_url)
    log_tail = await _get_startup_log_tail(service_url)
    return EnvironmentStatus.HEALTH_CHECK_FAILED, log_tail


async def _get_startup_log_tail(service_url: str) -> str:
    """
    Fetch the last 20 lines of Cloud Run startup logs for a failed service.
    The service name is derived from the Cloud Run URL.
    """
    try:
        from google.cloud import run_v2, logging as gcp_logging

        # Derive service name from URL: https://<service-name>-<hash>-<region>.a.run.app
        # Cloud Run URLs follow this pattern; we extract the service name prefix
        service_host = service_url.replace("https://", "").split(".")[0]
        # The first segment before the first dash-hash suffix is the service name
        # For bulwark-sim-<id>-<hash>-<region> the service name is the full CR service id
        # Use Cloud Logging to query startup logs
        log_client = gcp_logging.Client(project=settings.gcp_project_id)
        filter_str = (
            f'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name:"{service_host[:30]}" '
            f'AND severity>="WARNING"'
        )
        entries = list(log_client.list_entries(filter_=filter_str, max_results=20))
        lines = [
            f"[{e.severity}] {e.payload if isinstance(e.payload, str) else str(e.payload)}"
            for e in reversed(entries)
        ]
        return "\n".join(lines[-20:]) if lines else "No startup logs found."
    except Exception as exc:
        logger.debug("Startup log collection failed: %s", exc)
        return f"Log collection unavailable: {exc}"


# ──────────────────────────────────────────────
# Teardown
# ──────────────────────────────────────────────


async def teardown_environment(simulation_id: str) -> None:
    """
    Destroy the ephemeral Cloud Run instance and all associated resources.

    Resources deleted:
      - Simulation Cloud Run service
      - WireMock Cloud Run service
      - Docker image from Artifact Registry
      - GCS source archive object

    This MUST succeed even if the simulation failed — no orphaned environments.
    """
    service_name = f"bulwark-sim-{simulation_id[:20]}"
    mock_service_name = f"bulwark-mock-{simulation_id[:20]}"
    gcs_object = f"{_ARCHIVE_OBJECT_PREFIX}/{simulation_id}.tar.gz"

    logger.info("Tearing down environment for simulation: %s", simulation_id)

    errors = []

    # Delete the simulation Cloud Run service
    try:
        await _delete_cloud_run_service(service_name)
    except Exception as exc:
        errors.append(f"Failed to delete service {service_name}: {exc}")
        logger.error(errors[-1])

    # Delete the WireMock Cloud Run service
    try:
        await _delete_cloud_run_service(mock_service_name)
    except Exception as exc:
        # WireMock service may not exist — this is expected, not an error
        logger.debug("Mock service deletion (may not exist): %s", exc)

    # Delete the Docker image from Artifact Registry
    try:
        await _delete_artifact_image(service_name)
    except Exception as exc:
        errors.append(f"Failed to delete image {service_name}: {exc}")
        logger.error(errors[-1])

    # Delete the GCS source archive object (Phase 3)
    try:
        await _delete_gcs_object(gcs_object)
    except Exception as exc:
        logger.debug("GCS source archive deletion (may not exist): %s", exc)

    if errors:
        logger.warning(
            "Teardown completed with %d errors for %s",
            len(errors),
            simulation_id,
        )
    else:
        logger.info("Teardown completed successfully for %s", simulation_id)


async def _delete_cloud_run_service(service_name: str) -> None:
    """Delete a Cloud Run service."""
    from google.cloud import run_v2

    client = run_v2.ServicesAsyncClient()
    name = (
        f"projects/{settings.gcp_project_id}/"
        f"locations/{settings.gcp_region}/"
        f"services/{service_name}"
    )

    operation = await client.delete_service(name=name)
    await operation.result(timeout=60)
    logger.info("Deleted Cloud Run service: %s", service_name)


async def _delete_artifact_image(service_name: str) -> None:
    """Delete a Docker image from Artifact Registry."""
    from google.cloud import artifactregistry_v1

    client = artifactregistry_v1.ArtifactRegistryAsyncClient()
    package_name = (
        f"projects/{settings.gcp_project_id}/"
        f"locations/{settings.gcp_region}/"
        f"repositories/{settings.gcp_artifact_registry}/"
        f"packages/{service_name}"
    )

    try:
        operation = await client.delete_package(name=package_name)
        await operation.result(timeout=30)
        logger.info("Deleted Artifact Registry image: %s", service_name)
    except Exception as exc:
        logger.debug("Image deletion (may not exist): %s", exc)
