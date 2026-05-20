import { Fragment, useState, type ReactNode } from 'react';
import { useAppContext } from '../../state/AppContext';
import type { CatalogRecord } from '../../types/catalog';
import Badge from '../common/Badge';

const SOURCE_LABELS: Record<string, string> = {
  usgs_splib07: 'USGS',
  ecostress: 'ECOSTRESS',
  relab: 'RELAB',
  asu_tes: 'ASU TES',
  bishop: 'Bishop',
  ecosis: 'EcoSIS',
  ossl: 'OSSL',
};

function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-gray-400">{label}</dt>
      <dd className="text-xs text-gray-700 mt-0.5 break-words">{children}</dd>
    </div>
  );
}

function SpectrumDetails({ record }: { record: CatalogRecord }) {
  const { source, measurement, sample, quality, additional_properties } = record;
  // Older catalogs (pre-0.0.7) lack source.dataset and the extended Measurement
  // context fields. Treat each as optional so the panel still renders against
  // the currently-shipped data release.
  const dataset = source.dataset ?? null;
  const m = measurement ?? ({} as CatalogRecord['measurement']);
  const processing = m.processing ?? [];
  const acquisitionParts = [m.acquisition_method, m.light_source, m.venue, m.foreoptic].filter(
    Boolean,
  );
  const hasAdditional = additional_properties && Object.keys(additional_properties).length > 0;

  return (
    <div className="space-y-4 text-sm">
      {dataset && (
        <section>
          <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2">Dataset</h4>
          <div className="bg-white border border-gray-200 rounded p-3 space-y-2">
            <div className="flex items-start justify-between gap-3 flex-wrap">
              <div className="min-w-0">
                <div className="font-medium text-gray-900">{dataset.title}</div>
                {dataset.authors && (
                  <div className="text-xs text-gray-500 mt-0.5">{dataset.authors}</div>
                )}
                {dataset.organization && (
                  <div className="text-xs text-gray-500">{dataset.organization}</div>
                )}
              </div>
              {dataset.license && (
                <span
                  className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-amber-50 text-amber-800 border border-amber-200"
                  title={dataset.license_url ?? undefined}
                >
                  {dataset.license}
                </span>
              )}
            </div>
            {dataset.description && (
              <p className="text-xs text-gray-600 leading-relaxed">{dataset.description}</p>
            )}
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {dataset.url && (
                <a
                  href={dataset.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-indigo-600 hover:text-indigo-800 underline"
                >
                  Dataset page
                </a>
              )}
              {dataset.citation_doi && (
                <a
                  href={`https://doi.org/${dataset.citation_doi}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-indigo-600 hover:text-indigo-800 underline font-mono"
                >
                  doi:{dataset.citation_doi}
                </a>
              )}
            </div>
          </div>
        </section>
      )}

      <section>
        <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2">Measurement</h4>
        <dl className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {m.instrument && <DetailField label="Instrument">{m.instrument}</DetailField>}
          {m.instrument_type && <DetailField label="Instrument type">{m.instrument_type}</DetailField>}
          {m.laboratory && <DetailField label="Laboratory">{m.laboratory}</DetailField>}
          {m.geometry && <DetailField label="Geometry">{m.geometry}</DetailField>}
          {m.date && <DetailField label="Date">{m.date}</DetailField>}
          {acquisitionParts.length > 0 && (
            <DetailField label="Acquisition">{acquisitionParts.join(' · ')}</DetailField>
          )}
        </dl>
        {processing.length > 0 && (
          <div className="mt-3">
            <span className="text-[10px] uppercase tracking-wide text-gray-400 mr-2">Processing</span>
            {processing.map((p) => (
              <span
                key={p}
                className="inline-flex items-center px-2 py-0.5 mr-1 rounded text-[11px] font-medium bg-indigo-50 text-indigo-700 border border-indigo-200"
              >
                {p}
              </span>
            ))}
          </div>
        )}
      </section>

      <section>
        <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2">Sample</h4>
        <dl className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {sample.id && <DetailField label="ID">{sample.id}</DetailField>}
          {sample.description && <DetailField label="Description">{sample.description}</DetailField>}
          {sample.particle_size && <DetailField label="Particle size">{sample.particle_size}</DetailField>}
          {sample.origin && <DetailField label="Origin">{sample.origin}</DetailField>}
          {sample.owner && <DetailField label="Owner">{sample.owner}</DetailField>}
          {sample.collection_date && <DetailField label="Collected">{sample.collection_date}</DetailField>}
          {sample.preparation && <DetailField label="Preparation">{sample.preparation}</DetailField>}
        </dl>
      </section>

      <section>
        <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2">Quality</h4>
        <dl className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          <DetailField label="Coverage">{(quality.coverage_fraction * 100).toFixed(1)}%</DetailField>
          <DetailField label="Bad bands">{quality.has_bad_bands ? `yes (${quality.bad_band_count})` : 'no'}</DetailField>
          {quality.notes && <DetailField label="Notes">{quality.notes}</DetailField>}
        </dl>
      </section>

      {hasAdditional && (
        <details className="text-xs">
          <summary className="cursor-pointer text-gray-500 hover:text-gray-700">
            Raw additional properties
          </summary>
          <pre className="mt-2 p-2 bg-white border border-gray-200 rounded overflow-x-auto text-[11px] leading-snug">
            {JSON.stringify(additional_properties, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

export default function ResultsTable() {
  const { state, dispatch } = useAppContext();
  const { searchResults, resultCount, pageOffset, pageLimit, libraryIds, searchLoading } = state;
  const [expandedId, setExpandedId] = useState<string | null>(null);

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
                <th className="w-8 px-3 py-2" aria-label="Details" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {searchResults.map((r) => {
                const inLibrary = libraryIds.includes(r.id);
                const sourceLabel = SOURCE_LABELS[r.source.library] ?? r.source.library;
                const isExpanded = expandedId === r.id;
                // Build a tooltip with available context
                const tooltipParts = [
                  r.sample.description,
                  r.material.formula && `Formula: ${r.material.formula}`,
                  r.sample.particle_size && `Particle size: ${r.sample.particle_size}`,
                  r.sample.origin && `Origin: ${r.sample.origin}`,
                ].filter(Boolean);
                const tooltip = tooltipParts.join('\n');

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
                      <td className="px-3 py-2.5 text-center">
                        <button
                          type="button"
                          aria-label={isExpanded ? 'Hide details' : 'Show details'}
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
                        <td colSpan={9} className="px-6 py-4">
                          <SpectrumDetails record={r} />
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
