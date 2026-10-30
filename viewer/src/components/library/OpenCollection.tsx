import { useRef, useState } from 'react';
import { useAppContext } from '../../state/AppContext';

/**
 * Open any OpenSpecLib Format collection (a local .parquet file or a URL)
 * and explore it next to the OpenSpecLib data for this session.
 */
export default function OpenCollection() {
  const { state, dispatch, openCollection, cancelOpen, removeSource } = useAppContext();
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const os = state.openState;
  const busy = !!os?.running;
  const opened = [...state.sources.values()].filter((s) => s.kind !== 'release');
  const urlValid = /^https?:\/\/\S+$/i.test(url.trim());

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full flex items-center justify-between px-3 py-2 text-xs border border-dashed border-gray-300 rounded-lg text-gray-600 hover:border-indigo-400 hover:text-indigo-700 transition-colors"
      >
        <span className="flex items-center gap-1.5">
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          Open a collection
        </span>
        <span className="text-[10px] text-gray-500">your own .parquet</span>
      </button>

      {open && (
        <div className="mt-2 p-3 bg-gray-50 border border-gray-200 rounded-lg space-y-2.5 text-xs">
          <p className="text-gray-600">
            Explore any file in the{' '}
            <button
              onClick={() => dispatch({ type: 'SET_VIEW', view: 'format', page: 'reference', section: 'overview' })}
              className="text-indigo-600 hover:text-indigo-800 underline"
            >
              OpenSpecLib Format
            </button>{' '}
            next to the OpenSpecLib data. It is checked first; files that fail a required rule do not open. Nothing is
            uploaded: your browser reads the file in place.
          </p>
          <div className="flex items-center gap-2">
            <button
              onClick={() => inputRef.current?.click()}
              disabled={busy}
              className="px-2.5 py-1.5 bg-white border border-gray-200 rounded-md hover:bg-gray-100 disabled:opacity-50"
            >
              Choose files…
            </button>
            <span className="text-[10px] text-gray-500">An index with its collection files works too.</span>
            <input
              ref={inputRef}
              type="file"
              accept=".parquet,application/vnd.apache.parquet"
              multiple
              className="hidden"
              aria-label="Choose collection files"
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                e.target.value = '';
                if (files.length) void openCollection({ input: { kind: 'files', files }, check: true });
              }}
            />
          </div>
          <form
            className="flex gap-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (urlValid && !busy) void openCollection({ input: { kind: 'url', url: url.trim() }, check: true });
            }}
          >
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://…/collection.parquet"
              aria-label="Collection URL"
              className="flex-1 min-w-0 px-2 py-1.5 bg-white border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
            <button
              type="submit"
              disabled={!urlValid || busy}
              className="px-2.5 py-1.5 bg-indigo-600 text-white rounded-md hover:bg-indigo-700 disabled:opacity-50"
            >
              Open
            </button>
          </form>
          <p className="text-[10px] text-gray-500">A URL must allow cross-origin requests with HTTP range requests.</p>
        </div>
      )}

      {os && (
        <div
          className={`mt-2 p-2.5 rounded-lg border text-xs ${
            os.error ? 'bg-red-50 border-red-200 text-red-800' : os.running ? 'bg-white border-gray-200 text-gray-700' : 'bg-emerald-50 border-emerald-200 text-emerald-900'
          }`}
          role="status"
          aria-live="polite"
        >
          {os.running ? (
            <>
              <div className="flex items-center justify-between gap-2">
                <span className="truncate" title={os.label}>
                  {os.phase}: {os.label}
                </span>
                <button onClick={cancelOpen} className="text-gray-500 hover:text-red-600 flex-shrink-0">
                  Cancel
                </button>
              </div>
              <div className="mt-1.5 h-1 bg-gray-100 rounded-full overflow-hidden">
                <div
                  className={`h-full bg-indigo-500 ${os.fraction == null ? 'w-1/3 animate-pulse' : ''}`}
                  style={os.fraction == null ? undefined : { width: `${Math.max(2, os.fraction * 100)}%` }}
                />
              </div>
            </>
          ) : (
            <div className="flex items-start justify-between gap-2">
              <div className="space-y-1 break-words min-w-0">
                {os.error ? <p>{os.error}</p> : os.messages.map((m, i) => <p key={i}>{m}</p>)}
                {os.error && state.openReports.length > 0 && (
                  <button
                    onClick={() => dispatch({ type: 'SET_VIEW', view: 'format', page: 'check' })}
                    className="underline text-red-700 hover:text-red-900"
                  >
                    Open the checker for a full report
                  </button>
                )}
              </div>
              <button
                onClick={() => dispatch({ type: 'SET_OPEN_STATE', open: null })}
                className="text-gray-400 hover:text-gray-600 flex-shrink-0"
                aria-label="Dismiss"
              >
                ×
              </button>
            </div>
          )}
        </div>
      )}

      {opened.length > 0 && (
        <ul className="mt-2 space-y-1">
          {opened.map((s) => (
            <li key={s.key} className="flex items-center justify-between gap-2 text-[11px] text-gray-600">
              <span className="truncate" title={s.label}>
                <span className="px-1 py-px mr-1 text-[9px] rounded bg-emerald-50 text-emerald-700 border border-emerald-200">
                  opened
                </span>
                {s.label}
              </span>
              <button
                onClick={() => removeSource(s.key)}
                className="text-gray-400 hover:text-red-600 flex-shrink-0"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
