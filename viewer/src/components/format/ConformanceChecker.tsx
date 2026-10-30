import { useEffect, useRef, useState, type DragEvent } from 'react';
import { useAppContext } from '../../state/AppContext';
import { runBrowserCheck, type CheckInput } from '../../lib/conformance/browser';
import { baseName } from '../../lib/conformance/files';
import { CheckAbortedError, type ConformanceReport, type Progress } from '../../lib/conformance/types';
import { FORMAT_VERSION } from '../../format/spec';
import ConformanceReportView from './ConformanceReportView';

/** Shortest interval between progress announcements within one phase. */
const ANNOUNCE_EVERY_MS = 5000;

interface Running {
  label: string;
  progress: Progress;
}

export default function ConformanceChecker() {
  const { state, dispatch, openCollection } = useAppContext();
  const [lastInput, setLastInput] = useState<CheckInput | null>(null);
  const [qualityProfile, setQualityProfile] = useState(false);
  const [url, setUrl] = useState('');
  const [dragging, setDragging] = useState(false);
  const [running, setRunning] = useState<Running | null>(null);
  const [reports, setReports] = useState<ConformanceReport[]>([]);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // Screen-reader announcements: phase changes, then at most every few seconds.
  const [announcement, setAnnouncement] = useState('');
  const lastAnnounced = useRef({ phase: '', at: 0 });
  const announce = (text: string, phase = '') => {
    const now = Date.now();
    if (phase && phase === lastAnnounced.current.phase && now - lastAnnounced.current.at < ANNOUNCE_EVERY_MS) return;
    lastAnnounced.current = { phase, at: now };
    setAnnouncement(text);
  };

  useEffect(() => () => abortRef.current?.abort(), []);

  const start = async (input: CheckInput) => {
    abortRef.current?.abort();
    const ctl = new AbortController();
    abortRef.current = ctl;
    setError(null);
    setReports([]);
    setLastInput(input);
    setRunning({ label: input.kind === 'url' ? input.url : input.files.map((f) => f.name).join(', '), progress: { phase: 'Starting the query engine', fraction: null } });
    try {
      const result = await runBrowserCheck(input, {
        qualityProfile,
        signal: ctl.signal,
        onProgress: (label, progress) => {
          setRunning({ label, progress });
          const pctText = progress.fraction != null ? `, ${Math.round(progress.fraction * 100)} %` : '';
          announce(`${progress.phase}${pctText}`, progress.phase);
        },
      });
      if (!ctl.signal.aborted) {
        setReports(result);
        const verdicts = result.map((r) => `${baseName(r.label)}: ${r.verdict === 'conformant' ? 'conformant' : r.verdict === 'not-conformant' ? 'not conformant' : 'not fully checked'}`);
        announce(`Check finished. ${verdicts.join('. ')}.`);
      }
    } catch (e) {
      if (e instanceof CheckAbortedError || ctl.signal.aborted) setError('Check cancelled.');
      else setError(e instanceof Error ? e.message : String(e));
      announce(ctl.signal.aborted ? 'Check cancelled.' : 'The check failed.');
    } finally {
      if (abortRef.current === ctl) {
        setRunning(null);
        abortRef.current = null;
      }
    }
  };

  // Open a file that passed in the explorer, without checking it again.
  const explore = async (report: ConformanceReport, input: CheckInput) => {
    // Match dropped files by name: index entries may carry relative paths.
    const names = new Set([report.label, ...report.collections.map((c) => c.label)].map(baseName));
    const req: CheckInput = input.kind === 'url' ? input : { kind: 'files', files: input.files.filter((f) => names.has(f.name)) };
    dispatch({ type: 'SET_VIEW', view: 'explore' });
    await openCollection({ input: req, check: false });
  };

  const onFiles = (files: FileList | null) => {
    const list = Array.from(files ?? []);
    if (list.length > 0) void start({ kind: 'files', files: list });
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (!running) onFiles(e.dataTransfer.files);
  };

  const urlValid = /^https?:\/\/\S+$/i.test(url.trim());
  const busy = running !== null;
  const pct = running?.progress.fraction != null ? Math.round(running.progress.fraction * 100) : null;

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg border border-gray-200">
        <div className="px-5 py-3 border-b border-gray-100">
          <h2 className="text-base font-semibold text-gray-900">Check a file</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            Checks a Parquet file against every rule of the OpenSpecLib Format {FORMAT_VERSION}.
          </p>
        </div>
        <div className="px-5 py-4 space-y-4 text-sm">
          <div className="flex items-start gap-2 text-xs text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-md px-3 py-2">
            <svg className="w-4 h-4 flex-shrink-0 mt-px" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
            </svg>
            <span>
              Nothing is uploaded. Your browser reads the file in place with DuckDB-WASM; no data leaves this computer. A URL
              is fetched by your browser straight from that server.
            </span>
          </div>

          <div
            onDragOver={(e) => {
              e.preventDefault();
              if (!busy) setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`border-2 border-dashed rounded-lg px-4 py-8 text-center transition-colors ${
              dragging ? 'border-indigo-400 bg-indigo-50' : 'border-gray-300 bg-gray-50'
            } ${busy ? 'opacity-60' : ''}`}
          >
            <p className="text-sm text-gray-700">
              Drop .parquet files here, or{' '}
              <button
                onClick={() => inputRef.current?.click()}
                disabled={busy}
                className="text-indigo-600 hover:text-indigo-800 underline disabled:no-underline disabled:text-gray-400"
              >
                choose files
              </button>
              .
            </p>
            <p className="text-xs text-gray-500 mt-1">
              To check an index file, drop it together with the collection files it lists.
            </p>
            <input
              ref={inputRef}
              type="file"
              accept=".parquet,application/vnd.apache.parquet"
              multiple
              className="hidden"
              onChange={(e) => {
                onFiles(e.target.files);
                e.target.value = '';
              }}
            />
          </div>

          <form
            className="flex flex-wrap gap-2 items-center"
            onSubmit={(e) => {
              e.preventDefault();
              if (urlValid && !busy) void start({ kind: 'url', url: url.trim() });
            }}
          >
            <label htmlFor="check-url" className="text-xs text-gray-600">
              Or a URL:
            </label>
            <input
              id="check-url"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://example.org/my_library.parquet"
              className="flex-1 min-w-[16rem] px-2.5 py-1.5 text-xs border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
            <button
              type="submit"
              disabled={!urlValid || busy}
              className="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded-md hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Check URL
            </button>
            <p className="w-full text-[11px] text-gray-500">
              The server must allow cross-origin requests (CORS) and HTTP range requests. Collection files an index lists
              are fetched relative to its URL.
            </p>
          </form>

          <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
            <input type="checkbox" checked={qualityProfile} onChange={(e) => setQualityProfile(e.target.checked)} disabled={busy} />
            Also apply the OpenSpecLib quality profile (Q1–Q4: value ranges per quantity and plausible wavelength extents)
          </label>

          {state.duckdbError && (
            <p className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-md px-3 py-2">
              The query engine failed to start, so files cannot be checked: {state.duckdbError}
            </p>
          )}
        </div>
      </div>

      {/* Always mounted, so screen readers announce updates; throttled (see announce). */}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      {running && (
        <div className="bg-white rounded-lg border border-gray-200 px-5 py-4 space-y-2">
          <div className="flex items-center justify-between gap-3 text-xs">
            <span className="text-gray-700 truncate">
              <strong className="font-medium">{running.label}</strong> · {running.progress.phase}
              {running.progress.rowsTotal != null && (
                <span className="text-gray-500">
                  {' '}
                  ({(running.progress.rowsChecked ?? 0).toLocaleString('en-US')} of {running.progress.rowsTotal.toLocaleString('en-US')} rows)
                </span>
              )}
            </span>
            <button onClick={() => abortRef.current?.abort()} className="text-gray-500 hover:text-red-600">
              Cancel
            </button>
          </div>
          <div
            className="h-1.5 bg-gray-100 rounded-full overflow-hidden"
            role="progressbar"
            aria-label="Check progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct ?? undefined}
          >
            <div
              className={`h-full bg-indigo-500 transition-all ${pct === null ? 'w-1/3 animate-pulse' : ''}`}
              style={pct === null ? undefined : { width: `${Math.max(2, pct)}%` }}
            />
          </div>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-5 py-3 text-xs text-red-800" role="alert">
          {error}
        </div>
      )}

      {reports.map((r, i) => (
        <ConformanceReportView key={i} report={r} onExplore={lastInput ? () => void explore(r, lastInput) : undefined} />
      ))}
      {reports.length === 0 && !running && state.openReports.length > 0 && (
        <>
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
            Reports from your last "Open a collection"
          </h3>
          {state.openReports.map((r, i) => (
            <ConformanceReportView key={`open-${i}`} report={r} />
          ))}
        </>
      )}
    </div>
  );
}
