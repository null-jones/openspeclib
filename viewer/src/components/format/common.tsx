import { useState, type ReactNode } from 'react';

/** Render text with `backtick` spans as inline code. */
export function Prose({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith('`') && p.endsWith('`') && p.length > 1 ? (
          <Code key={i}>{p.slice(1, -1)}</Code>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="bg-gray-100 text-gray-800 px-1 py-px rounded text-[0.85em] font-mono">{children}</code>;
}

/** A section card in the Format reference. */
export function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={`fmt-${id}`} className="bg-white rounded-lg border border-gray-200 scroll-mt-4">
      {/* tabIndex -1: the section navigation moves focus here. */}
      <h2 tabIndex={-1} className="px-5 py-3 border-b border-gray-100 text-base font-semibold text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-500">
        {title}
      </h2>
      <div className="px-5 py-4 space-y-3 text-sm text-gray-700 leading-relaxed">{children}</div>
    </section>
  );
}

export function SubHeading({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-semibold text-gray-700 uppercase tracking-wide pt-2">{children}</h3>;
}

/** Compact table used throughout the reference. */
export function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto border border-gray-200 rounded-md">
      <table className="w-full text-xs">
        <thead className="bg-gray-50 text-gray-500">
          <tr>
            {head.map((h) => (
              <th key={h} className="text-left font-medium px-3 py-2 whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((r, i) => (
            <tr key={i} className="align-top">
              {r.map((c, j) => (
                <td key={j} className="px-3 py-2 text-gray-700">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Code block with a copy button. */
export function CodeBlock({ code, label, note }: { code: string; label?: string; note?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div className="rounded-md border border-slate-800 overflow-hidden">
      <div className="flex items-center justify-between bg-slate-800 px-3 py-1.5">
        <span className="text-[11px] text-slate-300 font-medium">
          {label}
          {note && <span className="ml-2 px-1.5 py-px rounded bg-amber-500/20 text-amber-300 font-normal">{note}</span>}
        </span>
        <button
          onClick={copy}
          className="text-[11px] text-slate-400 hover:text-slate-100 transition-colors"
          aria-label={`Copy ${label ?? 'code'}`}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="bg-slate-900 text-slate-100 text-xs leading-relaxed p-3 overflow-x-auto">
        <code>{code}</code>
      </pre>
    </div>
  );
}

export function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:text-indigo-800 underline">
      {children}
    </a>
  );
}
