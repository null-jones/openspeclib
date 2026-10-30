import { useMemo, useRef, useState, type RefObject } from 'react';
import { useDialog } from '../common/useDialog';
import { useAppContext } from '../../state/AppContext';
import SearchBar from '../search/SearchBar';
import SourceSelector from '../search/SourceSelector';
import CategoryFilter from '../search/CategoryFilter';
import SensorSelector from '../search/SensorSelector';
import QuantityFilter from '../search/QuantityFilter';
import GeometryFilter from '../search/GeometryFilter';
import WavelengthRangeFilter from '../search/WavelengthRangeFilter';
import ActiveFilters from '../search/ActiveFilters';
import OpenCollection from '../library/OpenCollection';
import { HYPERSPECTRAL_BAND_THRESHOLD } from '../../constants/sources';
import { buildAttribution, type CollectionUse } from '../../lib/data/attribution';
import type { SpectrumRecord } from '../../lib/data/types';
import type { SensorDefinition } from '../../types/sensors';

/** Expandable sensor details panel. */
function SensorDetails({ sensor }: { sensor: SensorDefinition }) {
  const [expanded, setExpanded] = useState(false);
  const bands = sensor.bands;
  const fwhms = bands.map((b) => b.fwhm * 1000);
  const avgFwhm = fwhms.reduce((a, b) => a + b, 0) / fwhms.length;
  const minFwhm = Math.min(...fwhms);
  const maxFwhm = Math.max(...fwhms);
  const isHyperspectral = bands.length > HYPERSPECTRAL_BAND_THRESHOLD;

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
        <span className="ml-1 text-[10px] text-gray-500">({sensor.group})</span>
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
          <p className="mt-1.5 text-[10px] text-gray-500">
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

/** Licence and citation for the selected spectra of one collection. */
function CollectionCitation({ use }: { use: CollectionUse }) {
  const [expanded, setExpanded] = useState(false);
  const link = use.doi ? `https://doi.org/${use.doi}` : use.url;
  return (
    <div className="p-2.5 bg-gray-50 rounded-lg border border-gray-100">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-gray-800">{use.title}</span>
            <span className="text-[10px] text-gray-500">({use.count} selected)</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {use.licenses.map((l) => (
              <span
                key={l.license}
                title={l.name ?? l.license}
                className="inline-block px-1.5 py-0.5 text-[10px] font-medium rounded bg-emerald-50 text-emerald-700 border border-emerald-200"
              >
                {l.name ?? l.license}
                {use.licenses.length > 1 && <span className="text-emerald-500"> ×{l.count}</span>}
              </span>
            ))}
          </div>
        </div>
        {link && (
          <a
            href={link}
            target="_blank"
            rel="noopener noreferrer"
            className="flex-shrink-0 text-gray-400 hover:text-indigo-600 transition-colors"
            title="Collection page"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
            </svg>
          </a>
        )}
      </div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="mt-1.5 text-[10px] text-indigo-500 hover:text-indigo-700 transition-colors"
      >
        {expanded ? 'Hide citation' : use.datasets.length ? `Show citations (${use.datasets.length} datasets)` : 'Show citation'}
      </button>
      {expanded && (
        <div className="mt-1 space-y-1.5 text-[11px] leading-relaxed text-gray-500 break-words">
          {use.citation && <p className="italic">{use.citation}</p>}
          {use.datasets.map((d) => (
            <div key={d.id} className="pl-2 border-l-2 border-gray-200">
              <div className="text-gray-700 not-italic">
                {d.title} <span className="text-gray-400">({d.count}{d.license ? `, ${d.license}` : ''})</span>
              </div>
              {d.citation && <p className="italic">{d.citation}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Dialog behaviour for the open drawer (below md): focus inside, Tab trapped, Escape closes. */
function DrawerFocus({ target, onClose }: { target: RefObject<HTMLElement | null>; onClose: () => void }) {
  useDialog(target, onClose);
  return null;
}

const noop = (): void => undefined;

export default function Sidebar({ open = false, onClose }: { open?: boolean; onClose?: () => void }) {
  const { state } = useAppContext();
  const { libraryIds, recordById, collections, selectedSensor } = state;

  // Licence and citation for the selection, from per-row licences and the
  // collection footers.
  const uses = useMemo(() => {
    const recs = libraryIds.map((id) => recordById.get(id)).filter((r): r is SpectrumRecord => !!r);
    return buildAttribution(recs, collections);
  }, [libraryIds, recordById, collections]);

  const asideRef = useRef<HTMLElement>(null);

  return (
    <>
    {/* Below md the sidebar is a drawer, opened from the "Search and filters" button. */}
    {open && <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={onClose} aria-hidden />}
    {open && <DrawerFocus target={asideRef} onClose={onClose ?? noop} />}
    <aside
      ref={asideRef}
      id="explorer-sidebar"
      aria-label="Search and filters"
      // Open only as the drawer below md (App closes it at md and up): a modal dialog then.
      role={open ? 'dialog' : undefined}
      aria-modal={open ? true : undefined}
      className={`fixed inset-y-0 left-0 z-40 w-80 max-w-[85vw] transform transition-transform md:static md:z-auto md:max-w-none md:translate-x-0 md:visible flex-shrink-0 bg-white border-r border-gray-200 p-4 space-y-5 overflow-y-auto ${
        // Closed below md: off screen and out of the tab order.
        open ? 'translate-x-0 shadow-xl' : '-translate-x-full invisible'
      }`}
    >
      <div className="flex justify-end md:hidden">
        <button type="button" onClick={onClose} className="px-2 py-1 text-xs text-gray-600 border border-gray-200 rounded-md">
          Close
        </button>
      </div>
      <div>
        <h2 className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">
          Search
        </h2>
        <SearchBar />
      </div>

      <SourceSelector />
      <OpenCollection />
      <SensorSelector />

      {selectedSensor && <SensorDetails sensor={selectedSensor} />}

      <CategoryFilter />
      <QuantityFilter />
      <GeometryFilter />
      <WavelengthRangeFilter />

      <ActiveFilters />

      {uses.length > 0 && (
        <div>
          <h2 className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">
            Licensing &amp; Citations
          </h2>
          <div className="space-y-2">
            {uses.map((u) => (
              <CollectionCitation key={u.id} use={u} />
            ))}
          </div>
          <p className="mt-2 text-[10px] text-amber-600">
            Each spectrum carries its own licence. Exports include this attribution.
          </p>
        </div>
      )}
    </aside>
    </>
  );
}
