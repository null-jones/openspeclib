/**
 * DuckDB-WASM's Node build (the same WASM engine the browser uses), for
 * running the conformance engine in tests.
 */
import { createRequire } from 'node:module';
import * as duckdb from '@duckdb/duckdb-wasm/blocking';
import { tableToColumns, tableToRows } from '../../src/lib/conformance/arrow-rows';
import type { DataEngine } from '../../src/lib/data/engine';
import type { QueryFn } from '../../src/lib/conformance/types';

const require = createRequire(import.meta.url);

const BUNDLES: duckdb.DuckDBBundles = {
  mvp: {
    mainModule: require.resolve('@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm'),
    mainWorker: require.resolve('@duckdb/duckdb-wasm/dist/duckdb-node-mvp.worker.cjs'),
  },
  eh: {
    mainModule: require.resolve('@duckdb/duckdb-wasm/dist/duckdb-eh.wasm'),
    mainWorker: require.resolve('@duckdb/duckdb-wasm/dist/duckdb-node-eh.worker.cjs'),
  },
};

export interface NodeDuckDB {
  query: QueryFn;
  /** Make a file on disk readable under `name`. */
  register(name: string, path: string): void;
  /** Copy an in-memory DuckDB file (e.g. written by COPY) out as bytes. */
  readBuffer(name: string): Uint8Array;
  registerBuffer(name: string, bytes: Uint8Array): void;
  /** The data layer's engine interface. */
  engine: DataEngine;
  /** The raw connection, returning Arrow tables (as the browser API does). */
  raw(): duckdb.DuckDBConnection;
  close(): void;
}

export async function openDuckDB(): Promise<NodeDuckDB> {
  const db = await duckdb.createDuckDB(BUNDLES, new duckdb.VoidLogger(), duckdb.NODE_RUNTIME);
  await db.instantiate();
  const conn = db.connect();
  return {
    query: async (sql) => tableToRows(conn.query(sql)),
    register: (name, path) => db.registerFileURL(name, path, duckdb.DuckDBDataProtocol.NODE_FS, true),
    readBuffer: (name) => db.copyFileToBuffer(name),
    registerBuffer: (name, bytes) => db.registerFileBuffer(name, bytes),
    raw: () => conn,
    engine: {
      rows: async (sql) => tableToRows(conn.query(sql)),
      columns: async (sql) => tableToColumns(conn.query(sql)),
    },
    close: () => {
      conn.close();
      db.reset();
    },
  };
}
