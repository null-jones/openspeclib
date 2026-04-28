import * as duckdb from '@duckdb/duckdb-wasm';
import * as arrow from 'apache-arrow';
import { getParquetUrl, getWavelengthsUrl } from '../constants/urls';

let db: duckdb.AsyncDuckDB | null = null;
let conn: duckdb.AsyncDuckDBConnection | null = null;

export const SOURCES = ['usgs_splib07', 'ecosis', 'ecostress', 'ossl'];

// In-memory wavelength grid registry, populated once on initDuckDB and shared
// by every subsequent fetchSpectraByIds call. Keyed by grid_id (int32) so
// queries can return a small int reference instead of repeating the full
// 2000-float wavelength array per row.
let wavelengthGrids: Map<number, Float64Array> | null = null;

export function getWavelengthGrid(gridId: number): Float64Array {
  if (!wavelengthGrids) throw new Error('Wavelength registry not initialized');
  const grid = wavelengthGrids.get(gridId);
  if (!grid) throw new Error(`Unknown wavelength grid_id: ${gridId}`);
  return grid;
}

export async function initDuckDB(): Promise<duckdb.AsyncDuckDBConnection> {
  if (conn) return conn;

  const base = import.meta.env.BASE_URL;

  // Use locally-hosted WASM files instead of jsDelivr CDN
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

  // Register each parquet source as a view. Each `CREATE VIEW` fetches the
  // parquet footer to resolve the schema, so running them in parallel shaves
  // off the serialized Range-request latency (~one RTT per source). The
  // wavelengths registry is also registered as a view so we can read it via
  // the same DuckDB connection.
  await Promise.all([
    ...SOURCES.map((source) => {
      const url = getParquetUrl(source);
      return conn!.query(`CREATE VIEW ${source} AS SELECT * FROM '${url}'`);
    }),
    conn!.query(
      `CREATE VIEW wavelengths AS SELECT * FROM '${getWavelengthsUrl()}'`,
    ),
  ]);

  // Materialise the wavelength grid registry once. The file is small
  // (a few KB to MB total) and every spectral fetch needs it to rehydrate
  // the wavelength axis from a grid_id reference.
  wavelengthGrids = await loadWavelengthGrids(conn);

  return conn;
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

export async function getConnection(): Promise<duckdb.AsyncDuckDBConnection> {
  if (!conn) throw new Error('DuckDB not initialized');
  return conn;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function query(sql: string): Promise<arrow.Table<any>> {
  const c = await getConnection();
  return c.query(sql);
}
