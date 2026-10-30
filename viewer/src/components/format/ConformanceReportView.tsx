import { useState } from 'react';
import type { RuleLevel } from '../../format/spec';
import type { ConformanceReport, RuleResult, RuleStatus, Verdict } from '../../lib/conformance/types';
import { plural } from '../../lib/conformance/results';
import { Prose } from './common';

const STATUS_STYLE: Record<RuleStatus, { label: string; className: string; icon: string }> = {
  pass: { label: 'Pass', className: 'bg-emerald-50 text-emerald-700 border-emerald-200', icon: '✓' },
  fail: { label: 'Fail', className: 'bg-red-50 text-red-700 border-red-200', icon: '✕' },
  warn: { label: 'Warn', className: 'bg-amber-50 text-amber-800 border-amber-200', icon: '!' },
  skip: { label: 'Not checked', className: 'bg-gray-50 text-gray-500 border-gray-200', icon: '–' },
};

const VERDICT_STYLE: Record<Verdict, { label: string; className: string; text: string }> = {
  conformant: {
    label: 'Conformant',
    className: 'bg-emerald-600 text-white',
    text: 'The file meets every required rule.',
  },
  'not-conformant': {
    label: 'Not conformant',
    className: 'bg-red-600 text-white',
    text: 'At least one required rule fails. Details below.',
  },
  incomplete: {
    label: 'Not fully checked',
    className: 'bg-amber-500 text-white',
    text: 'No required rule fails, but part of the check could not run. See the rules marked below.',
  },
};

const LEVEL_TITLES: Record<RuleLevel, string> = {
  must: 'Required rules',
  should: 'Recommendations (warnings only)',
  qa: 'OpenSpecLib quality profile (does not affect conformance)',
};

/**
 * A required rule with warnings (spec section 11: things a newer 1.x version
 * may add) is met: it passes, with the warnings listed.
 */
const MET_WITH_WARNINGS = { ...STATUS_STYLE.warn, label: 'Passed, with warnings' };

/** A quality rule with issues: reported, but it does not make the file non-conformant. */
const QUALITY_ISSUES = { ...STATUS_STYLE.fail, label: 'Issues' };

function StatusPill({ status, level }: { status: RuleStatus; level: RuleLevel }) {
  const s =
    status === 'warn' && level === 'must' ? MET_WITH_WARNINGS : status === 'fail' && level === 'qa' ? QUALITY_ISSUES : STATUS_STYLE[status];
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[11px] font-medium whitespace-nowrap ${s.className}`}>
      <span aria-hidden>{s.icon}</span>
      {s.label}
    </span>
  );
}

function RuleRow({ r }: { r: RuleResult }) {
  const [open, setOpen] = useState(false);
  const detail = r.messages.length > 0 || r.examples.length > 0 || r.waived.length > 0;
  return (
    <tr className="align-top">
      <td className="px-3 py-2 w-px">
        <StatusPill status={r.status} level={r.level} />
      </td>
      <td className="px-2 py-2 w-px font-mono font-semibold text-gray-900">{r.id}</td>
      <td className="px-3 py-2 min-w-0">
        <button
          onClick={() => setOpen(!open)}
          className="text-left text-gray-800 hover:text-indigo-700"
          aria-expanded={open}
          title="Show the rule text"
        >
          {r.title}
          {r.count > 0 && (
            <span className="ml-2 text-gray-500">
              {r.count.toLocaleString('en-US')} {r.count === 1 ? r.unit.replace(/s$/, '') : r.unit}
            </span>
          )}
          {!r.complete && r.status !== 'skip' && <span className="ml-2 text-amber-700">(partly checked)</span>}
        </button>
        {open && (
          <p className="mt-1 text-gray-500">
            <Prose text={r.text} />
          </p>
        )}
        {detail && (
          <div className="mt-1 space-y-1">
            {r.messages.map((m, i) => (
              <p key={i} className={`whitespace-pre-wrap break-words ${r.status === 'skip' ? 'text-gray-400' : 'text-gray-600'}`}>
                {m}
              </p>
            ))}
            {r.waived.length > 0 && (
              <div className="rounded border border-sky-200 bg-sky-50 px-2 py-1.5 text-sky-900">
                <p className="font-medium">
                  {plural(r.waived.length, 'issue')} waived by the allowlist that ships with{' '}
                  <code className="font-mono">openspeclib validate</code>:
                </p>
                <ul className="mt-1 space-y-1 list-disc pl-4">
                  {r.waived.slice(0, 10).map((w, i) => (
                    <li key={i} className="break-words">
                      {w.message}. <span className="text-sky-800">Waived: {w.reason}</span>
                    </li>
                  ))}
                  {r.waived.length > 10 && <li>… and {r.waived.length - 10} more</li>}
                </ul>
              </div>
            )}
            {r.examples.length > 0 && (
              <p className="text-gray-500 break-all">
                e.g.{' '}
                {r.examples.map((e, i) => (
                  <span key={i}>
                    {i > 0 && ', '}
                    <code className="bg-gray-100 px-1 rounded font-mono text-[11px]">{e}</code>
                  </span>
                ))}
              </p>
            )}
          </div>
        )}
      </td>
    </tr>
  );
}

function download(report: ConformanceReport) {
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${report.label.split(/[\\/]/).pop()?.replace(/\.parquet$/i, '') ?? 'report'}.conformance.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function ConformanceReportView({
  report,
  nested = false,
  onExplore,
}: {
  report: ConformanceReport;
  nested?: boolean;
  /** Offered for conforming files: open them in the explorer. */
  onExplore?: () => void;
}) {
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [showFooter, setShowFooter] = useState(false);
  const [showColumns, setShowColumns] = useState(false);
  const v = VERDICT_STYLE[report.verdict];
  // Leave out rules for the other kind of file (I1 on a collection file, the
  // collection rules on an index), and the quality profile when it is off.
  const fileKind = report.kind === 'index' ? 'index' : 'collection';
  const shown = report.unreadable
    ? report.results.filter((r) => r.status !== 'skip')
    : report.results.filter((r) => r.appliesTo === fileKind && (r.level !== 'qa' || report.qualityProfile));
  // Conformance (required rules and recommendations) and the quality profile
  // are counted apart: quality findings never change the verdict.
  const counts = { pass: 0, fail: 0, warn: 0, skip: 0 } as Record<RuleStatus, number>;
  for (const r of shown) if (r.level !== 'qa') counts[r.status]++;
  const quality = shown.filter((r) => r.level === 'qa');
  const qualityCounts = {
    fail: quality.filter((r) => r.status === 'fail').length,
    warn: quality.filter((r) => r.status === 'warn').length,
    skip: quality.filter((r) => r.status === 'skip').length,
    waived: quality.reduce((n, r) => n + r.waived.length, 0),
  };
  const levels = (['must', 'should', 'qa'] as RuleLevel[]).filter((l) => shown.some((r) => r.level === l));
  const footerPretty = (() => {
    if (!report.footerText) return null;
    try {
      return JSON.stringify(JSON.parse(report.footerText), null, 2);
    } catch {
      return report.footerText;
    }
  })();

  return (
    <div className={`bg-white rounded-lg border ${nested ? 'border-gray-200' : 'border-gray-300 shadow-sm'}`}>
      <div className="px-4 py-3 border-b border-gray-100 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`px-2 py-0.5 rounded text-xs font-semibold ${v.className}`}>{v.label}</span>
            <h3 className="text-sm font-semibold text-gray-900 break-all">{report.label}</h3>
            <span className="text-[11px] text-gray-500">
              {report.kind === 'index' ? 'index file' : report.kind === 'collection' ? 'collection file' : ''}
            </span>
          </div>
          <p className="text-xs text-gray-500 mt-1">{v.text}</p>
          {!report.unreadable && (
            <p className="text-[11px] text-gray-500 mt-1">
              {plural(report.numRows, 'row')} · {plural(report.numRowGroups, 'row group')} ·{' '}
              {plural(report.columns.length, 'column')} · checked in {(report.durationMs / 1000).toFixed(1)} s
              {report.createdBy && <> · written by {report.createdBy}</>}
            </p>
          )}
        </div>
        <div className="flex flex-col items-end gap-1 text-xs">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="text-gray-500">Format rules:</span>
            <span className="text-emerald-700">{counts.pass} passed</span>
            <span className="text-red-700">{counts.fail} failed</span>
            <span className="text-amber-700">{plural(counts.warn, 'warning')}</span>
            {!report.unreadable && <span className="text-gray-400">{counts.skip} not checked</span>}
          </div>
          {quality.length > 0 && !report.unreadable && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="text-gray-500">Quality profile:</span>
              <span className={qualityCounts.fail ? 'text-red-700' : 'text-emerald-700'}>
                {qualityCounts.fail === 0 ? 'no issues' : `${plural(qualityCounts.fail, 'rule')} with issues`}
              </span>
              {qualityCounts.warn > 0 && <span className="text-amber-700">{plural(qualityCounts.warn, 'warning')}</span>}
              {qualityCounts.waived > 0 && <span className="text-sky-700">{qualityCounts.waived} waived</span>}
              {qualityCounts.skip > 0 && <span className="text-gray-400">{qualityCounts.skip} not checked</span>}
            </div>
          )}
          <div className="flex items-center gap-3">
          {!nested && onExplore && report.verdict === 'conformant' && (
            <button
              onClick={onExplore}
              className="px-2 py-1 rounded-md bg-indigo-600 text-white hover:bg-indigo-700"
              title="Open this file in the explorer for this session"
            >
              Explore this file
            </button>
          )}
          {!nested && (
            <button
              onClick={() => download(report)}
              className="px-2 py-1 border border-gray-200 rounded-md text-gray-600 hover:bg-gray-50"
              title="Save this report as JSON"
            >
              Save JSON
            </button>
          )}
          </div>
        </div>
      </div>

      {report.unreadable && (
        <div className="mx-4 mt-3 px-3 py-2 rounded-md bg-red-50 border border-red-200 text-xs text-red-800">
          <strong>This is not a Parquet file DuckDB can read.</strong> Check that it is a complete .parquet file, not CSV,
          Excel or a partial download. <span className="text-red-600 break-all">({report.unreadable})</span>
        </div>
      )}
      {!report.unreadable && report.kind === 'unknown' && (
        <div className="mx-4 mt-3 px-3 py-2 rounded-md bg-amber-50 border border-amber-200 text-xs text-amber-900">
          <strong>This Parquet file has no <code>openspeclib</code> footer key</strong>, so it is not an OpenSpecLib file
          yet. The report below shows what else would need to change; add the footer metadata first.
        </div>
      )}

      <div className="px-4 py-2 flex items-center gap-4 text-xs text-gray-600">
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} />
          Only failures and warnings
        </label>
        {footerPretty && (
          <button onClick={() => setShowFooter(!showFooter)} className="text-indigo-600 hover:text-indigo-800">
            {showFooter ? 'Hide' : 'Show'} footer JSON
          </button>
        )}
        {report.columns.length > 0 && (
          <button onClick={() => setShowColumns(!showColumns)} className="text-indigo-600 hover:text-indigo-800">
            {showColumns ? 'Hide' : 'Show'} columns
          </button>
        )}
      </div>
      {showFooter && footerPretty && (
        <pre className="mx-4 mb-2 max-h-72 overflow-auto bg-slate-900 text-slate-100 text-[11px] p-3 rounded-md">{footerPretty}</pre>
      )}
      {showColumns && (
        <div className="mx-4 mb-2 text-[11px] text-gray-600 flex flex-wrap gap-x-4 gap-y-1">
          {report.columns.map((c, i) => (
            <span key={i}>
              <span className="font-mono text-gray-900">{c.name}</span> <span className="font-mono text-indigo-700">{c.type}</span>
              {c.arrowType && <span className="font-mono text-gray-400"> (Arrow hint: {c.arrowType})</span>}
            </span>
          ))}
          <span className="text-gray-500">
            (types from the Parquet schema, which decide rules F2–F4; Arrow hints are shown for reference)
</span>
        </div>
      )}

      <div className="px-4 pb-4 space-y-3">
        {levels.map((level) => {
          const rows = shown.filter(
            (r) => r.level === level && (!onlyProblems || r.status === 'fail' || r.status === 'warn' || r.waived.length > 0),
          );
          if (rows.length === 0) return null;
          return (
            <div key={level}>
              <h4 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide mb-1">{LEVEL_TITLES[level]}</h4>
              <div className="overflow-x-auto border border-gray-200 rounded-md">
                <table className="w-full text-xs">
                  <tbody className="divide-y divide-gray-100">
                    {rows.map((r) => (
                      <RuleRow key={r.id} r={r} />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
        {fileKind === 'index' && !report.unreadable && (
          <p className="text-xs text-gray-500">
            Rules F1–F13 and R1–R5 apply to collection files; the ones listed in this index are checked below.
          </p>
        )}
        {fileKind === 'collection' && !report.qualityProfile && !report.unreadable && (
          <p className="text-xs text-gray-400">The OpenSpecLib quality profile (Q1–Q4) was not applied.</p>
        )}
        {report.collections.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide">
              Collection files listed in this index
            </h4>
            <p className="text-xs text-gray-500">An index conforms only if every collection file it lists conforms.</p>
            {report.collections.map((c, i) => (
              <ConformanceReportView key={i} report={c} nested />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
