/**
 * Getting the user's files into DuckDB-WASM, shared by the checker and
 * "Open a collection": dropped files are registered by handle and read in
 * place (nothing is uploaded or copied into memory); URLs are read with
 * HTTP range requests. An index dropped together with its collection files
 * finds them by name; an index opened by URL finds them relative to it.
 */
import * as duckdb from '@duckdb/duckdb-wasm';
import { resolveFileRef, type Companion } from './index-file';
import { sha256OfBlob, sha256OfUrl } from './sha256';
import { lit } from './sql';
import type { QueryFn } from './types';
import { INDEX_FOOTER_KEY } from '../../format/spec-keys';

export type CheckInput = { kind: 'files'; files: File[] } | { kind: 'url'; url: string };

/** Last path segment. Only `/` separates: a backslash is part of a name (as in the reference checker). */
export function baseName(path: string): string {
  return path.split('/').pop() ?? path;
}

let counter = 0;

/**
 * A unique DuckDB name for a dropped file. DuckDB treats `* ? [ ] { }` in
 * file names as glob patterns, so they are replaced.
 */
function registeredName(prefix: string, fileName: string): string {
  return `${prefix}-${++counter}-${fileName.replace(/[*?[\]{}\\]/g, '_')}`;
}

/** Check a URL can be fetched from this page: throws a readable error (CORS, HTTP status), or reports 404. */
export async function probeUrl(url: string): Promise<'ok' | 'missing'> {
  let res: Response;
  try {
    res = await fetch(url, { method: 'HEAD' });
  } catch {
    throw new Error(
      `Could not reach ${url}. The server may not allow cross-origin requests (CORS), or the address is wrong.`,
    );
  }
  if (res.status === 404 || res.status === 410) return 'missing';
  if (!res.ok) throw new Error(`${url} answered HTTP ${res.status}.`);
  return 'ok';
}

/** Find the collection files an index URL lists, relative to it. */
export function urlCompanions(indexUrl: string): (file: string) => Promise<Companion> {
  return async (file) => {
    let target: string;
    try {
      target = resolveFileRef(file, indexUrl);
    } catch {
      return { status: 'missing', reason: 'not a valid relative path or URL' };
    }
    try {
      if ((await probeUrl(target)) === 'missing') return { status: 'missing', reason: `${target} returned 404` };
    } catch (e) {
      return { status: 'unavailable', reason: (e as Error).message };
    }
    return { status: 'found', ref: target, sha256: () => sha256OfUrl(target) };
  };
}

export interface DroppedFiles {
  /** Registered DuckDB names, to drop when done. */
  registered: string[];
  /** Files to treat as top level: indexes and files no dropped index lists. */
  topLevel: Array<{ name: string; ref: string; isIndex: boolean }>;
  /** Find a companion among the dropped files by name. */
  resolve: (file: string) => Promise<Companion>;
  /** The registered name of a dropped file, by an index `file` entry. */
  refFor: (file: string) => string | undefined;
}

/** Register dropped files and work out which are indexes and which they list. */
export async function registerDropped(
  db: duckdb.AsyncDuckDB,
  query: QueryFn,
  files: File[],
  prefix: string,
): Promise<DroppedFiles> {
  const byName = new Map<string, { ref: string; file: File }>();
  const registered: string[] = [];
  for (const file of files) {
    const ref = registeredName(prefix, file.name);
    await db.registerFileHandle(ref, file, duckdb.DuckDBDataProtocol.BROWSER_FILEREADER, true);
    registered.push(ref);
    byName.set(file.name, { ref, file });
  }
  const indexes = new Set<string>();
  const listed = new Set<string>();
  for (const [name, { ref }] of byName) {
    try {
      const rows = await query(
        `SELECT decode(value) AS v FROM parquet_kv_metadata(${lit(ref)}) WHERE decode(key) = ${lit(INDEX_FOOTER_KEY)}`,
      );
      if (rows.length === 0) continue;
      indexes.add(name);
      const footer = JSON.parse(String(rows[0].v)) as { collections?: Array<{ file?: unknown }> };
      for (const c of footer.collections ?? []) if (typeof c.file === 'string') listed.add(baseName(c.file));
    } catch {
      // Not Parquet, or a malformed index footer: the check reports it.
    }
  }
  const topLevel = [...byName.entries()]
    .filter(([name]) => indexes.has(name) || !listed.has(name))
    .map(([name, { ref }]) => ({ name, ref, isIndex: indexes.has(name) }));
  return {
    registered,
    topLevel,
    resolve: async (file) => {
      const hit = byName.get(baseName(file));
      return hit
        ? { status: 'found', ref: hit.ref, sha256: () => sha256OfBlob(hit.file) }
        : { status: 'unavailable', reason: 'not chosen together with the index' };
    },
    refFor: (file) => byName.get(baseName(file))?.ref,
  };
}
