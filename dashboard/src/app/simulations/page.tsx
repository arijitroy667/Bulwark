'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

export default function SimulationsPage() {
  const [simulations, setSimulations] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();

  useEffect(() => {
    fetch('http://localhost:8000/api/simulations')
      .then(res => res.json())
      .then(data => {
        setSimulations(data.data || []);
        setIsLoading(false);
      })
      .catch(err => {
        console.error(err);
        setIsLoading(false);
      });
  }, []);

  return (
    <main className="container fade-in" style={{ padding: '2rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem' }}>Recent <span className="text-gradient">Simulations</span></h1>
        <button onClick={() => router.push('/')} className="btn btn-primary" style={{ padding: '0.5rem 1rem', fontSize: '0.875rem' }}>
          + New Setup
        </button>
      </div>

      {isLoading ? (
        <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-secondary)' }}>Loading history...</div>
      ) : (
        <div style={{ display: 'grid', gap: '1rem' }}>
          {simulations.length === 0 ? (
            <div className="glass-card" style={{ padding: '3rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
              No simulations found. Connect your repo to start!
            </div>
          ) : (
            simulations.map(sim => (
              <div key={sim._id} className="glass-card" style={{ padding: '1.5rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer', transition: 'transform 0.2s ease' }} onClick={() => router.push(`/simulation/${sim._id}`)} onMouseOver={(e) => e.currentTarget.style.transform = 'translateY(-2px)'} onMouseOut={(e) => e.currentTarget.style.transform = 'none'}>
                <div>
                  <div style={{ fontWeight: '600', fontSize: '1.1rem', marginBottom: '0.25rem' }}>
                    MR #{sim.mr_id} <span style={{ color: 'var(--text-secondary)', fontWeight: 'normal', fontSize: '0.9rem' }}>• {sim.service_name}</span>
                  </div>
                  <div style={{ color: 'var(--text-muted)', fontSize: '0.875rem' }}>
                    {new Date(sim.created_at).toLocaleString()}
                  </div>
                </div>
                <div>
                  <span className={`badge badge-${sim.status === 'completed' ? 'success' : sim.status === 'error' ? 'danger' : 'warning'}`}>
                    {sim.status}
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </main>
  );
}
