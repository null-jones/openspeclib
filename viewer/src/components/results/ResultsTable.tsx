import { useAppContext } from '../../state/AppContext';
import Badge from '../common/Badge';

const SOURCE_LABELS: Record<string, string> = {
  usgs_splib07: 'USGS',
  ecostress: 'ECOSTRESS',
  relab: 'RELAB',
  asu_tes: 'ASU TES',
  bishop: 'Bishop',
};

export default function ResultsTable() {
  const { state, dispatch } = useAppContext();
  const { searchResults, resultCount, pageOffset, pageLimit, libraryIds, searchLoading } = state;

  const totalPages = Math.ceil(resultCount / pageLimit);
  const currentPage = Math.floor(pageOffset / pageLimit) + 1;

  if (!state.catalogLoaded) {
    return (
      <div className="flex items-center justify-center h-48 text-gray-400">
        <div className="text-center">
          <div className="animate-spin w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full mx-auto mb-3" />
          <p className="text-sm">Loading spectral catalog...</p>
        </div>
      </div>
    );
  }

  if (searchResults.length === 0 && !searchLoading) {
    return (
      <div className="flex items-center justify-center h-48 text-gray-400">
        <div className="text-center">
          <svg className="w-12 h-12 mx-auto mb-3 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
          </svg>
          <p className="text-sm font-medium">No spectra found</p>
          <p className="text-xs mt-1">Try adjusting your search or filters</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Table header info */}
      <div className="flex items-center justify-between mb-2 px-1">
        <span className="text-xs text-gray-500">
          {resultCount.toLocaleString()} result{resultCount !== 1 ? 's' : ''}
          {state.selectedSensor && ' (reflectance only)'}
        </span>
        <span className="text-xs text-gray-400">
          {libraryIds.length} in library
        </span>
      </div>

      {/* Table */}
      <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[950px]">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="w-10 px-3 py-2" />
                <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Material</th>
                <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Sample ID</th>
                <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Category</th>
                <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Source</th>
                <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Technique</th>
                <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Range (μm)</th>
                <th className="text-right px-3 py-2 text-xs font-medium text-gray-500 uppercase">Points</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {searchResults.map((r) => {
                const inLibrary = libraryIds.includes(r.id);
                const sourceLabel = SOURCE_LABELS[r.source.library] ?? r.source.library;
                // Build a tooltip with available context
                const tooltipParts = [
                  r.sample.description,
                  r.material.formula && `Formula: ${r.material.formula}`,
                  r.sample.particle_size && `Particle size: ${r.sample.particle_size}`,
                  r.sample.origin && `Origin: ${r.sample.origin}`,
                ].filter(Boolean);
                const tooltip = tooltipParts.join('\n');

                return (
                  <tr
                    key={r.id}
                    className={`hover:bg-gray-50 cursor-pointer transition-colors ${
                      inLibrary ? 'bg-indigo-50/50' : ''
                    }`}
                    onClick={() =>
                      dispatch(
                        inLibrary
                          ? { type: 'REMOVE_FROM_LIBRARY', id: r.id }
                          : { type: 'ADD_TO_LIBRARY', id: r.id },
                      )
                    }
                  >
                    <td className="px-3 py-2.5 text-center">
                      <input
                        type="checkbox"
                        checked={inLibrary}
                        readOnly
                        className="w-3.5 h-3.5 rounded text-indigo-600 focus:ring-indigo-500"
                      />
                    </td>
                    <td className="px-3 py-2.5" title={tooltip}>
                      <div className="font-medium text-gray-900">
                        {r.material.name}
                      </div>
                      {r.material.formula && (
                        <div className="text-xs text-gray-400 mt-0.5">{r.material.formula}</div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-gray-500 max-w-[260px]">
                      <div className="truncate text-xs cursor-help" title={r.name}>
                        {r.name}
                      </div>
                      {r.sample.description && (
                        <div className="truncate text-xs text-gray-400 mt-0.5 cursor-help" title={r.sample.description}>
                          {r.sample.description}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <Badge category={r.material.category} />
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="text-xs text-gray-600 font-medium">{sourceLabel}</span>
                    </td>
                    <td className="px-3 py-2.5 text-gray-500 text-xs">{r.measurement.technique}</td>
                    <td className="px-3 py-2.5 text-gray-500 text-xs font-mono">
                      {r.spectral_data.wavelength_min.toFixed(2)}–{r.spectral_data.wavelength_max.toFixed(2)}
                    </td>
                    <td className="px-3 py-2.5 text-gray-500 text-xs text-right">{r.spectral_data.num_points.toLocaleString()}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-3 px-1">
          <button
            onClick={() => dispatch({ type: 'SET_PAGE_OFFSET', offset: Math.max(0, pageOffset - pageLimit) })}
            disabled={pageOffset === 0}
            className="px-3 py-1.5 text-xs bg-white border border-gray-200 rounded-md
                       disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
          >
            Previous
          </button>
          <span className="text-xs text-gray-500">
            Page {currentPage} of {totalPages}
          </span>
          <button
            onClick={() =>
              dispatch({
                type: 'SET_PAGE_OFFSET',
                offset: Math.min((totalPages - 1) * pageLimit, pageOffset + pageLimit),
              })
            }
            disabled={currentPage >= totalPages}
            className="px-3 py-1.5 text-xs bg-white border border-gray-200 rounded-md
                       disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
