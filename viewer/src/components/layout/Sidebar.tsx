import { useState } from 'react';
import { useAppContext } from '../../state/AppContext';
import SearchBar from '../search/SearchBar';
import CategoryFilter from '../search/CategoryFilter';
import SensorSelector from '../search/SensorSelector';
import ActiveFilters from '../search/ActiveFilters';
import type { CatalogRecord } from '../../types/catalog';
import type { SensorDefinition } from '../../types/sensors';

/** Human-readable labels for source library identifiers. */
const SOURCE_LABELS: Record<string, string> = {
  usgs_splib07: 'USGS Spectral Library v7',
  ecostress: 'ECOSTRESS',
  relab: 'RELAB',
  asu_tes: 'ASU TES',
  bishop: 'Bishop',
};

interface SourceLicenseInfo {
  library: string;
  license: string;
  citation: string;
  url: string;
  count: number;
}

/** Expandable sensor details panel. */
function SensorDetails({ sensor }: { sensor: SensorDefinition }) {
  const [expanded, setExpanded] = useState(false);
  const bands = sensor.bands;
  const fwhms = bands.map((b) => b.fwhm * 1000);
  const avgFwhm = fwhms.reduce((a, b) => a + b, 0) / fwhms.length;
  const minFwhm = Math.min(...fwhms);
  const maxFwhm = Math.max(...fwhms);
  const isHyperspectral = bands.length > 30;

  return (
    <div className="p-2.5 bg-indigo-50/50 rounded-lg border border-indigo-100">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between gap-2 text-left"
      >
        <span className="text-xs font-semibold text-gray-800">{sensor.name}</span>
        <svg
          className={`w-3.5 h-3.5 text-gray-400 transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      <p className="mt-1 text-[11px] text-gray-500">
        {sensor.description}
        <span className="ml-1 text-[10px] text-gray-400">({sensor.group})</span>
      </p>

      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-gray-500">
        <span><strong className="text-gray-700">{bands.length}</strong> bands</span>
        <span>{(sensor.wavelengthMin * 1000).toFixed(0)}–{(sensor.wavelengthMax * 1000).toFixed(0)} nm</span>
        <span>FWHM {minFwhm.toFixed(1)}–{maxFwhm.toFixed(1)} nm</span>
      </div>

      {expanded && (
        <div className="mt-2 pt-2 border-t border-indigo-100">
          <div className="grid grid-cols-3 gap-x-2 text-[10px] text-gray-500 mb-1">
            <span className="font-medium text-gray-600">Band</span>
            <span className="font-medium text-gray-600">Center (nm)</span>
            <span className="font-medium text-gray-600">FWHM (nm)</span>
          </div>
          <div className={`space-y-px ${isHyperspectral ? 'max-h-40' : 'max-h-32'} overflow-y-auto`}>
            {bands.map((band) => (
              <div key={band.name} className="grid grid-cols-3 gap-x-2 text-[10px] text-gray-500">
                <span>{band.name}</span>
                <span>{(band.centerWavelength * 1000).toFixed(1)}</span>
                <span>{(band.fwhm * 1000).toFixed(1)}</span>
              </div>
            ))}
          </div>
          <p className="mt-1.5 text-[10px] text-gray-400">
            Avg FWHM: {avgFwhm.toFixed(1)} nm
          </p>
          <a
            href={sensor.sourceUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-1.5 inline-flex items-center gap-1 text-[10px] text-indigo-500 hover:text-indigo-700 transition-colors"
          >
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
            </svg>
            {sensor.source}
          </a>
        </div>
      )}
    </div>
  );
}

/** Expandable citation block for a single source. */
function SourceCitation({ info }: { info: SourceLicenseInfo }) {
  const [expanded, setExpanded] = useState(false);
  const label = SOURCE_LABELS[info.library] ?? info.library;
  const hasLicense = info.license && info.license.length > 0;
  const hasCitation = info.citation && info.citation.length > 0;

  return (
    <div className="p-2.5 bg-gray-50 rounded-lg border border-gray-100">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-gray-800">{label}</span>
            <span className="text-[10px] text-gray-400">({info.count} selected)</span>
          </div>
          {hasLicense ? (
            <span className="inline-block mt-1 px-1.5 py-0.5 text-[10px] font-medium rounded bg-emerald-50 text-emerald-700 border border-emerald-200">
              {info.license}
            </span>
          ) : (
            <span className="inline-block mt-1 px-1.5 py-0.5 text-[10px] font-medium rounded bg-gray-100 text-gray-400 border border-gray-200">
              License not available
            </span>
          )}
        </div>
        {info.url && (
          <a
            href={info.url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex-shrink-0 text-gray-400 hover:text-indigo-600 transition-colors"
            title="View source"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
            </svg>
          </a>
        )}
      </div>

      {hasCitation ? (
        <div className="mt-1.5">
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[10px] text-indigo-500 hover:text-indigo-700 transition-colors"
          >
            {expanded ? 'Hide citation' : 'Show citation'}
          </button>
          {expanded && (
            <p className="mt-1 text-[11px] leading-relaxed text-gray-500 italic break-words">
              {info.citation}
            </p>
          )}
        </div>
      ) : (
        <p className="mt-1.5 text-[10px] text-gray-400 italic">Citation not available</p>
      )}
    </div>
  );
}

export default function Sidebar() {
  const { state } = useAppContext();
  const { libraryIds, catalogRecords, selectedSensor } = state;

  // Group selected spectra by source library, deduplicating license info.
  const sourceLicenses: SourceLicenseInfo[] = [];
  if (libraryIds.length > 0) {
    const grouped = new Map<string, { record: CatalogRecord; count: number }>();
    for (const id of libraryIds) {
      const entry = catalogRecords.find((r) => r.id === id);
      if (!entry) continue;
      const lib = entry.source.library;
      const existing = grouped.get(lib);
      if (existing) {
        existing.count++;
      } else {
        grouped.set(lib, { record: entry, count: 1 });
      }
    }
    for (const [library, { record, count }] of grouped) {
      sourceLicenses.push({
        library,
        license: record.source.license ?? '',
        citation: record.source.citation ?? '',
        url: record.source.url ?? '',
        count,
      });
    }
  }

  return (
    <aside className="w-80 flex-shrink-0 bg-white border-r border-gray-200 p-4 space-y-5 overflow-y-auto">
      <div>
        <h2 className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">
          Search
        </h2>
        <SearchBar />
      </div>

      <SensorSelector />

      {selectedSensor && <SensorDetails sensor={selectedSensor} />}

      <CategoryFilter />

      <ActiveFilters />

      {sourceLicenses.length > 0 && (
        <div>
          <h2 className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">
            Licensing &amp; Citations
          </h2>
          <div className="space-y-2">
            {sourceLicenses.map((info) => (
              <SourceCitation key={info.library} info={info} />
            ))}
          </div>
          <p className="mt-2 text-[10px] text-amber-600">
            Licensing terms differ between sources. Check each source's terms before use.
          </p>
        </div>
      )}
    </aside>
  );
}
