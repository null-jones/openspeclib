/** In-memory search, filtering and sorting of search rows. Pure. */
import rulesDoc from '@spec/rules.json';
import type { SpectrumRecord } from './types';

export type SortKey =
  | 'material'
  | 'sample'
  | 'category'
  | 'collection'
  | 'quantity'
  | 'geometry'
  | 'range'
  | 'points';

export interface SortSpec {
  key: SortKey;
  dir: 'asc' | 'desc';
}

export interface SearchFilters {
  text: string;
  categories: string[];
  /** Collection ids (empty = all). */
  collections: string[];
  quantity: string | null;
  /** Geometry values (empty = all). */
  geometries: string[];
  hideSimulated: boolean;
  /** Overlap filter, in µm (null = none). */
  wavelengthMin: number | null;
  wavelengthMax: number | null;
  /** Sensor coverage, in µm; also restricts results to reflectance. */
  sensorWavelengthMin: number | null;
  sensorWavelengthMax: number | null;
  sort: SortSpec | null;
}

/**
 * Plausible axis extent in nm (quality rule Q4, `qa_wavelength_nm` in
 * rules.json). Outside it, the wavelength unit is almost always mislabelled.
 */
export const PLAUSIBLE_NM: [number, number] = [rulesDoc.qa_wavelength_nm[0], rulesDoc.qa_wavelength_nm[1]];

export function implausibleExtent(r: SpectrumRecord): boolean {
  return r.wavelength_min_nm < PLAUSIBLE_NM[0] || r.wavelength_max_nm > PLAUSIBLE_NM[1];
}

/** Axis extent in µm, from the nm columns every row carries. */
function rangeUm(r: SpectrumRecord): [number, number] {
  return [r.wavelength_min_nm / 1000, r.wavelength_max_nm / 1000];
}

function comparator(sort: SortSpec): (a: SpectrumRecord, b: SpectrumRecord) => number {
  const sign = sort.dir === 'asc' ? 1 : -1;
  const by = (f: (r: SpectrumRecord) => string) => (a: SpectrumRecord, b: SpectrumRecord) =>
    sign * f(a).localeCompare(f(b));
  switch (sort.key) {
    case 'material':
      return by((r) => r.material_name);
    case 'sample':
      return by((r) => r.name);
    case 'category':
      return by((r) => r.material_category);
    case 'collection':
      return by((r) => r.collection);
    case 'quantity':
      return by((r) => r.quantity);
    case 'geometry':
      return by((r) => r.geometry);
    case 'range':
      return (a, b) => sign * (a.wavelength_min_nm - b.wavelength_min_nm);
    case 'points':
      return (a, b) => sign * ((a.num_points ?? -1) - (b.num_points ?? -1));
  }
}

/** Apply the filters except paging. */
export function filterRecords(records: SpectrumRecord[], f: SearchFilters): SpectrumRecord[] {
  const q = f.text.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  const cats = f.categories.length ? new Set(f.categories) : null;
  const colls = f.collections.length ? new Set(f.collections) : null;
  const geos = f.geometries.length ? new Set(f.geometries) : null;
  const sensor = f.sensorWavelengthMin !== null && f.sensorWavelengthMax !== null;
  return records.filter((r) => {
    if (terms.length && !terms.every((t) => r.search.includes(t))) return false;
    if (colls && !colls.has(r.collection)) return false;
    if (cats && !cats.has(r.material_category)) return false;
    if (f.quantity && r.quantity !== f.quantity) return false;
    if (geos && !geos.has(r.geometry)) return false;
    if (f.hideSimulated && r.simulated) return false;
    const [lo, hi] = rangeUm(r);
    // A row without a finite extent cannot be placed in a wavelength range.
    const rangeFilter = f.wavelengthMin !== null || f.wavelengthMax !== null || sensor;
    if (rangeFilter && !(Number.isFinite(lo) && Number.isFinite(hi))) return false;
    if (f.wavelengthMin !== null && hi < f.wavelengthMin) return false;
    if (f.wavelengthMax !== null && lo > f.wavelengthMax) return false;
    if (sensor) {
      if (r.quantity !== 'reflectance') return false;
      if (hi < f.sensorWavelengthMin! || lo > f.sensorWavelengthMax!) return false;
    }
    return true;
  });
}

/** Search with paging. */
export function searchRecords(
  records: SpectrumRecord[],
  filters: SearchFilters,
  offset: number,
  limit: number,
): { results: SpectrumRecord[]; total: number } {
  let filtered = filterRecords(records, filters);
  if (filters.sort) filtered = [...filtered].sort(comparator(filters.sort));
  return { results: filtered.slice(offset, offset + limit), total: filtered.length };
}

/** Count records per value of a field, for filter chips. */
export function countBy(records: SpectrumRecord[], key: (r: SpectrumRecord) => string): Map<string, number> {
  const m = new Map<string, number>();
  for (const r of records) {
    const k = key(r);
    m.set(k, (m.get(k) ?? 0) + 1);
  }
  return m;
}
