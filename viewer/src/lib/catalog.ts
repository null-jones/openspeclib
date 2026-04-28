import { CATALOG_URL, CHECKSUMS_URL, LICENSES_URL } from '../constants/urls';
import type { CatalogFile, CatalogRecord, LicensesFile } from '../types/catalog';

let cachedCatalog: CatalogFile | null = null;

export async function fetchCatalog(): Promise<CatalogFile> {
  if (cachedCatalog) return cachedCatalog;

  const resp = await fetch(CATALOG_URL);
  if (!resp.ok) throw new Error(`Failed to fetch catalog: ${resp.status}`);
  cachedCatalog = (await resp.json()) as CatalogFile;
  return cachedCatalog;
}

let cachedLicenses: LicensesFile | null = null;

export async function fetchLicenses(): Promise<LicensesFile | null> {
  if (cachedLicenses) return cachedLicenses;

  try {
    const resp = await fetch(LICENSES_URL);
    if (!resp.ok) return null;
    cachedLicenses = (await resp.json()) as LicensesFile;
    return cachedLicenses;
  } catch {
    return null;
  }
}

let cachedChecksums: Map<string, string> | null = null;

/**
 * Fetch and parse the release `checksums.txt` (`<sha256>  <filename>` per
 * line). Returns `null` if the file isn't present in the deployment — older
 * releases may not have shipped one and the viewer can still function.
 */
export async function fetchChecksums(): Promise<Map<string, string> | null> {
  if (cachedChecksums) return cachedChecksums;
  try {
    const resp = await fetch(CHECKSUMS_URL);
    if (!resp.ok) return null;
    const text = await resp.text();
    const map = new Map<string, string>();
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      // Standard sha256sum format: "<hex>  <filename>"; tolerate either two
      // spaces or any run of whitespace between hash and name.
      const m = trimmed.match(/^([a-f0-9]+)\s+(.+)$/i);
      if (m) {
        map.set(m[2], m[1]);
      }
    }
    cachedChecksums = map;
    return cachedChecksums;
  } catch {
    return null;
  }
}

export interface SearchFilters {
  text: string;
  categories: string[];
  sources: string[];
  technique: string | null;
  wavelengthMin: number | null;
  wavelengthMax: number | null;
  sensorWavelengthMin: number | null;
  sensorWavelengthMax: number | null;
}

/** Convert a wavelength value to micrometers based on the record's unit. */
function toMicrometerRange(r: CatalogRecord): [number, number] {
  const unit = r.spectral_data.wavelength_unit;
  let min = r.spectral_data.wavelength_min;
  let max = r.spectral_data.wavelength_max;
  if (unit === 'nm') {
    min /= 1000;
    max /= 1000;
  } else if (unit === 'cm-1') {
    // wavenumber: higher cm-1 = shorter wavelength
    const lo = 10000 / max;
    const hi = 10000 / min;
    min = lo;
    max = hi;
  }
  return [min, max];
}

/** In-memory search against catalog records (used before DuckDB is ready). */
export function searchCatalog(
  records: CatalogRecord[],
  filters: SearchFilters,
  offset: number,
  limit: number,
): { results: CatalogRecord[]; total: number } {
  let filtered = records;

  if (filters.text) {
    const q = filters.text.toLowerCase();
    filtered = filtered.filter(
      (r) =>
        r.name.toLowerCase().includes(q) ||
        r.material.name.toLowerCase().includes(q) ||
        r.material.keywords.some((k) => k.toLowerCase().includes(q)) ||
        (r.material.formula && r.material.formula.toLowerCase().includes(q)),
    );
  }

  if (filters.sources.length > 0) {
    const srcs = new Set(filters.sources);
    filtered = filtered.filter((r) => srcs.has(r.source.library));
  }

  if (filters.categories.length > 0) {
    const cats = new Set(filters.categories);
    filtered = filtered.filter((r) => cats.has(r.material.category));
  }

  if (filters.technique) {
    filtered = filtered.filter(
      (r) => r.measurement.technique === filters.technique,
    );
  }

  if (filters.wavelengthMin !== null) {
    filtered = filtered.filter((r) => {
      const [, max] = toMicrometerRange(r);
      return max >= filters.wavelengthMin!;
    });
  }
  if (filters.wavelengthMax !== null) {
    filtered = filtered.filter((r) => {
      const [min] = toMicrometerRange(r);
      return min <= filters.wavelengthMax!;
    });
  }

  // Sensor range overlap filter (sensor ranges are already in micrometers)
  if (filters.sensorWavelengthMin !== null && filters.sensorWavelengthMax !== null) {
    filtered = filtered.filter((r) => {
      const [min, max] = toMicrometerRange(r);
      return max >= filters.sensorWavelengthMin! && min <= filters.sensorWavelengthMax!;
    });
    // Also restrict to reflectance when a sensor is selected
    filtered = filtered.filter((r) => r.measurement.technique === 'reflectance');
  }

  const total = filtered.length;
  const results = filtered.slice(offset, offset + limit);
  return { results, total };
}
