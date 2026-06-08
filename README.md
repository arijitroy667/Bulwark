# 🛡️ Bulwark

**Pre-Deployment Production Simulation Agent** — gates every GitLab Merge Request on a unified confidence score that combines infrastructure regression signals (real traffic replay) with LLM quality regression signals (Arize evaluators), before any code reaches production.

[![Google Cloud](https://img.shields.io/badge/Google%20Cloud-Agent%20Builder-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/agent-builder)
[![GitLab](https://img.shields.io/badge/GitLab-MCP%20Integration-FC6D26?logo=gitlab&logoColor=white)](https://gitlab.com)
[![Dynatrace](https://img.shields.io/badge/Dynatrace-MCP%20Integration-1496FF?logo=dynatrace&logoColor=white)](https://dynatrace.com)
[![Arize](https://img.shields.io/badge/Arize-LLM%20Evaluator-7C3AED)](https://arize.com)
[![Model](https://img.shields.io/badge/Model-Gemini%202.0%20Flash-EA4335?logo=google&logoColor=white)](https://deepmind.google/technologies/gemini/)

> **Google Cloud Rapid Agent Hackathon 2026** · Sponsors: GitLab · Dynatrace · Arize · Google Cloud

---

## The Problem

Production failures from pre-deploy code changes cost engineering teams 2.5+ hours of P1 incident response (DORA 2024). For services with LLM components, quality regressions from prompt changes go undetected for a median **3.2 days** — because there is no automated pre-merge gate.

Bulwark closes this gap.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         BULWARK SYSTEM                               │
│                                                                      │
│  ┌─────────────┐   webhook    ┌──────────────────────────────────┐   │
│  │   GitLab    │ ────────────►│     Webhook Receiver             │   │
│  │  (MR open)  │             │     FastAPI / Cloud Run           │   │
│  └─────────────┘             └────────────────┬─────────────────┘   │
│                                               │ Pub/Sub              │
│                                               ▼                      │
│                              ┌──────────────────────────────────┐   │
│                              │     Bulwark Orchestrator          │   │
│                              │  Google Cloud Agent Builder       │   │
│                              │  Gemini 2.0 Flash                 │   │
│                              └──┬──────────┬────────────┬───────┘   │
│                                 │          │            │            │
│                    ┌────────────┘     ┌────┘       ┌───┘            │
│                    ▼                  ▼             ▼                │
│  ┌──────────────────────┐  ┌──────────────────┐ ┌──────────────┐   │
│  │   Dynatrace MCP      │  │ Environment      │ │  Arize MCP   │   │
│  │  - Incident check    │  │ Builder          │ │ (if LLM      │   │
│  │  - p50/p95/p99       │  │  - Docker build  │ │  detected)   │   │
│  │    baseline          │  │  - Cloud Run     │ │  - Baseline  │   │
│  │  - 200 req sample    │  │  - WireMock      │ │    fetch     │   │
│  │  - SLO targets       │  │    static stubs  │ │  - runEval   │   │
│  └──────────────────────┘  └────────┬─────────┘ └──────┬───────┘   │
│                                     │                   │            │
│                                     ▼                   │            │
│                        ┌────────────────────────┐       │            │
│                        │  Traffic Replay Engine  │       │            │
│                        │  - PII anonymisation   │       │            │
│                        │  - 200 req replay      │       │            │
│                        │  - Metric aggregation  │       │            │
│                        └────────────┬───────────┘       │            │
│                                     └─────────┬─────────┘            │
│                                               ▼                      │
│                              ┌──────────────────────────────────┐   │
│                              │  MongoDB: Historical Context      │   │
│                              │  - Last 5 verdicts, same service  │   │
│                              └──────────────┬───────────────────┘   │
│                                             ▼                        │
│                              ┌──────────────────────────────────┐   │
│                              │     Gemini Reasoning Step         │   │
│                              │  Stateless prompt → JSON verdict  │   │
│                              └──────────────┬───────────────────┘   │
│                                             ▼                        │
│                              ┌──────────────────────────────────┐   │
│                              │     Verdict Writer                │   │
│                              │  - MR comment + metric table     │   │
│                              │  - Pipeline status gate          │   │
│                              │  - MongoDB persistence           │   │
│                              └──────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## How It Works — 9-Step Pipeline

| Step | Name | Description |
|---|---|---|
| 1 | **INGEST** | Fetch MR metadata from GitLab; run two-stage LLM component detection |
| 2+3 | **BASELINE** (parallel) | Fetch Dynatrace production metrics + Arize LLM baseline concurrently |
| 3.5 | **HISTORY** | Inject last 5 simulation verdicts for same service into Gemini context |
| 4 | **PROVISION** | Build Docker image; deploy Cloud Run service + WireMock stubs (parallel) |
| 5 | **REPLAY** | Anonymise + replay 200 production requests at 10× concurrency |
| 6 | **LLM EVAL** | Run Arize hallucination / relevance / groundedness evaluators (if LLM detected) |
| 7 | **TEARDOWN** | Destroy Cloud Run service + WireMock + Artifact Registry image |
| 8 | **REASONING** | Gemini analyses all evidence → structured JSON verdict |
| 9 | **ENFORCE** | Post verdict comment to GitLab MR; set `Bulwark/production-ready` pipeline status |

**Total time:** P50 = 5–8 minutes · Hard ceiling = 15 minutes

---

## Sample Verdict Comment

```
🚫 BLOCKED — 43/100 confidence

| Metric        | Baseline | Simulation | Delta   | Status              |
|---------------|----------|------------|---------|---------------------|
| p95 Latency   | 142ms    | 329ms      | +187ms  | 🚫 5.3x SLO budget  |
| Error Rate    | 0.12%    | 0.18%      | +0.06%  | ✅ Within noise band |
| 5xx Responses | 0        | 0          | —       | ✅ None              |
| Novel Errors  | 0        | 1          | +1      | 🚫 Code regression  |

Novel error: "ConnectionPool timeout after 500ms" — assessed as code regression
(matches N+1 query pattern introduced in this MR; not present in production baseline)

Gemini's reasoning: The p95 latency regression of +187ms is 5.3x the measurement
noise floor of ±35ms — high-confidence regression signal. The novel ConnectionPool
timeout aligns with the loop-per-record pattern visible in the diff.

ℹ️ Measurement context: 200 production request samples. p95 uncertainty: ±35ms.
   Bulwark flags probable regressions — human judgement is the final gate.
   Override: add `bulwark-override` label to bypass.
```

---

## Local Setup

### Prerequisites

- Docker + Docker Compose
- Python 3.12 or 3.13 (Python 3.14 is not supported because of MongoDB Atlas TLS issues)
- MongoDB Atlas account (free M0 tier)
- GitLab account + Personal Access Token
- Dynatrace environment
- Arize account (for LLM evaluation)
- Google Cloud project (for production deployment)
- Gemini API key

### Quick Start

```bash
# 1. Clone the repository
git clone https://gitlab.com/your-group/bulwark.git
cd bulwark

# 2. Copy and fill in your secrets
cp .env.example .env
# Edit .env with your actual values (see table below)

# 3. Start the development stack
docker-compose up

# 4. Run tests to verify
pytest tests/ -v
# Expected: 96+ tests passing
```

### Environment Variables

| Variable | Description | Example |
|---|---|---|
| `GITLAB_URL` | GitLab instance URL | `https://gitlab.com` |
| `GITLAB_PAT` | GitLab Personal Access Token | `glpat-xxxx` |
| `GITLAB_WEBHOOK_TOKEN` | Webhook secret (must match GitLab config) | `your-secret` |
| `DYNATRACE_ENV_ID` | Dynatrace environment ID | `abc12345` |
| `DYNATRACE_API_TOKEN` | Dynatrace API token | `dt0s16.xxxx` |
| `ARIZE_API_KEY` | Arize API key | `ak-xxxx` |
| `ARIZE_SPACE_KEY` | Arize space key | `U3Bhxxx` |
| `MONGODB_URI` | MongoDB Atlas connection string | `mongodb+srv://...` |
| `MONGODB_DB_NAME` | MongoDB database name | `Bulwark` |
| `GCP_PROJECT_ID` | Google Cloud project ID | `my-project-123` |
| `GCP_REGION` | Google Cloud region | `us-central1` |
| `GCP_ARTIFACT_REGISTRY` | Artifact Registry repo name | `bulwark-images` |
| `GEMINI_API_KEY` | Gemini API key | `AQ.xxxx` |
| `GEMINI_MODEL` | Gemini model ID | `gemini-2.5-flash` |
| `BULWARK_MODE` | `development` or `production` | `development` |
| `REPLAY_CONCURRENCY` | Concurrent replay requests | `10` |
| `REPLAY_REQUEST_LIMIT` | Max production requests to sample | `200` |

---

## GitLab Webhook Configuration

1. Go to your GitLab project → **Settings → Webhooks**
2. Set URL to your Bulwark webhook receiver URL
3. Set **Secret token** to the value of `GITLAB_WEBHOOK_TOKEN`
4. Enable triggers: **Merge request events**
5. Click **Add webhook**

Bulwark will trigger on `merge_request:opened`, `merge_request:updated`, `merge_request:reopened`.

---

## MCP Configuration

Bulwark uses three MCP integrations. Each is wrapped as a Python async client:

| MCP | Module | Key operations |
|---|---|---|
| **GitLab** | `orchestrator/tools/gitlab_tool.py` | MR read, diff fetch, comment post, pipeline status |
| **Dynatrace** | `orchestrator/tools/dynatrace_tool.py` | Baseline metrics, request logs, topology, incident check |
| **Arize** | `orchestrator/tools/arize_tool.py` | LLM baseline fetch, eval run, span logging |

Bulwark integrations use real external API calls. `BULWARK_MODE=development` only changes local queueing behavior.

---

## GCP Infrastructure Provisioning

The `terraform/` directory contains all GCP infrastructure as code.

**Resources provisioned:**
- Cloud Run service (webhook receiver)
- Pub/Sub topic + subscription + dead-letter queue
- Artifact Registry repository (`bulwark-images`)
- IAM service account with least-privilege roles
- Secret Manager secrets for all credentials

**To provision (requires `gcloud` + `terraform` installed):**

```bash
cd terraform

# Initialise Terraform
terraform init

# Review what will be created
terraform plan \
  -var="project_id=YOUR_GCP_PROJECT_ID" \
  -var="gitlab_pat=YOUR_GITLAB_PAT" \
  -var="gitlab_webhook_token=YOUR_WEBHOOK_TOKEN" \
  -var="mongodb_uri=YOUR_MONGODB_URI" \
  -var="gemini_api_key=YOUR_GEMINI_KEY"

# Apply
terraform apply
```

> **Note:** As of this submission, `gcloud` and `terraform` CLI are not installed on the development machine. The infrastructure definitions are complete and ready to apply against the GCP project `bulwark-498512` once the CLI tools are installed.

**Pre-warm base images after provisioning:**

```bash
BULWARK_MODE=production ./scripts/prewarm_images.sh
```

---

## Demo Scenario

The `demo-app/` directory contains a FastAPI service that reproduces the exact PRD §18 demo:

```bash
# Branch 1: introduce the regression
DEMO_VERSION=regression uvicorn demo-app/main:app --port 8080
# → Open MR → Bulwark posts BLOCKED (43/100, p95 +187ms)

# Branch 2: fix the regression
DEMO_VERSION=fixed uvicorn demo-app/main:app --port 8080
# → Push fix → Bulwark posts APPROVED (91/100, p95 +3ms)
```

---

## What Bulwark Catches vs Does Not Catch

| ✅ Catches | ❌ Does not catch |
|---|---|
| p95 latency regressions > 40ms (above ±35ms noise floor) | Conditional regressions (specific data states) |
| Error rate regressions > 1.0% (above binomial noise floor) | Race conditions (controlled concurrency only) |
| Any 5xx responses | Database constraint violations (no real DB) |
| Novel error strings (Gemini-classified) | Async/queue-dependent behaviour |
| LLM hallucination/relevance/groundedness delta > 0.10 | LLM quality changes < evaluator noise band |

---

## Confidence Score

Bulwark computes a 0–100 confidence score using statistically-grounded deduction tables:

- **Infrastructure score** (0–100): deductions for p95 delta, error rate delta, 5xx count
- **Reliability score** (0–100): deductions for Gemini-classified novel errors (code regressions only)
- **LLM quality score** (0–100, conditional): deductions for Arize evaluator deltas > 0.10

**Verdict thresholds:**

| Score | Verdict | Pipeline action |
|---|---|---|
| 80–100 | ✅ APPROVED | `success` (MR can merge) |
| 50–79 | ⚠️ WARNING | `success` with caveats |
| 0–49 | 🚫 BLOCKED | `failed` (merge button greyed out) |

**Override:** Add `bulwark-override` label to bypass a BLOCKED verdict. The override is logged to MongoDB and included in historical context for future simulations.

---

## Project Structure

```
bulwark/
├── webhook_receiver/      # FastAPI webhook handler (Cloud Run)
├── orchestrator/          # 9-step simulation orchestrator
│   ├── agent.py           # Main orchestration loop
│   ├── reasoning.py       # Gemini prompt assembly + scoring
│   ├── llm_detector.py    # Two-stage LLM component detection
│   └── tools/             # MCP wrappers (GitLab, Dynatrace, Arize)
├── environment_builder/   # Docker build + Cloud Run deploy + WireMock
├── traffic_replay/        # Async HTTP replay engine
├── verdict_writer/        # MR comment formatter + pipeline gate
├── shared/                # Models, config, DB, PII anonymiser
├── demo-app/              # N+1 demo service (PRD §18 scenario)
├── scripts/               # prewarm_images.sh
├── terraform/             # GCP infrastructure as code
└── tests/                 # 96+ unit + integration tests
```

---

## Running Tests

```bash
pytest tests/ -v
```

Expected output: **96+ tests passing** covering:
- `test_reasoning.py` — 39 tests (tiered scoring, LLM formula, noise floors)
- `test_llm_detector.py` — 30 tests (Stage 1 path heuristics, Stage 2 AST scan)
- `test_webhook.py` — 15 tests (token validation, cancellation, SHA idempotency)
- `test_anonymizer.py` — anonymisation correctness
- `test_replayer.py` — traffic replay mechanics
- `test_environment_builder.py` — parallel deploy + teardown
- `test_integration_smoke.py` — end-to-end pipeline smoke tests

---

## License

MIT — see [LICENSE](LICENSE)

---

*Built for the Google Cloud Rapid Agent Hackathon 2026 · Sponsors: GitLab · Dynatrace · Arize · Google Cloud*
