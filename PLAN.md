# Phase-by-Phase Implementation Plan: Real Bulwark Flow

## Summary
Build the complete PRD v3 flow with real integrations only: GitLab webhook → Pub/Sub → orchestrator Cloud Run worker → MCP calls for GitLab/Dynatrace/Arize → GitLab archive Cloud Build → Cloud Run simulation + real WireMock service → replay real Dynatrace requests → `/traces` Arize eval → Gemini verdict → GitLab gate + Mongo persistence. Use the official MCP Python SDK for MCP client support: https://modelcontextprotocol.io/docs/sdk.

Assumptions locked:
- Use **actual MCP**, not direct REST wrappers, for GitLab/Dynatrace/Arize.
- Use **GitLab branch archive** as Cloud Build source, not Cloud Build repo connections.
- LLM trace capture follows PRD intent via a required real ephemeral service `/traces` endpoint.
- No fabricated vendor data or fake scoring. If real data is unavailable, mark evidence missing and skip that scoring path.

## Phase 1: Runtime, Deployment, and Queue Wiring
- Create separate runnable images/services:
  - `webhook-receiver`: FastAPI webhook app only.
  - `orchestrator-worker`: includes `orchestrator/`, `environment_builder/`, `traffic_replay/`, `verdict_writer/`, `shared/`.
- Update Terraform:
  - Add Cloud Run service for `orchestrator-worker`.
  - Convert Pub/Sub subscription to push delivery to an authenticated orchestrator endpoint, or add a real pull-loop worker service. Choose push for PRD alignment.
  - Add all required secrets/env vars: MCP endpoints, Mongo DB name, Gemini key/model, GitLab token, GCP config, Arize identifiers, Dynatrace identifiers.
- Add orchestrator HTTP endpoint:
  - `POST /pubsub/orchestrate`
  - Decode Pub/Sub envelope, call `handle_pubsub_message()`, return 2xx only after durable processing decision.
- Fix webhook behavior:
  - Production always publishes to Pub/Sub.
  - Development may keep Mongo queue only for local testing, clearly outside production path.
  - Add `bulwark-override` label event handling: log override, post acknowledgement, set pipeline status success.
- Add 15-minute hard timeout around `run_simulation()` with guaranteed teardown.

## Phase 2: Actual MCP Integration Layer
- Add a shared MCP client adapter:
  - Config: `GITLAB_MCP_URL`, `DYNATRACE_MCP_URL`, `ARIZE_MCP_URL`, optional auth headers/tokens.
  - Use official `mcp` Python SDK.
  - Provide one typed `call_tool(server, tool_name, payload)` wrapper with timeout, structured logging, and explicit config errors.
- Replace direct REST tool implementations with MCP-backed wrappers:
  - GitLab tools: `getMergeRequest`, `getRepositoryFiles`, `getMergeRequestDiff`, `createMergeRequestNote`, `createPipelineStatus`, approval/update operations if available.
  - Dynatrace tools: `hasActiveIncident`, `getServiceMetrics`, `getLogs`, `getServiceTopology`, `getSLOs`.
  - Arize tools: `getDataset`/baseline, `runEval`, `compareDatasets`, `logSpans`.
- Keep current function names as internal Python interfaces so `agent.py` changes minimally.
- No production REST fallback. Tests can inject fake MCP responses.

## Phase 3: Real Environment Build and WireMock
- Implement GitLab archive source build:
  - Fetch branch archive for `source_branch` using GitLab MCP/file API or GitLab archive URL through MCP-supported operation.
  - Upload archive to a new GCS staging bucket.
  - Cloud Build uses `StorageSource` from that bucket.
  - Build image with MR Dockerfile path and push to Artifact Registry.
- Implement real WireMock deployment:
  - Generate mappings from Dynatrace topology.
  - Build/package a WireMock container or use official WireMock image.
  - Deploy `bulwark-mock-<simulation_id>` to Cloud Run.
  - Upload mappings before replay, either baked into image or posted to `__admin/mappings` after deploy.
- Deploy simulation Cloud Run service:
  - Inject `BULWARK_MODE=simulation`, `SIMULATION_ID`, `WIREMOCK_BASE_URL`.
  - Replace configured production credentials with mock values from `environment_configs.env_vars_to_mock`.
  - Do not pass production secrets into simulation.
  - Enforce max instances = 1 and 1 vCPU/512Mi default.
- Health check:
  - Use service-specific `health_check_path` from `environment_configs`; default `/health`.
  - On failure, collect Cloud Run startup log tail and post BUILD_FAILED comment.
- Teardown:
  - Delete simulation service, WireMock service, GCS source object, and Artifact Registry package.
  - Run teardown on success, failure, cancellation, and timeout.

## Phase 4: Real Replay, Trace, and Scoring Flow
- Dynatrace request samples:
  - Require 50-200 real production samples for full replay scoring.
  - If fewer than 50, skip replay scoring and mark “insufficient real production samples”; do not fabricate probes.
  - Parse method/path/status/headers/body from real Dynatrace log/span fields, not a single assumed text format.
  - Anonymize before replay, Mongo persistence, or Arize transmission.
- Replay:
  - Use only real Dynatrace-derived requests.
  - Persist individual results and aggregate metrics.
  - Detect novel errors and response shape changes.
- LLM trace capture:
  - Require LLM services to expose `GET /traces`.
  - Accepted response: `{"traces":[{"prompt": str, "response": str, "context": object|null, ...}]}`.
  - If no valid traces, Arize eval is skipped with explicit missing evidence.
- Arize:
  - Submit captured simulation traces through Arize MCP `logSpans`.
  - Run hallucination/relevance/groundedness eval through MCP.
  - Compare against production baseline dataset.
  - Store raw scores, deltas, dataset IDs, and skip reasons.

## Phase 5: Control Flow, Persistence, and Enforcement
- Cancellation:
  - Check cancelled status before every major step: baseline, provision, replay, Arize, reasoning, enforcement.
  - If cancelled after environment creation, teardown immediately and stop.
- Error handling:
  - Add bounded retries for MCP calls and Mongo writes.
  - Step failures must append structured `error_log` entries and continue only when PRD allows partial evidence.
- Mongo:
  - Add TTL index for 90-day simulation retention.
  - Store complete evidence: MR metadata, baseline, request sample count, replay result, Arize result, verdict, GitLab enforcement, override events.
- Gemini:
  - Keep deterministic pre-score before Gemini.
  - Gemini classifies novel errors and produces narrative only from supplied evidence.
  - On Gemini failure, fall back to deterministic verdict and state that reasoning was unavailable.
- GitLab enforcement:
  - Post comment on every completed simulation.
  - Set `Bulwark/production-ready` status: success for APPROVED/WARNING, failed for BLOCKED.
  - Override label sets success and logs override.

## Phase 6: Tests and Acceptance
- Unit tests:
  - MCP adapter: success, timeout, auth/config missing, malformed tool result.
  - GitLab/Dynatrace/Arize wrappers with fake MCP server responses.
  - GitLab archive build source construction.
  - WireMock mapping generation and deployment calls.
  - Timeout/cancellation teardown.
  - Override label handling.
  - `/traces` validation.
- Integration tests with mocked external services:
  - Webhook enqueues Pub/Sub message.
  - Pub/Sub endpoint runs orchestrator.
  - Orchestrator completes non-LLM MR and posts GitLab gate.
  - LLM MR captures `/traces`, calls Arize MCP, and includes LLM score.
  - Build failure posts BUILD_FAILED with logs.
- Real smoke test acceptance:
  - GitLab MR webhook reaches Cloud Run.
  - Pub/Sub message triggers orchestrator.
  - Cloud Build builds GitLab archive branch.
  - Simulation and WireMock Cloud Run services appear and are deleted.
  - 50-200 real Dynatrace requests replay.
  - GitLab MR receives verdict and commit status.
  - Mongo simulation record contains full evidence.
  - LLM demo MR produces Arize eval section from real `/traces`.

## Public Interface Changes
- New env vars:
  - `GITLAB_MCP_URL`, `DYNATRACE_MCP_URL`, `ARIZE_MCP_URL`
  - `MCP_AUTH_TOKEN` or per-server auth vars if needed
  - `GCP_SOURCE_ARCHIVE_BUCKET`
  - `MONGODB_DB_NAME`
- New endpoint:
  - `POST /pubsub/orchestrate` on orchestrator worker.
- Required LLM service contract:
  - `GET /traces` returns `{"traces":[{"prompt": "...", "response": "...", "context": ...}]}`.
- `EnvironmentConfig` becomes operational:
  - `health_check_path`
  - `env_vars_to_mock`
  - resource defaults
  - optional service-specific mock env values.
