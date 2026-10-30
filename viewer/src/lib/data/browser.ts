/**
 * DuckDB-WASM plumbing for the explorer: load the OpenSpecLib release index
 * and give the rest of the app a query engine. Opening user collections is
 * in open.ts, loaded on demand because it brings the conformance checker.
 */
import { getDuckDB } from '../duckdb';
import { tableToColumns, tableToRows } from '../conformance/arrow-rows';
import { CHECKSUMS_FILE, INDEX_FILE, MIGRATION_FILE, dataFileUrl } from '../../constants/urls';
import type { DataEngine } from './engine';
import { loadSource, type LoadedSource } from './loader';
import { parseMigrationCsv } from './migration';
import { remoteFileResolver } from './remote';

let enginePromise: Promise<DataEngine> | null = null;

/** One long-lived connection for the explorer's queries. */
export function getDataEngine(): Promise<DataEngine> {
  if (!enginePromise) {
    enginePromise = (async () => {
      const db = await getDuckDB();
      const conn = await db.connect();
      return {
        rows: async (sql) => tableToRows(await conn.query(sql)),
        columns: async (sql) => tableToColumns(await conn.query(sql)),
        readable: remoteFileResolver((name, bytes) => db.registerFileBuffer(name, bytes)),
      } satisfies DataEngine;
    })().catch((e) => {
      enginePromise = null;
      throw e;
    });
  }
  return enginePromise;
}

const RELEASE_SOURCE_KEY = 'release';

/**
 * Where an index `file` entry lives: an http(s) URL is used as is; a
 * relative path resolves against the index URL, keeping its query string
 * (the release version, for cache busting). A backslash is part of a file
 * name, not a separator, so it is percent-encoded.
 */
function resolveIndexFile(file: string, indexUrl: string): string {
  if (/^https?:\/\//i.test(file)) return new URL(file).href;
  const index = new URL(indexUrl);
  const target = new URL(file.replace(/\\/g, '%5C'), index);
  target.search = index.search;
  return target.href;
}

let releasePromise: Promise<LoadedSource> | null = null;

/**
 * Load the release index. The file is small (under 1 MB), so it is fetched
 * in one request and registered as a buffer; collection files stay remote
 * and are read with range requests, one row group per spectrum, where the
 * host supports them (see remote.ts).
 */
export function loadRelease(): Promise<LoadedSource> {
  if (!releasePromise) {
    releasePromise = fetchRelease().catch((e) => {
      releasePromise = null;
      throw e;
    });
  }
  return releasePromise;
}

async function fetchRelease(): Promise<LoadedSource> {
  // Fetch the index while DuckDB-WASM starts, not after.
  const url = dataFileUrl(INDEX_FILE);
  const bytesPromise = fetch(url).then(async (res) => {
    if (!res.ok) throw new Error(`Could not load ${INDEX_FILE}: HTTP ${res.status}`);
    return new Uint8Array(await res.arrayBuffer());
  });
  const [engine, db, bytes] = await Promise.all([getDataEngine(), getDuckDB(), bytesPromise]);
  const ref = `release/${INDEX_FILE}`;
  await db.registerFileBuffer(ref, bytes);
  return loadSource(engine, {
    key: RELEASE_SOURCE_KEY,
    label: 'OpenSpecLib',
    kind: 'release',
    ref,
    resolveFile: (file) => resolveIndexFile(file, url),
  });
}

let migrationPromise: Promise<Map<string, string>> | null = null;

/** The release's old-id → new-id map, fetched only when an old link needs it. */
export function loadMigrationMap(): Promise<Map<string, string>> {
  if (!migrationPromise) {
    migrationPromise = fetch(dataFileUrl(MIGRATION_FILE))
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then(parseMigrationCsv)
      .catch((e) => {
        migrationPromise = null;
        throw e;
      });
  }
  return migrationPromise;
}

/** `checksums.txt` of the release (`<sha256>  <file>` per line), or null. */
export async function loadChecksums(): Promise<Map<string, string> | null> {
  try {
    const r = await fetch(dataFileUrl(CHECKSUMS_FILE));
    if (!r.ok) return null;
    const map = new Map<string, string>();
    for (const line of (await r.text()).split('\n')) {
      const m = line.trim().match(/^([a-f0-9]{64})\s+(.+)$/i);
      if (m) map.set(m[2], m[1]);
    }
    return map;
  } catch {
    return null;
  }
}
