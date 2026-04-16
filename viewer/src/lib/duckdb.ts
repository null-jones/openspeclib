import * as duckdb from '@duckdb/duckdb-wasm';
import * as arrow from 'apache-arrow';
import { getParquetUrl } from '../constants/urls';

let db: duckdb.AsyncDuckDB | null = null;
let conn: duckdb.AsyncDuckDBConnection | null = null;

const SOURCES = ['usgs_splib07'];

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

  // Register each parquet source as a view
  for (const source of SOURCES) {
    const url = getParquetUrl(source);
    await conn.query(`CREATE VIEW ${source} AS SELECT * FROM '${url}'`);
  }

  return conn;
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
