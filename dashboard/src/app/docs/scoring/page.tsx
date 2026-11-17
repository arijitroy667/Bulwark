import type { Metadata } from 'next';
import { Code, DocHeader, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'Scoring' };

export default function Scoring() {
  return (
    <>
      <DocHeader
        eyebrow="Concepts"
        title="Scoring"
        lead="A verdict is three sub-scores, a weighted overall score and one gate. All of it is deterministic: the same measurements always produce the same verdict."
      />

      <h2 id="noise">Noise floors</h2>
      <p>
        A measurement from 200 requests is not exact. Bulwark only deducts points for deltas larger than the
        measurement can resolve; anything inside the noise floor appears in the comment as an observation and is not
        scored.
      </p>
      <Table
        head={['Signal', 'Noise floor', 'Why']}
        rows={[
          ['p95 latency delta', '±35 ms', 'Bootstrap uncertainty of a p95 from 200 log-normal samples'],
          ['Error rate delta', '±1.0 %', 'Binomial confidence interval at n = 200'],
          ['LLM evaluator deltas', '±0.10', 'Variability of LLM-as-judge evaluators'],
        ]}
      />
      <p>The error rate is the share of replayed requests that returned a 5xx response.</p>

      <h2 id="infrastructure">Infrastructure score</h2>
      <p>Starts at 100. “Excess” is the delta minus the noise floor.</p>
      <Table
        head={['Signal', 'Condition', 'Deduction']}
        rows={[
          ['p95 latency', 'Δ ≤ 35 ms', '0'],
          ['p95 latency', '35 ms < Δ ≤ 100 ms', '15 per 20 ms of excess (max 50)'],
          ['p95 latency', 'Δ > 100 ms', '30 per 20 ms of excess (max 80)'],
          ['Error rate', 'Δ ≤ 1 %', '0'],
          ['Error rate', '1 % < Δ ≤ 3 %', '20 per 1 % of excess (max 40)'],
          ['Error rate', 'Δ > 3 %', '40 per 1 % of excess (max 60)'],
          ['5xx responses', 'any', '40, flat'],
        ]}
      />

      <h2 id="reliability">Reliability score</h2>
      <p>
        Starts at 100. Each novel error is classified by Gemini after it reads the error together with the diff. A
        404 from an endpoint the stubs do not know is an environment artefact, not a regression.
      </p>
      <Table
        head={['Classification', 'Meaning', 'Deduction']}
        rows={[
          [<code key="c">code_regression</code>, 'Caused by the change', '25'],
          [<code key="u">uncertain</code>, 'Cannot be decided from the evidence (and the default without Gemini)', '10'],
          [<code key="s">stub_artefact</code>, 'Produced by the simulated environment', '0'],
        ]}
      />

      <h2 id="llm">LLM quality score</h2>
      <p>
        Only when LLM components were detected and evaluated. Starts at 100; deltas are signed so that a positive value
        is a degradation.
      </p>
      <Table
        head={['Evaluator', 'Condition', 'Deduction']}
        rows={[
          ['Hallucination', 'Δ ≤ 0.10', '0'],
          ['Hallucination', '0.10 < Δ ≤ 0.25', '20'],
          ['Hallucination', 'Δ > 0.25', '40'],
          ['Relevance drop', 'Δ > 0.10', '15'],
          ['Groundedness drop', 'Δ > 0.10', '15'],
        ]}
      />

      <h2 id="overall">Overall score</h2>
      <Code>{`no LLM components:   overall = 0.40 × infrastructure + 0.60 × reliability
LLM components:      overall = 0.30 × infrastructure + 0.40 × reliability + 0.30 × llm_quality`}</Code>

      <h2 id="slo-gate">The SLO gate</h2>
      <p>
        With these weights a pure latency regression can never block a merge: infrastructure is 40 % of the score, so
        its worst case still leaves 60 points. The SLO gate closes that hole. If either condition holds, the overall
        score is capped at 49 and the verdict is blocked; the breach is listed first in the evidence.
      </p>
      <ul>
        <li>The p95 delta is a high-confidence regression (over 100 ms) <strong>and</strong> the simulated p95 exceeds the service’s p95 latency budget.</li>
        <li>The error-rate delta exceeds its noise floor <strong>and</strong> the simulated error rate exceeds the service’s error budget.</li>
      </ul>

      <h2 id="verdicts">Verdicts</h2>
      <Table
        head={['Score', 'Verdict', 'Bulwark/production-ready']}
        rows={[
          ['80 – 100', 'Approved', 'success'],
          ['50 – 79', 'Warning', 'success, with caveats in the comment'],
          ['0 – 49', 'Blocked', 'failed — the merge is held until fixed or overridden'],
        ]}
      />

      <h2 id="example">Worked example</h2>
      <p>The demo’s N+1 regression, as measured:</p>
      <Table
        head={['Step', 'Result']}
        rows={[
          ['p95 66 ms → 595 ms', 'Δ = +529 ms, over 100 ms: (529 − 35) / 20 × 30 = 741 → capped at 80'],
          ['Infrastructure', '100 − 80 = 20'],
          ['Errors', 'none — reliability stays 100'],
          ['Weighted overall', '0.4 × 20 + 0.6 × 100 = 68 (warning)'],
          ['SLO gate', '+529 ms > 100 ms and 595 ms > 200 ms budget → capped at 49'],
          ['Verdict', 'Blocked, 49/100; pipeline status failed'],
        ]}
      />

      <h2 id="gemini">What Gemini can and cannot change</h2>
      <p>
        Gemini classifies novel errors and writes the summary, recommendation and historical note. Its classifications
        feed the reliability score; everything else — every delta, the infrastructure and LLM scores, the weights and
        the gate — is recomputed locally after it answers. If Gemini is unavailable, novel errors count as uncertain and
        the verdict is marked <code>deterministic-fallback</code>.
      </p>

      <h2 id="disclosure">Measurement disclosure</h2>
      <p>Every MR comment ends with this block:</p>
      <Code>{`ℹ️ Measurement context: This verdict is based on 200 production request samples.
p95 latency measurement uncertainty: ±35 ms. LLM evaluator noise band: ±0.08.
Signals within these bands are informational observations, not scored regressions.
Bulwark flags probable regressions — it does not certify safety. Human judgement is the final gate.`}</Code>

      <h2 id="tuning">Tuning</h2>
      <p>
        Noise floors and verdict thresholds are configurable: <code>P95_NOISE_FLOOR_MS</code>,{' '}
        <code>ERROR_RATE_NOISE_FLOOR_PERCENT</code>, <code>LLM_EVAL_NOISE_BAND</code>,{' '}
        <code>CONFIDENCE_THRESHOLD_APPROVED</code> and <code>CONFIDENCE_THRESHOLD_WARNING</code>. SLO budgets come from
        Dynatrace per service.
      </p>
    </>
  );
}
