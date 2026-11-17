import { DocsNav, DocsPager } from './nav';

export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="wrap docs">
      <DocsNav />
      <article className="prose">
        {children}
        <DocsPager />
      </article>
    </div>
  );
}
