import type { Metadata } from 'next';
import { Code, DocHeader, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'Quickstart' };

export default function Quickstart() {
  return (
    <>
      <DocHeader
        eyebrow="Getting started"
        title="Quickstart"
        lead="Run the full Bulwark pipeline on your machine in demo mode: an N+1 regression gets blocked, and the fix pushed to the same merge request gets approved."
      />

      <h2 id="prerequisites">Prerequisites</h2>
      <ul>
        <li>Docker (for MongoDB and WireMock)</li>
        <li>Python 3.11 or newer</li>
        <li>Node.js 20 or newer — only for the dashboard</li>
      </ul>

      <h2 id="setup">1. Set up</h2>
      <Code>{`git clone https://github.com/arijitroy667/Bulwark.git
cd Bulwark
cp .env.example .env
pip install -r requirements.txt`}</Code>
      <p>
        No credentials are required. A <code>GEMINI_API_KEY</code> is optional; see{' '}
        <a href="#gemini">Gemini</a> below.
      </p>

      <h2 id="infra">2. Start MongoDB and WireMock</h2>
      <Code>{`docker compose up -d mongodb wiremock`}</Code>

      <h2 id="orchestrator">3. Start the orchestrator</h2>
      <p>
        The orchestrator runs the pipeline and serves the dashboard API on port 8000. Environment variables override{' '}
        <code>.env</code>.
      </p>
      <Code>{`# bash
BULWARK_DEMO_MODE=true BULWARK_MODE=development \\
MONGODB_URI=mongodb://localhost:27017 \\
  uvicorn orchestrator.main:app --port 8000

# PowerShell
$env:BULWARK_DEMO_MODE="true"; $env:BULWARK_MODE="development"
$env:MONGODB_URI="mongodb://localhost:27017"
uvicorn orchestrator.main:app --port 8000`}</Code>

      <h2 id="run">4. Run the scenarios</h2>
      <p>Each scenario is a merge request against the bundled <code>demo-app</code> service.</p>
      <Code>{`python scripts/run_demo.py regression fixed --comment`}</Code>
      <p>Expected output (latencies vary by machine):</p>
      <Code>{`▶ regression: sim_8f9c14ac6626
  · fetching_baseline
  · provisioning
  · replaying
  · reasoning
  · completed
  ⇒ BLOCKED 49/100  (infra 20, reliability 100, llm None, model deterministic-fallback)
    p95 41ms → 713ms (+672ms) in 19s

▶ fixed: sim_8b76ee74362a
  ⇒ APPROVED 100/100  (infra 100, reliability 100, llm None, model deterministic-fallback)
    p95 40ms → 41ms (+1ms) in 15s`}</Code>
      <Table
        head={['Scenario', 'The change', 'Expected verdict']}
        rows={[
          [<code key="r">regression</code>, 'GET /users loads each user’s preferences in its own call (N+1)', 'Blocked — p95 regression past the 200 ms SLO'],
          [<code key="f">fixed</code>, 'Same MR, one batched call. Cancels an in-flight regression run', 'Approved — delta inside the ±35 ms noise floor'],
          [<code key="l">llm-regression</code>, 'The ticket-summary prompt is edited and starts over-promising', 'LLM quality drop (requires Gemini)'],
        ]}
      />

      <h2 id="dashboard">5. Open the dashboard</h2>
      <Code>{`npm install --prefix dashboard
npm run dev --prefix dashboard`}</Code>
      <p>
        Open <code>http://localhost:3000/dashboard</code>, pick a scenario and watch it move through the pipeline. The
        dashboard reads the orchestrator URL from <code>NEXT_PUBLIC_API_URL</code> (default{' '}
        <code>http://127.0.0.1:8000</code>).
      </p>

      <h2 id="stand-ins">What demo mode replaces</h2>
      <p>Everything Bulwark itself does is real. Only the external systems are stood in for:</p>
      <Table
        head={['Production', 'Demo mode']}
        rows={[
          ['Cloud Build + Cloud Run environment', 'demo-app launched as a local process for each simulation'],
          ['WireMock on Cloud Run', 'The local WireMock, loaded with stubs that add a 12 ms database round trip'],
          ['Dynatrace baseline', 'Measured by replaying the same traffic against the production build of demo-app'],
          ['Dynatrace request logs', 'A fixed mix of 200 requests (no personal data involved)'],
          ['Arize baseline', 'LLM traces captured from the production build, scored by the same evaluators'],
          ['GitLab merge request', 'Synthetic MRs — the comment is stored and shown, not posted'],
        ]}
      />

      <h2 id="gemini">Gemini</h2>
      <p>
        Gemini writes the verdict narrative, classifies unexpected errors and acts as the judge for LLM evaluation.
        AI Studio keys (<code>AIza…</code>) and Vertex AI express keys (<code>AQ.…</code>) both work. Without a working
        key the verdict is computed from thresholds alone and marked <code>deterministic-fallback</code>, and LLM
        evaluation is skipped with the reason recorded.
      </p>

      <h2 id="containers">Everything in containers</h2>
      <Code>{`docker compose up --build
# orchestrator on :8000, webhook receiver on :8080
python scripts/run_demo.py regression fixed`}</Code>

      <h2 id="troubleshooting">Troubleshooting</h2>
      <Table
        head={['Symptom', 'Fix']}
        rows={[
          ['“local WireMock … is not reachable”', <>Start it: <code>docker compose up -d wiremock</code></>],
          ['Dashboard says it cannot reach the orchestrator', <>Check the orchestrator is on port 8000, or set <code>NEXT_PUBLIC_API_URL</code> in <code>dashboard/.env.local</code></>],
          ['Slow, noisy latencies on Windows', <>Use <code>WIREMOCK_BASE_URL=http://127.0.0.1:8082</code> — <code>localhost</code> tries IPv6 first</>],
        ]}
      />
    </>
  );
}
