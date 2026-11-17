import type { Metadata } from 'next';
import Link from 'next/link';
import { Code, DocHeader, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'Integrations' };

export default function Integrations() {
  return (
    <>
      <DocHeader
        eyebrow="Reference"
        title="Integrations"
        lead="What Bulwark uses each system for, how it connects, and what access it needs. Credentials are listed in Configuration."
      />

      <h2 id="gitlab">GitLab — trigger and gate</h2>
      <p>
        Bulwark receives merge request webhooks and talks to the GitLab REST API (v4) with a personal or project access
        token. The token needs the <code>api</code> scope, because Bulwark writes comments and commit statuses.
      </p>
      <Table
        head={['Purpose', 'API']}
        rows={[
          ['MR metadata and diff', <code key="1">GET /projects/:id/merge_requests/:iid[/changes]</code>],
          ['Source files for the import scan', <code key="2">GET /projects/:id/repository/files/:path/raw</code>],
          ['Branch archive for the build', <code key="3">GET /projects/:id/repository/archive.tar.gz</code>],
          ['Verdict comment', <code key="4">POST /projects/:id/merge_requests/:iid/notes</code>],
          ['Pipeline gate', <code key="5">POST /projects/:id/statuses/:sha</code>],
          ['Approve / unapprove', <code key="6">POST /projects/:id/merge_requests/:iid/(un)approve</code>],
        ]}
      />
      <p>
        Webhook: <em>Settings → Webhooks</em>, URL <code>https://&lt;webhook-receiver&gt;/webhook/gitlab</code>,
        secret token = <code>GITLAB_WEBHOOK_TOKEN</code>, trigger <em>Merge request events</em>. The dashboard’s{' '}
        <Link href="/dashboard">Connect GitLab</Link> form can register it for you.
      </p>

      <h2 id="dynatrace">Dynatrace — production memory</h2>
      <p>
        Bulwark calls the Dynatrace MCP gateway (<code>https://&lt;env&gt;.apps.dynatrace.com/platform-reserved/mcp-gateway/v0.1/servers/dynatrace-mcp/mcp</code>,
        derived from <code>DYNATRACE_ENV_ID</code>) and runs DQL through its <code>execute-dql</code> tool. It
        authenticates with an OAuth client (client-credentials grant) that needs these scopes:
      </p>
      <Code>{`mcp-gateway:servers:invoke  mcp-gateway:servers:read
storage:logs:read  storage:metrics:read  storage:entities:read
storage:events:read  storage:bizevents:read`}</Code>
      <Table
        head={['Data', 'DQL source']}
        rows={[
          ['Active incidents', <code key="1">fetch problems</code>],
          ['Latency percentiles', <code key="2">builtin:service.response.time</code>],
          ['Error rate', <code key="3">builtin:service.errors.total.rate</code>],
          ['Throughput', <code key="4">builtin:service.requestCount.total</code>],
          ['Request samples', <code key="5">fetch logs</code>],
          ['Dependencies', <code key="6">fetch entities</code>],
          ['SLO targets', <code key="7">fetch slos</code>],
        ]}
      />
      <p>
        Request logs are parsed from structured fields, OpenTelemetry span attributes, the Logs API v2{' '}
        <code>log.body</code> wrapper or free-text lines such as <code>HTTP GET /api/users 200 45ms</code>.
      </p>

      <h2 id="arize">Arize — LLM quality</h2>
      <p>
        Bulwark uses the official Phoenix MCP server. With <code>ARIZE_MCP_URL=stdio</code> it launches the pinned{' '}
        <code>@arizeai/phoenix-mcp</code> package through <code>npx</code> (Node.js required); any http(s) URL is used as
        a hosted MCP server instead.
      </p>
      <Table
        head={['Tool', 'Used for']}
        rows={[
          [<code key="1">list-datasets</code>, 'Find bulwark-prod-<service>'],
          [<code key="2">list-experiments-for-dataset</code>, 'Latest baseline experiment'],
          [<code key="3">get-experiment-by-id</code>, 'Baseline evaluator scores'],
          [<code key="4">add-dataset-examples</code>, 'Archive simulation outputs as bulwark-sim-<id>'],
        ]}
      />
      <p>
        The Phoenix MCP server stores data but does not run evaluators, so Bulwark runs them itself — see{' '}
        <Link href="/docs/llm-evaluation">LLM evaluation</Link>.
      </p>

      <h2 id="gemini">Gemini — reasoning and judging</h2>
      <p>
        One structured call per simulation for the verdict narrative and novel-error classification, plus one judging
        call per evaluated build. Responses are JSON at temperature 0.1 with a 30-second ceiling. AI Studio keys
        (<code>AIza…</code>) use the Gemini API; Vertex AI express keys (<code>AQ.…</code>) use Vertex AI.
      </p>

      <h2 id="mongodb">MongoDB — audit trail</h2>
      <Table
        head={['Collection', 'Contents']}
        rows={[
          [<code key="1">simulations</code>, 'One record per run: MR, status, baseline, replay result, LLM result, verdict, MR comment, errors. 90-day TTL'],
          [<code key="2">verdicts</code>, 'The verdict of each simulation'],
          [<code key="3">environment_configs</code>, 'Optional per-project settings, e.g. health_check_path'],
          [<code key="4">message_queue</code>, 'Development-mode queue between webhook receiver and orchestrator'],
        ]}
      />

      <h2 id="gcp">Google Cloud — isolation</h2>
      <p>
        Cloud Build builds each MR; Artifact Registry holds the image until teardown; Cloud Run hosts the simulation,
        its WireMock and Bulwark’s own services; Pub/Sub decouples webhook receipt from processing; Cloud Storage
        stages the branch archive. See <Link href="/docs/deployment">Deployment</Link>.
      </p>
    </>
  );
}
