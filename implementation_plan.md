# Bulwark Demo Interface — End-to-End GitLab → Verdict Flow

## The Ask

Build a public-facing demo interface where **any user** can:
1. Connect their GitLab account
2. Pick a repo
3. Open or simulate an MR
4. Watch Bulwark's entire 9-step pipeline run live
5. See the verdict appear — both in the UI and on the GitLab MR

---

## Brutal Analysis: What Is Actually Broken Right Now

Before recommending any interface, here is a hard look at what the current codebase can and cannot do for an end-to-end demo.

### ✅ What Already Works
| Component | Status |
|---|---|
| Webhook receiver (`/webhook/gitlab`) | Working. Validates token, enqueues to Mongo (dev) or Pub/Sub (prod) |
| Orchestrator agent (`agent.py`) | All 9 steps coded. Runs in dev mode against MongoDB queue. |
| MCP client adapter | Connects to real GitLab/Dynatrace/Arize via configured MCP URLs |
| MongoDB models + TTL | Schema complete |
| 310 unit + integration tests | All passing |
| Docker Compose stack | Brings up webhook, orchestrator, WireMock, Mongo |

### ❌ What Is Critically Missing or Broken for a Real Demo

**1. The webhook receiver is not reachable from the internet.**

The webhook receiver runs on `localhost:8080`. GitLab cannot send webhooks to localhost.
For the full flow to work, the webhook receiver **must be deployed to a public URL**.
Options (in order of speed):
- **Render.com** free tier — the `.env` already has a comment "needs to be deployed on render"
- **ngrok** tunnel (localhost:8080) — fastest for a demo, but fragile
- **Google Cloud Run** — the intended production target, but requires gcloud + terraform

> [!CAUTION]
> This is the single most critical missing piece. Without a public webhook URL, GitLab cannot trigger anything, and the entire demo is dead on arrival.

---

**2. The MCP URLs in `.env` are placeholder stubs.**

```
GITLAB_MCP_URL=https://gitlab-mcp-<hash>-<region>.a.run.app/sse
DYNATRACE_MCP_URL=https://dynatrace-mcp-<hash>-<region>.a.run.app/sse
ARIZE_MCP_URL=https://arize-mcp-<hash>-<region>.a.run.app/sse
```

These are not real URLs. The orchestrator will fail at Step 1 (INGEST) the moment it tries to call the GitLab MCP. The Docker Compose does spin up *local* FastMCP test servers (ports 8001/8002/8003), which the tests use — but those only serve fake data.

For a real demo, the orchestrator needs either:
- Real GitLab MCP URL pointing at a deployed GitLab MCP server (the official `gitlab-mcp` tool from GitLab), OR
- The direct GitLab REST API path (the `gitlab_tool.py` already calls GitLab REST API directly using `GITLAB_PAT`)

> [!IMPORTANT]
> Checking `orchestrator/tools/gitlab_tool.py` will clarify whether it calls MCP or REST. If it's REST-backed, this is NOT a blocker — the `GITLAB_PAT` in `.env` is real.

---

**3. Dynatrace and Arize: real credentials exist but service is not instrumented.**

The `.env` has real Dynatrace credentials (`qeo26399` env). But:
- The demo app is a Python FastAPI service running locally — it's **not instrumented in Dynatrace**
- This means `get_production_requests()` will return 0 samples
- The orchestrator will hit the `< MIN_REPLAY_REQUESTS (50)` guard → skip replay → no infra score
- The demo scenario from PRD §18 (N+1 query regression) will NOT produce the expected BLOCKED result

For a real demo, you need either:
- A service already instrumented in Dynatrace with >50 request samples, OR
- A **demo mode** that injects synthetic-but-realistic baseline data and request samples

---

**4. Cloud Run / environment builder cannot work locally.**

`environment_builder/builder.py` calls Cloud Build + Cloud Run APIs. Without real GCP credentials and `gcloud` auth, Step 4 (PROVISION) will fail immediately.

The `.env` has `GCP_PROJECT_ID=bulwark-498512`, but the `README.md` says:
> "gcloud and terraform CLI are not installed on the development machine"

This means the ephemeral environment step is completely non-functional locally.

---

**5. No user interface exists.**

There is no UI. Verdicts appear as GitLab MR comments. Simulation state lives in MongoDB. Pipeline logs go to stdout/Docker logs. 

For a demo to be compelling to any user, they need a dashboard that shows:
- Pipeline steps progressing in real time
- The final verdict rendered beautifully
- Live logs streaming

---

## Recommended Architecture: Demo Interface

### The Right Approach: A "Bulwark Dashboard" Web App

Given the above constraints, here is the recommended strategy for a demo that **actually works** end-to-end:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     BULWARK DEMO DASHBOARD                          │
│                (React/Next.js · Deployed on Render)                 │
│                                                                      │
│  ┌──────────────────┐  ┌────────────────────────────────────────┐   │
│  │  SETUP PANEL     │  │  LIVE PIPELINE VIEWER                  │   │
│  │                  │  │                                        │   │
│  │  GitLab PAT ──►  │  │  Step 1: INGEST          ✅ Done       │   │
│  │  GitLab URL ──►  │  │  Step 2: BASELINE        ⏳ Running   │   │
│  │  Project ID ──►  │  │  Step 3: LLM BASELINE    ⏸ Waiting   │   │
│  │  MR IID   ──►  │  │  Step 4: PROVISION       ⏸ Waiting   │   │
│  │                  │  │  Step 5: REPLAY          ⏸ Waiting   │   │
│  │  [▶ RUN DEMO]   │  │  Step 6: LLM EVAL        ⏸ Waiting   │   │
│  │                  │  │  Step 7: TEARDOWN        ⏸ Waiting   │   │
│  │  OR              │  │  Step 8: REASONING       ⏸ Waiting   │   │
│  │                  │  │  Step 9: ENFORCE         ⏸ Waiting   │   │
│  │  [⚡ QUICK DEMO] │  │                                        │   │
│  │   (pre-loaded)   │  │  Live Logs ──────────────────────────► │   │
│  └──────────────────┘  └────────────────────────────────────────┘   │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │                    VERDICT CARD                                │  │
│  │                                                                │  │
│  │  🚫 BLOCKED — 43/100                                           │  │
│  │  ┌──────────┬──────────┬──────────┬──────────┬─────────────┐  │  │
│  │  │ Metric   │ Baseline │ Sim      │ Delta    │ Status      │  │  │
│  │  ├──────────┼──────────┼──────────┼──────────┼─────────────┤  │  │
│  │  │ p95 ms   │ 142ms    │ 329ms    │ +187ms   │ 🚫 5.3x SLO │  │  │
│  │  │ Err Rate │ 0.12%    │ 0.18%    │ +0.06%   │ ✅ Noise    │  │  │
│  │  │ 5xx      │ 0        │ 0        │ —        │ ✅ None     │  │  │
│  │  │ Errors   │ 0        │ 1        │ +1       │ 🚫 Regress. │  │  │
│  │  └──────────┴──────────┴──────────┴──────────┴─────────────┘  │  │
│  └────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Proposed Changes

### Tier 0 — Unblock the Webhook (CRITICAL prerequisite)

#### [MODIFY] `.env` + Deployment
Deploy the webhook receiver to a public URL using one of:
- **Render.com** (free, instant) — `BULWARK_MODE=development`, `MONGODB_URI` points to Atlas (already configured)
- **ngrok** for a quick local demo tunnel

Set `GITLAB_WEBHOOK_TOKEN` to the value in `.env` and configure the GitLab project webhook to point at `https://<your-url>/webhook/gitlab`.

#### Verify MCP tool wiring
Check `orchestrator/tools/gitlab_tool.py` to confirm it calls GitLab REST API directly (not an MCP server URL). If so, the `GITLAB_PAT` in `.env` is sufficient and MCP URLs are only needed for Dynatrace/Arize.

---

### Tier 1 — Demo Mode (bypasses broken external deps for a reliable demo)

A **`BULWARK_DEMO_MODE=true`** env flag that:
- Injects a synthetic but realistic Dynatrace baseline (p95=142ms, error=0.12%) with 200 fake request samples
- Skips the real Cloud Run environment build — deploys the local `demo-app/` instead (already exists!)
- Uses demo-app's WireMock on port 8082 for mocked dependencies
- Still calls real Gemini for the reasoning step (adds credibility)
- Still posts real verdict to GitLab MR (the whole point)

This makes the demo **reliable** (no GCP dependency) while keeping 100% of the output authentic.

#### [NEW] `shared/demo_mode.py`
Synthetic request injection + demo baseline factory.

#### [MODIFY] `orchestrator/agent.py`
If `BULWARK_DEMO_MODE=true`:
- Skip `provision_environment()` → use `DEMO_APP_URL` (localhost:8080 or docker service) instead
- Skip Dynatrace call → inject `synthetic_baseline` + `synthetic_requests`

---

### Tier 2 — Dashboard Web App

#### [NEW] `dashboard/` — Next.js app

| File | Purpose |
|---|---|
| `dashboard/app/page.tsx` | Setup panel: GitLab PAT input, project URL, MR IID, or "Quick Demo" button |
| `dashboard/app/simulation/[id]/page.tsx` | Real-time pipeline viewer: WebSocket or polling against `/api/simulation/{id}` |
| `dashboard/app/api/simulate/route.ts` | Triggers simulation: calls webhook receiver `POST /webhook/gitlab` with synthetic payload |
| `dashboard/app/api/simulation/[id]/route.ts` | Polls MongoDB via backend and returns current status + step progression |

#### [NEW] `orchestrator/main.py` — Add `/api/simulation/{id}` endpoint
Returns simulation document from MongoDB with current step status, error log, and verdict. This is what the dashboard polls.

#### [NEW] SSE streaming endpoint `/api/simulation/{id}/stream`
Server-Sent Events endpoint that pushes status updates in real time (MongoDB change stream or polling).

---

### Tier 3 — Polish

#### GitLab webhook self-configuration
After user enters GitLab PAT + project ID in the dashboard, auto-configure the webhook:
- `POST /api/v4/projects/{id}/hooks` with the Bulwark webhook URL
- User only needs their PAT and project URL — no manual webhook setup

#### Pipeline log streaming
Attach structured log lines to each simulation step in MongoDB, surface them in the dashboard.

---

## Implementation Priority

| Priority | Item | Effort | Impact |
|---|---|---|---|
| 🔴 P0 | Deploy webhook receiver to public URL (Render/ngrok) | 15 min | Demo-blocking |
| 🔴 P0 | Verify GitLab tool uses REST (not MCP URLs) | 5 min | Demo-blocking |
| 🔴 P1 | `BULWARK_DEMO_MODE` — bypass GCP/Dynatrace for reliable demo | 2h | Makes demo fully reliable |
| 🟡 P2 | Dashboard web app — setup panel + pipeline viewer + verdict card | 4h | WOW factor |
| 🟡 P2 | `/api/simulation/{id}` status endpoint | 1h | Required for dashboard |
| 🟢 P3 | Auto-configure GitLab webhook from dashboard | 1h | UX polish |
| 🟢 P3 | SSE streaming for real-time log display | 1h | UX polish |

---

## Open Questions

> [!CAUTION]
> **Q1 — ANSWERED (BLOCKER):** `orchestrator/tools/gitlab_tool.py` calls **only `call_tool(MCPServer.GITLAB, ...)`** — it has ZERO direct REST calls. This means `GITLAB_MCP_URL` must be a real, deployed GitLab MCP server URL or Step 1 (INGEST) will fail immediately with `MCPConfigError`. The `GITLAB_PAT` in `.env` is not used by the orchestrator at all. A real GitLab MCP server must be deployed (or `BULWARK_DEMO_MODE` must bypass it entirely).

> [!IMPORTANT]
> **Q2:** Do you want the dashboard to be a **self-hosted local app** (user runs `npm run dev` alongside Docker Compose) or a **deployed web app** (anyone can visit the URL and use their own GitLab PAT)?

> [!IMPORTANT]
> **Q3:** For the demo, should `BULWARK_DEMO_MODE` use the **local `demo-app/`** as the simulation target, or should it still attempt a real Cloud Run build (requires GCP auth)?

> [!WARNING]
> **Q4 (Security):** If the dashboard is a publicly deployed web app and accepts any user's GitLab PAT, the backend must **never persist PATs** and must transmit them only over HTTPS. The demo mode pattern (synthetic requests, no real production data) would be the safe default.

---

## Verification Plan

### After P0 (webhook deployment)
1. Configure GitLab project webhook → Bulwark URL
2. Open a test MR
3. Observe: MongoDB gains a `QUEUED` simulation → `INGESTING` → progresses through steps
4. Observe: GitLab MR gets a verdict comment

### After P1 (demo mode)
1. Set `BULWARK_DEMO_MODE=true`, `DEMO_APP_URL=http://localhost:8080`
2. Run `docker-compose up`
3. Manually POST to `/webhook/gitlab` with a synthetic MR payload
4. Verify full pipeline completes in 2–3 minutes (no GCP wait)
5. Verify verdict comment appears on the target GitLab MR

### After P2 (dashboard)
1. Visit dashboard URL
2. Enter GitLab PAT + project URL
3. Click "Run Demo"
4. Observe pipeline steps progressing with live status
5. Observe final verdict card render
6. Observe GitLab MR comment posted automatically
