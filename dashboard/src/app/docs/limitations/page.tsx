import type { Metadata } from 'next';
import { DocHeader, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'Limitations' };

export default function Limitations() {
  return (
    <>
      <DocHeader
        eyebrow="Reference"
        title="Limitations"
        lead="A tool that knows its limits is easier to trust. Bulwark catches a subset of regressions — those visible in a few hundred replayed HTTP requests against static dependency stubs. It flags probable regressions; it does not certify safety."
      />

      <h2 id="cannot-detect">What a simulation cannot catch</h2>
      <Table
        head={['Failure class', 'Why it is invisible', 'What covers it instead']}
        rows={[
          ['Conditional regressions', 'They need data states that the sampled requests never reach', 'Canary releases, feature flags'],
          ['Race conditions', 'Replay runs at a controlled 10× concurrency, far below production', 'Load testing (k6, JMeter)'],
          ['Database constraint violations', 'Databases are not mocked, so DB-dependent paths are not exercised', 'Migration and integration tests'],
          ['Async and queue-driven behaviour', 'Message consumers are not simulated', 'Event-driven integration tests'],
          ['Small LLM quality shifts', 'Changes within ±0.10 are indistinguishable from evaluator noise', 'Stricter custom evaluators'],
          ['Third-party API changes', 'Stubs return the last known response shape', 'Contract tests, regular stub refresh'],
          ['Infrastructure failures', 'OOM kills, disk exhaustion and network partitions are not simulated', 'Chaos engineering'],
        ]}
      />

      <h2 id="measurement">Measurement limits</h2>
      <ul>
        <li>A p95 from 200 samples carries roughly ±35 ms of uncertainty; smaller deltas are reported, not scored.</li>
        <li>An error-rate change under 1 % cannot be told apart from chance at this sample size.</li>
        <li>With fewer than 50 production samples, replay scoring is skipped instead of being guessed.</li>
        <li>WireMock stubs return static responses: novel endpoints return 404, which Gemini classifies as a stub artefact or a code regression.</li>
      </ul>

      <h2 id="demo">Demo-mode caveats</h2>
      <ul>
        <li>Traffic is a fixed synthetic mix rather than sampled production logs.</li>
        <li>The baseline is measured locally against the production build, not taken from Dynatrace.</li>
        <li>The demo service’s LLM call is a deterministic stand-in, so runs are reproducible.</li>
        <li>Merge requests are synthetic; the comment is stored and displayed, not posted to GitLab.</li>
      </ul>

      <h2 id="timing">Timing</h2>
      <p>
        In production, image builds dominate run time; the design target is 5–8 minutes per simulation, with a hard
        cut-off at 15. Demo-mode simulations finish in 10–20 seconds because nothing is built.
      </p>
    </>
  );
}
