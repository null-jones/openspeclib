import * as duckdb from '@duckdb/duckdb-wasm';
import * as arrow from 'apache-arrow';
import { getParquetUrl, getWavelengthsUrl } from '../constants/urls';

let db: duckdb.AsyncDuckDB | null = null;
let conn: duckdb.AsyncDuckDBConnection | null = null;

export const SOURCES = ['usgs_splib07', 'ecosis', 'ecostress'];

// Two-phase init:
//   1. `connectionPromise` resolves once the DuckDB-WASM worker is up and a
//      connection is open. This is what gates the visual "Ready" indicator.
//   2. `dataPromise` resolves once the per-source views are created and the
//      wavelength registry is materialised. It's started immediately after
//      the connection comes up but runs in the background — UI doesn't wait
//      for it. Anything that needs to query data (currently only
//      fetchSpectraByIds) awaits it via `ensureDataReady()`.
//
// This split shaves the parquet-footer Range fetches and the wavelength
// registry SELECT off the perceived init time. Users searching the catalog
// don't need DuckDB at all, so they get a "Ready" status as soon as the WASM
// is instantiated.
let connectionPromise: Promise<duckdb.AsyncDuckDBConnection> | null = null;
let dataPromise: Promise<void> | null = null;

// In-memory wavelength grid registry, populated on demand via dataPromise and
// shared by every subsequent fetchSpectraByIds call. Keyed by grid_id (int32)
// so queries can return a small int reference instead of repeating the full
// ~2000-float wavelength array per row.
let wavelengthGrids: Map<number, Float64Array> | null = null;

export function getWavelengthGrid(gridId: number): Float64Array {
  if (!wavelengthGrids) throw new Error('Wavelength registry not initialized');
  const grid = wavelengthGrids.get(gridId);
  if (!grid) throw new Error(`Unknown wavelength grid_id: ${gridId}`);
  return grid;
}

export function initDuckDB(): Promise<duckdb.AsyncDuckDBConnection> {
  if (!connectionPromise) {
    connectionPromise = openConnection();
    // Kick off data loading in the background. We intentionally do NOT
    // await this here — the visual "Ready" indicator should flip as soon
    // as the WASM is up, not after the parquet footers are fetched.
    dataPromise = connectionPromise.then(loadData);
  }
  return connectionPromise;
}

async function openConnection(): Promise<duckdb.AsyncDuckDBConnection> {
  const base = import.meta.env.BASE_URL;

  // Use locally-hosted WASM files instead of jsDelivr CDN.
  const MANUAL_BUNDLES: duckdb.DuckDBBundles = {
    mvp: {
      mainModule: `${base}duckdb/duckdb-mvp.wasm`,
      mainWorker: `${base}duckdb/duckdb-browser-mvp.worker.js`,
    },
    eh: {
      mainModule: `${base}duckdb/duckdb-eh.wasm`,
      mainWorker: `${base}duckdb/duckdb-browser-eh.worker.js`,
    },
  };

  const bundle = await duckdb.selectBundle(MANUAL_BUNDLES);

  const worker = new Worker(bundle.mainWorker!);
  const logger = new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING);
  db = new duckdb.AsyncDuckDB(logger, worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);

  conn = await db.connect();
  return conn;
}

async function loadData(c: duckdb.AsyncDuckDBConnection): Promise<void> {
  // Register each parquet source as a view. Each `CREATE VIEW` fetches the
  // parquet footer to resolve the schema, so we run them in parallel along
  // with the wavelengths view to overlap the Range-request latency.
  await Promise.all([
    ...SOURCES.map((source) =>
      c.query(`CREATE VIEW ${source} AS SELECT * FROM '${getParquetUrl(source)}'`),
    ),
    c.query(`CREATE VIEW wavelengths AS SELECT * FROM '${getWavelengthsUrl()}'`),
  ]);

  // Materialise the wavelength grid registry once. The file is small (~100s
  // of KB) and every spectral fetch needs it to rehydrate the wavelength
  // axis from a grid_id reference.
  wavelengthGrids = await loadWavelengthGrids(c);
}

async function loadWavelengthGrids(
  c: duckdb.AsyncDuckDBConnection,
): Promise<Map<number, Float64Array>> {
  const result = await c.query('SELECT grid_id, wavelengths FROM wavelengths');
  const grids = new Map<number, Float64Array>();
  for (let i = 0; i < result.numRows; i++) {
    const row = result.get(i);
    if (!row) continue;
    const gridId = row.grid_id as number;
    const wlVal = row.wavelengths;
    let wavelengths: Float64Array;
    if (wlVal && typeof wlVal === 'object' && 'toArray' in wlVal) {
      wavelengths = Float64Array.from(
        (wlVal as { toArray(): ArrayLike<number> }).toArray(),
      );
    } else if (Array.isArray(wlVal)) {
      wavelengths = Float64Array.from(wlVal as number[]);
    } else {
      wavelengths = new Float64Array(0);
    }
    grids.set(gridId, wavelengths);
  }
  return grids;
}

/**
 * Wait until the per-source views are registered and the wavelength
 * registry is loaded. Callers that issue queries against the source views
 * (e.g. `fetchSpectraByIds`) must await this before running their SELECT;
 * `initDuckDB` itself returns earlier so the UI doesn't block on it.
 */
export async function ensureDataReady(): Promise<void> {
  if (!dataPromise) {
    // initDuckDB was never called — fall back to triggering the full init.
    await initDuckDB();
  }
  await dataPromise;
}

export async function getConnection(): Promise<duckdb.AsyncDuckDBConnection> {
  if (!conn) throw new Error('DuckDB not initialized');
  return conn;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function query(sql: string): Promise<arrow.Table<any>> {
  await ensureDataReady();
  const c = await getConnection();
  return c.query(sql);
}
