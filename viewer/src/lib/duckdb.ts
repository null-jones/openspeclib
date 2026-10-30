import * as duckdb from '@duckdb/duckdb-wasm';

let db: duckdb.AsyncDuckDB | null = null;

/**
 * HTTP settings for DuckDB-WASM. Both flags matter: without
 * `forceFullHTTPReads: false` the 1.33 runtime fetches whole files.
 * tests/http-range.test.ts checks the bytes this transfers.
 */
export const DUCKDB_FILESYSTEM = { allowFullHTTPReads: true, forceFullHTTPReads: false } as const;
let connectionPromise: Promise<duckdb.AsyncDuckDB> | null = null;

/**
 * Start DuckDB-WASM (once). The explorer's data layer, the conformance
 * checker and "open collection" each open their own connection on it.
 */
export function initDuckDB(): Promise<duckdb.AsyncDuckDB> {
  if (!connectionPromise) {
    connectionPromise = instantiate().catch((e) => {
      connectionPromise = null;
      throw e;
    });
  }
  return connectionPromise;
}

async function instantiate(): Promise<duckdb.AsyncDuckDB> {
  const base = import.meta.env.BASE_URL;
  // Locally hosted WASM files instead of the jsDelivr CDN.
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
  const instance = new duckdb.AsyncDuckDB(logger, worker);
  await instance.instantiate(bundle.mainModule, bundle.pthreadWorker);
  // Read remote Parquet files with HTTP Range requests. DuckDB-WASM 1.33
  // otherwise downloads a whole file on first access (14.8 MB for one USGS
  // spectrum instead of ~1.5 MB). Full reads stay allowed as a fallback for
  // servers without Range support.
  await instance.open({ filesystem: DUCKDB_FILESYSTEM });
  // Cache Parquet footers across queries, so per-spectrum lookups do not
  // fetch the same footer again. Harmless if a build lacks the settings.
  const c = await instance.connect();
  try {
    await c.query('SET enable_http_metadata_cache=true');
    await c.query('SET enable_object_cache=true');
  } catch (e) {
    console.warn('DuckDB cache settings unavailable:', e);
  } finally {
    await c.close();
  }
  db = instance;
  return instance;
}

/** The DuckDB-WASM instance, once it is running. */
export async function getDuckDB(): Promise<duckdb.AsyncDuckDB> {
  await initDuckDB();
  if (!db) throw new Error('DuckDB not initialized');
  return db;
}
