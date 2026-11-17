'use client';

import { useEffect, useState, use } from 'react';
import { API_URL, PIPELINE_STEPS, Simulation, TERMINAL_STATUSES, statusBadge, verdictColor } from '@/lib/api';

const cell: React.CSSProperties = { padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border-color)', textAlign: 'left' };
const fmt = (v: number | null | undefined, digits = 0, unit = '') => (v == null ? '—' : `${v.toFixed(digits)}${unit}`);
const signed = (v: number | null | undefined, digits = 0, unit = '') => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(digits)}${unit}`);

export default function SimulationLiveView({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [sim, setSim] = useState<Simulation | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const source = new EventSource(`${API_URL}/api/simulation/${id}/stream`);
    source.addEventListener('message', (e) => {
      const data: Simulation = JSON.parse(e.data);
      setSim(data);
      if (TERMINAL_STATUSES.includes(data.status)) source.close();
    });
    source.addEventListener('error', () => {
      source.close();
      setError((prev) => prev || 'Live stream closed.');
    });
    return () => source.close();
  }, [id]);

  if (!sim) {
    return (
      <div className="container fade-in" style={{ padding: '4rem', textAlign: 'center', color: error ? 'var(--danger)' : undefined }}>
        {error ? `Cannot load simulation ${id} from ${API_URL}` : 'Connecting to live stream...'}
      </div>
    );
  }

  const done = TERMINAL_STATUSES.includes(sim.status);
  const completed = sim.status === 'completed';
  const currentIdx = PIPELINE_STEPS.findIndex((s) => s.id === sim.status);
  const v = sim.verdict;
  const base = sim.dynatrace?.baseline;
  const replay = sim.dynatrace?.replay_result;
  const arize = sim.arize;
  const llmSkipped = completed && !arize?.llm_detected;

  return (
    <main className="container fade-in" style={{ padding: '2rem 0', maxWidth: '1000px' }}>
      <div style={{ marginBottom: '2rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: '2.25rem', marginBottom: '0.5rem' }}>
            Pipeline <span className="text-gradient">Live View</span>
          </h1>
          <div style={{ color: 'var(--text-secondary)' }}>
            {sim.project_id} !{sim.mr_id} • {sim.service_name || '—'} • {sim.source_branch} • {sim.head_sha?.substring(0, 8)}
            {sim.demo_scenario && <> • demo scenario <code>{sim.demo_scenario}</code> (dry run)</>}
          </div>
        </div>
        <span className={`badge badge-${statusBadge(sim)}`} style={{ fontSize: '1rem', padding: '0.5rem 1rem' }}>
          {sim.status.toUpperCase()}
        </span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(220px, 1fr) 2.5fr', gap: '2rem' }}>
        <div className="glass-card" style={{ padding: '1.5rem', alignSelf: 'start' }}>
          <h3 style={{ marginBottom: '1.5rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem' }}>Execution Flow</h3>
          <div className="progress-container">
            {PIPELINE_STEPS.map((step, idx) => {
              const skipped = step.id === 'evaluating' && llmSkipped;
              const isDone = completed || currentIdx > idx;
              const isActive = currentIdx === idx && !done;
              return (
                <div key={step.id} className={`progress-step ${isActive ? 'active' : ''} ${isDone ? 'completed' : ''}`}>
                  <div className="step-indicator">{skipped ? '–' : isDone ? '✓' : idx + 1}</div>
                  <div style={{ fontWeight: isActive ? 600 : 400, color: isActive ? 'white' : 'inherit' }}>
                    {step.label}{skipped ? ' (skipped)' : ''}
                  </div>
                </div>
              );
            })}
          </div>
          {sim.total_duration_ms > 0 && (
            <div style={{ marginTop: '1.5rem', color: 'var(--text-muted)', fontSize: '0.875rem' }}>
              Total: {(sim.total_duration_ms / 1000).toFixed(1)}s
            </div>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', minWidth: 0 }}>
          {!done && (
            <div className="glass-card" style={{ padding: '1.5rem' }}>
              <div className="animate-pulse-shadow" style={{ display: 'inline-block', width: '10px', height: '10px', borderRadius: '50%', background: 'var(--warning)', marginRight: '0.75rem' }} />
              {PIPELINE_STEPS[currentIdx]?.label || 'Queued'}…
            </div>
          )}

          {done && !completed && (
            <div className="glass-card fade-in" style={{ padding: '1.5rem', border: '1px solid rgba(239, 68, 68, 0.3)' }}>
              <h3 style={{ color: sim.status === 'cancelled' ? 'var(--text-secondary)' : 'var(--danger)', marginBottom: '1rem' }}>
                {sim.status === 'cancelled' ? 'Cancelled' : `Simulation ended: ${sim.status}`}
              </h3>
              <ul style={{ paddingLeft: '1.25rem', color: 'var(--text-secondary)' }}>
                {(sim.error_log || []).map((line, i) => <li key={i}>{line}</li>)}
              </ul>
            </div>
          )}

          {v && (
            <div className="glass-card fade-in" style={{ padding: '2rem', border: `1px solid ${verdictColor(v.verdict)}55` }}>
              <h2 style={{ marginBottom: '0.5rem' }}>
                <span style={{ color: verdictColor(v.verdict) }}>{v.verdict}</span> — {v.overall_score}/100
              </h2>
              <p style={{ color: 'var(--text-secondary)', marginBottom: '1.5rem' }}>{v.summary}</p>

              <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', marginBottom: '1.5rem' }}>
                {[
                  ['Infrastructure', v.infrastructure_score],
                  ['Reliability', v.reliability_score],
                  ['LLM quality', v.llm_quality_score],
                ].map(([label, score]) => (
                  <div key={label as string} style={{ background: 'rgba(255,255,255,0.03)', padding: '0.75rem 1rem', borderRadius: 'var(--radius-sm)', minWidth: '130px' }}>
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{label}</div>
                    <div style={{ fontSize: '1.4rem', fontWeight: 600 }}>{score == null ? 'n/a' : `${score}/100`}</div>
                  </div>
                ))}
              </div>

              {replay && base && !replay.skip_reason && (
                <table style={{ width: '100%', borderCollapse: 'collapse', marginBottom: '1.5rem', fontSize: '0.9rem' }}>
                  <thead>
                    <tr style={{ color: 'var(--text-muted)' }}>
                      <th style={cell}>Metric</th><th style={cell}>Baseline</th><th style={cell}>Simulation</th><th style={cell}>Delta</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr><td style={cell}>p95 latency</td><td style={cell}>{fmt(base.p95_latency_ms, 0, 'ms')}</td><td style={cell}>{fmt(replay.actual_p95_ms, 0, 'ms')}</td><td style={cell}>{signed(replay.latency_delta_p95_ms, 0, 'ms')}</td></tr>
                    <tr><td style={cell}>p50 latency</td><td style={cell}>{fmt(base.p50_latency_ms, 0, 'ms')}</td><td style={cell}>{fmt(replay.actual_p50_ms, 0, 'ms')}</td><td style={cell}>{signed(replay.actual_p50_ms - base.p50_latency_ms, 0, 'ms')}</td></tr>
                    <tr><td style={cell}>Error rate</td><td style={cell}>{fmt(base.error_rate_percent, 2, '%')}</td><td style={cell}>{fmt(replay.actual_error_rate_percent, 2, '%')}</td><td style={cell}>{signed(replay.error_rate_delta_percent, 2, '%')}</td></tr>
                    <tr><td style={cell}>5xx / novel errors</td><td style={cell}>0 / 0</td><td style={cell}>{replay.five_xx_count} / {replay.novel_error_messages.length}</td><td style={cell}></td></tr>
                  </tbody>
                </table>
              )}

              {arize?.llm_detected && (
                <div style={{ marginBottom: '1.5rem' }}>
                  <h4 style={{ marginBottom: '0.5rem', color: 'var(--text-secondary)' }}>LLM quality (Arize evaluators)</h4>
                  {arize.skip_reason ? (
                    <div style={{ color: 'var(--warning)' }}>Skipped: {arize.skip_reason}</div>
                  ) : (
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.9rem' }}>
                      <tbody>
                        {([
                          ['Hallucination', arize.baseline_hallucination_score, arize.sim_hallucination_score],
                          ['Relevance', arize.baseline_relevance_score, arize.sim_relevance_score],
                          ['Groundedness', arize.baseline_groundedness_score, arize.sim_groundedness_score],
                        ] as const).map(([name, b, s]) => (
                          <tr key={name}><td style={cell}>{name}</td><td style={cell}>{fmt(b, 2)}</td><td style={cell}>{fmt(s, 2)}</td><td style={cell}>{b != null && s != null ? signed(s - b, 2) : '—'}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}

              {v.evidence.length > 0 && (
                <>
                  <h4 style={{ marginBottom: '0.5rem', color: 'var(--text-secondary)' }}>Evidence</h4>
                  <ul style={{ paddingLeft: '1.25rem', marginBottom: '1.5rem' }}>
                    {v.evidence.map((e, i) => <li key={i} style={{ marginBottom: '0.25rem' }}>{e}</li>)}
                  </ul>
                </>
              )}
              {v.recommendation && <p style={{ marginBottom: '0.75rem' }}><strong>Recommendation:</strong> {v.recommendation}</p>}
              {v.historical_note && <p style={{ color: 'var(--text-secondary)', fontStyle: 'italic' }}>{v.historical_note}</p>}
              <div style={{ marginTop: '1rem', fontSize: '0.8rem', color: 'var(--text-muted)' }}>Reasoning model: {v.gemini_model}</div>
            </div>
          )}

          {sim.gitlab?.comment_body && (
            <div className="glass-card fade-in" style={{ padding: '1.5rem' }}>
              <h3 style={{ marginBottom: '1rem' }}>
                MR comment {sim.demo_scenario ? '(dry run — not posted)' : `(posted · pipeline ${sim.gitlab.pipeline_status})`}
              </h3>
              <pre style={{ whiteSpace: 'pre-wrap', background: 'rgba(0,0,0,0.3)', padding: '1rem', borderRadius: 'var(--radius-sm)', fontSize: '0.8rem', maxHeight: '480px', overflowY: 'auto' }}>
                {sim.gitlab.comment_body}
              </pre>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
