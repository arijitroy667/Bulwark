'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';

export default function SetupPage() {
  const [pat, setPat] = useState('');
  const [projectId, setProjectId] = useState('');
  const [webhookUrl, setWebhookUrl] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const router = useRouter();

  const handleSetup = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setError('');

    try {
      const res = await fetch('http://localhost:8000/api/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pat,
          project_id: projectId,
          webhook_url: webhookUrl,
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || 'Failed to setup webhook');
      }

      // Success - we can redirect to a simulations list or just show success
      router.push('/simulations');
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main className="setup-container">
      <div className="glass-card setup-card fade-in">
        <h1 className="setup-title">
          Connect <span className="text-gradient">GitLab</span>
        </h1>
        <p className="setup-subtitle">
          Configure Bulwark to automatically evaluate Merge Requests with zero human intervention.
        </p>

        {error && (
          <div style={{ marginBottom: '1.5rem', padding: '1rem', background: 'rgba(239, 68, 68, 0.1)', color: 'var(--danger)', borderRadius: 'var(--radius-md)', border: '1px solid rgba(239, 68, 68, 0.2)' }}>
            <strong>Error:</strong> {error}
          </div>
        )}

        <form onSubmit={handleSetup}>
          <div className="form-group">
            <label className="form-label" htmlFor="pat">Personal Access Token (PAT)</label>
            <input
              id="pat"
              type="password"
              className="form-input"
              placeholder="glpat-..."
              value={pat}
              onChange={(e) => setPat(e.target.value)}
              required
            />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="projectId">GitLab Project ID</label>
            <input
              id="projectId"
              type="text"
              className="form-input"
              placeholder="e.g., 12345678"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              required
            />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="webhookUrl">Webhook URL (Backend)</label>
            <input
              id="webhookUrl"
              type="url"
              className="form-input"
              placeholder="https://your-ngrok-url.ngrok.io/webhook/gitlab"
              value={webhookUrl}
              onChange={(e) => setWebhookUrl(e.target.value)}
              required
            />
            <small style={{ color: 'var(--text-muted)', display: 'block', marginTop: '0.5rem', fontSize: '0.75rem' }}>
              Must point to the Bulwark backend /webhook/gitlab endpoint.
            </small>
          </div>

          <button
            type="submit"
            className={`btn btn-primary ${isLoading ? 'animate-pulse-shadow' : ''}`}
            style={{ width: '100%', marginTop: '1rem' }}
            disabled={isLoading}
          >
            {isLoading ? 'Configuring Webhook...' : 'Connect & Automate'}
          </button>
        </form>
      </div>
    </main>
  );
}
