import { useState } from 'react';
import { useAppContext } from '../../state/AppContext';
import { exportLibrary, type ExportFormat } from '../../lib/export';
import Badge from '../common/Badge';

export default function LibraryPanel() {
  const { state, dispatch } = useAppContext();
  const { libraryIds, librarySpectra, libraryLoading, selectedSensor, downsamplingEnabled, downsampledData } = state;
  const [exportFormat, setExportFormat] = useState<ExportFormat>('csv');

  if (libraryIds.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 px-4 py-3 text-center text-gray-400">
        <p className="text-sm">
          Your spectral library is empty.
          <span className="text-gray-500"> Click rows in the table below to add spectra.</span>
        </p>
      </div>
    );
  }

  const handleExport = () => {
    const items = librarySpectra.map((s) => ({
      spectrum: s,
      downsampled: downsampledData.get(s.id) ?? null,
    }));
    exportLibrary(items, downsamplingEnabled && !!selectedSensor, exportFormat);
  };

  return (
    <div className="bg-white rounded-lg border border-gray-200">
      {/* Header */}
      <div className="px-4 py-2.5 border-b border-gray-100">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-900 whitespace-nowrap">
            My Spectral Library ({libraryIds.length})
            {libraryLoading && (
              <span className="ml-2 text-xs font-normal text-gray-400">Loading...</span>
            )}
          </h3>
          <div className="flex items-center gap-2 flex-shrink-0">
            <select
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

      {/* Selected spectra list */}
      <div className="max-h-40 overflow-y-auto divide-y divide-gray-50">
        {libraryIds.map((id) => {
          const spectrum = librarySpectra.find((s) => s.id === id);
          const catalogEntry = state.searchResults.find((r) => r.id === id) ??
            state.catalogRecords.find((r) => r.id === id);

          const materialName = spectrum?.material_name ?? catalogEntry?.material.name ?? 'Unknown';
          const sampleName = spectrum?.name ?? catalogEntry?.name ?? id;
          const category = spectrum?.material_category ?? catalogEntry?.material.category ?? 'other';

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
              <button
                onClick={(e) => { e.stopPropagation(); dispatch({ type: 'REMOVE_FROM_LIBRARY', id }); }}
                className="text-gray-300 hover:text-red-500 flex-shrink-0 ml-2 opacity-0 group-hover:opacity-100 transition-opacity"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
