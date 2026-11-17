'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

export const DOC_GROUPS = [
  {
    title: 'Getting started',
    pages: [
      { href: '/docs', label: 'Introduction' },
      { href: '/docs/quickstart', label: 'Quickstart' },
    ],
  },
  {
    title: 'Concepts',
    pages: [
      { href: '/docs/how-it-works', label: 'How it works' },
      { href: '/docs/scoring', label: 'Scoring' },
      { href: '/docs/llm-evaluation', label: 'LLM evaluation' },
    ],
  },
  {
    title: 'Reference',
    pages: [
      { href: '/docs/integrations', label: 'Integrations' },
      { href: '/docs/configuration', label: 'Configuration' },
      { href: '/docs/deployment', label: 'Deployment' },
      { href: '/docs/limitations', label: 'Limitations' },
    ],
  },
];

const PAGES = DOC_GROUPS.flatMap((g) => g.pages);

export function DocsNav() {
  const pathname = usePathname();
  return (
    <nav className="docs-nav" aria-label="Documentation">
      {DOC_GROUPS.map((group) => (
        <div className="docs-nav-group" key={group.title}>
          <p className="docs-nav-title">{group.title}</p>
          {group.pages.map((p) => (
            <Link key={p.href} href={p.href} className={pathname === p.href ? 'active' : ''} aria-current={pathname === p.href ? 'page' : undefined}>
              {p.label}
            </Link>
          ))}
        </div>
      ))}
    </nav>
  );
}

export function DocsPager() {
  const pathname = usePathname();
  const i = PAGES.findIndex((p) => p.href === pathname);
  if (i === -1) return null;
  const prev = PAGES[i - 1];
  const next = PAGES[i + 1];
  return (
    <div className="pager">
      {prev && (
        <Link href={prev.href}>
          <small>Previous</small>
          <span>{prev.label}</span>
        </Link>
      )}
      {next && (
        <Link href={next.href} className="next">
          <small>Next</small>
          <span>{next.label}</span>
        </Link>
      )}
    </div>
  );
}
