/**
 * Reading a spectrum over HTTP must use Range requests: about one row group
 * (~1.5 MB for a USGS spectrum), not the whole 14.8 MB file. Runs DuckDB-WASM
 * with its *browser* runtime (the code that issues the HTTP requests in the
 * app) against a Range-capable server in a child process, and counts bytes.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import * as duckdb from '@duckdb/duckdb-wasm/blocking';
import { tableToColumns, tableToRows } from '../src/lib/conformance/arrow-rows';
import { DUCKDB_FILESYSTEM } from '../src/lib/duckdb';
import { fetchSpectra } from '../src/lib/data/spectra';
import type { DataEngine } from '../src/lib/data/engine';
import type { DataSource, SpectrumRecord } from '../src/lib/data/types';
import { SyncXHR } from './helpers/sync-xhr';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const DATA = join(here, '../public/data');
const FILE = 'usgs_splib07.parquet';
const available = existsSync(join(DATA, FILE));

interface LogEntry {
  method: string;
  range: string | null;
  status: number;
  bytes: number;
}

let server: ChildProcess;
let base = '';

async function serverLog(): Promise<LogEntry[]> {
  return (await (await fetch(`${base}/__log`)).json()) as LogEntry[];
}

/** DuckDB-WASM (Node build) wired to the browser runtime's file system code. */
async function browserRuntimeDuckDB(config?: duckdb.DuckDBConfig) {
  (globalThis as { XMLHttpRequest?: unknown }).XMLHttpRequest = SyncXHR;
  // The browser bundle's bundled sha256 would otherwise pick its Node path.
  (globalThis as { window?: unknown }).window = { JS_SHA256_NO_NODE_JS: true };
  const dist = dirname(require.resolve('@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm'));
  const browser = require(join(dist, 'duckdb-browser-blocking.cjs')) as { BROWSER_RUNTIME: duckdb.DuckDBRuntime };
  delete (globalThis as { window?: unknown }).window;
  const bundles: duckdb.DuckDBBundles = {
    mvp: { mainModule: require.resolve('@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm'), mainWorker: '' },
    eh: { mainModule: require.resolve('@duckdb/duckdb-wasm/dist/duckdb-eh.wasm'), mainWorker: '' },
  };
  const db = await duckdb.createDuckDB(bundles, new duckdb.VoidLogger(), browser.BROWSER_RUNTIME);
  await db.instantiate();
  if (config) db.open(config);
  const conn = db.connect();
  const engine: DataEngine = {
    rows: async (sql) => tableToRows(conn.query(sql)),
    columns: async (sql) => tableToColumns(conn.query(sql)),
  };
  return { engine, close: () => db.reset() };
}

const record: SpectrumRecord = {
  id: 'usgs_splib07:splib07a_Olivine_GDS70.a_Fo89_165um_BECKb_AREF',
  sourceKey: 'http',
  collection: 'usgs_splib07',
  name: 'Olivine',
  material_name: 'Olivine',
  material_category: 'mineral',
  material_formula: null,
  sample_description: null,
  quantity: 'reflectance',
  geometry: 'unknown',
  wavelength_unit: 'um',
  wavelength_min_nm: 0,
  wavelength_max_nm: 0,
  num_points: null,
  license: 'LicenseRef-USGS-PublicDomain',
  dataset_id: null,
  simulated: false,
  file: FILE,
  search: '',
};

describe.skipIf(!available)('HTTP Range reads', () => {
  beforeAll(async () => {
    server = spawn(process.execPath, [join(here, 'helpers/range-server.cjs'), DATA]);
    const port = await new Promise<number>((resolve) =>
      server.stdout!.on('data', (d) => {
        const m = /port (\d+)/.exec(String(d));
        if (m) resolve(Number(m[1]));
      }),
    );
    base = `http://127.0.0.1:${port}`;
  });
  afterAll(() => {
    server?.kill();
  });

  test('without the settings, DuckDB-WASM downloads the whole file (why they exist)', async () => {
    const before = (await serverLog()).length;
    const { engine, close } = await browserRuntimeDuckDB();
    const source = { key: 'http', fileRefs: { [FILE]: `${base}/${FILE}?v=default` } } as unknown as DataSource;
    await fetchSpectra(engine, [record], new Map([['http', source]]));
    close();
    const log = (await serverLog()).slice(before).filter((l) => l.method === 'GET');
    expect(log.reduce((s, l) => s + l.bytes, 0)).toBe(statSync(join(DATA, FILE)).size);
  }, 120_000);

  test('one spectrum transfers about one row group, all by Range', async () => {
    const before = (await serverLog()).length;
    const { engine, close } = await browserRuntimeDuckDB({ filesystem: DUCKDB_FILESYSTEM });
    const source = { key: 'http', fileRefs: { [FILE]: `${base}/${FILE}?v=test` } } as unknown as DataSource;
    const [spectrum] = await fetchSpectra(engine, [record], new Map([['http', source]]));
    close();
    expect(spectrum.values.length).toBeGreaterThan(100);
    const log = (await serverLog()).slice(before).filter((l) => l.method === 'GET');
    const total = log.reduce((s, l) => s + l.bytes, 0);
    const size = statSync(join(DATA, FILE)).size;
    expect(log.every((l) => l.range !== null && l.status === 206)).toBe(true);
    expect(total).toBeLessThan(Math.min(3_000_000, size / 4));
  }, 120_000);
});
