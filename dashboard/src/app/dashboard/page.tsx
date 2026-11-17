'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { API_URL, SCENARIOS } from '@/lib/api';

export default function HomePage() {
  const [pat, setPat] = useState('');
  const [projectId, setProjectId] = useState('');
  const [webhookUrl, setWebhookUrl] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [demoMode, setDemoMode] = useState<boolean | null>(null);
  const [starting, setStarting] = useState('');
  const router = useRouter();

  useEffect(() => {
    fetch(`${API_URL}/health`)
      .then((res) => res.json())
      .then((data) => setDemoMode(Boolean(data.demo_mode)))
      .catch(() => setError(`Cannot reach the Bulwark orchestrator at ${API_URL}`));
  }, []);

  const runScenario = async (scenario: string) => {
    setStarting(scenario);
    setError('');
    try {
      const res = await fetch(`${API_URL}/api/demo/simulations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scenario }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Failed to start simulation');
      router.push(`/simulation/${data.simulation_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStarting('');
    }
  };

  const handleSetup = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setError('');
    try {
      const res = await fetch(`${API_URL}/api/setup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pat, project_id: projectId, webhook_url: webhookUrl }),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || 'Failed to setup webhook');
      }
      router.push('/simulations');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main className="setup-container" style={{ flexDirection: 'column', gap: '2rem' }}>
      {error && (
        <div style={{ maxWidth: '720px', width: '100%', padding: '1rem', background: 'rgba(239, 68, 68, 0.1)', color: 'var(--danger)', borderRadius: 'var(--radius-md)', border: '1px solid rgba(239, 68, 68, 0.2)' }}>
          <strong>Error:</strong> {error}
        </div>
      )}

      {demoMode && (
        <div className="glass-card fade-in" style={{ maxWidth: '720px', width: '100%', padding: '2rem' }}>
          <h1 className="setup-title">
            Run a <span className="text-gradient">simulation</span>
          </h1>
          <p className="setup-subtitle">
            Demo mode: each scenario is a merge request against the local demo-app. Bulwark measures the production
            build, replays 200 requests against the MR build, scores the deltas and writes the MR comment (dry run —
            nothing is posted to GitLab).
          </p>
          <div style={{ display: 'grid', gap: '1rem' }}>
            {SCENARIOS.map((s) => (
              <button
                key={s.id}
                className="btn"
                disabled={Boolean(starting)}
                onClick={() => runScenario(s.id)}
                style={{ textAlign: 'left', display: 'block', width: '100%', padding: '1rem 1.25rem', background: 'var(--bg-secondary)', border: '1px solid var(--border-color)', color: 'var(--text-primary)' }}
              >
                <div style={{ fontWeight: 600 }}>{starting === s.id ? 'Starting…' : s.title}</div>
                <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', fontWeight: 400 }}>{s.text}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="glass-card setup-card fade-in">
        <h1 className="setup-title">
          Connect <span className="text-gradient">GitLab</span>
        </h1>
        <p className="setup-subtitle">
          Register the Bulwark webhook on a GitLab project so every merge request is evaluated automatically.
        </p>

        <form onSubmit={handleSetup}>
          <div className="form-group">
            <label className="form-label" htmlFor="pat">Personal Access Token (PAT)</label>
            <input id="pat" type="password" className="form-input" placeholder="glpat-..." value={pat} onChange={(e) => setPat(e.target.value)} required />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="projectId">GitLab Project ID</label>
            <input id="projectId" type="text" className="form-input" placeholder="e.g., 12345678" value={projectId} onChange={(e) => setProjectId(e.target.value)} required />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="webhookUrl">Webhook URL (Backend)</label>
            <input id="webhookUrl" type="url" className="form-input" placeholder="https://your-host/webhook/gitlab" value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} required />
            <small style={{ color: 'var(--text-muted)', display: 'block', marginTop: '0.5rem', fontSize: '0.75rem' }}>
              Must be a public URL that reaches the Bulwark webhook receiver&apos;s /webhook/gitlab endpoint.
            </small>
          </div>

          <button type="submit" className={`btn btn-primary ${isLoading ? 'animate-pulse-shadow' : ''}`} style={{ width: '100%', marginTop: '1rem' }} disabled={isLoading}>
            {isLoading ? 'Configuring Webhook...' : 'Connect & Automate'}
          </button>
        </form>
      </div>
    </main>
  );
}
