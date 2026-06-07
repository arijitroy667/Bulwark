# Bulwark — Pre-Deployment Production Simulation Agent
### Product Requirements Document (PRD) · v1.0

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Product Vision](#3-product-vision)
4. [Sponsor Integrations](#4-sponsor-integrations)
5. [System Architecture](#5-system-architecture)
6. [Tech Stack](#6-tech-stack)
7. [Core Agentic Loop](#7-core-agentic-loop)
8. [Functional Requirements](#8-functional-requirements)
9. [Non-Functional Requirements](#9-non-functional-requirements)
10. [Phase 1 — Foundation & Core Pipeline](#10-phase-1--foundation--core-pipeline)
11. [Phase 2 — Intelligence & Simulation Depth](#11-phase-2--intelligence--simulation-depth)
12. [Phase 3 — Confidence Scoring, UI & Polish](#12-phase-3--confidence-scoring-ui--polish)
13. [Data Models](#13-data-models)
14. [API Contracts](#14-api-contracts)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Security Considerations](#16-security-considerations)
17. [Demo Scenario](#17-demo-scenario)
18. [Success Metrics](#18-success-metrics)
19. [Deliverables Checklist](#19-deliverables-checklist)

---

## 1. Executive Summary

**Bulwark** is an autonomous pre-deployment production simulation agent. When a developer opens a Merge Request (MR) in GitLab, Bulwark intercepts it before any code reaches production. It autonomously provisions an ephemeral, production-identical environment on Google Cloud Run, replays real production traffic patterns (sourced from Dynatrace) against the new code, uses Arize to evaluate behavioral regressions, and then uses Gemini's reasoning to produce a structured **production-readiness verdict** — either approving or blocking the MR with a full explainability report.

No engineer sets up the environment. No code touches production during evaluation. The agent does everything.

**Hackathon tracks targeted:** GitLab · Dynatrace · Arize  
**Built with:** Gemini 2.0 Flash · Google Cloud Agent Builder · GitLab MCP · Dynatrace MCP · Arize MCP  
**Submission category:** Google Cloud Rapid Agent Hackathon — Building Agents for Real-World Challenges

---

## 2. Problem Statement

### The Current Reality

Every software team faces the same impossible trade-off: **you cannot know if your code will break production without deploying it to production.**

Existing approaches all fail at different stages:

| Approach | When it catches failures | Problem |
|---|---|---|
| Unit / integration tests | Pre-deploy | Only tests what the developer anticipated |
| Static code analysis (SAST) | Pre-deploy | Finds security issues, not runtime behavioral failures |
| Staging environments | Pre-deploy | Manual to set up; rarely mirrors actual production traffic patterns |
| Canary deployments | Post-deploy | Code is already live when the failure is found |
| Shadow deployments | Post-deploy | Code is already deployed; real users' requests are being consumed |
| Monitoring / alerting | Post-deploy | Discovers failures after production is already impacted |

### The Cost

- Engineers are woken up at 3 AM to handle production incidents caused by code that "passed tests"
- Mean Time to Recovery (MTTR) for production failures averages 1–4 hours
- Failed deployments require emergency rollbacks, burning engineering hours and eroding user trust
- According to GitLab's 2025 DevSecOps Report, developers spend **11 hours per month** remediating vulnerabilities **after release** — when they are already exploitable

### The Gap

No tool today autonomously:
1. Provisions a production-identical clone per MR without human intervention
2. Replays real production traffic against the incoming code
3. Reasons over the results to produce a go/no-go decision
4. Blocks or approves the MR pipeline with full evidence

Bulwark fills this gap entirely.

---

## 3. Product Vision

### What Bulwark Does

> "Bulwark guarantees that every line of code touching production has already been run against production — before production ever sees it."

When a developer opens a Merge Request in GitLab:

1. Bulwark is **triggered automatically** via GitLab webhook
2. It **clones the production environment** into an isolated Google Cloud Run instance
3. It **fetches the last N hours of real production request logs** from Dynatrace
4. It **replays that traffic** against the new code in the sandboxed environment
5. It **monitors behavioral differences** — latency, error rates, response shape changes, crashes
6. Arize **evaluates any LLM-powered components** in the stack for drift or degradation
7. Gemini **reasons over all evidence** and produces a structured verdict with confidence score
8. Bulwark **posts the verdict as a GitLab MR comment** with full evidence, and either approves or blocks the pipeline
9. The ephemeral environment is **automatically torn down**

### What Bulwark Does NOT Do

- Bulwark does not replace the developer's test suite — it runs alongside it
- Bulwark does not permanently host any environment — everything is ephemeral
- Bulwark does not make code changes — it evaluates and reports only
- Bulwark does not require any per-project configuration — it infers environment shape from GitLab CI/CD definitions and Dynatrace service maps

---

## 4. Sponsor Integrations

### 4.1 GitLab MCP

**Role:** The trigger and the enforcer.

GitLab is the entry point for Bulwark. Every workflow begins when a developer opens or updates a Merge Request.

**MCP Operations Used:**

| Operation | Purpose |
|---|---|
| `listMergeRequests` | Poll for new/updated MRs to process |
| `getMergeRequest` | Fetch full MR metadata — title, description, source branch, diff |
| `getRepositoryFiles` | Read `gitlab-ci.yml`, `Dockerfile`, `docker-compose.yml`, environment config files |
| `getBranches` | Compare source branch against target (main/production) |
| `getCommits` | Fetch commit history and diff stats for the MR |
| `createMergeRequestNote` | Post the Bulwark verdict as a comment on the MR |
| `updateMergeRequest` | Approve or request changes via MR status update |
| `createPipelineStatus` | Set a custom pipeline status check ("Bulwark/production-ready") to block or pass the pipeline gate |

**Webhook Events Consumed:**
- `merge_request:opened`
- `merge_request:updated`
- `merge_request:reopened`

### 4.2 Dynatrace MCP

**Role:** The production memory — provides real traffic patterns and the baseline to compare against.

Dynatrace holds the ground truth of what production actually looks like: real request logs, response times, error rates, service topology, and SLO baselines.

**MCP Operations Used:**

| Operation | Purpose |
|---|---|
| `getProblems` | Check for any active production incidents that would invalidate a clean baseline |
| `getServiceMetrics` | Fetch p50/p95/p99 latency, error rate, throughput for the affected service |
| `getLogs` | Retrieve the last N hours of production request logs (request path, method, headers, body shape, response codes) for traffic replay |
| `getServiceTopology` | Map upstream/downstream dependencies of the service being changed |
| `getSLOs` | Fetch the service's SLO targets (latency budgets, error budgets) to use as pass/fail thresholds in simulation |
| `getEvents` | Check for recent deployment events to understand the current production baseline's age |

**What Bulwark extracts from Dynatrace:**
- A representative sample of 100–500 real production requests (anonymized) covering the last 24 hours
- The production baseline: p95 latency, error rate, and throughput for the service under test
- The service's direct dependencies — so Bulwark can mock them accurately in the sandboxed environment

### 4.3 Arize MCP

**Role:** The intelligence evaluator — assesses behavioral and quality regressions, especially in LLM-powered components.

Arize specializes in tracing, evaluating, and monitoring AI/LLM components. If the service being deployed contains any AI features (chat, recommendations, classification, summarization), Arize evaluates them for drift, hallucination, or quality regression between the current production version and the new MR version.

**MCP Operations Used:**

| Operation | Purpose |
|---|---|
| `getSpans` / `getTraces` | Retrieve production LLM traces for the service — prompts, outputs, latencies, token counts |
| `runEval` | Execute Arize's built-in evaluators (hallucination, relevance, toxicity, groundedness) on simulation outputs |
| `compareDatasets` | Compare simulation outputs against production baseline outputs for the same inputs |
| `getDataset` | Fetch the production golden dataset for the service — used as reference in simulation |
| `logSpans` | Log simulation traces into Arize for evaluation and archival |

**What Bulwark extracts from Arize:**
- Production LLM output quality scores as the baseline
- Post-simulation LLM output quality scores from the new code
- A regression delta: does the new code produce worse AI outputs than production?

> **Note:** For services with no LLM components, the Arize step is skipped and the verdict is based entirely on infrastructure metrics from Dynatrace simulation results.

---

## 5. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Bulwark SYSTEM                                │
│                                                                         │
│  ┌──────────────┐    webhook     ┌────────────────────────────────────┐ │
│  │   GitLab     │ ─────────────► │        Bulwark Orchestrator       │ │
│  │   (MR open)  │                │   (Google Cloud Agent Builder)     │ │
│  └──────────────┘                │         Gemini 2.0 Flash           │ │
│                                  └────────────┬───────────────────────┘ │
│                                               │                         │
│                    ┌──────────────────────────┼──────────────────────── │
│                    │                          │                    │    │
│                    ▼                          ▼                    ▼    │
│  ┌─────────────────────┐  ┌──────────────────────┐  ┌────────────────┐ │
│  │  Dynatrace MCP      │  │  Environment Builder  │  │  Arize MCP     │ │
│  │  - Fetch logs       │  │  - Parse Dockerfile   │  │  - Fetch LLM   │ │
│  │  - Get baselines    │  │  - Spin Cloud Run     │  │    baselines   │ │
│  │  - Get topology     │  │  - Mock dependencies  │  │  - Run evals   │ │
│  └─────────────────────┘  └──────────┬───────────┘  └───────┬────────┘ │
│                                      │                       │          │
│                                      ▼                       │          │
│                        ┌─────────────────────────┐           │          │
│                        │  Traffic Replay Engine   │           │          │
│                        │  - Replay N requests     │           │          │
│                        │  - Collect metrics       │           │          │
│                        │  - Diff vs baselines     │           │          │
│                        └────────────┬────────────┘           │          │
│                                     │                        │          │
│                                     └────────────┬───────────┘          │
│                                                  │                      │
│                                                  ▼                      │
│                                  ┌───────────────────────────┐          │
│                                  │    Gemini Reasoning Step   │          │
│                                  │  - Weigh all evidence      │          │
│                                  │  - Generate confidence score│         │
│                                  │  - Write verdict report    │          │
│                                  └──────────────┬────────────┘          │
│                                                 │                       │
│                                                 ▼                       │
│                                  ┌───────────────────────────┐          │
│                                  │    GitLab MCP (Enforce)    │          │
│                                  │  - Post MR comment         │          │
│                                  │  - Set pipeline status     │          │
│                                  │  - Approve / Block MR      │          │
│                                  └───────────────────────────┘          │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Component Breakdown

| Component | Responsibility | Hosted on |
|---|---|---|
| **Bulwark Orchestrator** | The central agent brain; manages the entire workflow, calls all tools, makes decisions | Google Cloud Agent Builder |
| **Webhook Receiver** | Receives GitLab webhook events, validates signatures, enqueues jobs | Google Cloud Run (always-on, lightweight) |
| **Environment Builder** | Parses Dockerfile/compose files, provisions the ephemeral clone, tears it down post-evaluation | Google Cloud Run Jobs |
| **Traffic Replay Engine** | Takes Dynatrace request logs, anonymizes them, replays them against the sandboxed environment, collects diff metrics | Python service on Cloud Run |
| **Verdict Store** | Persists all simulation results, verdicts, and evidence for audit and learning | MongoDB Atlas |
| **Arize Eval Runner** | Sends simulation outputs to Arize for LLM-specific quality evaluation | Arize via MCP |

---

## 6. Tech Stack

### 6.1 AI / Agent Layer

| Technology | Version | Purpose |
|---|---|---|
| **Google Cloud Agent Builder** | Latest | Primary agent orchestration platform; used to build and deploy the Bulwark agent workflow |
| **Gemini 2.0 Flash** | `gemini-2.0-flash` | The agent's reasoning engine; analyzes simulation results, generates verdict, writes MR comments |
| **Model Context Protocol (MCP)** | Latest | Communication protocol for all three sponsor tool integrations |

### 6.2 Sponsor MCPs

| MCP Server | Endpoint | Auth |
|---|---|---|
| GitLab MCP | `https://gitlab.com/-/mcp/v1` | GitLab Personal Access Token (PAT) with `api` scope |
| Dynatrace MCP | `https://<env-id>.live.dynatrace.com/mcp/v1` | Dynatrace API Token with `logs.read`, `metrics.read`, `DataExport`, `entities.read` scopes |
| Arize MCP | `https://app.arize.com/mcp/v1` | Arize API Key + Space Key |

### 6.3 Backend / Orchestration

| Technology | Purpose |
|---|---|
| **Python 3.12** | Primary language for all backend services |
| **FastAPI** | Webhook receiver HTTP server; async, production-grade |
| **Google Cloud Run** | Serverless container execution — webhook receiver, ephemeral environment clones, traffic replay engine |
| **Google Cloud Run Jobs** | One-off execution for environment provisioning and teardown |
| **Google Cloud Pub/Sub** | Message queue between webhook receiver and orchestrator — decouples MR event ingestion from agent execution |
| **Google Cloud Secret Manager** | Secure storage of API tokens, MCP credentials, Dynatrace keys |
| **Docker** | Container format for ephemeral environment clones |
| **WireMock / Hoverfly** | HTTP mock server for simulating downstream service dependencies inside the sandboxed environment |

### 6.4 Database

| Technology | Purpose |
|---|---|
| **MongoDB Atlas** | Primary data store for all simulation results, verdicts, MR history, traffic replay logs, and Arize evaluation scores. Uses MongoDB's flexible document model — ideal because simulation results have variable schema depending on the service type (some have LLM components, some don't) |
| **MongoDB Collections** | `simulations`, `verdicts`, `traffic_replays`, `arize_evaluations`, `environment_configs` |

> **Why MongoDB?** Simulation results are heterogeneous documents — some contain LLM eval fields (hallucination score, relevance score), others contain only infrastructure metrics. MongoDB's schema-flexible document model is a natural fit. It also enables fast querying of historical verdicts for trend analysis in Phase 3.

### 6.5 Traffic Replay

| Technology | Purpose |
|---|---|
| **GoReplay (goreplay)** | Capture and replay HTTP traffic; lightweight, production-proven |
| **httpx (Python)** | Async HTTP client for replay execution and response collection |
| **Apache JMeter (optional Phase 2)** | Load simulation for stress-testing the new code under peak production traffic volumes |

### 6.6 Frontend / Dashboard (Phase 3)

| Technology | Purpose |
|---|---|
| **Next.js 14** | Dashboard UI — shows simulation history, verdict details, confidence trends |
| **Tailwind CSS** | Styling |
| **shadcn/ui** | UI components |
| **Recharts** | Latency comparison charts, error rate timelines, confidence score history |
| **Vercel** | Hosting the dashboard |

### 6.7 Infrastructure & DevOps

| Technology | Purpose |
|---|---|
| **Google Cloud Platform (GCP)** | Primary cloud — Cloud Run, Pub/Sub, Secret Manager, Artifact Registry |
| **Terraform** | Infrastructure as Code for all GCP resources |
| **Docker** | Containerization of all services |
| **GitHub Actions / GitLab CI** | CI/CD pipeline for Bulwark itself (yes, Bulwark is built with CI/CD best practices) |

---

## 7. Core Agentic Loop

This is the complete step-by-step execution of Bulwark's agent workflow, as it runs in Google Cloud Agent Builder with Gemini 2.0 Flash.

```
TRIGGER: GitLab webhook fires → merge_request:opened event received

STEP 1 — INGEST [GitLab MCP]
  ├── Fetch MR metadata (title, description, author, source branch, target branch)
  ├── Fetch the full code diff
  ├── Read gitlab-ci.yml from the repository
  ├── Read Dockerfile / docker-compose.yml
  └── Identify which services are modified by this MR

STEP 2 — FETCH PRODUCTION BASELINE [Dynatrace MCP]
  ├── Check for active production problems → if critical incident active, DEFER evaluation
  ├── Fetch service topology for all modified services
  ├── Fetch p50/p95/p99 latency and error rate baseline (last 24 hours)
  ├── Fetch SLO targets for the service
  └── Fetch last 24 hours of production request logs → sample 200 representative requests

STEP 3 — FETCH LLM BASELINE [Arize MCP] (skip if no LLM components detected)
  ├── Detect if the service has LLM components (scan diff for LLM API calls, prompt files)
  ├── If yes: fetch production LLM trace dataset (last N traces)
  └── Fetch production quality scores (hallucination, relevance, groundedness) as baseline

STEP 4 — PROVISION EPHEMERAL ENVIRONMENT [Environment Builder]
  ├── Parse Dockerfile from repository
  ├── Build Docker image for the MR's source branch code
  ├── Deploy to isolated Google Cloud Run instance (unique URL per simulation)
  ├── Spin up WireMock stubs for all downstream dependencies (from Dynatrace topology)
  ├── Inject production-accurate mock data shapes into dependency stubs
  ├── Health check the ephemeral instance → confirm it is responding
  └── Record environment_id and base_url for replay

STEP 5 — REPLAY PRODUCTION TRAFFIC [Traffic Replay Engine]
  ├── Anonymize PII from the 200 sampled production requests (mask emails, IDs, tokens)
  ├── Replay all 200 requests against the ephemeral environment
  ├── For each request: record response_time, status_code, response_body_hash, error_message
  ├── Aggregate: p50/p95/p99 latency, error rate, 5xx count, response shape changes
  └── Compute deltas vs Dynatrace production baseline

STEP 6 — LLM EVALUATION [Arize MCP] (skip if no LLM components)
  ├── Send LLM outputs from simulation to Arize for evaluation
  ├── Run hallucination, relevance, and groundedness evaluators
  ├── Compare scores against production baseline
  └── Compute LLM quality regression delta

STEP 7 — TEARDOWN [Environment Builder]
  └── Destroy the ephemeral Cloud Run instance and all associated resources

STEP 8 — GEMINI REASONING [Agent Brain]
  ├── Receive: diff summary, latency delta, error rate delta, LLM quality delta, SLO targets
  ├── Reason: does the new code meet production SLOs? Are latency/error regressions within acceptable thresholds?
  ├── Reason: if LLM components exist, does quality regress beyond threshold?
  ├── Reason: are there any crash signatures, 5xx patterns, or novel error messages not seen in production?
  ├── Generate: confidence score (0–100) with breakdown by category
  └── Generate: structured verdict report with evidence, reasoning, and recommendation

STEP 9 — ENFORCE [GitLab MCP]
  ├── Persist full simulation result to MongoDB
  ├── Post formatted verdict comment on the MR (confidence score + evidence + recommendation)
  ├── Set custom GitLab pipeline status: "Bulwark/production-ready" → PASS or FAIL
  └── If confidence < threshold: request changes on MR (blocks merge until re-evaluated or overridden)

END: Production was never touched. Code is either cleared for merge or blocked with full evidence.
```

---

## 8. Functional Requirements

### FR-001 — Webhook Ingestion
- The system MUST receive GitLab webhook events for `merge_request:opened`, `merge_request:updated`, and `merge_request:reopened`
- The system MUST validate the GitLab webhook signature (`X-Gitlab-Token` header) before processing any event
- The system MUST enqueue the event to Pub/Sub within 200ms of receipt to prevent webhook timeout
- The system MUST be idempotent — replaying the same webhook event MUST NOT create duplicate simulations

### FR-002 — MR Analysis
- The system MUST fetch the full code diff for the MR
- The system MUST identify all services modified by the MR by parsing the diff and `gitlab-ci.yml`
- The system MUST read `Dockerfile` and `docker-compose.yml` to understand how to build the environment
- The system MUST skip evaluation if the MR only modifies documentation files (`.md`, `.txt`, `docs/`)

### FR-003 — Production Baseline Retrieval
- The system MUST check Dynatrace for active production problems before starting evaluation
- If a P1 incident is active on the affected service, the system MUST defer evaluation and post a comment stating why
- The system MUST fetch service latency (p50/p95/p99), error rate, and throughput as the production baseline
- The system MUST fetch between 100 and 500 production request log entries representing the last 24 hours
- The system MUST handle services with no Dynatrace instrumentation gracefully (skip Dynatrace step, note in verdict)

### FR-004 — Ephemeral Environment Provisioning
- The system MUST build a Docker image from the MR's source branch code
- The system MUST deploy the image to an isolated Google Cloud Run instance
- Each simulation MUST receive a unique, isolated URL — no shared state between simulations
- The system MUST inject WireMock stubs for all downstream dependencies identified from the Dynatrace service topology
- The system MUST health-check the ephemeral instance before beginning traffic replay
- If health check fails after 3 retries, the system MUST mark the simulation as `BUILD_FAILED` and post a comment

### FR-005 — Traffic Replay
- The system MUST anonymize all PII from production request logs before replay (mask email addresses, authentication tokens, user IDs, phone numbers)
- The system MUST replay requests sequentially with a configurable concurrency limit (default: 10 concurrent requests)
- For each replayed request, the system MUST record: response time, HTTP status code, response body hash, and any error message
- The system MUST compute the delta between simulation metrics and the production baseline for: p95 latency, error rate, 5xx count
- The system MUST detect novel error messages — error patterns not seen in the production baseline logs

### FR-006 — LLM Evaluation (Arize)
- The system MUST detect whether the modified service contains LLM API calls by scanning the diff for patterns (OpenAI, Anthropic, Gemini SDK calls, prompt template files)
- If LLM components are detected, the system MUST send simulation LLM outputs to Arize for evaluation
- The system MUST run at minimum: hallucination detection, relevance scoring, and groundedness evaluation
- The system MUST compare simulation scores against the production Arize baseline
- If no LLM components are detected, this step MUST be skipped and noted in the verdict

### FR-007 — Teardown
- The system MUST destroy the ephemeral Cloud Run instance and all associated resources upon simulation completion (pass or fail)
- The system MUST destroy the environment if the simulation exceeds the maximum allowed runtime (default: 15 minutes)
- Teardown MUST succeed even if the simulation step failed — no orphaned environments

### FR-008 — Gemini Reasoning & Verdict
- The reasoning step MUST receive: diff summary, latency delta, error rate delta, novel error patterns, LLM quality delta (if applicable), SLO targets
- The system MUST produce a confidence score from 0–100 with a breakdown: `infrastructure_score`, `reliability_score`, `llm_quality_score` (if applicable), `overall_score`
- Verdicts with `overall_score >= 80` MUST be marked `APPROVED`
- Verdicts with `overall_score >= 50 and < 80` MUST be marked `WARNING` — approved with caveats
- Verdicts with `overall_score < 50` MUST be marked `BLOCKED`
- The reasoning step MUST generate a human-readable explanation of why each score was assigned

### FR-009 — MR Enforcement
- The system MUST post a formatted comment on the GitLab MR containing: overall verdict, confidence score, metric deltas table, Gemini's reasoning, and a link to the full simulation report in MongoDB
- The system MUST set a custom GitLab pipeline status check named `Bulwark/production-ready`
- `APPROVED` → pipeline status = `success`
- `WARNING` → pipeline status = `success` (with caveat in comment)
- `BLOCKED` → pipeline status = `failed` (MR cannot be merged until re-evaluation passes or human override)
- Human engineers MUST be able to override a `BLOCKED` verdict by adding a `Bulwark-override` label to the MR

### FR-010 — Persistence
- Every simulation run MUST be persisted to MongoDB with full evidence (metrics, logs, Arize scores, verdict, reasoning)
- The system MUST retain simulation history for a minimum of 90 days
- Simulations MUST be queryable by: MR ID, repository, service name, verdict status, date range

---

## 9. Non-Functional Requirements

### NFR-001 — Performance
- Total Bulwark execution time (webhook receipt to verdict posted) MUST be under **5 minutes** for services with fewer than 10 dependencies
- Ephemeral environment boot time MUST be under 90 seconds
- Traffic replay of 200 requests MUST complete in under 120 seconds
- The webhook receiver MUST respond with HTTP 200 within 200ms of receiving the event

### NFR-002 — Reliability
- The Bulwark orchestrator MUST have 99.5% uptime
- If any single step fails (environment build, Dynatrace fetch, Arize eval), the system MUST log the failure, continue with partial data where possible, and note the missing evidence in the verdict
- No simulation failure MUST cause a system crash — all failures MUST be gracefully handled and reported

### NFR-003 — Isolation
- Each simulation MUST run in a completely isolated Cloud Run instance — no shared file system, no shared memory, no shared network namespace between simulations
- Ephemeral environments MUST NOT have outbound internet access except to WireMock stubs and Bulwark's own control plane
- Production credentials MUST NOT be injected into ephemeral environments — they receive mock credentials only

### NFR-004 — Cost Efficiency
- Ephemeral Cloud Run instances MUST be destroyed within 60 seconds of simulation completion
- Cloud Run instances for simulation MUST use minimum viable CPU/memory (1 vCPU / 512MB RAM default, configurable)
- Target simulation cost per MR: under $0.05 USD

### NFR-005 — Observability
- All agent steps MUST emit structured logs to Google Cloud Logging with: `simulation_id`, `mr_id`, `step_name`, `duration_ms`, `status`
- Bulwark's own latency and error rate MUST be monitored in Dynatrace (self-monitoring)
- Every Gemini reasoning call MUST be logged to Arize (Bulwark monitors itself using the same tools it uses to evaluate others)

---

## 10. Phase 1 — Foundation & Core Pipeline

**Goal:** Get the complete end-to-end flow working for a single service. Verdict must be posted to GitLab within 10 minutes. No dashboard, no stress testing, minimal UI.

**Duration estimate:** 3–4 days of focused development

### 10.1 Milestone 1A — Project Setup & Infrastructure

**Tasks:**

1. **Initialize the repository**
   - Create GitLab repository: `Bulwark-agent`
   - Directory structure:
     ```
     Bulwark/
     ├── orchestrator/        # Google Cloud Agent Builder agent definition
     ├── webhook-receiver/    # FastAPI webhook server
     ├── environment-builder/ # Ephemeral env provisioning scripts
     ├── traffic-replay/      # Request replay engine
     ├── verdict-writer/      # MR comment formatter + GitLab enforcer
     ├── shared/              # Shared models, utilities, MongoDB client
     ├── terraform/           # GCP infrastructure as code
     ├── tests/               # Unit and integration tests
     └── docker-compose.yml   # Local development stack
     ```

2. **Provision GCP infrastructure (Terraform)**
   - Google Cloud Run service: `Bulwark-webhook-receiver`
   - Google Cloud Pub/Sub topic: `Bulwark-mr-events`
   - Google Cloud Pub/Sub subscription: `Bulwark-orchestrator-sub`
   - Google Cloud Artifact Registry: `Bulwark-images`
   - Google Cloud Secret Manager secrets:
     - `GITLAB_WEBHOOK_TOKEN`
     - `GITLAB_PAT`
     - `DYNATRACE_API_TOKEN`
     - `DYNATRACE_ENV_ID`
     - `ARIZE_API_KEY`
     - `ARIZE_SPACE_KEY`
     - `MONGODB_URI`
   - IAM service account: `Bulwark-orchestrator@<project>.iam.gserviceaccount.com` with roles: `run.invoker`, `pubsub.publisher`, `secretmanager.secretAccessor`

3. **Set up MongoDB Atlas**
   - Create cluster: `Bulwark-cluster` (M0 free tier for hackathon)
   - Create database: `Bulwark`
   - Create collections: `simulations`, `verdicts`, `environment_configs`
   - Create indexes: `simulations.mr_id`, `simulations.status`, `verdicts.overall_score`

### 10.2 Milestone 1B — Webhook Receiver

**File:** `webhook-receiver/main.py`

**Implementation:**

```python
# FastAPI app that receives GitLab webhook events,
# validates signatures, and publishes to Pub/Sub

POST /webhook/gitlab
  → validate X-Gitlab-Token header
  → parse event body
  → filter: only process merge_request events
  → filter: only process opened / updated / reopened actions
  → filter: skip if MR only modifies .md / .txt / docs/ files
  → publish to Pub/Sub topic: Bulwark-mr-events
  → return HTTP 200 immediately

GET /health
  → return {"status": "ok"}
```

**Pub/Sub message schema:**
```json
{
  "simulation_id": "sim_<uuid>",
  "mr_id": 123,
  "project_id": "group/project",
  "source_branch": "feature/new-auth",
  "target_branch": "main",
  "mr_url": "https://gitlab.com/...",
  "received_at": "2026-06-05T10:00:00Z"
}
```

**Validation rules:**
- Reject if `X-Gitlab-Token` does not match stored secret
- Reject if `object_kind` != `merge_request`
- Reject if `object_attributes.state` not in `[opened, updated, reopened]`
- Accept and skip (return 200, do not enqueue) if diff only touches documentation files

### 10.3 Milestone 1C — GitLab MCP Integration

**File:** `orchestrator/tools/gitlab_tool.py`

Implement wrapper functions over the GitLab MCP for all operations defined in Section 4.1.

Key function signatures:
```python
async def get_mr_details(project_id: str, mr_id: int) -> MergeRequestDetails
async def get_mr_diff(project_id: str, mr_id: int) -> list[FileDiff]
async def get_file_content(project_id: str, ref: str, filepath: str) -> str
async def post_mr_comment(project_id: str, mr_id: int, body: str) -> None
async def set_pipeline_status(project_id: str, sha: str, status: str, description: str) -> None
```

**Data model: `MergeRequestDetails`**
```python
@dataclass
class MergeRequestDetails:
    mr_id: int
    title: str
    source_branch: str
    target_branch: str
    author: str
    changed_files: list[str]
    dockerfile_path: str | None
    ci_config_path: str
    has_llm_components: bool  # detected from diff scan
```

### 10.4 Milestone 1D — Dynatrace MCP Integration

**File:** `orchestrator/tools/dynatrace_tool.py`

Implement wrapper functions over the Dynatrace MCP for all operations defined in Section 4.2.

Key function signatures:
```python
async def get_service_baseline(service_name: str) -> ServiceBaseline
async def get_production_requests(service_name: str, hours: int = 24, limit: int = 200) -> list[ProductionRequest]
async def get_service_topology(service_name: str) -> ServiceTopology
async def has_active_incident(service_name: str) -> bool
```

**Data model: `ServiceBaseline`**
```python
@dataclass
class ServiceBaseline:
    service_name: str
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    error_rate_percent: float
    throughput_rpm: float
    slo_latency_budget_ms: float
    slo_error_budget_percent: float
    recorded_at: datetime
```

**Data model: `ProductionRequest`**
```python
@dataclass
class ProductionRequest:
    method: str           # GET, POST, etc.
    path: str             # /api/users/{id} → anonymized
    headers: dict         # Authorization header stripped
    body_schema: dict     # Body with PII values replaced by type-accurate fakes
    expected_status: int  # What production returned
    expected_p95_ms: float
```

**PII Anonymization rules (MUST apply before any replay):**
- Email addresses → `user_<hash>@example.com`
- Bearer tokens → `Bearer mock_token_<hash>`
- UUIDs in paths → `00000000-0000-0000-0000-<sequential_number>`
- Phone numbers → `+1555000<sequential>`
- Numeric IDs in paths → sequential integers starting from 1001

### 10.5 Milestone 1E — Ephemeral Environment Builder

**File:** `environment-builder/builder.py`

```python
async def provision_environment(simulation_id: str, source_branch: str, project_id: str) -> EphemeralEnvironment:
    # 1. Clone the repository at source_branch
    # 2. Build Docker image: Bulwark-sim-<simulation_id>
    # 3. Push to Artifact Registry
    # 4. Deploy to Cloud Run with:
    #    - unique service name: Bulwark-sim-<simulation_id>
    #    - environment variables: Bulwark_MODE=simulation, all real credentials replaced with mocks
    #    - max-instances: 1 (no scaling during simulation)
    #    - timeout: 600s
    # 5. Start WireMock server for dependencies (separate Cloud Run instance)
    # 6. Health check: GET /health → must return 2xx within 90s
    # 7. Return EphemeralEnvironment object

async def teardown_environment(simulation_id: str) -> None:
    # Delete Cloud Run service: Bulwark-sim-<simulation_id>
    # Delete Cloud Run service: Bulwark-mock-<simulation_id>
    # Delete Docker image from Artifact Registry
    # Log teardown completion
```

**Data model: `EphemeralEnvironment`**
```python
@dataclass
class EphemeralEnvironment:
    simulation_id: str
    base_url: str          # https://Bulwark-sim-<id>-<hash>-uc.a.run.app
    mock_base_url: str     # WireMock instance URL
    build_duration_ms: int
    deployed_at: datetime
    status: str            # healthy | build_failed | health_check_failed
```

### 10.6 Milestone 1F — Traffic Replay Engine

**File:** `traffic-replay/replayer.py`

```python
async def replay_traffic(
    environment: EphemeralEnvironment,
    requests: list[ProductionRequest],
    baseline: ServiceBaseline,
    concurrency: int = 10
) -> ReplayResult:
    # 1. For each request in parallel (up to concurrency limit):
    #    - Send request to environment.base_url + request.path
    #    - Record: actual_status_code, actual_latency_ms, actual_body_hash, error_message
    # 2. Aggregate results:
    #    - actual_p50, actual_p95, actual_p99 latency
    #    - actual_error_rate = (5xx_count / total_requests) * 100
    #    - novel_errors = error messages not seen in production baseline logs
    #    - response_shape_changes = responses where body structure differs from expected
    # 3. Compute deltas vs baseline
    # 4. Return ReplayResult
```

**Data model: `ReplayResult`**
```python
@dataclass
class ReplayResult:
    simulation_id: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    actual_p50_ms: float
    actual_p95_ms: float
    actual_p99_ms: float
    actual_error_rate_percent: float
    latency_delta_p95_ms: float      # actual - baseline
    error_rate_delta_percent: float  # actual - baseline
    novel_error_messages: list[str]
    response_shape_changes: list[str]
    five_xx_count: int
    replay_duration_ms: int
```

### 10.7 Milestone 1G — Gemini Reasoning (Basic)

**File:** `orchestrator/reasoning.py`

Phase 1 implements the core Gemini reasoning call with a structured prompt.

**System prompt for Gemini:**
```
You are Bulwark, a production safety agent. Your job is to evaluate whether a code change is safe to deploy to production based on simulation evidence.

You will receive:
1. A summary of the code changes (diff summary)
2. Simulation results: latency deltas, error rate deltas, novel errors
3. Production SLO targets

Your output MUST be a JSON object with this exact schema:
{
  "infrastructure_score": <0-100>,
  "reliability_score": <0-100>,
  "overall_score": <0-100>,
  "verdict": <"APPROVED" | "WARNING" | "BLOCKED">,
  "summary": <one sentence verdict>,
  "evidence": [<list of specific evidence items that informed your scoring>],
  "recommendation": <specific actionable advice for the developer>
}

Scoring rules:
- infrastructure_score: 100 if p95 latency delta < +20ms AND error_rate_delta < +0.5%. Deduct 10 points per additional 10ms latency regression. Deduct 20 points per 0.5% additional error rate regression.
- reliability_score: 100 if zero novel errors and zero 5xx responses. Deduct 30 points per novel error pattern. Deduct 50 points if 5xx_count > 0.
- overall_score: weighted average (infrastructure: 40%, reliability: 60%)
- verdict: APPROVED if overall >= 80, WARNING if 50-79, BLOCKED if < 50
```

### 10.8 Milestone 1H — Verdict Writer & MR Enforcement

**File:** `verdict-writer/writer.py`

**MR comment template:**

```markdown
## 🛡️ Bulwark Production Readiness Report

**Verdict:** ✅ APPROVED / ⚠️ WARNING / 🚫 BLOCKED
**Confidence Score:** 87/100
**Simulation ID:** sim_abc123 | [View Full Report](#)

---

### Simulation Results

| Metric | Production Baseline | Simulation Result | Delta | Status |
|--------|-------------------|-------------------|-------|--------|
| p95 Latency | 142ms | 156ms | +14ms | ✅ Within budget |
| Error Rate | 0.12% | 0.18% | +0.06% | ✅ Within budget |
| 5xx Responses | 0 | 0 | — | ✅ None |
| Novel Errors | — | 0 | — | ✅ None |

### Gemini's Reasoning
> The latency regression of +14ms on the p95 is within the 20ms SLO budget. No novel error patterns were observed. The change in the authentication middleware introduces a minor overhead but does not degrade reliability.

### Recommendation
> Safe to merge. Monitor p95 latency post-deploy — if it exceeds 160ms in production, consider caching the JWT verification result.

---
*Bulwark evaluated 200 production requests in an isolated ephemeral environment. Production was not touched during this evaluation.*
*Override: Add the `Bulwark-override` label to bypass this gate.*
```

**Phase 1 Deliverable:** Complete end-to-end flow. Open a test MR → Bulwark posts verdict within 10 minutes. No dashboard yet.

---

## 11. Phase 2 — Intelligence & Simulation Depth

**Goal:** Add Arize LLM evaluation, improve environment fidelity, add stress testing, and make the agent smarter with historical context from MongoDB.

**Duration estimate:** 2–3 days of focused development

### 11.1 Milestone 2A — Arize MCP Integration

**File:** `orchestrator/tools/arize_tool.py`

Implement the full Arize integration as defined in Section 4.3.

```python
async def get_llm_baseline(space_id: str, model_id: str) -> LLMBaseline
async def evaluate_simulation_outputs(
    simulation_outputs: list[LLMOutput],
    baseline: LLMBaseline
) -> ArizeEvalResult
```

**LLM Component Detection:**

Scan the diff for these patterns to determine if Arize eval is needed:
```python
LLM_SIGNATURES = [
    "openai.chat.completions",
    "anthropic.messages",
    "genai.GenerativeModel",
    "vertexai.generative_models",
    "langchain",
    "llamaindex",
    "from openai import",
    "import anthropic",
    "prompt_template",
    "system_prompt",
]
```

**Data model: `ArizeEvalResult`**
```python
@dataclass
class ArizeEvalResult:
    simulation_id: str
    llm_detected: bool
    baseline_hallucination_score: float    # 0.0 = no hallucination, 1.0 = all hallucinated
    simulation_hallucination_score: float
    baseline_relevance_score: float        # 0.0 = irrelevant, 1.0 = fully relevant
    simulation_relevance_score: float
    baseline_groundedness_score: float
    simulation_groundedness_score: float
    hallucination_delta: float
    relevance_delta: float
    groundedness_delta: float
    llm_quality_score: float               # 0-100, computed by Bulwark
```

**LLM quality score formula:**
```
llm_quality_score = 100
  - (hallucination_delta * 50)    # Hallucination regression is heavily penalized
  - (abs(relevance_delta) * 30)   # Relevance regression is moderately penalized
  - (abs(groundedness_delta) * 20)
```

### 11.2 Milestone 2B — Updated Gemini Reasoning (With LLM Scores)

Update the Gemini prompt in `orchestrator/reasoning.py` to include the `llm_quality_score` when Arize data is available.

**Updated scoring rules:**
- If `llm_detected = True`: `overall_score = infrastructure_score * 0.30 + reliability_score * 0.40 + llm_quality_score * 0.30`
- If `llm_detected = False`: `overall_score = infrastructure_score * 0.40 + reliability_score * 0.60` (Phase 1 formula)

The Gemini prompt MUST include the Arize evidence in the `evidence` array of the output.

### 11.3 Milestone 2C — Historical Context from MongoDB

Bulwark gets smarter over time by learning from past simulations.

**File:** `orchestrator/history.py`

Before the Gemini reasoning step, fetch:
- The last 5 simulations for the same service → what was their verdict? What patterns were found?
- The average production baseline drift over the last 30 days for this service
- Any MRs that were `BLOCKED` and later overridden → what happened in production after the override?

Inject this context into the Gemini reasoning prompt:
```
Historical context for service 'auth-service':
- Last 5 simulations: 4 APPROVED, 1 BLOCKED (blocker was a memory leak in v2.3.1 — developer overrode, caused P2 incident)
- Service has shown a consistent +5ms baseline latency increase per month (seasonal trend)
- No LLM regression has ever been observed in this service
```

This gives Gemini the ability to say things like:
> "This service has a known latency growth trend. The +14ms delta may partially be attributable to this trend rather than the MR change itself."

### 11.4 Milestone 2D — Stress Test Mode

Add an optional high-concurrency replay mode for MRs that touch performance-critical paths (database queries, caching layers, authentication middleware).

**Trigger:** If the diff modifies files matching any of: `**/database/**`, `**/cache/**`, `**/auth/**`, `**/middleware/**`

**Stress test:** Replay the 200 production requests at 50x concurrency instead of 10x. Measure peak latency and whether the service crashes under load.

Add `stress_test_result` field to `ReplayResult`:
```python
stress_test_result: StressTestResult | None

@dataclass
class StressTestResult:
    concurrency_level: int
    peak_p99_latency_ms: float
    crash_detected: bool
    max_error_rate_percent: float
    requests_per_second_at_peak: float
```

### 11.5 Milestone 2E — Dependency Fidelity Improvement

In Phase 1, WireMock stubs return static responses. In Phase 2, improve stubs to return production-accurate responses.

**Approach:** Use Dynatrace trace data to extract real response shapes from downstream services, then configure WireMock to return those exact shapes (with PII stripped).

This makes the simulation significantly more realistic — the new code now interacts with mocks that behave like real dependencies.

**File:** `environment-builder/mock_builder.py`

```python
async def build_dependency_mocks(
    topology: ServiceTopology,
    dynatrace_traces: list[ServiceTrace]
) -> list[WireMockStub]:
    # For each downstream dependency in topology:
    # 1. Find matching traces in dynatrace_traces
    # 2. Extract request/response pairs
    # 3. Strip PII from response bodies
    # 4. Generate WireMock stub JSON
    # Return list of stubs to configure
```

**Phase 2 Deliverable:** Bulwark now evaluates LLM-powered services via Arize, uses historical context for smarter reasoning, and can detect performance failures under load. The confidence score is now multi-dimensional and significantly more accurate.

---

## 12. Phase 3 — Confidence Scoring, UI & Polish

**Goal:** Build the Bulwark Dashboard, self-monitoring (Bulwark monitors itself with Arize + Dynatrace), polish the MR comment format, optimize performance, and prepare the hackathon demo.

**Duration estimate:** 2 days of focused development

### 12.1 Milestone 3A — Bulwark Dashboard (Next.js)

**File:** `dashboard/`

A web dashboard that shows the history of all simulations, trend data, and allows drilling into any verdict.

**Pages:**

**`/` — Overview Dashboard**
- Total simulations run (last 30 days)
- APPROVED / WARNING / BLOCKED breakdown (donut chart)
- Average confidence score over time (line chart)
- Recent simulation activity feed

**`/simulations` — Simulation List**
- Table: Simulation ID · MR Title · Service · Verdict · Score · Duration · Date
- Filter by: verdict status, service name, date range, author
- Click row → drill into simulation detail

**`/simulations/[id]` — Simulation Detail**
- Full verdict report
- Metric delta table (same as MR comment but interactive)
- Latency comparison chart: production baseline vs simulation results (Recharts line chart)
- Arize eval scores (if LLM service): hallucination, relevance, groundedness comparison bars
- Gemini's full reasoning text
- Raw evidence JSON (expandable)
- Historical context that was injected into Gemini's reasoning

**`/services/[name]` — Service Profile**
- Simulation history for this service
- Confidence score trend over time
- Common failure patterns
- Override history (how many times humans overrode BLOCKED verdicts)

**API routes (Next.js API routes):**
```
GET /api/simulations?status=&service=&limit=&page=
GET /api/simulations/[id]
GET /api/services
GET /api/services/[name]/history
GET /api/stats/overview
```

All API routes read from MongoDB Atlas.

### 12.2 Milestone 3B — Self-Monitoring (Bulwark Monitors Itself)

**Narrative hook for the demo:** "Bulwark uses the exact same tools it uses to evaluate your code to evaluate itself."

**Implementation:**

1. Log every Gemini reasoning call to Arize as a trace
   - Input: the structured evidence payload
   - Output: the verdict JSON
   - Evaluate: is the verdict reasoning grounded? Is it hallucinating evidence that wasn't in the input?

2. Monitor Bulwark's own Cloud Run service with Dynatrace
   - Set SLOs for Bulwark: p95 webhook response < 500ms, total simulation time < 5 minutes
   - Alert if Bulwark itself becomes unreliable

3. Add a "Bulwark Health" widget to the dashboard showing:
   - Bulwark's own uptime
   - Bulwark's average reasoning quality score (from Arize)
   - Bulwark's own latency metrics (from Dynatrace)

### 12.3 Milestone 3C — Performance Optimization

Target: total simulation time under 3 minutes for services with fewer than 5 dependencies.

**Optimizations:**
- Pre-warm a pool of base Docker images for common frameworks (Node.js, Python FastAPI, Java Spring Boot) to eliminate build time for most services
- Parallelize Dynatrace baseline fetch and Arize baseline fetch — they are independent
- Parallelize environment build and dependency mock preparation — they are independent
- Use streaming logs for Cloud Run deployment to detect health check success earlier

**Updated timing target:**
```
Webhook receipt → Pub/Sub publish:         < 0.2s
Pub/Sub → agent start:                     < 5s
GitLab MR analysis:                        < 10s
Dynatrace + Arize baseline fetch:          < 15s (parallel)
Environment build + mock setup:            < 60s (parallel)
Traffic replay (200 requests):             < 60s
Arize eval (if LLM):                       < 20s
Gemini reasoning:                          < 10s
MongoDB write + MR comment post:           < 5s
Teardown (async, non-blocking):            < 30s
─────────────────────────────────────────
Total (blocking path):                     ~3 minutes
```

### 12.4 Milestone 3D — Demo Preparation

Prepare a polished end-to-end demo for the hackathon video.

**Demo scenario (detailed in Section 17):**

1. Show the dashboard with clean history
2. Open a GitLab MR that introduces a subtle performance regression (add a synchronous database call inside a loop — classic N+1 query)
3. Show the Bulwark webhook fire in real-time logs
4. Watch the simulation spin up in Google Cloud console (live)
5. Watch the verdict post to the MR — BLOCKED, 43/100 confidence, with specific evidence: "p95 latency regression of +187ms detected — exceeds SLO budget of +20ms"
6. Show the dashboard simulation detail page with the latency chart
7. Fix the N+1 query in a new commit, push to the same MR
8. Watch Bulwark re-evaluate — APPROVED, 91/100
9. Show the MR is now unblocked and ready to merge
10. Narrate: "Production never saw either version of the bad code"

### 12.5 Milestone 3E — README & Submission Prep

**README.md must include:**
- One-line description
- Problem statement (2 sentences)
- Architecture diagram (text or image)
- How to run locally (docker-compose up)
- How to configure the three MCP integrations
- How to configure the GitLab webhook
- Screenshots of the dashboard and a sample MR comment
- Link to the demo video

**Open source license:** MIT License (must be detectable at repo root and in GitHub About section)

**Phase 3 Deliverable:** Bulwark is fully polished, has a live dashboard, self-monitors with the sponsor tools, and has a compelling 3-minute demo ready for submission.

---

## 13. Data Models

### MongoDB Collection: `simulations`

```json
{
  "_id": "sim_abc123",
  "mr_id": 456,
  "project_id": "mygroup/myrepo",
  "source_branch": "feature/new-auth",
  "target_branch": "main",
  "service_name": "auth-service",
  "status": "completed",
  "created_at": "2026-06-05T10:00:00Z",
  "completed_at": "2026-06-05T10:03:12Z",
  "total_duration_ms": 192000,
  "environment": {
    "base_url": "https://Bulwark-sim-abc123-uc.a.run.app",
    "build_duration_ms": 54000,
    "status": "healthy"
  },
  "dynatrace": {
    "baseline": {
      "p50_latency_ms": 45,
      "p95_latency_ms": 142,
      "p99_latency_ms": 310,
      "error_rate_percent": 0.12,
      "throughput_rpm": 3200
    },
    "active_incident_at_start": false,
    "requests_sampled": 200,
    "replay_result": {
      "actual_p50_ms": 48,
      "actual_p95_ms": 329,
      "actual_p99_ms": 601,
      "actual_error_rate_percent": 0.18,
      "latency_delta_p95_ms": 187,
      "error_rate_delta_percent": 0.06,
      "five_xx_count": 0,
      "novel_error_messages": ["ConnectionPool timeout after 500ms"],
      "stress_test": {
        "concurrency_level": 50,
        "peak_p99_latency_ms": 2100,
        "crash_detected": false,
        "max_error_rate_percent": 3.4
      }
    }
  },
  "arize": {
    "llm_detected": false,
    "eval_result": null
  },
  "verdict": {
    "infrastructure_score": 22,
    "reliability_score": 70,
    "llm_quality_score": null,
    "overall_score": 43,
    "verdict": "BLOCKED",
    "summary": "Significant p95 latency regression of +187ms detected, exceeding the 20ms SLO budget by 9x.",
    "evidence": [
      "p95 latency increased from 142ms to 329ms (+187ms)",
      "Novel error pattern detected: ConnectionPool timeout after 500ms",
      "Stress test shows p99 degradation to 2100ms at 50x concurrency",
      "Error rate delta (+0.06%) is within budget"
    ],
    "recommendation": "Review database query patterns in the modified authentication middleware. The ConnectionPool timeout suggests a possible N+1 query or missing index. Run EXPLAIN ANALYZE on queries added in this MR.",
    "gemini_model": "gemini-2.0-flash",
    "reasoning_duration_ms": 2100
  },
  "gitlab": {
    "comment_id": 789,
    "pipeline_status": "failed",
    "override_applied": false
  }
}
```

### MongoDB Collection: `environment_configs`

```json
{
  "_id": "auth-service",
  "project_id": "mygroup/myrepo",
  "dockerfile_path": "services/auth/Dockerfile",
  "health_check_path": "/health",
  "health_check_timeout_s": 90,
  "min_cpu": "1",
  "min_memory": "512Mi",
  "env_vars_to_mock": ["DATABASE_URL", "REDIS_URL", "JWT_SECRET"],
  "stress_test_paths": ["**/database/**", "**/auth/**"],
  "llm_signature_paths": ["**/prompts/**"],
  "last_updated": "2026-06-01T00:00:00Z"
}
```

---

## 14. API Contracts

### Webhook Receiver

```
POST /webhook/gitlab
Headers:
  X-Gitlab-Token: <secret>
  Content-Type: application/json
Body: GitLab MR webhook payload
Response: 200 OK {"status": "queued", "simulation_id": "sim_abc123"}
         400 Bad Request {"error": "invalid signature"}
         422 Unprocessable {"error": "unsupported event type"}

GET /health
Response: 200 OK {"status": "ok", "version": "1.0.0"}
```

### Dashboard API

```
GET /api/simulations
Query: ?status=BLOCKED&service=auth-service&limit=20&page=1
Response: {
  "simulations": [...],
  "total": 145,
  "page": 1,
  "pages": 8
}

GET /api/simulations/:id
Response: Full simulation document from MongoDB

GET /api/stats/overview
Response: {
  "total_simulations_30d": 312,
  "approved_count": 245,
  "warning_count": 48,
  "blocked_count": 19,
  "average_confidence_score": 81.4,
  "average_simulation_duration_ms": 187000,
  "production_incidents_prevented": 19
}
```

---

## 15. Error Handling & Edge Cases

| Scenario | Handling |
|---|---|
| GitLab webhook timeout (>10s) | Pub/Sub decouples receipt from processing — 200 returned immediately; processing continues async |
| Dynatrace API rate limit | Retry with exponential backoff (3 retries, max 30s wait); if all fail, proceed without production baseline and note in verdict |
| Docker build fails (syntax error in Dockerfile) | Mark simulation as `BUILD_FAILED`; post MR comment: "Bulwark could not build your code. Please check the Dockerfile for errors." |
| Ephemeral environment crashes on startup | Detect via health check failure; mark as `RUNTIME_CRASH`; post comment with the crash log snippet |
| Arize API unavailable | Skip LLM evaluation; note in verdict "LLM evaluation was skipped due to Arize API unavailability" |
| MongoDB write fails | Log to Cloud Logging; still post MR comment (verdict is not lost); retry MongoDB write 3 times |
| Gemini API timeout | Retry once; if second attempt fails, post a simplified verdict based on raw metric thresholds without AI reasoning |
| Service not instrumented in Dynatrace | Skip Dynatrace step; run simulation with synthetic test requests only; note in verdict |
| Active P1 incident in production | Defer evaluation; post comment: "Evaluation deferred: active P1 incident on this service. Bulwark will re-evaluate when the incident is resolved." |
| MR only changes documentation | Skip entirely; do not post any comment (avoid noise) |
| Simulation takes > 15 minutes | Force teardown; post comment: "Bulwark simulation timed out after 15 minutes. Please check if the service builds and starts correctly." |
| Developer overrides BLOCKED verdict | Log override in MongoDB with timestamp and author; add a warning to the next simulation's historical context |

---

## 16. Security Considerations

- **No production credentials in ephemeral environments.** All real credentials (`DATABASE_URL`, `JWT_SECRET`, `API_KEY`) are replaced with mock values. The `environment_configs` collection defines which env vars to mock per service.
- **No outbound internet from ephemeral environments.** Cloud Run VPC connector restricts egress — ephemeral environments can only reach WireMock stubs and Bulwark's control plane.
- **PII anonymization before any replay.** All production request logs are anonymized before being stored in MongoDB or sent to any external system.
- **Webhook signature validation.** All GitLab webhooks are validated against the stored `GITLAB_WEBHOOK_TOKEN` secret before processing.
- **Least-privilege IAM.** The `Bulwark-orchestrator` service account has only the permissions it needs — no Owner role, no Editor role.
- **Secrets in Secret Manager.** No credentials in environment variables, code, or Docker images. All secrets are fetched at runtime from Google Cloud Secret Manager.
- **Simulation isolation.** Each simulation runs in its own Cloud Run instance with a unique service name. No two simulations share resources.
- **Image cleanup.** Docker images built for simulations are deleted from Artifact Registry immediately after teardown.

---

## 17. Demo Scenario

This is the exact scenario to build and rehearse for the 3-minute hackathon video.

### Setup
- Repository: `demo-app` — a simple Python FastAPI service with a `/users` endpoint backed by a PostgreSQL database (mocked by WireMock in simulation)
- The service has Dynatrace instrumentation with 30 days of production history
- The dashboard shows 10 prior APPROVED simulations for `demo-app`

### Step 1 — Introduce a bug (0:00 – 0:30)
Open the code. Show the `GET /users` endpoint. It currently fetches all users in a single query. Make a "small change": add a loop that fetches user preferences for each user in a separate query (classic N+1).

Commit to a branch, open a Merge Request in GitLab.

### Step 2 — Watch Bulwark activate (0:30 – 1:00)
Switch to the Google Cloud console. Show the Pub/Sub message arrive. Show the Cloud Run container spinning up for `Bulwark-sim-abc123`. Show logs streaming: "Fetching Dynatrace baseline... Provisioning ephemeral environment... Replaying 200 production requests..."

### Step 3 — The verdict (1:00 – 1:45)
Switch to GitLab. The MR now has a Bulwark comment. Show the verdict:
```
🚫 BLOCKED — 43/100 confidence
p95 latency regression: +187ms (9x over SLO budget)
Novel error detected: ConnectionPool timeout after 500ms
```
The pipeline status shows `Bulwark/production-ready: failed`. The Merge button is greyed out.

### Step 4 — Show the evidence (1:45 – 2:00)
Open the dashboard simulation detail. Show the latency comparison chart — production baseline vs simulation. Show the spike. Show the Gemini reasoning paragraph pointing to the N+1 query pattern.

### Step 5 — Fix the bug (2:00 – 2:30)
Back to the code. Fix the N+1 query — use a single JOIN query instead of the loop. Push the fix to the same branch.

### Step 6 — Approved (2:30 – 3:00)
Bulwark re-evaluates automatically. New verdict:
```
✅ APPROVED — 91/100 confidence
p95 latency: +3ms (within budget)
Zero novel errors
```
Pipeline status: `Bulwark/production-ready: success`. MR is now mergeable.

Final screen: "Production never saw the N+1 query. Not for a single request."

---

## 18. Success Metrics

| Metric | Target | How Measured |
|---|---|---|
| End-to-end simulation time | < 5 minutes | `completed_at - created_at` in MongoDB |
| False negative rate (APPROVED code that later causes prod incident) | < 5% | Manual tracking post-hackathon |
| False positive rate (BLOCKED code that was actually safe) | < 15% | Manual review of overridden verdicts |
| Ephemeral environment cost per simulation | < $0.05 | GCP billing |
| Verdict confidence score correlation with actual production outcomes | > 0.75 Pearson | Post-hackathon analysis |
| System uptime during hackathon evaluation period | > 99% | Cloud Run uptime monitoring |

---

## 19. Deliverables Checklist

### Phase 1 Deliverables
- [ ] GCP infrastructure provisioned via Terraform
- [ ] MongoDB Atlas cluster with collections and indexes
- [ ] Webhook receiver deployed to Cloud Run, receiving GitLab events
- [ ] GitLab MCP integration — MR read + comment write + pipeline status
- [ ] Dynatrace MCP integration — baseline fetch + request log retrieval
- [ ] Ephemeral environment provisioner (Docker build + Cloud Run deploy)
- [ ] WireMock dependency mocking (static stubs)
- [ ] Traffic replay engine — 200 requests, metric aggregation
- [ ] PII anonymization for replay requests
- [ ] Gemini reasoning step — infrastructure + reliability score
- [ ] Verdict writer — formatted MR comment + pipeline gate
- [ ] End-to-end smoke test: MR open → verdict posted in < 10 minutes

### Phase 2 Deliverables
- [ ] Arize MCP integration — LLM baseline + eval run
- [ ] LLM component detection from diff
- [ ] Updated Gemini reasoning with LLM quality score
- [ ] Historical context retrieval from MongoDB
- [ ] Stress test mode for performance-critical paths
- [ ] Production-accurate WireMock stubs from Dynatrace traces
- [ ] End-to-end test with LLM service: Arize scores appear in verdict

### Phase 3 Deliverables
- [ ] Next.js dashboard deployed to Vercel
- [ ] Overview page with simulation stats
- [ ] Simulation list with filters
- [ ] Simulation detail with latency chart and Arize scores
- [ ] Service profile page
- [ ] Self-monitoring: Bulwark's Gemini calls logged to Arize
- [ ] Self-monitoring: Bulwark's Cloud Run monitored by Dynatrace
- [ ] Performance optimization: total time < 3 minutes
- [ ] README with setup guide and screenshots
- [ ] MIT License at repo root
- [ ] Demo scenario rehearsed and recorded (3-minute video)
- [ ] Devpost submission form completed

---

*Document version: 1.0 · Project: Bulwark · Hackathon: Google Cloud Rapid Agent Hackathon 2026*
*Sponsors: GitLab · Dynatrace · Arize · Platform: Google Cloud Agent Builder · Model: Gemini 2.0 Flash*
