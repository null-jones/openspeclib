import { useEffect, useState } from 'react';
import { OPENSPECLIB_VERSION, RELEASE_URL } from '../../constants/urls';
import { fetchChecksums, fetchLicenses } from '../../lib/catalog';
import { useAppContext } from '../../state/AppContext';
import type { LicensesFile } from '../../types/catalog';

type TabId = 'overview' | 'data' | 'resampling' | 'licensing';

const TABS: ReadonlyArray<{ id: TabId; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'data', label: 'Data sources' },
  { id: 'resampling', label: 'Resampling' },
  { id: 'licensing', label: 'Licensing' },
];

export function InfoModal({ onClose }: { onClose: () => void }) {
  const [activeTab, setActiveTab] = useState<TabId>('overview');

  // Close on escape so the modal feels native.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-xl max-w-2xl w-full mx-4 max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">About OpenSpecLib Viewer</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
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

        <div className="px-6 pt-3 border-b border-gray-100">
          <nav className="flex gap-1" aria-label="About sections">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={
                  'px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ' +
                  (activeTab === tab.id
                    ? 'border-indigo-500 text-indigo-700'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300')
                }
                aria-current={activeTab === tab.id ? 'page' : undefined}
              >
                {tab.label}
              </button>
            ))}
          </nav>
        </div>

        <div className="px-6 py-5 overflow-y-auto text-sm text-gray-700 space-y-4">
          {activeTab === 'overview' && <OverviewTab />}
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

function OverviewTab() {
  return (
    <>
      <p>
        <strong>OpenSpecLib</strong> is an open-source spectral library that unifies
        data from major reference collections (USGS Speclib 07, ECOSTRESS, EcoSIS,
        and more) into a single, standardized format.
      </p>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">What is this viewer?</h3>
        <p className="text-gray-600">
          A browser-based tool to search, explore, and visualize spectra from
          OpenSpecLib releases. It runs entirely client-side using DuckDB-WASM to
          query Parquet files directly from the release artifacts — no server, no
          accounts.
        </p>
      </div>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Use cases</h3>
        <ul className="list-disc list-inside space-y-1 text-gray-600">
          <li>Building custom spectral libraries for remote-sensing classification</li>
          <li>Comparing mineral, rock, soil, and vegetation reference spectra</li>
          <li>Previewing how materials appear at different sensor resolutions</li>
          <li>Exporting spectra in CSV or ENVI format for use in other tools</li>
          <li>Education and research in spectroscopy and remote sensing</li>
        </ul>
      </div>

      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Reflectance scale</h3>
        <p className="text-gray-600">
          All spectra are normalised to the <strong>0–1 unit interval</strong>.
          OpenSpecLib assumes every source scale is a power-of-10 multiplier of the
          unit interval — one of <code className="bg-gray-100 px-1 rounded">0–1</code>,{' '}
          <code className="bg-gray-100 px-1 rounded">0–100</code>, or{' '}
          <code className="bg-gray-100 px-1 rounded">0–10000</code> — and infers
          which one applies per dataset from the data itself. The pre-normalisation
          divisor, when non-trivial, is recorded in each record's
          <code className="bg-gray-100 px-1 rounded ml-1">additional_properties</code>{' '}
          for provenance.
        </p>
      </div>
    </>
  );
}

interface SourceRow {
  key: string;
  name: string;
  count?: number;
  license?: string;
  citationDoi?: string | null;
  url?: string;
}

function DataTab() {
  const { state } = useAppContext();
  const [licenses, setLicenses] = useState<LicensesFile | null>(null);
  const [checksums, setChecksums] = useState<Map<string, string> | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchLicenses(), fetchChecksums()]).then(([lic, sums]) => {
      if (cancelled) return;
      setLicenses(lic);
      setChecksums(sums);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Cross-reference catalog source counts (already in memory) with licenses.
  const sourceCounts = new Map<string, number>();
  for (const r of state.catalogRecords) {
    sourceCounts.set(r.source.library, (sourceCounts.get(r.source.library) ?? 0) + 1);
  }

  const rows: SourceRow[] = [];
  if (licenses) {
    for (const [key, info] of Object.entries(licenses.sources)) {
      rows.push({
        key,
        name: info.name || key,
        count: sourceCounts.get(key),
        license: info.license,
        citationDoi: info.citation_doi,
        url: info.url,
      });
    }
  } else {
    // Fallback when licenses.json isn't deployed yet — still show what
    // we know from the catalog.
    for (const [key, count] of sourceCounts) {
      rows.push({ key, name: key, count });
    }
  }

  return (
    <>
      <p>
        OpenSpecLib v{OPENSPECLIB_VERSION} ships {state.catalogRecords.length.toLocaleString()}{' '}
        spectra across {rows.length} source libraries. Each source is a single
        zstd-compressed Parquet file with column statistics enabled, so HTTP
        Range queries fetch only the row groups they need.
      </p>

      {loading && <p className="text-gray-400 text-xs">Loading source metadata…</p>}

      <div className="space-y-3">
        {rows.map((r) => {
          const filename = `${r.key}.parquet`;
          const sha = checksums?.get(filename);
          return (
            <div key={r.key} className="border border-gray-200 rounded-lg p-3">
              <div className="flex items-baseline justify-between gap-2 mb-1">
                <h4 className="font-semibold text-gray-900">{r.name}</h4>
                {r.count !== undefined && (
                  <span className="text-xs text-gray-500 font-mono">
                    {r.count.toLocaleString()} spectra
                  </span>
                )}
              </div>
              <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs text-gray-600">
                {r.url && (
                  <>
                    <dt className="text-gray-400">Source</dt>
                    <dd className="truncate">
                      <a
                        href={r.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-indigo-600 hover:text-indigo-800 underline"
                      >
                        {r.url}
                      </a>
                    </dd>
                  </>
                )}
                {r.license && (
                  <>
                    <dt className="text-gray-400">License</dt>
                    <dd>{r.license}</dd>
                  </>
                )}
                {r.citationDoi && (
                  <>
                    <dt className="text-gray-400">DOI</dt>
                    <dd>
                      <a
                        href={`https://doi.org/${r.citationDoi}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-indigo-600 hover:text-indigo-800 underline font-mono"
                      >
                        {r.citationDoi}
                      </a>
                    </dd>
                  </>
                )}
                <dt className="text-gray-400">File</dt>
                <dd className="font-mono">{filename}</dd>
                {sha && (
                  <>
                    <dt className="text-gray-400">SHA-256</dt>
                    <dd className="font-mono break-all text-[10px] leading-tight">
                      {sha}
                    </dd>
                  </>
                )}
              </dl>
            </div>
          );
        })}
      </div>

      {!loading && !checksums && (
        <p className="text-amber-700 text-xs">
          Checksums file not deployed — verify integrity against the{' '}
          <a
            href={RELEASE_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="underline"
          >
            release page
          </a>{' '}
          directly.
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
  const [licenses, setLicenses] = useState<LicensesFile | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchLicenses().then((lic) => {
      if (!cancelled) setLicenses(lic);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      <div className="bg-amber-50 border border-amber-200 rounded-lg p-3">
        <h3 className="font-semibold text-amber-900 mb-1">Licensing notice</h3>
        <p className="text-amber-800 text-xs">
          <strong>Licensing terms differ between source spectral libraries.</strong>{' '}
          Each source has its own license governing how its data may be used. Most
          sources are public domain, but some (e.g. the Bishop Spectral Library)
          restrict use to non-commercial purposes with mandatory citation. Always
          check the{' '}
          <code className="bg-amber-100 px-1 rounded">source.library</code> field
          per spectrum and consult the{' '}
          <a
            href="https://github.com/null-jones/openspeclib/blob/main/docs/licensing.md"
            target="_blank"
            rel="noopener noreferrer"
            className="text-amber-700 underline hover:text-amber-900"
          >
            licensing documentation
          </a>{' '}
          for full details.
        </p>
      </div>

      {licenses ? (
        <div className="space-y-2">
          {Object.entries(licenses.sources).map(([key, info]) => (
            <div key={key} className="border border-gray-200 rounded-lg p-3 text-xs">
              <div className="font-semibold text-gray-900 text-sm mb-1">{info.name}</div>
              <div className="text-gray-700">{info.license}</div>
              {info.citation && (
                <div className="text-gray-500 mt-1 italic">{info.citation}</div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-gray-400 text-xs">
          Per-source license details unavailable in this deployment.
        </p>
      )}
    </>
  );
}
