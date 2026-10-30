/**
 * DuckDB-WASM plumbing for the in-browser conformance checker. Files are
 * read where they are (see files.ts); nothing is uploaded.
 */
import { getDuckDB } from '../duckdb';
import { tableToRows } from './arrow-rows';
import { checkFile } from './engine';
import { applyShareWarnings, ShareTally } from './quality';
import { probeUrl, registerDropped, urlCompanions, type CheckInput } from './files';
import type { CompanionResolver } from './index-file';
import type { ConformanceReport, Progress, QueryFn } from './types';
import { CheckAbortedError } from './types';

export type { CheckInput } from './files';

export interface BrowserCheckOptions {
  qualityProfile: boolean;
  onProgress: (label: string, p: Progress) => void;
  signal: AbortSignal;
}

/**
 * Check dropped files or a URL. An index file dropped together with its
 * collection files is checked against them (they are reported inside the
 * index report); every other file gets its own report.
 */
export async function runBrowserCheck(input: CheckInput, options: BrowserCheckOptions): Promise<ConformanceReport[]> {
  const db = await getDuckDB();
  const conn = await db.connect();
  let registered: string[] = [];
  const onAbort = (): void => {
    void conn.cancelSent();
  };
  options.signal.addEventListener('abort', onAbort);
  const query: QueryFn = async (sql) => tableToRows(await conn.query(sql));
  // One bounds-share tally for the whole run, as `openspeclib validate` keeps.
  const share = new ShareTally();
  const run = (ref: string, label: string, resolveCompanion?: CompanionResolver) =>
    checkFile(query, ref, label, {
      qualityProfile: options.qualityProfile,
      share,
      signal: options.signal,
      onProgress: (p) => options.onProgress(label, p),
      resolveCompanion,
    });

  try {
    if (input.kind === 'url') {
      const url = input.url.trim();
      if ((await probeUrl(url)) === 'missing') throw new Error(`${url} was not found (HTTP 404).`);
      const reports = [await run(url, url, urlCompanions(url))];
      applyShareWarnings(reports, share);
      return reports;
    }
    const dropped = await registerDropped(db, query, input.files, 'check');
    registered = dropped.registered;
    const reports: ConformanceReport[] = [];
    for (const f of dropped.topLevel) reports.push(await run(f.ref, f.name, f.isIndex ? dropped.resolve : undefined));
    applyShareWarnings(reports, share);
    return reports;
  } catch (e) {
    if (options.signal.aborted) throw new CheckAbortedError('Check cancelled');
    throw e;
  } finally {
    options.signal.removeEventListener('abort', onAbort);
    await conn.close().catch(() => undefined);
    for (const ref of registered) await db.dropFile(ref).catch(() => undefined);
  }
}
