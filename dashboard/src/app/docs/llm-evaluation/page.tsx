import type { Metadata } from 'next';
import Link from 'next/link';
import { Code, DocHeader, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'LLM evaluation' };

export default function LlmEvaluation() {
  return (
    <>
      <DocHeader
        eyebrow="Concepts"
        title="LLM evaluation"
        lead="A prompt or model change can make answers worse while latency and error rates stay flat. When a merge request touches LLM code, Bulwark scores the outputs the new build produces and compares them with production."
      />

      <h2 id="detection">When it runs: two-stage detection</h2>
      <p>
        Evaluation runs only when the MR changes LLM components. Matching on loose strings like{' '}
        <code>system_prompt</code> flags most codebases, so detection works in two stages; a match at either one
        enables evaluation.
      </p>
      <h3>Stage 1 — file paths (all changed files)</h3>
      <ul>
        <li>Directories: <code>prompts/</code>, <code>system_prompts/</code>, <code>llm/</code>, <code>ai/</code>, <code>genai/</code>, <code>evals/</code>, <code>evaluations/</code>, <code>langchain/</code></li>
        <li>Files: <code>*_prompt.py</code>, <code>*_prompt.txt</code>, <code>prompt_*.py</code>, <code>*.prompt</code>, <code>*.system</code>, <code>*.jinja</code>, <code>*.jinja2</code></li>
      </ul>
      <h3>Stage 2 — imports (first 50 Python / JS / TS files)</h3>
      <ul>
        <li>Python, parsed with <code>ast</code>: <code>openai</code>, <code>anthropic</code>, <code>google.generativeai</code>, <code>google.genai</code>, <code>vertexai</code>, <code>litellm</code>, <code>langchain*</code>, <code>llama_index</code>, <code>cohere</code>, <code>mistralai</code></li>
        <li>JavaScript / TypeScript imports and requires: <code>openai</code>, <code>@anthropic-ai/sdk</code>, <code>@google/generative-ai</code>, <code>langchain</code>, <code>@langchain/*</code>, <code>llamaindex</code>, <code>@mistralai/mistralai</code>, <code>cohere-ai</code></li>
      </ul>

      <h2 id="traces">The trace contract</h2>
      <p>
        An LLM-backed service exposes <code>GET /traces</code>, returning the prompts and responses it produced while
        the traffic was replayed. A trace needs <code>prompt</code> and <code>response</code> strings;{' '}
        <code>context</code> — the facts the answer should rest on — is optional but makes groundedness meaningful.
      </p>
      <Code>{`GET /traces
{
  "traces": [
    {
      "prompt":   "Summarize the support ticket below ... Ticket: Customer Marcus, order A-1002 ...",
      "response": "Marcus reports that on order A-1002 the card was charged twice ...",
      "context":  "Customer Marcus, order A-1002: the card was charged twice. Requested: a refund ..."
    }
  ]
}`}</Code>
      <p>A bare list, or an object with a <code>spans</code> key, is accepted too.</p>

      <h2 id="evaluators">The evaluators</h2>
      <p>
        Bulwark runs the three Arize / Phoenix LLM-as-judge evaluators with Gemini as the judge. Repeated outputs are
        judged once (up to 20 distinct records per run) and the scores are averaged.
      </p>
      <Table
        head={['Evaluator', 'Score', 'Question asked of each output']}
        rows={[
          ['Hallucination', '0 or 1', 'Does it state anything the context does not support — invented facts, amounts, promises, timelines?'],
          ['Relevance', '0 – 1', 'How well does it do what the prompt asked?'],
          ['Groundedness', '0 – 1', 'What fraction of its claims are supported by the context?'],
        ]}
      />

      <h2 id="baseline">The baseline</h2>
      <Table
        head={['Mode', 'Baseline scores come from']}
        rows={[
          ['Production', <>The latest scored experiment on the Arize dataset <code key="d">bulwark-prod-&lt;service&gt;</code></>],
          ['Demo', 'The production build’s own traces, captured during its baseline replay and judged the same way'],
        ]}
      />
      <p>
        In production, the simulation’s outputs are also archived in Arize as the dataset{' '}
        <code>bulwark-sim-&lt;simulation id&gt;</code> through the Phoenix MCP server.
      </p>

      <h2 id="deltas">Deltas</h2>
      <p>All three deltas are signed so that a positive number means the change made things worse:</p>
      <Code>{`hallucination_delta = simulation − baseline
relevance_delta     = baseline − simulation
groundedness_delta  = baseline − simulation`}</Code>
      <p>
        Deltas within ±0.10 are inside the evaluator noise band and are not scored. How larger deltas turn into points
        is described in <Link href="/docs/scoring#llm">Scoring</Link>; with an LLM score present, it carries 30 % of
        the overall score.
      </p>

      <h2 id="skips">When evaluation is skipped</h2>
      <p>The verdict records the reason, and the overall score falls back to the infrastructure-and-reliability weighting.</p>
      <ul>
        <li>No LLM components detected (the comment says so explicitly).</li>
        <li><code>/traces</code> returned nothing, or no baseline is available.</li>
        <li>The judge is not configured or failed — e.g. no working <code>GEMINI_API_KEY</code>.</li>
      </ul>

      <h2 id="demo">In the demo</h2>
      <p>
        The <code>llm-regression</code> scenario edits the demo service’s ticket-summary prompt from “use only facts
        stated in the ticket” to “reassure the customer and tell them exactly what we will do”. The new build starts
        promising refunds, discounts and delivery dates that no ticket mentions — exactly what the hallucination and
        groundedness evaluators are there to catch, while latency does not move at all.
      </p>
    </>
  );
}
