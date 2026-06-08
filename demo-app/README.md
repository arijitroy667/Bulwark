# Bulwark Demo App

> Minimal FastAPI service used to demonstrate Bulwark's N+1 regression detection (PRD §18).

## What This Does

This service has two versions controlled by the `DEMO_VERSION` environment variable:

| Version | Behaviour | Expected Bulwark Verdict |
|---|---|---|
| `regression` | `GET /users` makes one HTTP call **per user** (N+1 pattern) | 🚫 **BLOCKED** — p95 +187ms |
| `fixed` (default) | `GET /users` makes a **single batched call** | ✅ **APPROVED** — p95 +3ms |

## Demo Scenario (PRD §18)

```
1. Open MR with DEMO_VERSION=regression → Bulwark posts BLOCKED (43/100)
2. Fix MR with DEMO_VERSION=fixed      → Bulwark posts APPROVED (91/100)
```

## Local Development

```bash
cd demo-app
pip install -r requirements.txt

# Run the regression version (triggers BLOCKED)
DEMO_VERSION=regression uvicorn main:app --port 8080

# Run the fixed version (triggers APPROVED)
DEMO_VERSION=fixed uvicorn main:app --port 8080
```

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Health check — required by Cloud Run and Bulwark |
| `GET /users` | List users with preferences (regression hotspot) |
| `GET /users/{id}` | Fetch single user |
| `GET /metrics` | App metrics |

## WireMock Stubs Expected

The demo app calls these endpoints on the WireMock URL:

| Endpoint | Used by |
|---|---|
| `GET /api/preferences/{id}` | N+1 regression path (one call per user) |
| `GET /api/preferences/batch` | Fixed path (single batched call) |
