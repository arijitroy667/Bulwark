import type { Metadata } from 'next';
import Link from 'next/link';
import { DocHeader, Flow, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'How it works' };

export default function HowItWorks() {
  return (
    <>
      <DocHeader
        eyebrow="Concepts"
        title="How it works"
        lead="Every merge request event becomes a simulation: nine steps that rebuild the change, rehearse it against production traffic and turn the evidence into a verdict. Steps that do not depend on each other run in parallel."
      />

      <Flow
        items={[
          ['Ingest', 'MR + LLM detection'],
          ['Baseline', 'Dynatrace'],
          ['LLM baseline', 'Arize'],
          ['History', 'last 5 verdicts'],
          ['Provision', 'build + stubs'],
          ['Replay', '200 requests'],
          ['Evaluate', 'LLM output'],
          ['Reason', 'scores + Gemini'],
          ['Enforce', 'comment + status'],
        ]}
      />

      <h2 id="trigger">Before the pipeline: the trigger</h2>
      <p>
        GitLab sends a merge request event to the webhook receiver. The receiver checks the{' '}
        <code>X-Gitlab-Token</code> header, keeps only <code>open</code>, <code>update</code> and <code>reopen</code>{' '}
        actions, and derives a deterministic simulation ID from the MR, branch and head commit. A repeat delivery of the
        same commit is answered as a duplicate. A push of a new commit marks any running simulation for that MR as
        cancelled; the old run stops at its next checkpoint and tears its environment down. The event is then queued —
        Pub/Sub in production, a MongoDB queue in development — and the receiver responds immediately.
      </p>

      <h2 id="ingest">1 · Ingest</h2>
      <p>
        The orchestrator fetches the MR metadata and diff from the GitLab API. MRs that only touch documentation are
        completed without a comment. The affected service is taken from the path (<code>services/</code>,{' '}
        <code>apps/</code>, <code>packages/</code>, <code>src/</code>) or, for single-service repositories, the project
        name. The changed files then go through{' '}
        <Link href="/docs/llm-evaluation#detection">two-stage LLM detection</Link>.
      </p>

      <h2 id="baseline">2 · Production baseline</h2>
      <p>From Dynatrace, through its MCP server:</p>
      <ul>
        <li>Open problems on the service. An active incident defers the evaluation and says so on the MR.</li>
        <li>p50, p95 and p99 response time, error rate and throughput over the last 24 hours.</li>
        <li>SLO targets — the p95 latency budget and the error budget (defaults: 200 ms and 1 %).</li>
        <li>The service’s downstream dependencies, which become WireMock stubs.</li>
        <li>Up to 200 request logs from the last 24 hours, anonymised before they are used or stored.</li>
      </ul>

      <h2 id="llm-baseline">3 · LLM baseline</h2>
      <p>
        Only when LLM components were detected, and in parallel with step 2: the latest scored experiment on the
        service’s production dataset in Arize.
      </p>

      <h2 id="history">3½ · History</h2>
      <p>
        The last five completed verdicts for the same service, including any human overrides, are read from MongoDB.
        They are passed to the reasoning step as context — Gemini has no memory of its own.
      </p>

      <h2 id="provision">4 · Provision</h2>
      <p>
        The MR branch is fetched as an archive, built with Cloud Build and deployed to Cloud Run as a single instance
        (1 vCPU, 512 MiB, no autoscaling) with <code>BULWARK_MODE=simulation</code>. Production credentials are never
        injected. In parallel, a WireMock service is deployed and loaded with static stubs for every dependency. The
        environment must answer its health check (default <code>/health</code>) within three attempts; otherwise the
        simulation ends as <code>build_failed</code>, <code>health_check_failed</code> or <code>runtime_crash</code> and
        the last startup log lines are posted to the MR.
      </p>

      <h2 id="replay">5 · Replay</h2>
      <p>
        Replay needs at least 50 real request samples; with fewer, it is skipped and the verdict says so rather than
        inventing traffic. A warm-up burst is sent first and discarded so cold start does not masquerade as a
        regression. The samples are then replayed at a concurrency of 10, recording latency, status code and a hash of
        each response. The result is p50/p95/p99 latency, the 5xx error rate, the count of 5xx responses, status-code
        mismatches and the <strong>novel errors</strong> — each distinct error message the MR build returned, which
        the reasoning step classifies.
      </p>

      <h2 id="evaluate">6 · Evaluate LLM output</h2>
      <p>
        For LLM changes, the environment’s <code>GET /traces</code> endpoint returns the prompts and responses produced
        during replay. They are scored for hallucination, relevance and groundedness and compared with the baseline.
        See <Link href="/docs/llm-evaluation">LLM evaluation</Link>.
      </p>

      <h2 id="teardown">7 · Teardown</h2>
      <p>
        The simulation service, the WireMock service, the built image and the source archive are deleted. Teardown
        also runs when a simulation fails, is cancelled or times out.
      </p>

      <h2 id="reason">8 · Reason</h2>
      <p>
        Scores are first computed deterministically from the measured deltas. Gemini then receives the diff, the
        metrics, the LLM deltas, the SLO targets and the history, and returns a classification for each novel error
        (code regression, stub artefact or uncertain), a summary, a recommendation and a note on the history. The
        reliability score is recomputed from those classifications; every other number stays exactly as measured. See{' '}
        <Link href="/docs/scoring">Scoring</Link>.
      </p>

      <h2 id="enforce">9 · Enforce</h2>
      <p>
        The verdict is posted as an MR comment with the metric table, evidence, analysis and a mandatory measurement
        disclosure. The commit status <code>Bulwark/production-ready</code> is set to <code>success</code> for approved
        and warning verdicts and <code>failed</code> for blocked ones; the MR is approved or unapproved where the token
        allows it. The full record is persisted for 90 days.
      </p>
      <p>
        Adding the <code>bulwark-override</code> label to a blocked MR sets the status to success, posts an
        acknowledgement and logs who overrode it. Future simulations of the service see the override in their history.
      </p>

      <h2 id="failures">Failure handling</h2>
      <p>A failing dependency degrades the verdict; it never crashes the pipeline or leaves an environment behind.</p>
      <Table
        head={['Situation', 'Behaviour']}
        rows={[
          ['Documentation-only MR', 'Completed silently, no comment'],
          ['Active production incident', 'Deferred; the MR comment explains why'],
          ['Dynatrace unavailable or < 50 samples', 'Replay scoring skipped and noted in the verdict'],
          ['Build or health check fails', 'Simulation ends with the failure status; log tail posted; teardown runs'],
          ['Arize or the evaluators unavailable', 'LLM score omitted; reason recorded in the verdict'],
          ['Gemini fails', 'One retry after 5 s, then a threshold-only verdict marked deterministic-fallback'],
          ['MongoDB write fails', 'Retried three times with exponential backoff'],
          ['New commit pushed', 'Old run cancelled at its next checkpoint and torn down'],
          ['Over 15 minutes', 'Marked timed_out; environment force-deleted'],
        ]}
      />

      <h2 id="privacy">Anonymisation</h2>
      <p>Production requests are anonymised before replay, storage or transmission:</p>
      <Table
        head={['Data', 'Becomes']}
        rows={[
          ['Email addresses', <code key="e">user_&lt;hash&gt;@example.com</code>],
          ['Bearer tokens', <code key="b">Bearer mock_token_&lt;hash&gt;</code>],
          ['Cookies, API keys, password and token fields', 'Hashed placeholders'],
          ['UUIDs', 'Sequential placeholder UUIDs'],
          ['Phone numbers', <code key="p">+1555000&lt;n&gt;</code>],
          ['Numeric IDs in paths', 'Sequential IDs starting at 1001'],
        ]}
      />
    </>
  );
}
