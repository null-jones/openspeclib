import { useEffect, useState, type ReactNode } from 'react';
import { useAppContext } from '../../state/AppContext';
import { getDataEngine } from '../../lib/data/browser';
import { fetchDetails, fetchSpectra, parseExtra } from '../../lib/data/spectra';
import type { SpectrumDetails as Details, SpectrumFull, SpectrumRecord } from '../../lib/data/types';
import { isMissingValue, toMicrometersWithValues } from '../../lib/wavelength-utils';
import { GEOMETRIES, QUANTITIES, capitalise, vocabLabel } from '../../format/vocab';
import GeometryDiagram from '../format/GeometryDiagram';
import { implausibleExtent } from '../../lib/data/search';
import { own } from '../../lib/own';

function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-gray-400">{label}</dt>
      <dd className="text-xs text-gray-700 mt-0.5 break-words">{children}</dd>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h4 className="text-xs font-semibold text-gray-700 uppercase tracking-wide mb-2">{title}</h4>
      {children}
    </section>
  );
}

/** Inline SVG quick-look of a spectrum — no charting library needed. */
function PreviewSparkline({ spectrum }: { spectrum: SpectrumFull }) {
  const { wavelengths, values } = toMicrometersWithValues(spectrum.wavelengths, spectrum.values, spectrum.wavelength_unit);
  const points: [number, number][] = [];
  for (let i = 0; i < values.length; i++) {
    if (!isMissingValue(values[i])) points.push([wavelengths[i], values[i]]);
  }
  if (points.length < 2) return <p className="text-xs text-gray-400 italic">No plottable values.</p>;
  // Loops, not Math.min(...array): spectra can be long enough to overflow the call stack.
  let [xMin, xMax, yMin, yMax] = [Infinity, -Infinity, Infinity, -Infinity];
  for (const [x, y] of points) {
    if (x < xMin) xMin = x;
    if (x > xMax) xMax = x;
    if (y < yMin) yMin = y;
    if (y > yMax) yMax = y;
  }
  const W = 640;
  const H = 96;
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;
  const path = points
    .map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${(((x - xMin) / xSpan) * W).toFixed(1)},${(H - ((y - yMin) / ySpan) * H).toFixed(1)}`)
    .join(' ');
  const missing = values.length - points.length;
  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-24 bg-white border border-gray-200 rounded"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Preview of ${spectrum.name}`}
      >
        <path d={path} fill="none" stroke="#4f46e5" strokeWidth={1.25} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between text-[10px] text-gray-500 mt-0.5 font-mono">
        <span>{xMin.toFixed(2)} μm</span>
        <span>
          {spectrum.quantity} {yMin.toFixed(3)}–{yMax.toFixed(3)}
          {missing > 0 && ` · ${missing} missing band${missing === 1 ? '' : 's'} (NaN)`}
        </span>
        <span>{xMax.toFixed(2)} μm</span>
      </div>
    </div>
  );
}

/** Columns of Arrow type date32 (DuckDB returns them as milliseconds since 1970). */
const DATE_COLUMNS = new Set(['sample_collection_date', 'measurement_date']);

function str(v: unknown, key?: string): string | null {
  if (v === null || v === undefined || v === '') return null;
  if (key && DATE_COLUMNS.has(key)) {
    if (typeof v === 'number' && Number.isFinite(v)) return new Date(v).toISOString().slice(0, 10);
    if (v instanceof Date) return v.toISOString().slice(0, 10);
  }
  if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === 'object' && x !== null ? JSON.stringify(x) : String(x))).join(', ') : null;
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** Fields of a row, with their labels, when present. */
function Fields({ details, fields }: { details: Details; fields: Array<[string, string]> }) {
  const shown = fields.map(([k, label]) => [label, str(details[k], k)] as const).filter(([, v]) => v);
  if (shown.length === 0) return <p className="text-xs text-gray-400 italic">Not recorded.</p>;
  return (
    <dl className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
      {shown.map(([label, v]) => (
        <DetailField key={label} label={label}>
          {v}
        </DetailField>
      ))}
    </dl>
  );
}

export function SimulatedBadge() {
  return (
    <span
      className="inline-flex items-center px-1.5 py-px rounded text-[10px] font-medium bg-fuchsia-50 text-fuchsia-700 border border-fuchsia-200"
      title="measurement_processing includes 'simulated': the spectrum is synthetic or modelled rather than measured"
    >
      simulated
    </span>
  );
}

export default function SpectrumDetails({ record }: { record: SpectrumRecord }) {
  const { state, dispatch } = useAppContext();
  const [spectrum, setSpectrum] = useState<SpectrumFull | null>(null);
  const [details, setDetails] = useState<Details | null>(null);
  const [error, setError] = useState<string | null>(null);
  const meta = state.collections.get(record.collection);
  const source = state.sources.get(record.sourceKey);

  useEffect(() => {
    if (!source) return;
    let cancelled = false;
    getDataEngine()
      .then(async (engine) => {
        const [d, s] = await Promise.all([
          fetchDetails(engine, record, source),
          fetchSpectra(engine, [record], new Map([[source.key, source]])),
        ]);
        if (cancelled) return;
        setDetails(d);
        setSpectrum(s[0] ?? null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [record, source]);

  const coll = meta?.footer.collection;
  const licenseRef = own(coll?.license_refs, record.license);
  const dataset = own(meta?.footer.datasets, record.dataset_id);
  const { json: extra, mask } = parseExtra(details?.extra);
  // A divisor of 1 means the source already stored fractions: nothing to say.
  const rescaled = mask.sourceDivisor != null && Number(mask.sourceDivisor) !== 1;
  const processing = Array.isArray(details?.measurement_processing) ? (details!.measurement_processing as string[]) : [];

  return (
    <div className="space-y-4 text-sm">
      <Section title="Preview">
        {error ? (
          <p className="text-xs text-red-500">Could not load this spectrum: {error}</p>
        ) : spectrum ? (
          <PreviewSparkline spectrum={spectrum} />
        ) : (
          <p className="text-xs text-gray-400 italic">Loading preview…</p>
        )}
      </Section>

      <Section title="What was measured">
        <div className="flex gap-4 items-start">
          <div className="w-28 flex-shrink-0 bg-white border border-gray-200 rounded p-1" title={own(GEOMETRIES, record.geometry)}>
            <GeometryDiagram geometry={record.geometry} />
          </div>
          <dl className="grid grid-cols-2 md:grid-cols-3 gap-3 flex-1">
            <DetailField label="Quantity">
              <span title={own(QUANTITIES, record.quantity)}>{capitalise(record.quantity)}</span>
            </DetailField>
            <DetailField label="Geometry">
              <span title={own(GEOMETRIES, record.geometry)}>{vocabLabel(record.geometry)}</span>{' '}
              <button
                onClick={() => dispatch({ type: 'SET_VIEW', view: 'format', page: 'reference', section: 'geometry' })}
                className="text-[10px] text-indigo-500 hover:text-indigo-700"
              >
                (explain)
              </button>
            </DetailField>
            <DetailField label="Wavelengths">
              {record.wavelength_min_nm.toLocaleString('en-US', { maximumFractionDigits: 1 })}–
              {record.wavelength_max_nm.toLocaleString('en-US', { maximumFractionDigits: 1 })} nm, stored in {record.wavelength_unit}
              {record.num_points != null && `, ${record.num_points.toLocaleString()} points`}
              {implausibleExtent(record) && (
                <span className="block mt-0.5 text-amber-700">
                  Outside 100 nm–1 mm: the wavelength unit is probably mislabelled in the source data.
                </span>
              )}
            </DetailField>
            {str(details?.measurement_reference) && (
              <DetailField label="Reference standard">{str(details?.measurement_reference)}</DetailField>
            )}
            {processing.length > 0 && (
              <DetailField label="Processing">
                <span className="flex flex-wrap gap-1">
                  {processing.map((p) =>
                    p === 'simulated' ? (
                      <SimulatedBadge key={p} />
                    ) : (
                      <span key={p} className="px-1.5 py-px rounded text-[10px] font-medium bg-indigo-50 text-indigo-700 border border-indigo-200">
                        {p}
                      </span>
                    ),
                  )}
                </span>
              </DetailField>
            )}
          </dl>
        </div>
      </Section>

      <Section title="Licence and source">
        <dl className="grid grid-cols-2 md:grid-cols-3 gap-3">
          <DetailField label="Licence">
            {licenseRef?.url ? (
              <a href={licenseRef.url} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:text-indigo-800 underline">
                {licenseRef.name}
              </a>
            ) : (
              licenseRef?.name ?? record.license
            )}
            {licenseRef && <span className="block text-[10px] text-gray-500 font-mono">{record.license}</span>}
          </DetailField>
          <DetailField label="Collection">
            {coll?.title ?? record.collection}
            {coll?.version && <span className="text-gray-400"> · version {coll.version}</span>}
          </DetailField>
          <DetailField label="Ids">
            <span className="font-mono text-[11px] break-all">{record.id}</span>
            {str(details?.original_id) && <span className="block text-[10px] text-gray-500">original: {str(details?.original_id)}</span>}
          </DetailField>
        </dl>
        {coll?.citation && !dataset && <p className="mt-2 text-xs text-gray-500 italic">{coll.citation}</p>}
      </Section>

      {dataset && (
        <Section title="Dataset">
          <div className="bg-white border border-gray-200 rounded p-3 space-y-2">
            <div className="flex items-start justify-between gap-3 flex-wrap">
              <div className="min-w-0">
                <div className="font-medium text-gray-900">{dataset.title}</div>
                {dataset.authors && <div className="text-xs text-gray-500 mt-0.5">{dataset.authors}</div>}
                {dataset.organization && <div className="text-xs text-gray-500">{dataset.organization}</div>}
              </div>
              {dataset.license && (
                <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-amber-50 text-amber-800 border border-amber-200">
                  {coll?.license_refs?.[dataset.license]?.name ?? dataset.license}
                </span>
              )}
            </div>
            {dataset.description && <p className="text-xs text-gray-600 leading-relaxed">{dataset.description}</p>}
            {dataset.citation && <p className="text-xs text-gray-500 italic">{dataset.citation}</p>}
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {dataset.url && (
                <a href={dataset.url} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:text-indigo-800 underline">
                  Dataset page
                </a>
              )}
              {dataset.doi && (
                <a href={`https://doi.org/${dataset.doi}`} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:text-indigo-800 underline font-mono">
                  doi:{dataset.doi}
                </a>
              )}
              {dataset.spectra_available != null && dataset.spectra_included != null && (
                <span className="text-gray-500">
                  {dataset.spectra_included.toLocaleString()} of {dataset.spectra_available.toLocaleString()} spectra included
                </span>
              )}
            </div>
          </div>
        </Section>
      )}

      {details && (
        <>
          <Section title="Measurement">
            <Fields
              details={details}
              fields={[
                ['measurement_instrument', 'Instrument'],
                ['measurement_instrument_type', 'Instrument type'],
                ['measurement_laboratory', 'Laboratory'],
                ['measurement_date', 'Date'],
                ['measurement_venue', 'Venue'],
                ['measurement_acquisition_method', 'Acquisition'],
                ['measurement_light_source', 'Light source'],
                ['measurement_foreoptic', 'Fore-optic'],
              ]}
            />
          </Section>
          <Section title="Sample">
            <Fields
              details={details}
              fields={[
                ['sample_id', 'ID'],
                ['sample_description', 'Description'],
                ['sample_particle_size', 'Particle size'],
                ['sample_origin', 'Origin'],
                ['sample_owner', 'Owner'],
                ['sample_collection_date', 'Collected'],
                ['sample_preparation', 'Preparation'],
                ['material_subcategory', 'Subcategory'],
                ['material_formula', 'Formula'],
              ]}
            />
          </Section>
        </>
      )}

      {(mask.maskedBandCount != null || mask.sourceMissingBandCount != null || mask.bandsReordered || rescaled || str(details?.quality_notes)) && (
        <Section title="Data handling">
          <ul className="text-xs text-gray-600 space-y-1 list-disc pl-4">
            {mask.maskedBandCount != null && (
              <li>
                {mask.maskedBandCount} band{mask.maskedBandCount === 1 ? '' : 's'} masked to NaN by the producer as invalid
                {mask.maskedBands.length > 0 &&
                  ` (original values: ${mask.maskedBands
                    .slice(0, 5)
                    .map(([w, v]) => `${w} → ${v}`)
                    .join(', ')}${mask.maskedBands.length > 5 ? ', …' : ''})`}
                .
              </li>
            )}
            {mask.sourceMissingBandCount != null && (
              <li>
                {mask.sourceMissingBandCount} band{mask.sourceMissingBandCount === 1 ? ' was' : 's were'} already missing in the source.
              </li>
            )}
            {mask.bandsReordered && <li>Bands were reordered by wavelength to make the axis monotonic.</li>}
            {rescaled && <li>Source values were divided by {mask.sourceDivisor} to give fractions.</li>}
            {str(details?.quality_notes) && <li>{str(details?.quality_notes)}</li>}
          </ul>
        </Section>
      )}

      {details && Object.keys(details).some((k) => k.startsWith('x_')) && (
        <Section title="Producer columns (x_)">
          <Fields
            details={details}
            fields={Object.keys(details)
              .filter((k) => k.startsWith('x_'))
              .map((k) => [k, k] as [string, string])}
          />
        </Section>
      )}

      {extra && (
        <details className="text-xs">
          <summary className="cursor-pointer text-gray-500 hover:text-gray-700">Producer metadata (extra)</summary>
          <pre className="mt-2 p-2 bg-white border border-gray-200 rounded overflow-x-auto text-[11px] leading-snug">
            {JSON.stringify(extra, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
