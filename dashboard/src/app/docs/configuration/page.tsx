import type { Metadata } from 'next';
import { DocHeader, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'Configuration' };

const c = (s: string) => <code>{s}</code>;

export default function Configuration() {
  return (
    <>
      <DocHeader
        eyebrow="Reference"
        title="Configuration"
        lead={<>Bulwark’s services read environment variables, or a <code>.env</code> file in the working directory. Real environment variables win over the file. In production, secrets come from Secret Manager.</>}
      />

      <h2 id="runtime">Runtime</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('BULWARK_MODE'), c('development'), <>{c('development')}: MongoDB queue, consumed by the orchestrator. {c('production')}: Pub/Sub push.</>],
          [c('BULWARK_DEMO_MODE'), c('false'), 'Local stand-ins for the environment, dependencies and baseline. See Quickstart.'],
          [c('WIREMOCK_BASE_URL'), c('http://localhost:8082'), 'Local WireMock used by demo mode. Prefer 127.0.0.1 on Windows.'],
          [c('SIMULATION_TIMEOUT_SECONDS'), c('900'), 'Hard ceiling per simulation; the environment is torn down when it fires.'],
          [c('REPLAY_CONCURRENCY'), c('10'), 'Concurrent requests during replay.'],
          [c('REPLAY_REQUEST_LIMIT'), c('200'), 'Maximum production samples replayed.'],
        ]}
      />

      <h2 id="gitlab">GitLab</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('GITLAB_URL'), c('https://gitlab.com'), 'GitLab instance.'],
          [c('GITLAB_PAT'), '—', 'Access token with the api scope.'],
          [c('GITLAB_WEBHOOK_TOKEN'), '—', 'Secret GitLab sends in X-Gitlab-Token; other requests are rejected.'],
        ]}
      />

      <h2 id="dynatrace">Dynatrace</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('DYNATRACE_ENV_ID'), '—', 'Environment ID; the MCP gateway URL is derived from it.'],
          [c('DYNATRACE_MCP_URL'), '—', 'Optional override of the gateway URL.'],
          [c('CLIENT_ID'), '—', 'OAuth client ID for the MCP gateway.'],
          [c('CLIENT_SECRET'), '—', 'OAuth client secret.'],
          [c('CLIENT_URL'), '—', <>Account URN, {c('urn:dtaccount:<uuid>')}.</>],
        ]}
      />

      <h2 id="arize">Arize</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('ARIZE_API_KEY'), '—', 'Passed to the Phoenix MCP server.'],
          [c('ARIZE_SPACE_ID'), '—', 'Arize space.'],
          [c('ARIZE_MCP_URL'), '—', <>{c('stdio')} to launch the Phoenix MCP server with npx, or an http(s) MCP URL.</>],
          [c('PHOENIX_COLLECTOR_ENDPOINT'), c('https://app.phoenix.arize.com'), 'Phoenix instance the MCP server talks to.'],
          [c('MCP_AUTH_TOKEN'), '—', 'Optional bearer token for non-Dynatrace MCP servers.'],
        ]}
      />

      <h2 id="gemini">Gemini</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('GEMINI_API_KEY'), '—', <>AI Studio ({c('AIza…')}) or Vertex AI express ({c('AQ.…')}) key. Optional: without it verdicts are threshold-only and LLM evaluation is skipped.</>],
          [c('GEMINI_MODEL'), c('gemini-2.0-flash'), <>Model for reasoning and judging, e.g. {c('gemini-2.5-flash')}.</>],
        ]}
      />

      <h2 id="storage">Storage and Google Cloud</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('MONGODB_URI'), c('mongodb://localhost:27017'), 'MongoDB connection string.'],
          [c('MONGODB_DB_NAME'), c('bulwark'), 'Database name.'],
          [c('GCP_PROJECT_ID'), c('bulwark-project'), 'Project for Cloud Build, Cloud Run and Pub/Sub.'],
          [c('GCP_REGION'), c('us-central1'), 'Region for Cloud Run and Artifact Registry.'],
          [c('GCP_ARTIFACT_REGISTRY'), c('bulwark-images'), 'Artifact Registry repository for simulation images.'],
          [c('GCP_SOURCE_ARCHIVE_BUCKET'), c('bulwark-build-sources'), 'Bucket that stages branch archives for Cloud Build.'],
        ]}
      />

      <h2 id="scoring">Scoring</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('P95_NOISE_FLOOR_MS'), c('35'), 'p95 deltas within this band are not scored.'],
          [c('ERROR_RATE_NOISE_FLOOR_PERCENT'), c('1.0'), 'Error-rate deltas within this band are not scored.'],
          [c('LLM_EVAL_NOISE_BAND'), c('0.10'), 'Evaluator deltas within this band are not scored.'],
          [c('CONFIDENCE_THRESHOLD_APPROVED'), c('80'), 'Minimum score for approved.'],
          [c('CONFIDENCE_THRESHOLD_WARNING'), c('50'), 'Minimum score for warning; below is blocked.'],
        ]}
      />

      <h2 id="dashboard">Dashboard</h2>
      <Table
        head={['Variable', 'Default', 'Description']}
        rows={[
          [c('NEXT_PUBLIC_API_URL'), c('http://127.0.0.1:8000'), <>Orchestrator URL. Set it in {c('dashboard/.env.local')}; it is fixed at build time.</>],
        ]}
      />

      <h2 id="per-project">Per-project settings</h2>
      <p>
        A document in the <code>environment_configs</code> collection whose <code>_id</code> is the GitLab project path
        can override the simulation’s health-check path:
      </p>
      <pre className="code">{`{ "_id": "group/service", "health_check_path": "/healthz" }`}</pre>
    </>
  );
}
