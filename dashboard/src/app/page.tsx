import Link from 'next/link';

const PROBLEMS = [
  {
    title: 'Tests never see real traffic',
    text: 'Unit and integration tests exercise the inputs someone thought of. An N+1 query passes every one of them and still multiplies latency once production traffic arrives.',
  },
  {
    title: 'LLM regressions are silent',
    text: 'A prompt or model change can make answers worse while latency and error rates stay flat. Without a quality check, the first signal is a user complaint.',
  },
  {
    title: 'No single gate before merge',
    text: 'Performance and LLM quality are watched by different tools, after deploy. Nothing turns both into one decision while the change is still a merge request.',
  },
];

const STEPS = [
  {
    title: 'Trigger',
    text: 'A merge request opens or updates. The webhook is verified, de-duplicated by commit SHA and queued. A newer push cancels the run in progress.',
  },
  {
    title: 'Rebuild',
    text: 'The MR branch is built into an isolated, single-instance environment. Downstream services are replaced by static WireMock stubs. No production credentials go in.',
  },
  {
    title: 'Replay and evaluate',
    text: 'Up to 200 anonymised production requests are replayed against it. If the change touches LLM code, its outputs are scored for hallucination, relevance and groundedness.',
  },
  {
    title: 'Verdict',
    text: 'Deltas are scored against the production baseline, Gemini explains the evidence, and the verdict lands on the MR with a pass or fail pipeline status.',
  },
];

const INTEGRATIONS = [
  { name: 'GitLab', role: 'Trigger and gate', text: 'Merge request webhooks in; the verdict comment and the Bulwark/production-ready commit status out. A bulwark-override label records human overrides.' },
  { name: 'Dynatrace', role: 'Production memory', text: 'Latency and error baselines, SLO targets, active-incident checks, and the request samples that get replayed.' },
  { name: 'Arize', role: 'LLM quality', text: 'Production quality baselines, plus an archive of the outputs from every LLM evaluation, through the Phoenix MCP server.' },
  { name: 'Gemini', role: 'Reasoning', text: 'Classifies unexpected errors against the diff, judges LLM output and writes the explanation. It narrates; it never sets the score.' },
  { name: 'Google Cloud', role: 'Isolation', text: 'Cloud Build, Cloud Run and Pub/Sub provide the ephemeral, single-instance environment each simulation runs in.' },
  { name: 'MongoDB', role: 'Audit trail', text: 'Every simulation and its evidence for 90 days. The last five verdicts per service feed the next reasoning step.' },
];

const LIMITS = [
  ['Race conditions', 'beyond the replay’s controlled 10× concurrency'],
  ['Database constraints', 'and migrations — databases are not mocked'],
  ['Async and queue-driven behaviour', 'message consumers are not simulated'],
  ['Data-dependent regressions', 'that the sampled requests never reach'],
  ['LLM quality shifts', 'smaller than the ±0.10 evaluator noise band'],
  ['Infrastructure failures', 'OOM kills, disk exhaustion, network partitions'],
];

function VerdictPreview() {
  const rows = [
    ['p95 latency', '66 ms', '595 ms', '+529 ms'],
    ['p50 latency', '23 ms', '464 ms', '+441 ms'],
    ['Error rate', '0.00%', '0.00%', '+0.00%'],
    ['5xx responses', '0', '0', '—'],
  ];
  return (
    <div>
      <div className="verdict" role="img" aria-label="Example Bulwark verdict: BLOCKED, 49 out of 100, p95 latency up 529 milliseconds">
        <div className="verdict-top">
          <span>local/demo-app !1 · Include user preferences in GET /users</span>
        </div>
        <div className="verdict-body">
          <div className="verdict-title">
            <span className="dot" style={{ background: 'var(--danger)' }} />
            BLOCKED <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>· 49 / 100</span>
          </div>
          <div className="verdict-row head"><span>Metric</span><span>Baseline</span><span>MR build</span><span>Delta</span></div>
          {rows.map(([m, b, s, d]) => (
            <div className="verdict-row" key={m}>
              <span style={{ color: 'var(--text)' }}>{m}</span><span>{b}</span><span>{s}</span>
              <span className={d.startsWith('+') && d !== '+0.00%' ? 'bad' : ''}>{d}</span>
            </div>
          ))}
          <div className="verdict-note">SLO breach: simulated p95 595 ms exceeds the 200 ms p95 budget.</div>
        </div>
        <div className="verdict-foot">
          <span>Bulwark/production-ready</span>
          <span style={{ color: 'var(--danger)' }}>failed</span>
        </div>
      </div>
      <div className="verdict-next">
        <span><span className="dot" style={{ background: 'var(--success)', marginRight: 8 }} />Fix pushed to the same MR</span>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12.5 }}>APPROVED · 100 / 100 · p95 +1 ms</span>
      </div>
      <p className="caption">Measured by the local demo simulation: 200 replayed requests per build.</p>
    </div>
  );
}

export default function LandingPage() {
  return (
    <main>
      <section className="hero">
        <div className="wrap hero-grid">
          <div>
            <p className="eyebrow">Pre-merge production simulation for GitLab</p>
            <h1 className="display">Catch production regressions before they merge.</h1>
            <p className="lead">
              Bulwark replays real production traffic against every merge request, evaluates the quality of its LLM
              output, and gates the merge on one statistically grounded verdict.
            </p>
            <div className="actions">
              <Link href="/dashboard" className="btn btn-primary">Run a simulation</Link>
              <Link href="/docs" className="btn btn-secondary">Read the docs</Link>
            </div>
            <p className="hero-note">Open source · Runs locally without cloud accounts · GitLab, Dynatrace, Arize, Gemini</p>
          </div>
          <VerdictPreview />
        </div>
      </section>

      <section className="section" id="problem">
        <div className="wrap">
          <div className="section-head">
            <p className="eyebrow">The problem</p>
            <h2>Green pipelines still ship regressions.</h2>
            <p>
              The most expensive failures are the ones a test suite cannot see: behaviour that only appears under real
              traffic, and quality that degrades without raising a single error.
            </p>
          </div>
          <div className="grid grid-3">
            {PROBLEMS.map((p) => (
              <div className="card" key={p.title}>
                <h3>{p.title}</h3>
                <p>{p.text}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="section" id="how">
        <div className="wrap">
          <div className="section-head">
            <p className="eyebrow">How it works</p>
            <h2>A production rehearsal for every merge request.</h2>
            <p>
              Bulwark stands up the change next to a copy of its dependencies, sends it the traffic production actually
              receives, and compares the result with what production does today.
            </p>
          </div>
          <div className="grid grid-4">
            {STEPS.map((s, i) => (
              <div className="step" key={s.title}>
                <p className="step-num">0{i + 1}</p>
                <h3>{s.title}</h3>
                <p>{s.text}</p>
              </div>
            ))}
          </div>
          <Link href="/docs/how-it-works" className="more">The full nine-step pipeline →</Link>
        </div>
      </section>

      <section className="section" id="scoring">
        <div className="wrap">
          <div className="section-head">
            <p className="eyebrow">Scoring</p>
            <h2>Built to tell signal from noise.</h2>
            <p>
              A p95 measured from 200 requests is only accurate to about ±35 ms. Bulwark penalises only the changes that
              are larger than the measurement can resolve. Everything inside the noise floor is reported, not scored.
            </p>
          </div>
          <div className="grid grid-2">
            <div className="card">
              <p className="card-label">Noise floors</p>
              <table className="table">
                <tbody>
                  <tr><td>p95 latency</td><td>±35 ms</td></tr>
                  <tr><td>Error rate</td><td>±1.0 %</td></tr>
                  <tr><td>LLM evaluators</td><td>±0.10</td></tr>
                </tbody>
              </table>
              <div className="formula">
                overall = 0.4 × infrastructure + 0.6 × reliability<br />
                <span style={{ color: 'var(--text-3)' }}>with LLM code: 0.3 × infra + 0.4 × reliability + 0.3 × LLM</span>
              </div>
            </div>
            <div className="card">
              <p className="card-label">Verdicts</p>
              <table className="table">
                <tbody>
                  <tr><td><span className="dot" style={{ background: 'var(--success)', marginRight: 8 }} />80 – 100 · Approved</td><td>Pipeline passes</td></tr>
                  <tr><td><span className="dot" style={{ background: 'var(--warning)', marginRight: 8 }} />50 – 79 · Warning</td><td>Passes, with caveats in the comment</td></tr>
                  <tr><td><span className="dot" style={{ background: 'var(--danger)', marginRight: 8 }} />0 – 49 · Blocked</td><td>Pipeline fails; merge is held</td></tr>
                </tbody>
              </table>
              <p style={{ marginTop: 16 }}>
                A high-confidence regression that also breaches the service’s SLO is always blocked. Engineers can
                override with a label; the override is logged and shown to future simulations.
              </p>
            </div>
          </div>
          <Link href="/docs/scoring" className="more">How every point is scored →</Link>
        </div>
      </section>

      <section className="section" id="integrations">
        <div className="wrap">
          <div className="section-head">
            <p className="eyebrow">Integrations</p>
            <h2>Uses the tools you already run.</h2>
            <p>Each integration has one job. Bulwark connects them into a single decision.</p>
          </div>
          <div className="grid grid-3">
            {INTEGRATIONS.map((x) => (
              <div className="card" key={x.name}>
                <p className="card-label">{x.role}</p>
                <h3>{x.name}</h3>
                <p>{x.text}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="section" id="limits">
        <div className="wrap">
          <div className="section-head">
            <p className="eyebrow">Limits</p>
            <h2>Clear about what it can’t see.</h2>
            <p>
              Bulwark flags probable regressions; it does not certify safety. Every verdict says what was measured, how
              precisely, and what the simulation could not exercise.
            </p>
          </div>
          <ul className="limits">
            {LIMITS.map(([k, v]) => (
              <li key={k}><strong>{k}</strong> {v}</li>
            ))}
          </ul>
          <Link href="/docs/limitations" className="more">Limitations in detail →</Link>
        </div>
      </section>

      <section className="section" id="try">
        <div className="wrap grid grid-2" style={{ gap: 48, alignItems: 'center' }}>
          <div>
            <p className="eyebrow">Try it</p>
            <h2 style={{ fontSize: 'clamp(28px, 3.4vw, 38px)', letterSpacing: '-0.03em', marginBottom: 14 }}>
              Run the whole pipeline on your laptop.
            </h2>
            <p className="muted" style={{ fontSize: 17 }}>
              Demo mode swaps the cloud for local stand-ins and keeps every Bulwark step real: replay, scoring,
              reasoning and the verdict. No accounts needed.
            </p>
            <div className="actions">
              <Link href="/docs/quickstart" className="btn btn-primary">Quickstart</Link>
              <Link href="/dashboard" className="btn btn-secondary">Open the dashboard</Link>
            </div>
          </div>
          <pre className="code">{`docker compose up -d mongodb wiremock

BULWARK_DEMO_MODE=true BULWARK_MODE=development \\
MONGODB_URI=mongodb://localhost:27017 \\
  uvicorn orchestrator.main:app --port 8000

python scripts/run_demo.py regression fixed`}</pre>
        </div>
      </section>
    </main>
  );
}
