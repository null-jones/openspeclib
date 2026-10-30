import { Fragment, useState } from 'react';
import { useAppContext } from '../../state/AppContext';
import { collectionLabel } from '../../constants/sources';
import { GEOMETRIES, vocabLabel } from '../../format/vocab';
import { implausibleExtent, type SortKey } from '../../lib/data/search';
import Badge from '../common/Badge';
import SpectrumDetails, { SimulatedBadge } from './SpectrumDetails';
import { own } from '../../lib/own';

function formatRange(minNm: number, maxNm: number): string {
  const um = (nm: number) => (nm < 10 ? (nm / 1000).toPrecision(2) : (nm / 1000).toFixed(nm < 10_000 ? 2 : 1));
  return `${um(minNm)}–${um(maxNm)}`;
}

const IMPLAUSIBLE_TITLE =
  'This extent is outside 100 nm to 1 mm, which almost always means the wavelength unit is mislabelled in the source data (OpenSpecLib quality rule Q4).';

/** `unit` is shown after the label in its own case (μm must not become ΜM). */
const SORTABLE_COLUMNS: { key: SortKey; label: string; unit?: string; align?: 'right' }[] = [
  { key: 'material', label: 'Material' },
  { key: 'sample', label: 'Name' },
  { key: 'category', label: 'Category' },
  { key: 'collection', label: 'Collection' },
  { key: 'quantity', label: 'Quantity' },
  { key: 'geometry', label: 'Geometry' },
  { key: 'range', label: 'Range', unit: 'μm' },
  { key: 'points', label: 'Points', align: 'right' },
];

export default function ResultsTable() {
  const { state, dispatch } = useAppContext();
  const { searchResults, resultCount, pageOffset, pageLimit, libraryIds, searchLoading, sort } = state;
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const totalPages = Math.ceil(resultCount / pageLimit);
  const currentPage = Math.floor(pageOffset / pageLimit) + 1;
  const librarySet = new Set(libraryIds);

  const handleSort = (key: SortKey) => {
    if (sort?.key === key) {
      dispatch({
        type: 'SET_SORT',
        sort: sort.dir === 'asc' ? { key, dir: 'desc' } : null,
      });
    } else {
      dispatch({ type: 'SET_SORT', sort: { key, dir: 'asc' } });
    }
  };

  if (state.indexError && !state.dataLoaded) {
    return (
      <div className="flex items-center justify-center h-48">
        <div className="text-center max-w-md">
          <p className="text-sm font-medium text-red-600">Failed to load the OpenSpecLib index</p>
          <p className="text-xs mt-1 text-gray-500 break-words">{state.indexError}</p>
          <button
            onClick={() => dispatch({ type: 'RETRY_INDEX' })}
            className="mt-3 px-3 py-1.5 text-xs bg-indigo-600 text-white rounded-md hover:bg-indigo-700"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!state.dataLoaded) {
    return (
      <div className="flex items-center justify-center h-48 text-gray-400">
        <div className="text-center">
          <div className="animate-spin w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full mx-auto mb-3" />
          <p className="text-sm">Loading the spectral index…</p>
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
          {state.selectedSensor ? (
            <p className="text-xs mt-1">
              The sensor filter limits results to reflectance spectra overlapping{' '}
              {state.selectedSensor.name}'s range — try removing it or adjusting your search
            </p>
          ) : (
            <p className="text-xs mt-1">Try adjusting your search or filters</p>
          )}
          <button
            onClick={() => dispatch({ type: 'CLEAR_FILTERS' })}
            className="mt-3 px-3 py-1.5 text-xs bg-white border border-gray-200 rounded-md hover:bg-gray-50 text-gray-600"
          >
            Clear all filters
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      {state.indexError && (
        <p className="mb-2 px-3 py-2 text-xs rounded-md bg-red-50 border border-red-200 text-red-800" role="alert">
          The OpenSpecLib index failed to load ({state.indexError}); only the collections you opened are shown.{' '}
          <button onClick={() => dispatch({ type: 'RETRY_INDEX' })} className="underline">
            Retry
          </button>
        </p>
      )}
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
          <table className="w-full text-sm min-w-[1050px]">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="w-10 px-3 py-2" />
                {SORTABLE_COLUMNS.map((col) => {
                  const active = sort?.key === col.key;
                  return (
                    <th
                      key={col.key}
                      className={`px-3 py-2 text-xs font-medium uppercase select-none ${
                        col.align === 'right' ? 'text-right' : 'text-left'
                      } ${active ? 'text-indigo-600' : 'text-gray-500'}`}
                    >
                      <button
                        type="button"
                        onClick={() => handleSort(col.key)}
                        className="inline-flex items-center gap-1 uppercase hover:text-indigo-600"
                        title={`Sort by ${col.label}${col.unit ? ` (${col.unit})` : ''}`}
                      >
                        {col.label}
                        {col.unit && <span className="normal-case">({col.unit})</span>}
                        <span className="w-2 text-[9px]">
                          {active ? (sort!.dir === 'asc' ? '▲' : '▼') : ''}
                        </span>
                      </button>
                    </th>
                  );
                })}
                <th className="w-8 px-3 py-2" aria-label="Details" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {searchResults.map((r) => {
                const inLibrary = librarySet.has(r.id);
                const sourceLabel = collectionLabel(r.collection, state.collections.get(r.collection)?.footer.collection.title);
                const isExpanded = expandedId === r.id;
                const tooltip = [r.sample_description, r.material_formula && `Formula: ${r.material_formula}`]
                  .filter(Boolean)
                  .join('\n');

                return (
                  <Fragment key={r.id}>
                    <tr
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
                          aria-label={`${inLibrary ? 'Remove' : 'Add'} ${r.material_name} (${r.name}) ${inLibrary ? 'from' : 'to'} the library`}
                          className="w-3.5 h-3.5 rounded text-indigo-600 focus:ring-indigo-500"
                        />
                      </td>
                      <td className="px-3 py-2.5" title={tooltip}>
                        <div className="font-medium text-gray-900">
                          {r.material_name} {r.simulated && <SimulatedBadge />}
                        </div>
                        {r.material_formula && (
                          <div className="text-xs text-gray-400 mt-0.5">{r.material_formula}</div>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-gray-500 max-w-[260px]">
                        <div className="truncate text-xs cursor-help" title={r.name}>
                          {r.name}
                        </div>
                        {r.sample_description && (
                          <div className="truncate text-xs text-gray-400 mt-0.5 cursor-help" title={r.sample_description}>
                            {r.sample_description}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <Badge category={r.material_category} />
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="text-xs text-gray-600 font-medium">{sourceLabel}</span>
                      </td>
                      <td className="px-3 py-2.5 text-gray-500 text-xs">{r.quantity}</td>
                      <td
                        className={`px-3 py-2.5 text-xs ${r.geometry === 'unknown' ? 'text-gray-400' : 'text-gray-600'}`}
                        title={own(GEOMETRIES, r.geometry)}
                      >
                        {vocabLabel(r.geometry)}
                      </td>
                      <td
                        className={`px-3 py-2.5 text-xs font-mono whitespace-nowrap ${implausibleExtent(r) ? 'text-amber-700' : 'text-gray-500'}`}
                        title={implausibleExtent(r) ? IMPLAUSIBLE_TITLE : undefined}
                      >
                        {formatRange(r.wavelength_min_nm, r.wavelength_max_nm)}
                        {implausibleExtent(r) && ' ⚠'}
                      </td>
                      <td className="px-3 py-2.5 text-gray-500 text-xs text-right">{r.num_points?.toLocaleString() ?? '—'}</td>
                      <td className="px-3 py-2.5 text-center">
                        <button
                          type="button"
                          aria-label={isExpanded ? 'Hide details' : 'Show details and preview'}
                          aria-expanded={isExpanded}
                          className="text-gray-400 hover:text-indigo-600 transition-colors"
                          onClick={(e) => {
                            e.stopPropagation();
                            setExpandedId(isExpanded ? null : r.id);
                          }}
                        >
                          <svg
                            className={`w-4 h-4 transform transition-transform ${isExpanded ? 'rotate-90' : ''}`}
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                          >
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                          </svg>
                        </button>
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr className="bg-gray-50">
                        <td colSpan={SORTABLE_COLUMNS.length + 2} className="px-3 py-3 md:px-6 md:py-4">
                          {/* Keep the panel within the visible width of the scrolling table: the
                              screen minus the page and cell padding, and from md up the sidebar. */}
                          <div className="sticky left-3 md:left-6 max-w-[calc(100vw-3.5rem)] md:max-w-[calc(100vw-27rem)]">
                            <SpectrumDetails record={r} />
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
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
