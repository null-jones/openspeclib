// Data files are served from public/data/ (same origin, no CORS issues).
const BASE = import.meta.env.BASE_URL;

export const OPENSPECLIB_VERSION = '0.0.6';
export const CATALOG_URL = `${BASE}data/openspeclib-catalog-${OPENSPECLIB_VERSION}.json`;
export const LICENSES_URL = `${BASE}data/licenses.json`;

// Parquet artifacts ship with version-less filenames (usgs_splib07.parquet,
// ecosis.parquet, …). Append a version query string so clients holding a
// cached parquet from the previous release re-fetch automatically when the
// schema changes. This matches how the catalog filename already carries the
// version inline.
const VERSION_QS = `?v=${OPENSPECLIB_VERSION}`;

/** Returns absolute URL for parquet files (DuckDB needs full URLs). */
export function getParquetUrl(source: string): string {
  const path = `${BASE}data/${source}.parquet${VERSION_QS}`;
  return new URL(path, window.location.origin).href;
}

/** Absolute URL for the master wavelength grid registry. */
export function getWavelengthsUrl(): string {
  return getParquetUrl('wavelengths');
}
