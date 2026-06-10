'use client';

import { useEffect, useState, use } from 'react';

const PIPELINE_STEPS = [
  { id: 'ingesting', label: '1. Ingest MR Event' },
  { id: 'fetching_baseline', label: '2. Fetch Production Baseline' },
  { id: 'history', label: '3. Historical Context' },
  { id: 'provisioning', label: '4. Provision Ephemeral Env' },
  { id: 'replaying', label: '5. Replay Production Traffic' },
  { id: 'evaluating', label: '6. Evaluate LLM Telemetry' },
  { id: 'teardown', label: '7. Teardown Ephemeral Env' },
  { id: 'reasoning', label: '8. Agentic Reasoning' },
  { id: 'enforcing', label: '9. Enforce MR Policy' },
];

export default function SimulationLiveView({ params }: { params: Promise<{ id: string }> }) {
  const unwrappedParams = use(params);
  const { id } = unwrappedParams;
  const [sim, setSim] = useState<any>(null);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    const eventSource = new EventSource(`http://localhost:8000/api/simulation/${id}/stream`);

    eventSource.addEventListener('message', (e) => {
      try {
        const data = JSON.parse(e.data);
        setSim(data);
      } catch (err) {
        console.error('Failed to parse SSE message', err);
      }
    });

    eventSource.addEventListener('error', (e) => {
      console.error('SSE Error', e);
      eventSource.close();
    });

    return () => {
      eventSource.close();
    };
  }, [id]);

  if (error) {
    return <div className="container" style={{ padding: '4rem', color: 'var(--danger)' }}>Error: {error}</div>;
  }

  if (!sim) {
    return <div className="container fade-in" style={{ padding: '4rem', textAlign: 'center' }}>Connecting to Live Stream...</div>;
  }

  const currentStepIndex = PIPELINE_STEPS.findIndex(s => s.id === sim.status);
  const isCompleted = sim.status === 'completed';
  const isFailed = sim.status === 'error';

  return (
    <main className="container fade-in" style={{ padding: '2rem 0', maxWidth: '900px' }}>
      <div style={{ marginBottom: '2rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: '2.5rem', marginBottom: '0.5rem' }}>
            Pipeline <span className="text-gradient">Live View</span>
          </h1>
          <div style={{ color: 'var(--text-secondary)' }}>
            MR #{sim.mr_id} • Service: {sim.service_name} • Head SHA: {sim.head_sha?.substring(0, 8) || 'N/A'}
          </div>
        </div>
        <div>
          <span className={`badge badge-${isCompleted ? 'success' : isFailed ? 'danger' : 'warning'}`} style={{ fontSize: '1rem', padding: '0.5rem 1rem' }}>
            {sim.status.toUpperCase()}
          </span>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: '2rem' }}>
        {/* Pipeline Steps Sidebar */}
        <div className="glass-card" style={{ padding: '1.5rem' }}>
          <h3 style={{ marginBottom: '1.5rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '0.5rem' }}>Execution Flow</h3>
          <div className="progress-container">
            {PIPELINE_STEPS.map((step, idx) => {
              // A step is completed if current index is greater, OR if pipeline is entirely complete
              const isStepCompleted = (currentStepIndex > idx) || isCompleted;
              const isStepActive = (currentStepIndex === idx) && !isCompleted && !isFailed;
              
              return (
                <div key={step.id} className={`progress-step ${isStepActive ? 'active' : ''} ${isStepCompleted ? 'completed' : ''}`}>
                  <div className="step-indicator">
                    {isStepCompleted ? '✓' : idx + 1}
                  </div>
                  <div style={{ fontWeight: isStepActive ? 600 : 400, color: isStepActive ? 'white' : 'inherit' }}>
                    {step.label}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Details & Verdict Area */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
          {isFailed && (
            <div className="glass-card fade-in" style={{ padding: '1.5rem', border: '1px solid rgba(239, 68, 68, 0.3)' }}>
              <h3 style={{ color: 'var(--danger)', marginBottom: '1rem' }}>Pipeline Failed</h3>
              <pre style={{ background: 'rgba(0,0,0,0.3)', padding: '1rem', borderRadius: 'var(--radius-sm)', overflowX: 'auto', fontSize: '0.875rem' }}>
                {JSON.stringify(sim.error_log, null, 2)}
              </pre>
            </div>
          )}

          {isCompleted && sim.verdict && (
            <div className="glass-card fade-in" style={{ padding: '2rem', border: `1px solid ${sim.verdict.verdict === 'approved' ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)'}` }}>
              <h2 style={{ marginBottom: '1.5rem', display: 'flex', alignItems: 'center', gap: '1rem' }}>
                Final Verdict: 
                <span className={sim.verdict.verdict === 'approved' ? 'text-success' : 'text-danger'} style={{ color: sim.verdict.verdict === 'approved' ? 'var(--success)' : 'var(--danger)' }}>
                  {sim.verdict.verdict.toUpperCase()}
                </span>
              </h2>
              
              <div style={{ background: 'rgba(0,0,0,0.2)', padding: '1.5rem', borderRadius: 'var(--radius-md)', marginBottom: '1.5rem' }}>
                <p style={{ fontStyle: 'italic', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                  "{sim.verdict.reasoning}"
                </p>
              </div>

              {sim.dynatrace?.replay_result && (
                <div style={{ marginBottom: '1.5rem' }}>
                  <h4 style={{ marginBottom: '1rem', color: 'var(--text-secondary)' }}>Performance Metrics</h4>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                    <div style={{ background: 'rgba(255,255,255,0.03)', padding: '1rem', borderRadius: 'var(--radius-sm)' }}>
                      <div style={{ fontSize: '0.875rem', color: 'var(--text-muted)' }}>Latency (p95)</div>
                      <div style={{ fontSize: '1.5rem', fontWeight: 600 }}>
                        {sim.dynatrace.replay_result.p95_latency_ms.toFixed(1)} ms
                        <span style={{ fontSize: '0.875rem', marginLeft: '0.5rem', color: sim.dynatrace.replay_result.latency_delta_p95_ms <= 0 ? 'var(--success)' : 'var(--danger)' }}>
                          ({sim.dynatrace.replay_result.latency_delta_p95_ms > 0 ? '+' : ''}{sim.dynatrace.replay_result.latency_delta_p95_ms.toFixed(1)}ms)
                        </span>
                      </div>
                    </div>
                    <div style={{ background: 'rgba(255,255,255,0.03)', padding: '1rem', borderRadius: 'var(--radius-sm)' }}>
                      <div style={{ fontSize: '0.875rem', color: 'var(--text-muted)' }}>Error Rate</div>
                      <div style={{ fontSize: '1.5rem', fontWeight: 600 }}>
                        {sim.dynatrace.replay_result.error_rate_percent.toFixed(2)}%
                        <span style={{ fontSize: '0.875rem', marginLeft: '0.5rem', color: sim.dynatrace.replay_result.error_rate_delta_percent <= 0 ? 'var(--success)' : 'var(--danger)' }}>
                          ({sim.dynatrace.replay_result.error_rate_delta_percent > 0 ? '+' : ''}{sim.dynatrace.replay_result.error_rate_delta_percent.toFixed(2)}%)
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              )}

            </div>
          )}

          {/* Live Logs Mockup (Optional - if we had a log stream) */}
          {!isCompleted && !isFailed && (
            <div className="glass-card fade-in" style={{ padding: '1.5rem', flex: 1, display: 'flex', flexDirection: 'column' }}>
              <h3 style={{ marginBottom: '1rem' }}>System Logs</h3>
              <div style={{ background: '#000', flex: 1, borderRadius: 'var(--radius-sm)', padding: '1rem', fontFamily: 'monospace', fontSize: '0.85rem', color: 'var(--success)', overflowY: 'auto' }}>
                <div>&gt; Connection established.</div>
                <div>&gt; Subscribed to simulation {id}...</div>
                <div className="animate-pulse-shadow" style={{ display: 'inline-block', width: '8px', height: '14px', background: 'var(--success)', marginTop: '0.5rem' }}></div>
              </div>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
