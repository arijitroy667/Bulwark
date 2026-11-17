'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { API_URL, Simulation, statusBadge } from '@/lib/api';

export default function SimulationsPage() {
  const [simulations, setSimulations] = useState<Simulation[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const router = useRouter();

  useEffect(() => {
    fetch(`${API_URL}/api/simulations?limit=50`)
      .then((res) => res.json())
      .then((data) => setSimulations(data.data || []))
      .catch(() => setError(`Cannot reach the Bulwark orchestrator at ${API_URL}`))
      .finally(() => setIsLoading(false));
  }, []);

  return (
    <main className="container fade-in" style={{ padding: '2rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem' }}>Recent <span className="text-gradient">Simulations</span></h1>
        <button onClick={() => router.push('/dashboard')} className="btn btn-primary" style={{ padding: '0.5rem 1rem', fontSize: '0.875rem' }}>
          + Run / Connect
        </button>
      </div>

      {error && <div style={{ color: 'var(--danger)', marginBottom: '1rem' }}>{error}</div>}

      {isLoading ? (
        <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-secondary)' }}>Loading history...</div>
      ) : simulations.length === 0 ? (
        <div className="glass-card" style={{ padding: '3rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
          No simulations yet. Run a demo scenario or connect a GitLab project.
        </div>
      ) : (
        <div style={{ display: 'grid', gap: '1rem' }}>
          {simulations.map((sim) => (
            <div
              key={sim._id}
              className="glass-card"
              style={{ padding: '1.25rem 1.5rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer' }}
              onClick={() => router.push(`/simulation/${sim._id}`)}
            >
              <div>
                <div style={{ fontWeight: 600, fontSize: '1.05rem', marginBottom: '0.25rem' }}>
                  {sim.project_id} !{sim.mr_id}
                  <span style={{ color: 'var(--text-secondary)', fontWeight: 'normal', fontSize: '0.9rem' }}>
                    {' '}• {sim.service_name || '—'} • {sim.source_branch}{sim.demo_scenario ? ` • demo: ${sim.demo_scenario}` : ''}
                  </span>
                </div>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.875rem' }}>
                  {new Date(sim.created_at).toLocaleString()} • {sim._id}
                  {sim.total_duration_ms ? ` • ${(sim.total_duration_ms / 1000).toFixed(0)}s` : ''}
                </div>
              </div>
              <span className={`badge badge-${statusBadge(sim)}`}>
                {sim.verdict ? `${sim.verdict.verdict} ${sim.verdict.overall_score}/100` : sim.status}
              </span>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
