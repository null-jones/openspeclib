// Data files are served from public/data/ (same origin, no CORS issues).
const BASE = import.meta.env.BASE_URL;

export const CATALOG_URL = `${BASE}data/openspeclib-catalog-0.0.3.json`;
export const LICENSES_URL = `${BASE}data/licenses.json`;

/** Returns absolute URL for parquet files (DuckDB needs full URLs). */
export function getParquetUrl(source: string): string {
  const path = `${BASE}data/${source}.parquet`;
  return new URL(path, window.location.origin).href;
}
