import Link from 'next/link';

export const REPO_URL = 'https://github.com/arijitroy667/Bulwark';

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 2.5 4.5 5.5v6c0 4.6 3.1 8.6 7.5 10 4.4-1.4 7.5-5.4 7.5-10v-6L12 2.5Z" />
      <path d="m8.8 12 2.2 2.2 4.2-4.4" />
    </svg>
  );
}

export function SiteHeader() {
  return (
    <header className="site-header">
      <div className="wrap header-inner">
        <Link href="/" className="brand">
          <Logo />
          Bulwark
        </Link>
        <nav className="header-nav" aria-label="Main">
          <Link href="/docs">Docs</Link>
          <Link href="/dashboard">Dashboard</Link>
          <Link href="/simulations" className="hide-sm">History</Link>
          <a href={REPO_URL} target="_blank" rel="noreferrer">GitHub</a>
        </nav>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="wrap footer-inner">
        <span>Bulwark — pre-merge production simulation. MIT licensed.</span>
        <nav aria-label="Footer">
          <Link href="/docs">Docs</Link>
          <Link href="/docs/quickstart">Quickstart</Link>
          <Link href="/docs/limitations">Limitations</Link>
          <a href={REPO_URL} target="_blank" rel="noreferrer">GitHub</a>
        </nav>
      </div>
    </footer>
  );
}
