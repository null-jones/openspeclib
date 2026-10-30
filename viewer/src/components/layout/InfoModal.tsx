import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useDialog } from '../common/useDialog';
import { INDEX_FILE, OPENSPECLIB_VERSION, RELEASE_URL } from '../../constants/urls';
import { loadChecksums } from '../../lib/data/browser';
import { useAppContext } from '../../state/AppContext';
import type { CollectionMeta, DatasetMeta } from '../../lib/data/types';
import { Prose } from '../format/common';
import { LICENSING_URL } from '../../format/links';

type TabId = 'overview' | 'data' | 'resampling' | 'licensing';

const TABS: ReadonlyArray<{ id: TabId; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'data', label: 'Collections' },
  { id: 'resampling', label: 'Resampling' },
  { id: 'licensing', label: 'Licensing' },
];

export function InfoModal({ onClose }: { onClose: () => void }) {
  const [activeTab, setActiveTab] = useState<TabId>('overview');
  const dialogRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});
  useDialog(dialogRef, onClose);

  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.id === activeTab);
    const next =
      e.key === 'ArrowRight' ? (i + 1) % TABS.length
      : e.key === 'ArrowLeft' ? (i - 1 + TABS.length) % TABS.length
      : e.key === 'Home' ? 0
      : e.key === 'End' ? TABS.length - 1
      : null;
    if (next === null) return;
    e.preventDefault();
    setActiveTab(TABS[next].id);
    tabRefs.current[TABS[next].id]?.focus();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="about-title"
        className="bg-white rounded-xl shadow-xl max-w-2xl w-full mx-4 max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 id="about-title" className="text-lg font-semibold text-gray-900">About OpenSpecLib Viewer</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700" aria-label="Close">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        <div className="px-6 pt-3 border-b border-gray-100 overflow-x-auto">
          {/* WAI-ARIA tabs: arrow keys, Home and End move between tabs; Tab moves into the panel. */}
          <div className="flex gap-1" role="tablist" aria-label="About sections" onKeyDown={onTabKey}>
            {TABS.map((tab) => {
              const selected = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  ref={(el) => {
                    tabRefs.current[tab.id] = el;
                  }}
                  type="button"
                  role="tab"
                  id={`about-tab-${tab.id}`}
                  aria-selected={selected}
                  aria-controls={`about-panel-${tab.id}`}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => setActiveTab(tab.id)}
                  className={
                    'px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap ' +
                    (selected
                      ? 'border-indigo-500 text-indigo-700'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300')
                  }
                >
                  {tab.label}
                </button>
              );
            })}
          </div>
        </div>

        <div
          role="tabpanel"
          id={`about-panel-${activeTab}`}
          aria-labelledby={`about-tab-${activeTab}`}
          tabIndex={0}
          className="px-6 py-5 overflow-y-auto text-sm text-gray-700 space-y-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-500"
        >
          {activeTab === 'overview' && <OverviewTab onClose={onClose} />}
          {activeTab === 'data' && <DataTab />}
          {activeTab === 'resampling' && <ResamplingTab />}
          {activeTab === 'licensing' && <LicensingTab />}
        </div>

        <div className="px-6 py-3 border-t border-gray-100 text-xs text-gray-400">
          Built with React, DuckDB-WASM, and Plotly.js. Data from{' '}
          <a
            href={RELEASE_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-indigo-500 hover:text-indigo-700"
          >
            OpenSpecLib v{OPENSPECLIB_VERSION}
          </a>
          .
        </div>
      </div>
    </div>
  );
}

function OverviewTab({ onClose }: { onClose: () => void }) {
  const { dispatch } = useAppContext();
  return (
    <>
      <p>
        <strong>OpenSpecLib</strong> gathers spectra from public reference libraries into one open format: the{' '}
        <strong>OpenSpecLib Format</strong>, one self-describing Parquet file per collection. This release holds the
        USGS Spectral Library 7, ECOSTRESS and EcoSIS collections (see Collections).
      </p>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">What is this viewer?</h3>
        <p className="text-gray-600">
          A browser tool to search, plot and export spectra. It runs entirely in your browser: DuckDB-WASM reads the
          release's index file, then fetches single spectra from the collection files with HTTP range requests. No
          server, no accounts.
        </p>
      </div>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Your own spectra</h3>
        <p className="text-gray-600">
          Any file in the OpenSpecLib Format opens here next to the OpenSpecLib data ("Open a collection" in the
          sidebar), and the{' '}
          <button
            type="button"
            onClick={() => {
              dispatch({ type: 'SET_VIEW', view: 'format', page: 'check' });
              onClose();
            }}
            className="text-indigo-600 hover:text-indigo-800 underline"
          >
            checker
          </button>{' '}
          tells you whether a file conforms. Files are read in place, never uploaded.
        </p>
      </div>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Values and units</h3>
        <p className="text-gray-600">
          Each spectrum states its quantity (reflectance, transmittance, emissivity or absorbance) and its measurement
          geometry. Reflectance, transmittance and emissivity are fractions (0–1); NaN marks a missing band.
          Wavelengths stay in the unit the source published; the viewer plots them in micrometres.
        </p>
      </div>
    </>
  );
}

function DatasetBreakdown({ meta, counts }: { meta: CollectionMeta; counts: Map<string, number> }) {
  const datasets = Object.entries(meta.footer.datasets ?? {}) as Array<[string, DatasetMeta]>;
  if (datasets.length === 0) return null;
  const entries = datasets
    .map(([id, d]) => ({ id, d, n: counts.get(id) ?? 0 }))
    .sort((a, b) => b.n - a.n || a.d.title.localeCompare(b.d.title));
  const licences = new Set(entries.map((e) => e.d.license).filter(Boolean));
  const refs = meta.footer.collection.license_refs ?? {};
  return (
    <details className="mt-3 pt-3 border-t border-gray-100">
      <summary className="cursor-pointer text-xs font-medium text-gray-600 hover:text-gray-900">
        Contributing datasets ({entries.length.toLocaleString()}
        {licences.size > 1 && <span className="text-amber-700"> · {licences.size} licences</span>})
      </summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-gray-400 uppercase tracking-wide">
              <th className="text-left font-medium px-2 py-1">Dataset</th>
              <th className="text-left font-medium px-2 py-1">Licence</th>
              <th className="text-left font-medium px-2 py-1">DOI</th>
              <th className="text-right font-medium px-2 py-1">Spectra</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {entries.map(({ id, d, n }) => (
              <tr key={id} className="align-top">
                <td className="px-2 py-1 max-w-[260px]">
                  {d.url ? (
                    <a href={d.url} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:text-indigo-800 underline" title={d.title}>
                      {d.title}
                    </a>
                  ) : (
                    <span className="text-gray-700">{d.title}</span>
                  )}
                  {d.organization && <div className="text-gray-400 text-[10px] mt-0.5">{d.organization}</div>}
                  {d.inclusion === 'subset' && d.spectra_available != null && (
                    <div className="text-gray-400 text-[10px]">
                      subset: {d.spectra_included ?? n} of {d.spectra_available}
                    </div>
                  )}
                </td>
                <td className="px-2 py-1 text-gray-700" title={d.license}>
                  {d.license ? refs[d.license]?.name ?? d.license : '—'}
                </td>
                <td className="px-2 py-1 font-mono">
                  {d.doi ? (
                    <a href={`https://doi.org/${d.doi}`} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:text-indigo-800 underline">
                      {d.doi}
                    </a>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="px-2 py-1 text-right text-gray-600 font-mono">{n.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function DataTab() {
  const { state } = useAppContext();
  const [checksums, setChecksums] = useState<Map<string, string> | null>(null);
  useEffect(() => {
    let cancelled = false;
    void loadChecksums().then((c) => {
      if (!cancelled) setChecksums(c);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const release = state.sources.get('release');
  const collections = [...state.collections.values()];
  const datasetCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of state.records) if (r.dataset_id) m.set(r.dataset_id, (m.get(r.dataset_id) ?? 0) + 1);
    return m;
  }, [state.records]);

  if (!state.dataLoaded) {
    return <p className="text-gray-400 text-xs">{state.indexError ? `The index failed to load: ${state.indexError}` : 'Loading the index…'}</p>;
  }

  return (
    <>
      <p>
        OpenSpecLib {release?.release ?? OPENSPECLIB_VERSION} holds{' '}
        {state.records.filter((r) => r.sourceKey === 'release').length.toLocaleString()} spectra in{' '}
        {collections.filter((c) => c.sourceKey === 'release').length} collections. Each collection is one Parquet file
        in the OpenSpecLib Format; <code className="bg-gray-100 px-1 rounded">{INDEX_FILE}</code> lists every spectrum
        without its arrays, for searching.
        {release?.created && <span className="text-gray-500"> Built {release.created.slice(0, 10)} by {release.generator}.</span>}
      </p>

      <div className="space-y-3">
        {collections.map((c) => {
          const coll = c.footer.collection;
          const src = state.sources.get(c.sourceKey);
          const sha = c.sha256 ?? checksums?.get(c.file);
          const link = coll.doi ? `https://doi.org/${coll.doi}` : coll.url;
          return (
            <div key={c.id} className="border border-gray-200 rounded-lg p-3">
              <div className="flex items-baseline justify-between gap-2 mb-1">
                <h4 className="font-semibold text-gray-900">
                  {coll.title}
                  {src && src.kind !== 'release' && (
                    <span className="ml-2 px-1 py-px text-[10px] font-normal rounded bg-emerald-50 text-emerald-700 border border-emerald-200">
                      opened this session
                    </span>
                  )}
                </h4>
                <span className="text-xs text-gray-500 font-mono">{c.numSpectra.toLocaleString()} spectra</span>
              </div>
              {coll.description && <p className="text-xs text-gray-600 mb-2"><Prose text={coll.description} /></p>}
              <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs text-gray-600">
                {coll.publisher && (
                  <>
                    <dt className="text-gray-400">Publisher</dt>
                    <dd>{coll.publisher}</dd>
                  </>
                )}
                {coll.version && (
                  <>
                    <dt className="text-gray-400">Version</dt>
                    <dd>{coll.version}</dd>
                  </>
                )}
                {link && (
                  <>
                    <dt className="text-gray-400">Source</dt>
                    <dd className="truncate">
                      <a href={link} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:text-indigo-800 underline">
                        {link}
                      </a>
                    </dd>
                  </>
                )}
                <dt className="text-gray-400">Licence</dt>
                <dd>
                  {coll.license_refs?.[coll.license]?.name ?? coll.license}
                  {coll.license_note && <span className="block text-gray-400"><Prose text={coll.license_note} /></span>}
                </dd>
                <dt className="text-gray-400">Inclusion</dt>
                <dd>
                  {coll.inclusion}
                  {coll.inclusion_notes && <span className="text-gray-400"> — <Prose text={coll.inclusion_notes} /></span>}
                </dd>
                <dt className="text-gray-400">File</dt>
                <dd className="font-mono">{c.file}</dd>
                {sha && (
                  <>
                    <dt className="text-gray-400">SHA-256</dt>
                    <dd className="font-mono break-all text-[10px] leading-tight">{sha}</dd>
                  </>
                )}
              </dl>
              <DatasetBreakdown meta={c} counts={datasetCounts} />
            </div>
          );
        })}
      </div>
      {checksums?.get(INDEX_FILE) && (
        <p className="text-[10px] text-gray-500 break-all">
          {INDEX_FILE} SHA-256: <span className="font-mono">{checksums.get(INDEX_FILE)}</span>
        </p>
      )}
    </>
  );
}

/**
 * Visualisation of how a source spectrum is convolved with a sensor's
 * spectral response functions to produce per-band reflectance values.
 *
 * Math (the same operation the viewer's downsampling code does):
 *   r_band = Σ_λ R(λ) · SRF(λ) / Σ_λ SRF(λ)
 *
 * The diagram has three layers in one SVG so visual relationships line up:
 *   1. Faint grey trace — original high-resolution spectrum R(λ)
 *   2. Three coloured Gaussian humps — sensor SRFs at three band centres
 *   3. Three coloured dots at the band centres — resulting band reflectances
 */
function ResamplingDiagram() {
  // SVG layout — pixel coords in the 0..420 × 0..160 viewbox.
  const width = 420;
  const height = 160;
  const left = 30;
  const right = 405;
  const top = 12;
  const bottom = 130;

  const wlMin = 400;
  const wlMax = 1000;
  const xOf = (wl: number) => left + ((wl - wlMin) / (wlMax - wlMin)) * (right - left);
  const yOf = (r: number) => top + (1 - r) * (bottom - top); // r in [0,1]

  // Synthetic high-resolution spectrum (vegetation-like).
  // A small smooth shape with an absorption dip near 670nm and a NIR plateau.
  const hires: { wl: number; r: number }[] = [];
  for (let wl = wlMin; wl <= wlMax; wl += 5) {
    const visGreen = 0.18 * Math.exp(-(((wl - 555) / 60) ** 2));
    const redEdge = 0.55 / (1 + Math.exp(-(wl - 720) / 12));
    const absorption = -0.12 * Math.exp(-(((wl - 670) / 14) ** 2));
    const r = Math.max(0.04, 0.05 + visGreen + redEdge + absorption);
    hires.push({ wl, r });
  }
  const hiresPath =
    'M ' + hires.map((p) => `${xOf(p.wl).toFixed(1)} ${yOf(p.r).toFixed(1)}`).join(' L ');

  // Three sensor bands: blue (490), red (660), NIR (840). Each with a
  // Gaussian SRF and a resulting band reflectance from a discrete weighted
  // sum over the high-res spectrum.
  const bands = [
    { centre: 490, fwhm: 30, color: '#2563EB', label: 'Blue' },
    { centre: 660, fwhm: 30, color: '#DC2626', label: 'Red' },
    { centre: 840, fwhm: 70, color: '#7C3AED', label: 'NIR' },
  ];

  function srf(wl: number, centre: number, fwhm: number) {
    const sigma = fwhm / 2.355;
    return Math.exp(-((wl - centre) ** 2) / (2 * sigma * sigma));
  }

  const bandResults = bands.map((b) => {
    let num = 0;
    let den = 0;
    for (const p of hires) {
      const w = srf(p.wl, b.centre, b.fwhm);
      num += w * p.r;
      den += w;
    }
    return { ...b, r: num / den };
  });

  // Build a closed path for each Gaussian SRF (filled hump).
  function srfPath(centre: number, fwhm: number) {
    const pts: string[] = [];
    for (let wl = wlMin; wl <= wlMax; wl += 4) {
      const s = srf(wl, centre, fwhm);
      // Scale so the SRF peaks at ~0.85 reflectance to share the y-axis.
      pts.push(`${xOf(wl).toFixed(1)} ${yOf(s * 0.85).toFixed(1)}`);
    }
    return `M ${xOf(wlMin)} ${yOf(0)} L ${pts.join(' L ')} L ${xOf(wlMax)} ${yOf(0)} Z`;
  }

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="w-full h-auto rounded-lg bg-gray-50 border border-gray-200"
      role="img"
      aria-label="Sensor spectral response convolution diagram"
    >
      {/* axes */}
      <line x1={left} y1={bottom} x2={right} y2={bottom} stroke="#9CA3AF" strokeWidth="1" />
      <line x1={left} y1={top} x2={left} y2={bottom} stroke="#9CA3AF" strokeWidth="1" />
      <text x={left - 4} y={top + 4} fill="#6B7280" fontSize="9" textAnchor="end">
        R
      </text>
      <text x={right} y={bottom + 14} fill="#6B7280" fontSize="9" textAnchor="end">
        wavelength (nm)
      </text>
      {[wlMin, 700, 1000].map((wl) => (
        <g key={wl}>
          <line x1={xOf(wl)} y1={bottom} x2={xOf(wl)} y2={bottom + 3} stroke="#9CA3AF" />
          <text x={xOf(wl)} y={bottom + 14} fill="#9CA3AF" fontSize="9" textAnchor="middle">
            {wl}
          </text>
        </g>
      ))}

      {/* SRF humps under the spectrum */}
      {bands.map((b) => (
        <path key={`srf-${b.centre}`} d={srfPath(b.centre, b.fwhm)} fill={b.color} opacity="0.18" />
      ))}

      {/* Original high-resolution spectrum */}
      <path d={hiresPath} fill="none" stroke="#374151" strokeWidth="1.4" />

      {/* Per-band resulting reflectance points */}
      {bandResults.map((b) => (
        <g key={`pt-${b.centre}`}>
          <line
            x1={xOf(b.centre)}
            y1={yOf(b.r)}
            x2={xOf(b.centre)}
            y2={bottom}
            stroke={b.color}
            strokeDasharray="2 3"
            strokeWidth="1"
          />
          <circle cx={xOf(b.centre)} cy={yOf(b.r)} r="4" fill={b.color} />
          <text
            x={xOf(b.centre)}
            y={yOf(b.r) - 8}
            fill={b.color}
            fontSize="9"
            fontWeight="600"
            textAnchor="middle"
          >
            {b.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

function ResamplingTab() {
  return (
    <>
      <p>
        Real satellite sensors don't see continuous spectra — they integrate
        incoming light over a handful of broad bands, each with its own{' '}
        <strong>spectral response function</strong> (SRF). The viewer simulates
        this when you select a sensor: it convolves each high-resolution
        reference spectrum with the sensor's SRFs to predict what the sensor
        would observe.
      </p>

      <ResamplingDiagram />

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">What's happening</h3>
        <p className="text-gray-600">
          Each coloured hump above is one sensor band's SRF (a Gaussian
          centred on the band wavelength). The grey trace is the source
          spectrum from the library. The resulting per-band reflectance is the
          weighted average of the source spectrum under that hump:
        </p>
        <pre className="mt-2 px-3 py-2 bg-gray-50 border border-gray-200 rounded text-xs overflow-x-auto">
{`r_band = Σ_λ R(λ) · SRF(λ)
          ────────────────
              Σ_λ SRF(λ)`}
        </pre>
        <p className="mt-2 text-gray-600">
          Bands with wider FWHM (full-width-at-half-maximum, e.g. NIR ~70 nm)
          smooth the spectrum more aggressively than narrow ones (e.g. Blue
          ~30 nm); the dashed lines in the diagram show each band's resulting
          reflectance value sitting on top of the source curve.
        </p>
      </div>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">A worked vector example</h3>
        <p className="text-gray-600">
          Consider a tiny source spectrum sampled at 5 wavelengths and a single
          sensor band centred at 600 nm with FWHM 100 nm:
        </p>
        <pre className="mt-2 px-3 py-2 bg-gray-50 border border-gray-200 rounded text-xs overflow-x-auto">
{`λ (nm)  500    550    600    650    700
R(λ)    0.05   0.08   0.30   0.45   0.50
SRF     0.32   0.78   1.00   0.78   0.32

weighted sum   = 0.05·0.32 + 0.08·0.78 + 0.30·1.00 + 0.45·0.78 + 0.50·0.32
               = 0.016 + 0.062 + 0.30 + 0.351 + 0.16
               = 0.889
SRF sum        = 0.32 + 0.78 + 1.00 + 0.78 + 0.32 = 3.20
r_band         = 0.889 / 3.20 ≈ 0.278`}
        </pre>
        <p className="mt-2 text-gray-600">
          Expand from 5 samples to ~2,000 (a typical VNIR axis) and from one
          band to ~10–200 (a multispectral or hyperspectral sensor) and you
          have what the viewer's downsampling does on every plot.
        </p>
      </div>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Supported sensors</h3>
        <p className="text-gray-600">
          Sentinel-2, Landsat-8/9, WorldView-3, SuperDove, Wyvern, EnMAP, PRISMA,
          and Tanager. Each ships its band centres and FWHM definitions; bands
          falling outside the source spectrum's wavelength range are dropped.
        </p>
      </div>
    </>
  );
}

function LicensingTab() {
  const { state } = useAppContext();
  const collections = [...state.collections.values()];
  return (
    <>
      <div className="bg-amber-50 border border-amber-200 rounded-lg p-3">
        <h3 className="font-semibold text-amber-900 mb-1">Licensing notice</h3>
        <p className="text-amber-800 text-xs">
          <strong>Licences differ between collections, and in EcoSIS between datasets.</strong> Every spectrum carries
          its own licence (its <code className="bg-amber-100 px-1 rounded">license</code> column, shown in its
          details), which is authoritative. Exports include the licences and citations of the spectra they contain.
          See the{' '}
          <a
            href={LICENSING_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-amber-700 underline hover:text-amber-900"
          >
            licensing documentation
          </a>
          .
        </p>
      </div>
      <div className="space-y-2">
        {collections.map((c) => {
          const coll = c.footer.collection;
          const refs = Object.entries(coll.license_refs ?? {});
          return (
            <div key={c.id} className="border border-gray-200 rounded-lg p-3 text-xs">
              <div className="font-semibold text-gray-900 text-sm mb-1">{coll.title}</div>
              <div className="text-gray-700">
                {coll.license_refs?.[coll.license]?.name ?? coll.license}
                {coll.license !== (coll.license_refs?.[coll.license]?.name ?? coll.license) && (
                  <span className="ml-1 font-mono text-gray-400">{coll.license}</span>
                )}
              </div>
              {coll.license_note && <div className="text-gray-500 mt-1"><Prose text={coll.license_note} /></div>}
              {refs.length > 0 && (
                <ul className="mt-1 space-y-0.5 text-gray-500">
                  {refs.map(([id, r]) => (
                    <li key={id}>
                      <span className="font-mono">{id}</span>: {r.name}
                      {r.url && (
                        <>
                          {' '}
                          <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-indigo-600 underline">
                            terms
                          </a>
                        </>
                      )}
                      {r.text && <p className="mt-0.5 text-gray-500">{r.text}</p>}
                    </li>
                  ))}
                </ul>
              )}
              {coll.citation && <div className="text-gray-500 mt-1 italic">{coll.citation}</div>}
            </div>
          );
        })}
      </div>
    </>
  );
}
