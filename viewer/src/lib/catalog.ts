import { CATALOG_URL, LICENSES_URL } from '../constants/urls';
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

export interface SearchFilters {
  text: string;
  categories: string[];
  technique: string | null;
  wavelengthMin: number | null;
  wavelengthMax: number | null;
  sensorWavelengthMin: number | null;
  sensorWavelengthMax: number | null;
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
    filtered = filtered.filter(
      (r) => r.spectral_data.wavelength_max >= filters.wavelengthMin!,
    );
  }
  if (filters.wavelengthMax !== null) {
    filtered = filtered.filter(
      (r) => r.spectral_data.wavelength_min <= filters.wavelengthMax!,
    );
  }

  // Sensor range overlap filter
  if (filters.sensorWavelengthMin !== null && filters.sensorWavelengthMax !== null) {
    filtered = filtered.filter(
      (r) =>
        r.spectral_data.wavelength_max >= filters.sensorWavelengthMin! &&
        r.spectral_data.wavelength_min <= filters.sensorWavelengthMax!,
    );
    // Also restrict to reflectance when a sensor is selected
    filtered = filtered.filter((r) => r.measurement.technique === 'reflectance');
  }

  const total = filtered.length;
  const results = filtered.slice(offset, offset + limit);
  return { results, total };
}
