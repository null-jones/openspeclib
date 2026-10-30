/**
 * Opening collections the user chooses (local files or URLs) for the
 * session: register them with DuckDB-WASM, refuse collection ids that are
 * already open (before any expensive work), check them against the format,
 * and load each conforming collection or index as a data source. Loaded on
 * demand: it brings the conformance checker (and ajv) with it.
 */
import { getDuckDB } from '../duckdb';
import { tableToColumns, tableToRows } from '../conformance/arrow-rows';
import { checkFile } from '../conformance/engine';
import { applyShareWarnings, ShareTally } from '../conformance/quality';
import { probeUrl, registerDropped, urlCompanions, type CheckInput } from '../conformance/files';
import { resolveFileRef, type Companion } from '../conformance/index-file';
import { lit } from '../conformance/sql';
import type { ConformanceReport, Progress, QueryFn } from '../conformance/types';
import { FOOTER_KEY, INDEX_FOOTER_KEY } from '../../format/spec-keys';
import type { DataEngine } from './engine';
import { friendlyError } from './errors';
import { loadSource, type LoadedSource } from './loader';

/**
 * Largest file the explorer opens. Search rows live in memory (about 1 kB
 * each with their table copy), so a bigger file would exhaust the tab.
 */
const MAX_OPEN_ROWS = 300_000;

export interface OpenOptions {
  /** Run the conformance check first (skip when the checker just did). */
  check: boolean;
  qualityProfile?: boolean;
  /** Collection ids already loaded; a file reusing one is refused. */
  loadedCollections: Set<string>;
  onProgress: (label: string, p: Progress) => void;
  signal?: AbortSignal;
}

export interface OpenOutcome {
  loaded: LoadedSource[];
  /** Files that were not opened, with the reason. */
  refused: Array<{ label: string; reason: string }>;
  /** Files that opened with warnings. */
  warnings: Array<{ label: string; rules: string[] }>;
  /** Full conformance reports, for the checker. */
  reports: ConformanceReport[];
}

let sourceCounter = 0;

/** One file to open, already readable by DuckDB as `ref`. */
export interface OpenEntry {
  label: string;
  ref: string;
  kind: 'local' | 'url';
  resolve: (f: string) => Promise<Companion>;
  fileRef: (f: string) => string;
  /** Registered files this entry needs (itself, and an index's collection files). */
  files: string[];
}

function failedRules(report: ConformanceReport): string {
  const own = report.results.filter((r) => r.status === 'fail').map((r) => `${r.id} ${r.title}`);
  for (const c of report.collections) if (c.verdict === 'not-conformant') own.push(`${c.label} does not conform`);
  return own.join(', ');
}

/** Check a URL is reachable and supports range requests, with readable errors. */
async function probeForOpen(url: string): Promise<void> {
  if ((await probeUrl(url)) === 'missing') throw new Error(`${url} was not found (HTTP 404).`);
  let res: Response;
  try {
    res = await fetch(url, { headers: { Range: 'bytes=0-3' } });
  } catch {
    throw new Error(
      `${url} answers, but not to range requests from this page. The server must allow the Range header in cross-origin requests (Access-Control-Allow-Headers: Range).`,
    );
  }
  void res.body?.cancel();
  if (res.status !== 206) {
    throw new Error(
      `${url} does not support HTTP range requests (it answered ${res.status} instead of 206), so the viewer cannot read single spectra from it.`,
    );
  }
}

/** Collection ids a file declares in its footer, and its row count (cheap: footer only). */
export async function peek(query: QueryFn, ref: string): Promise<{ ids: string[]; rows: number }> {
  const [meta] = await query(`SELECT num_rows::BIGINT AS n FROM parquet_file_metadata(${lit(ref)})`);
  const kv = await query(
    `SELECT decode(key) AS k, decode(value) AS v FROM parquet_kv_metadata(${lit(ref)})
     WHERE decode(key) IN (${lit(FOOTER_KEY)}, ${lit(INDEX_FOOTER_KEY)})`,
  );
  const ids: string[] = [];
  let rows = Number(meta?.n ?? 0);
  for (const r of kv) {
    try {
      const doc = JSON.parse(String(r.v)) as {
        collection?: { id?: unknown };
        collections?: Array<{ metadata?: { collection?: { id?: unknown } }; num_spectra?: unknown }>;
      };
      if (String(r.k) === FOOTER_KEY && typeof doc.collection?.id === 'string') ids.push(doc.collection.id);
      if (String(r.k) === INDEX_FOOTER_KEY) {
        for (const c of doc.collections ?? []) if (typeof c.metadata?.collection?.id === 'string') ids.push(c.metadata.collection.id);
      }
    } catch {
      // The check reports a broken footer.
    }
  }
  if (rows < 0) rows = 0;
  return { ids, rows };
}

/**
 * Open local files or a URL for this session. Files with only warnings
 * open; failing files are refused with the rules they fail. Registered
 * files and tables of refused files are released again.
 */
export async function openCollections(input: CheckInput, opts: OpenOptions): Promise<OpenOutcome> {
  const db = await getDuckDB();
  // A connection of our own, so cancelling aborts only this work.
  const conn = await db.connect();
  const onAbort = (): void => {
    void conn.cancelSent();
  };
  opts.signal?.addEventListener('abort', onAbort);
  const query: QueryFn = async (sql) => tableToRows(await conn.query(sql));
  const engine: DataEngine = { rows: query, columns: async (sql) => tableToColumns(await conn.query(sql)) };
  const outcome: OpenOutcome = { loaded: [], refused: [], warnings: [], reports: [] };
  let registered: string[] = [];
  const kept = new Set<string>();

  const entries: OpenEntry[] = [];
  try {
    if (input.kind === 'url') {
      const url = input.url.trim();
      await probeForOpen(url);
      entries.push({ label: url, ref: url, kind: 'url', resolve: urlCompanions(url), fileRef: (f) => resolveFileRef(f, url), files: [] });
    } else {
      const dropped = await registerDropped(db, query, input.files, 'open');
      registered = dropped.registered;
      for (const f of dropped.topLevel) {
        entries.push({
          label: f.name,
          ref: f.ref,
          kind: 'local',
          resolve: dropped.resolve,
          fileRef: (x) => dropped.refFor(x) ?? x,
          files: f.isIndex ? dropped.registered : [f.ref],
        });
      }
    }

    const done = await openEntries(entries, opts, query, engine);
    Object.assign(outcome, done);
    for (const l of done.loaded) for (const r of l.source.registered ?? []) kept.add(r);
    return outcome;
  } catch (err) {
    if (opts.signal?.aborted) throw new Error('Cancelled.');
    throw new Error(friendlyError(err, entries.map((e) => [e.ref, e.label] as [string, string])));
  } finally {
    opts.signal?.removeEventListener('abort', onAbort);
    await conn.close().catch(() => undefined);
    // Release files nothing was opened from.
    for (const r of registered) if (!kept.has(r)) await db.dropFile(r).catch(() => undefined);
  }
}

/**
 * The decisions behind opening: for each entry, refuse collection ids that
 * are already open and files that are too large (footer only, cheap), then
 * check the file (unless told not to), then load it. Exported for tests.
 */
export async function openEntries(
  entries: OpenEntry[],
  opts: OpenOptions,
  query: QueryFn,
  engine: DataEngine,
): Promise<OpenOutcome> {
  const outcome: OpenOutcome = { loaded: [], refused: [], warnings: [], reports: [] };
  // One bounds-share tally for everything opened together (quality profile).
  const share = new ShareTally();
  for (const e of entries) {
    const progress = (p: Progress) => opts.onProgress(e.label, p);
    progress({ phase: 'Reading the footer', fraction: null });
    // Cheap checks first: collection ids already open, and size.
    let peeked: { ids: string[]; rows: number };
    try {
      peeked = await peek(query, e.ref);
    } catch (err) {
      outcome.refused.push({ label: e.label, reason: `is not a Parquet file DuckDB can read (${friendlyError(err, [[e.ref, e.label]])})` });
      continue;
    }
    const clash = peeked.ids.find((id) => opts.loadedCollections.has(id));
    if (clash) {
      outcome.refused.push({ label: e.label, reason: `holds collection '${clash}', which is already open` });
      continue;
    }
    if (peeked.rows > MAX_OPEN_ROWS) {
      outcome.refused.push({
        label: e.label,
        reason: `has ${peeked.rows.toLocaleString('en-US')} spectra; the explorer opens at most ${MAX_OPEN_ROWS.toLocaleString('en-US')} at a time. Split the collection, or check it with the checker instead`,
      });
      continue;
    }
    if (opts.check) {
      const report = await checkFile(query, e.ref, e.label, {
        qualityProfile: opts.qualityProfile,
        share,
        signal: opts.signal,
        onProgress: progress,
        resolveCompanion: e.resolve,
      });
      outcome.reports.push(report);
      if (report.unreadable) {
        outcome.refused.push({ label: e.label, reason: 'is not a Parquet file DuckDB can read' });
        continue;
      }
      if (report.verdict === 'not-conformant') {
        outcome.refused.push({ label: e.label, reason: `does not conform to the format (fails ${failedRules(report)})` });
        continue;
      }
      if (report.verdict === 'incomplete') {
        outcome.refused.push({ label: e.label, reason: 'could not be fully checked (are all the collection files of the index included?)' });
        continue;
      }
      const warned = [report, ...report.collections].flatMap((r) => r.results.filter((x) => x.status === 'warn').map((x) => x.id));
      if (warned.length) outcome.warnings.push({ label: e.label, rules: [...new Set(warned)] });
    }
    progress({ phase: 'Loading rows', fraction: null });
    const loaded = await loadSource(engine, {
      key: `open-${++sourceCounter}`,
      label: e.label,
      kind: e.kind,
      ref: e.ref,
      resolveFile: e.fileRef,
    });
    loaded.source.registered = e.files;
    for (const c of loaded.collections) opts.loadedCollections.add(c.id);
    outcome.loaded.push(loaded);
  }
  applyShareWarnings(outcome.reports, share);
  return outcome;
}

/** Release what a removed source holds in DuckDB: its table and registered files. */
export async function disposeSource(source: { table: string; registered?: string[] }): Promise<void> {
  const db = await getDuckDB();
  const conn = await db.connect();
  try {
    await conn.query(`DROP TABLE IF EXISTS ${source.table}`);
  } finally {
    await conn.close().catch(() => undefined);
  }
  for (const r of source.registered ?? []) await db.dropFile(r).catch(() => undefined);
}
