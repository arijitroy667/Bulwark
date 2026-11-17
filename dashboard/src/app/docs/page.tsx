import type { Metadata } from 'next';
import Link from 'next/link';
import { DocHeader, Flow, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'Introduction' };

export default function DocsIntroduction() {
  return (
    <>
      <DocHeader
        eyebrow="Introduction"
        title="What is Bulwark?"
        lead="Bulwark is a pre-merge gate for GitLab. For every merge request it builds the change into an isolated environment, replays real production traffic against it, evaluates any LLM output it produces, and posts one verdict on the merge request — together with a pass or fail pipeline status."
      />

      <h2 id="problem">The problem</h2>
      <p>
        Pre-production testing leaves three gaps that existing tooling does not close on its own. Each one lets a
        change reach production while every check is green.
      </p>
      <Table
        head={['Gap', 'What usually exists', 'What is missing']}
        rows={[
          ['Runtime regressions', 'Unit tests with synthetic requests; staging rarely sees real traffic', 'Replaying actual production traffic against new code before merge'],
          ['LLM quality regressions', 'LLM observability that monitors quality in production', 'Evaluating whether a prompt or model change degrades quality before it ships'],
          ['A unified gate', 'CI gates on tests; replay tools gate on latency', 'One verdict covering infrastructure and LLM quality, enforced on the MR'],
        ]}
      />

      <h2 id="approach">The approach</h2>
      <p>
        Neither half is new on its own: traffic-replay tools test infrastructure, and LLM evaluation platforms score
        quality after deploy. Bulwark combines both into <strong>one score</strong> and enforces it{' '}
        <strong>before merge</strong>. Three principles shape every decision:
      </p>
      <ul>
        <li><strong>Real traffic, isolated environment.</strong> The MR runs alone, against anonymised production requests and static stubs for its dependencies.</li>
        <li><strong>Statistics before opinions.</strong> Scores are deterministic and ignore any change smaller than the measurement can resolve.</li>
        <li><strong>Explain, never decide.</strong> Gemini classifies ambiguous errors and writes the narrative; it cannot change a score.</li>
      </ul>

      <h2 id="architecture">Architecture</h2>
      <Flow
        items={[
          ['GitLab', 'MR opened or updated'],
          ['Webhook receiver', 'verify, de-duplicate'],
          ['Pub/Sub', 'durable queue'],
          ['Orchestrator', 'runs the pipeline'],
          ['Ephemeral env', 'build, stub, replay'],
          ['Verdict', 'comment + status'],
        ]}
      />
      <Table
        head={['Component', 'Responsibility', 'Runs on']}
        rows={[
          ['Webhook receiver', 'Validates the GitLab token, filters MR events, de-duplicates by commit SHA, cancels stale runs, enqueues', 'FastAPI · Cloud Run'],
          ['Orchestrator', 'Runs the nine-step pipeline; serves the dashboard API', 'FastAPI · Cloud Run'],
          ['Environment builder', 'Builds the MR branch, deploys it and a WireMock instance, health-checks, tears down', 'Cloud Build · Cloud Run'],
          ['Traffic replay', 'Anonymised requests at 10× concurrency; latency, errors, novel errors', 'Python · httpx'],
          ['Reasoning', 'Deterministic scores, then Gemini classification and narrative', 'Gemini'],
          ['Verdict writer', 'MR comment, commit status, approval, audit record', 'GitLab REST API'],
          ['Store', 'Simulations, verdicts, history for the next run (90-day retention)', 'MongoDB'],
          ['Dashboard', 'Run scenarios, watch simulations live, browse history', 'Next.js'],
        ]}
      />

      <h2 id="modes">Two ways to run it</h2>
      <p>
        <strong>Demo mode</strong> runs the complete pipeline on one machine with no cloud accounts. The cloud pieces are
        replaced by local stand-ins — a local build of the demo service, a local WireMock, a measured baseline — while
        replay, scoring, reasoning and the verdict are exactly the production code paths. Start with the{' '}
        <Link href="/docs/quickstart">Quickstart</Link>.
      </p>
      <p>
        <strong>Production</strong> runs on Google Cloud against your GitLab, Dynatrace and Arize accounts. See{' '}
        <Link href="/docs/deployment">Deployment</Link>.
      </p>

      <h2 id="next">Where to next</h2>
      <ul>
        <li><Link href="/docs/how-it-works">How it works</Link> — the pipeline, step by step</li>
        <li><Link href="/docs/scoring">Scoring</Link> — exactly how a verdict is computed</li>
        <li><Link href="/docs/limitations">Limitations</Link> — what a simulation cannot catch</li>
      </ul>
    </>
  );
}
