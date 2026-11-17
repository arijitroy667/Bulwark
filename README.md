# Bulwark

**Catch production regressions before they merge.**

[![GitLab](https://img.shields.io/badge/GitLab-webhooks%20%2B%20REST-FC6D26?logo=gitlab&logoColor=white)](https://gitlab.com)
[![Dynatrace](https://img.shields.io/badge/Dynatrace-MCP-1496FF?logo=dynatrace&logoColor=white)](https://dynatrace.com)
[![Arize](https://img.shields.io/badge/Arize-Phoenix%20MCP-7C3AED)](https://arize.com)
[![Gemini](https://img.shields.io/badge/Gemini-reasoning%20%2B%20LLM%20judge-4285F4?logo=google&logoColor=white)](https://ai.google.dev)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

Bulwark is a pre-merge gate for GitLab. For every merge request it builds the change
into an isolated environment, replays real (anonymised) production traffic against it,
evaluates any LLM output it produces, and posts one verdict — **approved**, **warning**
or **blocked** — on the merge request, together with a pass/fail pipeline status.

---

## The problem

- **Tests never see real traffic.** An N+1 query passes every unit test and still
  multiplies latency once production traffic arrives.
- **LLM regressions are silent.** A prompt or model change can make answers worse while
  latency and error rates stay flat.
- **No single gate before merge.** Performance and LLM quality are watched by different
  tools, after deploy. Nothing turns both into one decision while the change is still an MR.

Traffic replay and LLM evaluation each exist on their own. Bulwark combines them into a
single, statistically grounded score and enforces it **before** merge.

## How it works

```text
GitLab MR ──webhook──► Webhook receiver ──Pub/Sub──► Orchestrator
                       (verify, dedupe,               │
                        cancel stale runs)            ├─ 1 Ingest        MR + diff (GitLab REST), two-stage LLM detection
                                                      ├─ 2 Baseline      Dynatrace (MCP): latency, errors, SLOs, 200 request logs
                                                      ├─ 3 LLM baseline  Arize (Phoenix MCP), only for LLM changes  ─┐ parallel
                                                      ├─ 3½ History      last 5 verdicts for the service (MongoDB)
                                                      ├─ 4 Provision     Cloud Build → single-instance Cloud Run + WireMock stubs
                                                      ├─ 5 Replay        anonymised requests at 10× concurrency (warm-up discarded)
                                                      ├─ 6 Evaluate      /traces → hallucination, relevance, groundedness
                                                      ├─ 7 Teardown      services, image and source archive deleted — always
                                                      ├─ 8 Reason        deterministic scores, then Gemini classifies and explains
                                                      └─ 9 Enforce       MR comment + Bulwark/production-ready commit status
```

- The orchestrator is plain Python (FastAPI worker). **Scores are deterministic.** Gemini
  classifies novel errors (code regression / stub artefact / uncertain), writes the
  summary and recommendation, and acts as the judge for LLM evaluation — it never sets a
  score. Without Gemini the verdict is computed from thresholds alone.
- A new push to the MR cancels the run in progress; a duplicate delivery of the same
  commit is ignored. Runs are capped at 15 minutes, with forced teardown.
- Adding the `bulwark-override` label to a blocked MR sets the status to success and logs
  who overrode it; later simulations see the override in their history.

### Scoring

| | |
|---|---|
| Noise floors | p95 latency ±35 ms · error rate ±1.0 % · LLM evaluators ±0.10 — deltas inside them are reported, not scored |
| Sub-scores | infrastructure (latency, error rate, 5xx) · reliability (classified novel errors) · LLM quality (when LLM code changed) |
| Overall | `0.4 × infra + 0.6 × reliability`, or `0.3 / 0.4 / 0.3` with LLM quality |
| SLO gate | a high-confidence p95 regression (> +100 ms) that also exceeds the service's p95 budget, or an error rate past its error budget, caps the score at 49 |
| Verdict | 80–100 approved (status success) · 50–79 warning (success, with caveats) · 0–49 blocked (failed) |

Every MR comment ends with a mandatory measurement disclosure. Full tables:
[`docs/scoring`](dashboard/src/app/docs/scoring/page.tsx).

---

## Quickstart — run the full pipeline locally (demo mode)

Demo mode runs all nine steps on one machine with no GCP, Dynatrace or Arize accounts.
Replay, scoring, reasoning and the verdict are the production code paths; only the
external systems are stood in for:

| Production | Demo mode |
|---|---|
| Cloud Build + Cloud Run environment | `demo-app/` launched as a local process per simulation |
| WireMock on Cloud Run, stubs from Dynatrace topology | local WireMock (docker compose), stubs with a 12 ms DB-round-trip delay |
| Dynatrace baseline | **measured** by replaying the same traffic against the production build of demo-app |
| Dynatrace request logs | a fixed mix of 200 requests |
| Arize baseline | LLM traces from the production build, scored by the same evaluators |
| GitLab MR | synthetic MRs — dry run: the comment is stored and shown, not posted |

**Prerequisites:** Docker, Python 3.11+, Node.js 20+ (website and dashboard only).

```bash
cp .env.example .env                  # no credentials required; GEMINI_API_KEY is optional
pip install -r requirements.txt
docker compose up -d mongodb wiremock

# Orchestrator: dashboard API + queue consumer, in demo mode
BULWARK_DEMO_MODE=true BULWARK_MODE=development MONGODB_URI=mongodb://localhost:27017 \
  uvicorn orchestrator.main:app --port 8000
# PowerShell:
#   $env:BULWARK_DEMO_MODE="true"; $env:BULWARK_MODE="development"
#   $env:MONGODB_URI="mongodb://localhost:27017"; uvicorn orchestrator.main:app --port 8000

# The PRD §18 story: N+1 regression, then the fix pushed to the same MR
python scripts/run_demo.py regression fixed --comment
python scripts/run_demo.py llm-regression

# Website, docs and dashboard on http://localhost:3000
npm install --prefix dashboard && npm run dev --prefix dashboard
```

Or everything in containers: `docker compose up --build` (orchestrator on :8000, webhook
receiver on :8080).

| Scenario | The change | Measured locally | Verdict |
|---|---|---|---|
| `regression` | `GET /users` loads each user's preferences in its own call (N+1) | p95 ≈ 40–70 ms → 600–700 ms | **Blocked 49** (SLO breach) |
| `fixed` | same MR, one batched call; cancels an in-flight `regression` run | p95 Δ within ±35 ms | **Approved 100** |
| `llm-regression` | ticket-summary prompt edited to "reassure the customer"; answers start promising refunds | latency unchanged | LLM quality drop (needs Gemini) |

Each demo run takes 10–20 seconds.

**Gemini.** AI Studio keys (`AIza…`) and Vertex AI express keys (`AQ.…`) both work.
Without a working key, verdicts are marked `deterministic-fallback` and LLM evaluation is
skipped with the reason recorded in the verdict.

**Windows:** use `WIREMOCK_BASE_URL=http://127.0.0.1:8082` — `localhost` tries IPv6 first
and adds latency to every connection.

---

## Website, docs and dashboard

`dashboard/` is a Next.js app that serves the public site and the operator dashboard:

| Route | What it is |
|---|---|
| `/` | Landing page: the problem, how Bulwark works, scoring, integrations, limits |
| `/docs` | Documentation: Introduction, Quickstart, How it works, Scoring, LLM evaluation, Integrations, Configuration, Deployment, Limitations |
| `/dashboard` | Run demo scenarios (demo mode) or connect a GitLab project |
| `/simulations` | History of simulations with verdicts |
| `/simulation/[id]` | Live view (server-sent events): pipeline steps, metrics, evidence, LLM scores and the exact MR comment |

The dashboard reads the orchestrator URL from `NEXT_PUBLIC_API_URL` (default
`http://127.0.0.1:8000`; set it in `dashboard/.env.local`, it is fixed at build time).
The docs pages live in [`dashboard/src/app/docs/`](dashboard/src/app/docs).

---

## Production deployment

Webhook receiver (Cloud Run, public) → Pub/Sub topic `bulwark-mr-events` → push to the
orchestrator worker's `POST /pubsub/orchestrate` (`BULWARK_MODE=production`, demo mode off).

| Integration | What it needs |
|---|---|
| GitLab | `GITLAB_PAT` with the `api` scope (REST API v4); `GITLAB_WEBHOOK_TOKEN` as the webhook secret |
| Dynatrace | `DYNATRACE_ENV_ID` + OAuth client (`CLIENT_ID` / `CLIENT_SECRET` / `CLIENT_URL`) for the MCP gateway; a service instrumented with ≥ 50 request logs in 24 h |
| Arize | `ARIZE_API_KEY`; `ARIZE_MCP_URL=stdio` (official `@arizeai/phoenix-mcp`, pinned, via npx) or a hosted MCP URL; a `bulwark-prod-<service>` dataset with a scored experiment |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| Google Cloud | Cloud Build, Cloud Run, Artifact Registry, Pub/Sub, Secret Manager (Terraform) and a source-archive bucket (manual) |
| MongoDB | `MONGODB_URI` |

```bash
# 1. Registry first — Cloud Run needs the images to exist
cd terraform && terraform init
terraform apply -target=google_artifact_registry_repository.bulwark_images && cd ..
REG=us-central1-docker.pkg.dev/<project>/bulwark-images
docker build -f webhook_receiver/Dockerfile -t $REG/webhook-receiver:latest .
docker build -f orchestrator/Dockerfile     -t $REG/orchestrator-worker:latest .
docker push $REG/webhook-receiver:latest && docker push $REG/orchestrator-worker:latest

# 2. Build-source bucket (not created by Terraform)
gcloud storage buckets create gs://bulwark-build-sources --location=us-central1
gcloud storage buckets add-iam-policy-binding gs://bulwark-build-sources \
  --member=serviceAccount:bulwark-orchestrator@<project>.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin

# 3. Everything else (fill in terraform/terraform.tfvars first)
cd terraform && terraform apply
```

Then add a GitLab webhook (*Settings → Webhooks*, merge request events) pointing at
`https://<webhook-receiver>/webhook/gitlab`, or use *Connect GitLab* on `/dashboard`.
Require the `Bulwark/production-ready` status to make the gate mandatory.

### API

| Service | Endpoint | Purpose |
|---|---|---|
| Webhook receiver | `POST /webhook/gitlab` | GitLab merge request events (`X-Gitlab-Token` required) |
| Webhook receiver | `GET /health` | Liveness |
| Orchestrator | `POST /pubsub/orchestrate` | Pub/Sub push; acknowledges at once, runs the simulation in the background |
| Orchestrator | `GET /health` | Liveness; reports `mode` and `demo_mode` |
| Orchestrator | `GET /api/simulations` | Recent simulations |
| Orchestrator | `GET /api/simulation/{id}` · `/stream` | One simulation; `/stream` is server-sent events |
| Orchestrator | `POST /api/setup` | Store a token and register the GitLab webhook |
| Orchestrator | `POST /api/demo/simulations` | Start a demo scenario (demo mode only) |

### Configuration

All settings are environment variables (or `.env`); see [`.env.example`](.env.example)
and [`docs/configuration`](dashboard/src/app/docs/configuration/page.tsx). The ones you
are most likely to touch:

| Variable | Default | |
|---|---|---|
| `BULWARK_MODE` | `development` | `development`: MongoDB queue consumed by the orchestrator · `production`: Pub/Sub |
| `BULWARK_DEMO_MODE` | `false` | Local stand-ins for environment, dependencies and baseline |
| `MONGODB_URI` | `mongodb://localhost:27017` | |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — / `gemini-2.0-flash` | Reasoning and LLM judge |
| `P95_NOISE_FLOOR_MS`, `ERROR_RATE_NOISE_FLOOR_PERCENT`, `LLM_EVAL_NOISE_BAND` | `35`, `1.0`, `0.10` | Noise floors |
| `CONFIDENCE_THRESHOLD_APPROVED` / `_WARNING` | `80` / `50` | Verdict bands |

---

## Repository layout

```text
orchestrator/          Pipeline (agent.py), scoring + Gemini (reasoning.py), LLM detection, API (main.py)
  tools/               GitLab (REST), Dynatrace (MCP, DQL) and Arize (Phoenix MCP + evaluators) wrappers
webhook_receiver/      GitLab webhook → queue; override-label handling
environment_builder/   Cloud Build / Cloud Run / WireMock provisioning and teardown; stub generation
traffic_replay/        Async replay engine and metric aggregation
verdict_writer/        MR comment, commit status, approval, persistence
shared/                Config, models, MongoDB, MCP client, anonymiser, demo mode
demo-app/              Demo service: N+1 regression, the fix, and an LLM-backed ticket summary with /traces
dashboard/             Next.js website, docs and dashboard
scripts/run_demo.py    Run demo scenarios from the command line and print verdicts
terraform/             Google Cloud infrastructure
tests/                 321 tests; tests/servers/ holds local fake GitLab, Dynatrace and Arize servers
bulwark_prd_v3.md      Product requirements (v3, final)
```

## Tests

```bash
pytest                # 321 tests, ~1 minute, no network access
```

Tests run against local fake GitLab (REST + MCP), Dynatrace and Arize servers and never
read your `.env`. `tests/test_simulation_flow.py` additionally runs the live
regression → blocked / fix → approved flow when the local WireMock is up.

## Limitations

Bulwark flags probable regressions; it does not certify safety. A simulation cannot see
race conditions beyond its 10× replay concurrency, database constraints and migrations
(databases are not mocked), async/queue-driven behaviour, regressions that need data
states the sampled requests never reach, LLM quality shifts smaller than ±0.10, or
infrastructure failures. See [`docs/limitations`](dashboard/src/app/docs/limitations/page.tsx).

---

## What's new

Changes since commit `4640b96`:

**Pipeline**
- Demo mode now works end to end: the demo flag is a plain setting (it was an un-awaited
  coroutine, so every run took the demo branch), the missing `WIREMOCK_BASE_URL` setting
  exists, WireMock stubs are actually loaded, and the baseline is measured instead of hard-coded.
- Gemini is called asynchronously with a 30 s ceiling and **one** retry; it previously
  retried recursively until the 15-minute timeout. Prompt building no longer crashes on
  stored history.
- Replay discards a warm-up burst, so cold start no longer shows up as a regression.
- **SLO gate:** a high-confidence latency regression that breaches the service's SLO is
  blocked — the PRD weights alone could never block a pure latency regression.
- **LLM evaluation runs:** the Phoenix MCP server stores datasets but cannot run
  evaluators, so Bulwark runs hallucination / relevance / groundedness with Gemini as the
  judge. Relevance and groundedness drops are now scored (their sign was inverted).
- Gemini sees the actual diff, not just file names; the MR comment lists the evidence.
- The Dynatrace log parser handles OpenTelemetry and Logs API v2 records again.
- Cancellation and override lookups are scoped to the project (MR numbers are per project).
- Noise-floor settings in `.env` take effect; they were ignored before.

**Integrations**
- The MCP client honours `ARIZE_MCP_URL` (`stdio` launches the pinned Phoenix MCP server)
  and only uses Dynatrace SSO for the Dynatrace gateway.

**Product**
- New website and docs, restyled dashboard; the dashboard shows real verdicts, metrics,
  evidence and the MR comment, and can start demo scenarios.
- In development mode the orchestrator serves the API and consumes the queue in one process.
- `scripts/run_demo.py`, `.dockerignore`, and Docker images that include demo-app.

**Removed**
- `mock_server/`, which overwrote real simulation results with a hard-coded verdict.

**Tests:** 321 passing (previously 49 of 310 failing), isolated from `.env` and the network.

### Not yet verified

The production path — Cloud Build, Cloud Run, Pub/Sub, a live Dynatrace gateway and
posting to real GitLab MRs — has not been exercised end to end since these changes.

---

MIT licensed · Originally built for the Google Cloud Rapid Agent Hackathon 2026
(GitLab · Dynatrace · Arize · Google Cloud).
