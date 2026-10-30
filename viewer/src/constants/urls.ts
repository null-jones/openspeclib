// Release data is served from public/data/ (same origin, no CORS issues).
const BASE = import.meta.env.BASE_URL;

// The deploy workflow and scripts/deploy_local.sh read this exact line to
// pick the GitHub release to download; keep its form.
export const OPENSPECLIB_VERSION = '0.2.0';
export const RELEASE_URL = `https://github.com/null-jones/openspeclib/releases/tag/v${OPENSPECLIB_VERSION}`;

// Release files ship with version-less names. A version query string makes
// clients holding a cached file from the previous release fetch it again.
const VERSION_QS = `?v=${OPENSPECLIB_VERSION}`;

/** Absolute URL of a release data file (DuckDB needs absolute URLs). */
export function dataFileUrl(file: string): string {
  return new URL(`${BASE}data/${file}${VERSION_QS}`, window.location.origin).href;
}

export const INDEX_FILE = 'openspeclib-index.parquet';
export const MIGRATION_FILE = 'id-migration.csv';
export const CHECKSUMS_FILE = 'checksums.txt';
