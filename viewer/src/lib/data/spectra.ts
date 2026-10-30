/**
 * Fetching spectral arrays and row details by id.
 *
 * Each id becomes its own `WHERE id = '…'` branch: DuckDB prunes row groups
 * only for a single equality constant (an `IN (…)` list scans the whole
 * file), so a lookup reads about one row group per spectrum.
 */
import { ident, lit } from '../conformance/sql';
import type { DataEngine } from './engine';
import type { DataSource, SpectrumDetails, SpectrumFull, SpectrumRecord } from './types';
import { own } from '../own';

function toNumbers(v: unknown): number[] {
  if (Array.isArray(v)) return v.map((x) => (x == null ? NaN : Number(x)));
  return [];
}

/** The collection file ref of each record, made readable by the engine. */
async function readableRefs(
  engine: DataEngine,
  records: SpectrumRecord[],
  sources: Map<string, DataSource>,
): Promise<string[]> {
  const refs = records.map((r) => {
    const ref = own(sources.get(r.sourceKey)?.fileRefs, r.file);
    if (!ref) throw new Error(`No collection file for ${r.id} (${r.file})`);
    return ref;
  });
  const readable = engine.readable;
  if (!readable) return refs;
  const unique = [...new Set(refs)];
  const resolved = new Map(await Promise.all(unique.map(async (ref) => [ref, await readable(ref)] as const)));
  return refs.map((ref) => resolved.get(ref)!);
}

/** SQL fetching the arrays of `records` (one UNION ALL branch per id), reading `refs[i]` for `records[i]`. */
function spectraSql(records: SpectrumRecord[], refs: string[]): string {
  return records
    .map(
      (r, i) =>
        `SELECT id, wavelengths, ${ident('values')} FROM read_parquet(${lit(refs[i])}) WHERE id = ${lit(r.id)}`,
    )
    .join('\nUNION ALL\n');
}

/** Fetch the arrays for some records. Records whose row is missing are left out. */
export async function fetchSpectra(
  engine: DataEngine,
  records: SpectrumRecord[],
  sources: Map<string, DataSource>,
): Promise<SpectrumFull[]> {
  if (records.length === 0) return [];
  const rows = await engine.rows(spectraSql(records, await readableRefs(engine, records, sources)));
  const byId = new Map(rows.map((r) => [String(r.id), r]));
  const out: SpectrumFull[] = [];
  for (const rec of records) {
    const row = byId.get(rec.id);
    if (!row) continue;
    out.push({ ...rec, wavelengths: toNumbers(row.wavelengths), values: toNumbers(row.values) });
  }
  return out;
}

/** All non-array columns of one row. */
export async function fetchDetails(
  engine: DataEngine,
  record: SpectrumRecord,
  source: DataSource,
): Promise<SpectrumDetails | null> {
  const rows = await engine.rows(`SELECT * FROM ${source.table} WHERE id = ${lit(record.id)} LIMIT 1`);
  return rows[0] ?? null;
}

/** Masked-band provenance a producer recorded in `extra` (spec section 4.1). */
export interface MaskProvenance {
  maskedBandCount: number | null;
  sourceMissingBandCount: number | null;
  /** Up to a few [wavelength, original value] pairs. */
  maskedBands: Array<[number, number]>;
  bandsReordered: boolean;
  sourceDivisor: number | null;
}

/** Parse the producer metadata in `extra`, tolerating junk. */
export function parseExtra(extra: unknown): { json: Record<string, unknown> | null; mask: MaskProvenance } {
  const mask: MaskProvenance = {
    maskedBandCount: null,
    sourceMissingBandCount: null,
    maskedBands: [],
    bandsReordered: false,
    sourceDivisor: null,
  };
  if (typeof extra !== 'string' || extra === '') return { json: null, mask };
  let json: unknown;
  try {
    json = JSON.parse(extra);
  } catch {
    return { json: null, mask };
  }
  if (typeof json !== 'object' || json === null || Array.isArray(json)) return { json: null, mask };
  const o = json as Record<string, unknown>;
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  mask.maskedBandCount = num(o.masked_band_count);
  mask.sourceMissingBandCount = num(o.source_missing_band_count);
  mask.sourceDivisor = num(o.source_reflectance_divisor);
  mask.bandsReordered = o.bands_reordered_by_wavelength === true;
  if (Array.isArray(o.masked_bands)) {
    for (const b of o.masked_bands) {
      if (Array.isArray(b) && b.length >= 2 && typeof b[0] === 'number') mask.maskedBands.push([b[0], Number(b[1])]);
    }
  }
  return { json: o, mask };
}
