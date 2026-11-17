// Orchestrator API (orchestrator/main.py). Inlined at build time — see .env.example.
export const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000';

export const TERMINAL_STATUSES = [
  'completed', 'cancelled', 'error', 'timed_out',
  'build_failed', 'build_timeout', 'health_check_failed', 'runtime_crash',
];

// Order in which orchestrator/agent.py moves a simulation through its statuses
export const PIPELINE_STEPS = [
  { id: 'ingesting', label: 'Ingest MR + LLM detection' },
  { id: 'fetching_baseline', label: 'Production baseline + history' },
  { id: 'provisioning', label: 'Provision ephemeral environment' },
  { id: 'replaying', label: 'Replay production traffic' },
  { id: 'evaluating', label: 'LLM quality evaluation' },
  { id: 'reasoning', label: 'Teardown + Gemini reasoning' },
  { id: 'enforcing', label: 'Enforce verdict on MR' },
];

export const SCENARIOS = [
  { id: 'regression', title: 'N+1 regression', text: 'GET /users loads preferences one user at a time. Expect BLOCKED.' },
  { id: 'fixed', title: 'Push the fix', text: 'Same MR, batched query. Cancels an in-flight run. Expect APPROVED.' },
  { id: 'llm-regression', title: 'Prompt change', text: 'Summary prompt starts over-promising. Arize-style evals (needs Gemini).' },
];

export type Verdict = {
  verdict: 'APPROVED' | 'WARNING' | 'BLOCKED';
  overall_score: number;
  infrastructure_score: number;
  reliability_score: number;
  llm_quality_score: number | null;
  summary: string;
  evidence: string[];
  recommendation: string;
  historical_note: string;
  gemini_model: string;
};

export type Simulation = {
  _id: string;
  mr_id: number;
  project_id: string;
  service_name?: string;
  source_branch: string;
  head_sha: string;
  demo_scenario?: string;
  status: string;
  created_at: string;
  total_duration_ms: number;
  error_log: string[] | null;
  verdict: Verdict | null;
  dynatrace?: {
    baseline?: { p50_latency_ms: number; p95_latency_ms: number; error_rate_percent: number; slo_latency_budget_p95_ms: number };
    requests_sampled?: number;
    replay_result?: {
      actual_p50_ms: number; actual_p95_ms: number; actual_error_rate_percent: number;
      latency_delta_p95_ms: number; error_rate_delta_percent: number; five_xx_count: number;
      novel_error_messages: string[]; total_requests: number; skip_reason: string | null;
    };
  };
  arize?: {
    llm_detected?: boolean; skip_reason?: string | null;
    hallucination_delta?: number | null; relevance_delta?: number | null; groundedness_delta?: number | null;
    sim_hallucination_score?: number | null; sim_relevance_score?: number | null; sim_groundedness_score?: number | null;
    baseline_hallucination_score?: number | null; baseline_relevance_score?: number | null; baseline_groundedness_score?: number | null;
  };
  gitlab?: { comment_body?: string; pipeline_status?: string };
};

export function verdictColor(v?: string): string {
  return v === 'APPROVED' ? 'var(--success)' : v === 'WARNING' ? 'var(--warning)' : 'var(--danger)';
}

export function statusBadge(sim: Pick<Simulation, 'status' | 'verdict'>): string {
  if (sim.verdict) return sim.verdict.verdict === 'APPROVED' ? 'success' : sim.verdict.verdict === 'WARNING' ? 'warning' : 'danger';
  if (sim.status === 'cancelled') return 'neutral';
  return TERMINAL_STATUSES.includes(sim.status) ? 'danger' : 'warning';
}
