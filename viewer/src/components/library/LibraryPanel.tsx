import { useEffect, useRef, useState } from 'react';
import { useAppContext } from '../../state/AppContext';
import { exportLibrary, type ExportFormat } from '../../lib/export';
import Badge from '../common/Badge';

export default function LibraryPanel() {
  const { state, dispatch } = useAppContext();
  const { libraryIds, librarySpectra, libraryLoading, libraryError, libraryNotice, selectedSensor, downsamplingEnabled, downsampledData } = state;
  const [exportFormat, setExportFormat] = useState<ExportFormat>('csv');
  const spectraById = new Map(librarySpectra.map((s) => [s.id, s]));
  // After a remove, focus moves to the next row's remove button (or the
  // previous one, or the empty-library message), not back to the page start.
  const focusAfterRemove = useRef<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const emptyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const target = focusAfterRemove.current;
    if (target === null) return;
    focusAfterRemove.current = null;
    if (target === '') emptyRef.current?.focus();
    else listRef.current?.querySelector<HTMLElement>(`[data-remove-id="${CSS.escape(target)}"]`)?.focus();
  }, [libraryIds]);
  // Accessible names for the remove buttons: the sample name usually repeats
  // the material, so say it once; where two spectra would sound the same
  // (e.g. one sample measured twice), add the spectrum's own id.
  const spokenNames = (() => {
    const base = new Map<string, string>();
    for (const id of libraryIds) {
      const r = state.recordById.get(id);
      const material = r?.material_name ?? 'Unknown';
      const sample = r?.name ?? id;
      base.set(id, sample.toLowerCase().includes(material.toLowerCase()) ? sample : `${material}, ${sample}`);
    }
    const uses = new Map<string, number>();
    for (const n of base.values()) uses.set(n, (uses.get(n) ?? 0) + 1);
    return new Map([...base].map(([id, n]) => [id, (uses.get(n) ?? 0) > 1 ? `${n} (${id.slice(id.indexOf(':') + 1)})` : n]));
  })();
  const remove = (id: string, index: number) => {
    focusAfterRemove.current = libraryIds[index + 1] ?? libraryIds[index - 1] ?? '';
    dispatch({ type: 'REMOVE_FROM_LIBRARY', id });
  };

  const notice = libraryNotice && (
    <div className="px-4 py-2 bg-amber-50 border border-amber-200 rounded-lg flex items-start justify-between gap-3 text-xs text-amber-900" role="status">
      <span>{libraryNotice}</span>
      <button onClick={() => dispatch({ type: 'SET_LIBRARY_NOTICE', notice: null })} className="text-amber-700 hover:text-amber-900" aria-label="Dismiss">
        ×
      </button>
    </div>
  );

  if (libraryIds.length === 0) {
    return (
      <>
      {notice}
      <div ref={emptyRef} tabIndex={-1} className="bg-white rounded-lg border border-gray-200 px-4 py-3 text-center text-gray-400 focus:outline-none">
        <p className="text-sm">
          Your spectral library is empty.
          <span className="text-gray-500"> Click rows in the table below to add spectra.</span>
        </p>
      </div>
      </>
    );
  }

  const handleExport = () => {
    const items = librarySpectra.map((s) => ({
      spectrum: s,
      downsampled: downsampledData.get(s.id) ?? null,
    }));
    exportLibrary(items, downsamplingEnabled && !!selectedSensor, exportFormat, state.collections);
  };

  return (
    <div className="space-y-2">
    {notice}
    <div className="bg-white rounded-lg border border-gray-200">
      {/* Header */}
      <div className="px-4 py-2.5 border-b border-gray-100">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <h3 className="text-sm font-semibold text-gray-900 whitespace-nowrap">
            My Spectral Library ({libraryIds.length})
            {libraryLoading && (
              <span className="ml-2 text-xs font-normal text-gray-400">Loading...</span>
            )}
          </h3>
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Export format"
              value={exportFormat}
              onChange={(e) => setExportFormat(e.target.value as ExportFormat)}
              className="px-2 py-1.5 text-xs bg-white border border-gray-200 rounded-md
                         focus:outline-none focus:ring-1 focus:ring-indigo-500"
            >
              <option value="csv">CSV</option>
              <option value="envi">ENVI (.sli/.hdr)</option>
            </select>
            <button
              onClick={handleExport}
              disabled={librarySpectra.length === 0}
              className="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded-md hover:bg-indigo-700
                         disabled:opacity-50 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
            >
              Export
            </button>
            <button
              onClick={() => dispatch({ type: 'CLEAR_LIBRARY' })}
              className="px-3 py-1.5 text-xs text-red-600 border border-red-200 rounded-md
                         hover:bg-red-50 transition-colors whitespace-nowrap"
            >
              Clear
            </button>
          </div>
        </div>

        {/* Downsample controls */}
        {selectedSensor && (
          <div className="mt-2 pt-2 border-t border-gray-50">
            <label className="flex items-center gap-2 text-xs text-gray-600">
              <input
                type="checkbox"
                checked={downsamplingEnabled}
                onChange={(e) =>
                  dispatch({ type: 'SET_DOWNSAMPLING_ENABLED', enabled: e.target.checked })
                }
                className="w-3.5 h-3.5 rounded text-indigo-600"
              />
              <span>
                Downsample to <span className="font-medium text-gray-700">{selectedSensor.name}</span>
                <span className="text-gray-400 ml-1">({selectedSensor.bands.length} bands)</span>
              </span>
            </label>
          </div>
        )}
      </div>

      {/* Fetch failure — spectra couldn't be loaded from the parquet files */}
      {libraryError && (
        <div className="px-4 py-2 bg-red-50 border-b border-red-100 flex items-center justify-between gap-3">
          <p className="text-xs text-red-600 truncate" title={libraryError}>
            Failed to load spectral data: {libraryError}
          </p>
          <button
            onClick={() => dispatch({ type: 'RETRY_LIBRARY' })}
            className="px-2 py-1 text-xs bg-white border border-red-200 text-red-600 rounded hover:bg-red-50 flex-shrink-0"
          >
            Retry
          </button>
        </div>
      )}

      {/* Selected spectra list */}
      <div ref={listRef} className="max-h-40 overflow-y-auto divide-y divide-gray-50">
        {libraryIds.map((id, index) => {
          const spectrum = spectraById.get(id);
          const record = state.recordById.get(id);

          const materialName = record?.material_name ?? 'Unknown';
          const sampleName = record?.name ?? id;
          const category = record?.material_category ?? 'other';
          const spoken = spokenNames.get(id) ?? sampleName;

          return (
            <div key={id} className="flex items-center justify-between px-4 py-1.5 hover:bg-gray-50 group">
              <div className="flex items-center gap-2 min-w-0">
                <Badge category={category} />
                <span className="text-sm text-gray-800 font-medium truncate">{materialName}</span>
                <span className="text-xs text-gray-400 truncate hidden sm:inline" title={sampleName}>
                  {sampleName}
                </span>
                {!spectrum && state.duckdbReady && (
                  <span className="text-xs text-gray-400 italic flex-shrink-0">(loading...)</span>
                )}
                {!spectrum && !state.duckdbReady && (
                  <span className="text-xs text-amber-500 italic flex-shrink-0">(waiting for DuckDB)</span>
                )}
              </div>
              {/* Always visible on touch screens; on hover devices it appears on row hover or focus. */}
              <button
                type="button"
                data-remove-id={id}
                onClick={(e) => {
                  e.stopPropagation();
                  remove(id, index);
                }}
                aria-label={`Remove ${spoken} from the library`}
                title="Remove from the library"
                className="text-gray-400 hover:text-red-500 focus:text-red-500 flex-shrink-0 ml-2 p-1 -m-1 rounded transition-opacity
                           focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500
                           [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100
                           [@media(hover:hover)]:group-focus-within:opacity-100 [@media(hover:hover)]:focus:opacity-100"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          );
        })}
      </div>
    </div>
    </div>
  );
}
