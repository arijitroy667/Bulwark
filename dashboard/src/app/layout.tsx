import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Bulwark Dashboard',
  description: 'Automated MR Evaluation Pipeline',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <nav className="navbar">
          <a href="/" className="nav-brand">
            <span className="text-gradient">🛡️ Bulwark</span>
          </a>
          <div>
            <a href="/simulations" className="btn btn-primary" style={{ padding: '0.5rem 1rem', fontSize: '0.875rem' }}>
              History
            </a>
          </div>
        </nav>
        {children}
      </body>
    </html>
  );
}
