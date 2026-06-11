# 🛡️ Bulwark
**Pre-Deployment Production Simulation Agent**

[![Google Cloud](https://img.shields.io/badge/Google%20Cloud-Agent%20Builder-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/agent-builder)
[![GitLab](https://img.shields.io/badge/GitLab-MCP%20Integration-FC6D26?logo=gitlab&logoColor=white)](https://gitlab.com)
[![Dynatrace](https://img.shields.io/badge/Dynatrace-MCP%20Integration-1496FF?logo=dynatrace&logoColor=white)](https://dynatrace.com)
[![Arize](https://img.shields.io/badge/Arize-LLM%20Evaluator-7C3AED)](https://arize.com)
[![Model](https://img.shields.io/badge/Model-Gemini%202.0%20Flash-EA4335?logo=google&logoColor=white)](https://deepmind.google/technologies/gemini/)

Bulwark is an AI-powered, automated pre-merge gate that tests every GitLab Merge Request against real production conditions. It catches infrastructure regressions and LLM hallucinations before your code ever reaches production.

---

## 🌟 Why Bulwark? (The Problem It Solves)
Production failures from pre-deploy code changes cost engineering teams 2.5+ hours of P1 incident response. For AI applications, quality regressions from prompt changes go undetected for days because there is no automated pre-merge gate. 

**Bulwark solves this by:**
- Spinning up an ephemeral replica of the modified service.
- Replaying real (anonymised) production traffic against it.
- Analyzing both **infrastructure signals** (latency, error rates) and **LLM quality signals** (hallucinations, groundedness).
- Assigning a strict 0-100 confidence score to automatically approve or block the Merge Request.

---

## 🛠️ Technology Stack & How We Use It

Bulwark integrates cutting-edge technologies to provide a seamless simulation pipeline:

- **Google Cloud Agent Builder & Gemini 2.0 Flash:** Acts as the brain of Bulwark. It orchestrates the 9-step simulation pipeline and performs complex reasoning on the resulting metrics to generate a JSON verdict.
- **GitLab MCP:** Fetches Merge Request metadata, diffs, and posts the final verdict comment directly to the developer's PR.
- **Dynatrace MCP:** Establishes the production baseline (p50/p95 latency, error rates, SLO targets) and provides the anonymised traffic sample for replay.
- **Arize MCP:** Automatically triggered if LLM components are detected. Runs evaluators to catch hallucination, relevance, and groundedness regressions in prompt changes.
- **FastAPI, Cloud Run, Pub/Sub:** Handles the event-driven architecture. Webhooks are ingested by a FastAPI service on Cloud Run, which publishes to Pub/Sub to asynchronously trigger the orchestrator.
- **React & Next.js:** Powers the interactive web dashboard for real-time monitoring of simulation progress and historical verdicts.
- **Terraform:** Automates the provisioning of the entire Google Cloud infrastructure.

---

## 🏗️ System Architecture

```text
┌─────────────┐   Webhook    ┌──────────────────┐   Pub/Sub   ┌───────────────────────────┐
│   GitLab    ├─────────────►│ Webhook Receiver ├────────────►│   Bulwark Orchestrator    │
│  (MR open)  │              │    (Cloud Run)   │             │ (Gemini 2.0 Flash Agent)  │
└─────────────┘              └──────────────────┘             └─────────────┬─────────────┘
                                                                            │
      ┌───────────────────────────────┬─────────────────────────────────────┼──────────────────────────────┐
      ▼                               ▼                                     ▼                              ▼
┌───────────────┐             ┌───────────────┐                    ┌─────────────────┐            ┌─────────────────┐
│ Dynatrace MCP │             │   Arize MCP   │                    │ Environment     │            │ Traffic Replay  │
│ (Fetch prod   │             │ (Eval LLM     │                    │ Builder         │            │ Engine          │
│  baseline)    │             │  performance) │                    │ (Docker build & │            │ (Replay 200     │
└───────────────┘             └───────────────┘                    │  Cloud Run)     │            │  prod requests) │
                                                                   └─────────────────┘            └─────────────────┘
                                                                            │
                                                                            ▼
                                                              ┌───────────────────────────┐
                                                              │      Verdict Writer       │
                                                              │ (Posts Comment to GitLab) │
                                                              └───────────────────────────┘
```

**The 9-Step Flow:**
1. **INGEST:** Fetch MR metadata and run LLM component detection.
2. **BASELINE:** Concurrently fetch Dynatrace production metrics + Arize LLM baselines.
3. **HISTORY:** Inject the last 5 simulation verdicts from MongoDB to provide context.
4. **PROVISION:** Build the Docker image and deploy an ephemeral Cloud Run service with WireMock stubs.
5. **REPLAY:** Anonymise and replay 200 production requests against the ephemeral service.
6. **LLM EVAL:** Run Arize evaluators if LLM paths are detected.
7. **TEARDOWN:** Destroy the ephemeral service and artifacts.
8. **REASONING:** Gemini analyzes all evidence and deducts points to compute a 0-100 confidence score.
9. **ENFORCE:** Post the verdict comment to the GitLab MR and set the pipeline status.

---

## 🚀 Getting Started

### Prerequisites
- Docker + Docker Compose
- Google Cloud Project & Artifact Registry
- API Keys: Gemini, GitLab PAT, Dynatrace, Arize
- MongoDB Atlas (for historical context)

### 1. Local Setup
```bash
git clone https://gitlab.com/your-group/bulwark.git
cd bulwark
cp .env.example .env  # Fill in your secrets

# Start the dashboard and backend
npm run dev --prefix dashboard
```

### 2. Infrastructure Deployment
```bash
cd terraform
terraform init
terraform apply -var="project_id=YOUR_PROJECT" ...
```

### 3. GitLab Integration
- Navigate to **Settings → Webhooks** in your GitLab project.
- Set the URL to your deployed `bulwark-webhook-receiver`.
- Check **Merge request events**.

---

*Built for the Google Cloud Rapid Agent Hackathon 2026 · Sponsors: GitLab · Dynatrace · Arize · Google Cloud*
