# Bulwark Demo App

> Minimal FastAPI service used to demonstrate Bulwark's N+1 regression detection (PRD §18).

## What This Does

`DEMO_VERSION` selects which "commit" the process runs:

| Version | Behaviour | Bulwark verdict |
|---|---|---|
| `fixed` (default, = production) | `GET /users` makes **one batched** preferences call | ✅ APPROVED |
| `regression` | `GET /users` makes **one call per user** (N+1, 25 round trips) | 🚫 BLOCKED |
| `llm-regression` | ticket-summary prompt edited; answers over-promise | LLM quality drop |

Bulwark (demo mode) launches this app itself — once as the production build to measure
the baseline, once as the MR build — and loads WireMock stubs with a 12 ms delay per
preferences call. Each `GET /tickets/{id}/summary` records a `{prompt, response, context}`
trace that Bulwark reads from `GET /traces` for LLM evaluation. The LLM call is a
deterministic stand-in so runs are reproducible.

## Local Development

```bash
cd demo-app
pip install -r requirements.txt
DEMO_VERSION=regression WIREMOCK_BASE_URL=http://127.0.0.1:8082 uvicorn main:app --port 8080
```

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Health check — required by Cloud Run and Bulwark |
| `GET /users` | List users with preferences (regression hotspot) |
| `GET /users/{id}` | Fetch single user |
| `GET /metrics` | App metrics |
| `GET /tickets/{id}/summary` | LLM-backed ticket summary (records a trace) |
| `GET /traces` | Traces captured since start |

## WireMock Stubs Expected

The demo app calls these endpoints on the WireMock URL:

| Endpoint | Used by |
|---|---|
| `GET /api/preferences/{id}` | N+1 regression path (one call per user) |
| `GET /api/preferences/batch` | Fixed path (single batched call) |
