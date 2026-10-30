/**
 * The conformance check: runs SQL through a `QueryFn` (DuckDB-WASM in the
 * browser, DuckDB-WASM's Node build in tests) and turns the answers into a
 * report with one result per rule in rules.json.
 *
 * The engine never pulls spectral arrays into JavaScript: row rules are
 * aggregated inside DuckDB, batch by batch, aligned to row groups.
 */
import { ARRAY_COLUMNS, COLUMN_BY_NAME, FOOTER_KEY, INDEX_FOOTER_KEY, RULES, VOCABULARY_COLUMNS } from '../../format/spec';
import { columnsFromParquetSchema, decodeArrowSchema, type ParquetSchemaRow } from './arrow-schema';
import { checkCollectionColumns } from './columns';
import { checkFooter, decodeUtf8, footerContext, IMPLEMENTED_VERSION } from './footer';
import { checkLicense, isBlank } from './license';
import { own } from '../own';
import { applyShareWarnings, EXTENT_COLUMNS, QUALITY_COLUMNS, QualityTally, qualityCandidatesSql, ShareTally } from './quality';
import { addBatch, batchSql, emptyTotals, planRowChecks, recordRowFindings, scan } from './row-checks';
import { ident, lit, num } from './sql';
import { MAX_EXAMPLES, plural, RuleAccumulator, verdictOf } from './results';
import { CheckAbortedError, checkerFailure, QueryError, type ConformanceReport, type FileColumn, type FileKind, type Progress, type QueryFn } from './types';
import { checkIndexFile, type CompanionResolver } from './index-file';

export interface CheckOptions {
  /** Also check the OpenSpecLib quality profile (Q1–Q4). */
  qualityProfile?: boolean;
  onProgress?: (p: Progress) => void;
  signal?: AbortSignal;
  /** For index files: find the collection files it lists. */
  resolveCompanion?: CompanionResolver;
  /** Check the file as an index (I1) whatever its footer keys. */
  asIndex?: boolean;
  /**
   * The run's bounds-share tally, when several files are checked together:
   * the caller adds the warnings at the end (applyShareWarnings). Without
   * one, this file (with an index's collection files) is the whole run.
   */
  share?: ShareTally;
}

/** R2: row groups should hold at most this many spectra. */
const R2_MAX_ROWS_PER_GROUP = 250;

/** Spectra per row-scan batch: small enough for steady progress, large enough to be cheap. */
export function batchTarget(totalRows: number): number {
  return Math.min(10_000, Math.max(500, Math.ceil(totalRows / 40)));
}

/** Split row groups into `[start, end)` row ranges of about `target` rows. */
export function batchRanges(rowGroupSizes: number[], target: number): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  let start = 0;
  let end = 0;
  for (const n of rowGroupSizes) {
    end += n;
    if (end - start >= target) {
      out.push([start, end]);
      start = end;
    }
  }
  if (end > start) out.push([start, end]);
  return out;
}

function bytes(v: unknown): Uint8Array | null {
  if (v instanceof Uint8Array) return v;
  if (typeof v === 'string') return new TextEncoder().encode(v);
  return null;
}

/** Read the footer key-value metadata into a map of raw values. */
async function readKeyValues(query: QueryFn, ref: string): Promise<Map<string, Uint8Array>> {
  const rows = await query(`SELECT key, value FROM parquet_kv_metadata(${lit(ref)})`);
  const out = new Map<string, Uint8Array>();
  for (const r of rows) {
    const key = bytes(r.key);
    const value = bytes(r.value);
    if (key && value) out.set(new TextDecoder().decode(key), value);
  }
  return out;
}

/**
 * Top-level columns with types from the Parquet schema (spec section 4).
 * The `ARROW:schema` hint is decoded for display only; a broken or spoofed
 * hint is ignored.
 */
async function readColumns(query: QueryFn, ref: string, kv: Map<string, Uint8Array>): Promise<FileColumn[]> {
  const schemaRows = (await query(
    `SELECT name, type, repetition_type, num_children::INTEGER AS num_children, converted_type, logical_type FROM parquet_schema(${lit(ref)})`,
  )) as unknown as ParquetSchemaRow[];
  const columns = columnsFromParquetSchema(
    schemaRows.map((r) => ({ ...r, num_children: r.num_children === null ? null : num(r.num_children) })),
  );
  const arrowRaw = kv.get('ARROW:schema');
  if (arrowRaw) {
    try {
      const hints = decodeArrowSchema(new TextDecoder().decode(arrowRaw));
      if (hints.length === columns.length && hints.every((h, i) => h.name === columns[i].name)) {
        columns.forEach((c, i) => {
          if (hints[i].type !== c.type) c.arrowType = hints[i].type;
        });
      }
    } catch {
      // Display hint only: ignore a malformed one.
    }
  }
  return columns;
}

interface Chunk {
  rowGroup: number;
  rowGroupRows: number;
  column: string;
  encodings: string;
  compression: string;
  hasStats: boolean;
}

async function readLayout(query: QueryFn, ref: string): Promise<{ rowGroups: number[]; chunks: Chunk[] }> {
  const rows = await query(
    `SELECT row_group_id::BIGINT AS rg, row_group_num_rows::BIGINT AS rg_rows,
            split_part(path_in_schema, ', ', 1) AS col, path_in_schema AS path, encodings, compression,
            (stats_min_value IS NOT NULL OR stats_min IS NOT NULL) AND
            (stats_max_value IS NOT NULL OR stats_max IS NOT NULL) AS has_stats
     FROM parquet_metadata(${lit(ref)})
     ORDER BY row_group_id, column_id`,
  );
  const sizes = new Map<number, number>();
  const chunks: Chunk[] = [];
  for (const r of rows) {
    const rg = num(r.rg);
    sizes.set(rg, num(r.rg_rows));
    chunks.push({
      rowGroup: rg,
      rowGroupRows: num(r.rg_rows),
      // R1 statistics are located by column path, never by position.
      column: String(r.path) === 'id' ? 'id' : String(r.col) === 'id' ? 'id.nested' : String(r.col),
      encodings: String(r.encodings ?? ''),
      compression: String(r.compression ?? ''),
      hasStats: r.has_stats === true,
    });
  }
  const rowGroups = [...sizes.entries()].sort((a, b) => a[0] - b[0]).map(([, n]) => n);
  return { rowGroups, chunks };
}

/** R1 (statistics part), R2 and R3 from the Parquet layout. */
export function checkLayout(rowGroups: number[], chunks: Chunk[], acc: RuleAccumulator, idUsable = true): void {
  const totalRows = rowGroups.reduce((s, n) => s + n, 0);
  if (idUsable && totalRows > 0) {
    // Every row group, empty ones included, as the reference checker.
    const noStats = chunks.filter((c) => c.column === 'id' && !c.hasStats);
    acc.issue(
      'R1',
      noStats.length,
      `${plural(noStats.length, 'row group')} without min/max statistics for id`,
      noStats.slice(0, MAX_EXAMPLES).map((c) => `row group ${c.rowGroup}`),
      'row groups',
    );
  }

  const big = rowGroups.map((n, i) => [i, n]).filter(([, n]) => n > R2_MAX_ROWS_PER_GROUP);
  acc.issue(
    'R2',
    big.length,
    `${plural(big.length, 'row group')} with more than ${R2_MAX_ROWS_PER_GROUP} spectra (largest: ${Math.max(0, ...rowGroups).toLocaleString('en-US')})`,
    big.slice(0, MAX_EXAMPLES).map(([i, n]) => `row group ${i}: ${n} rows`),
    'row groups',
  );

  // R3: every per-band array's leaf column, in row groups that hold rows.
  for (const col of ARRAY_COLUMNS) {
    const bad = chunks
      .filter((c) => c.column === col && c.rowGroupRows > 0)
      .map((c) => {
        const problems: string[] = [];
        if (!c.encodings.includes('BYTE_STREAM_SPLIT')) problems.push('not BYTE_STREAM_SPLIT');
        if (/DICTIONARY/.test(c.encodings)) problems.push('dictionary encoded');
        if (c.compression.toUpperCase() !== 'ZSTD') problems.push(`compression ${c.compression}`);
        return { c, problems };
      })
      .filter((x) => x.problems.length > 0);
    if (bad.length === 0) continue;
    const kinds = [...new Set(bad.flatMap((b) => b.problems))].join(', ');
    acc.issue(
      'R3',
      bad.length,
      `${col}: ${plural(bad.length, 'column chunk')} ${kinds} (found encodings ${[...new Set(bad.map((b) => b.c.encodings))].join(' / ')})`,
      [],
      'column chunks',
    );
  }
}

/** Bad licence values reported one by one; any more are summed. */
const MAX_LICENCE_MESSAGES = 50;

/**
 * Every distinct value of a string column with its row count. All of them:
 * F11 and F12 must see every value (as the reference checker does), and
 * there are at most as many as rows.
 */
async function distinctCounts(query: QueryFn, ref: string, column: string, where = 'true'): Promise<Array<{ value: string; n: number }>> {
  const rows = await query(
    `SELECT ${ident(column)} AS v, count(*)::BIGINT AS n FROM ${scan(ref)} WHERE ${ident(column)} IS NOT NULL AND (${where}) GROUP BY 1 ORDER BY n DESC, v`,
  );
  return rows.map((r) => ({ value: String(r.v), n: num(r.n) }));
}

/**
 * Why an `extra` value is not a JSON object, or null if it is one (F4).
 * JSON.parse already rejects NaN and Infinity, as the spec requires.
 */
export function extraProblem(text: string): string | null {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch {
    return 'extra is not valid JSON';
  }
  if (typeof v === 'object' && v !== null && !Array.isArray(v)) return null;
  const kind = v === null ? 'null' : Array.isArray(v) ? 'an array' : typeof v === 'string' ? 'a string' : typeof v === 'number' ? 'a number' : 'a boolean';
  return `extra is ${kind}, not a JSON object`;
}

/** Row-level rules skipped when the row data cannot be decoded (F1). */
const ROW_RULES = ['F2', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'F13', 'R1', 'R4', 'R5', 'Q1', 'Q2', 'Q3', 'Q4'];

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw new CheckAbortedError('Check cancelled');
}

/**
 * The rule a column's values must satisfy beyond F2: a value that is not
 * valid UTF-8 can never satisfy it (as the reference checker finds after
 * replacing the bad bytes).
 */
function utf8DependentRule(column: string): string | null {
  if (column === 'id') return 'F5';
  if (column === 'collection') return 'F6';
  if (column === 'license') return 'F11';
  if (column === 'dataset_id') return 'F12';
  if ((VOCABULARY_COLUMNS as readonly string[]).includes(column)) return 'F7';
  return null;
}

/** Further rules evaluated on a column's decodable rows (R5 counts geometry 'unknown'; a non-UTF-8 value is not). */
const UTF8_ALSO: Record<string, string[]> = { geometry: ['R5'] };

/** Whether a DuckDB error is about text that is not valid UTF-8. */
export function isInvalidUtf8Error(e: unknown): boolean {
  return /invalid string encoding|not valid utf-?8/i.test(e instanceof Error ? e.message : String(e));
}

/**
 * The text columns (strings and lists of strings) holding values that are
 * not valid UTF-8, found by decoding each one on its own. Only run after a
 * scan has failed with a UTF-8 error, so the extra reads are rare.
 */
async function invalidUtf8Columns(q: QueryFn, ref: string, columns: FileColumn[], known: Set<string>): Promise<string[]> {
  const candidates = [...new Set(columns.filter((c) => /string/.test(c.type)).map((c) => c.name))].filter((n) => !known.has(n));
  const bad: string[] = [];
  for (const name of candidates) {
    try {
      await q(`SELECT count(*) FILTER (WHERE ${ident(name)} IS NOT NULL)::BIGINT AS n FROM ${scan(ref)}`);
    } catch (e) {
      if (isInvalidUtf8Error(e)) bad.push(name);
    }
  }
  return bad;
}

/** First line of a DuckDB error, with the internal file name replaced. */
export function cleanError(e: unknown, ref: string, label: string): string {
  const msg = (e instanceof Error ? e.message : String(e)).split('\n')[0];
  // Control characters from undecodable bytes are shown escaped (\x0F).
  const visible = Array.from(msg, (ch) => {
    const code = ch.codePointAt(0) ?? 0;
    return code < 0x20 || code === 0x7f ? `\\x${code.toString(16).toUpperCase().padStart(2, '0')}` : ch;
  }).join('');
  return visible.split(ref).join(label);
}

/**
 * Report for a file DuckDB cannot read as Parquet: F1 fails and every other
 * rule is skipped (as in the reference checker).
 */
function unreadableReport(label: string, error: string, qualityProfile = false): ConformanceReport {
  const acc = new RuleAccumulator();
  acc.issue('F1', 1, `Not a readable Parquet file: ${error}`, [], 'problems');
  for (const rule of RULES) {
    if (rule.id !== 'F1') acc.skip(rule.id, 'na', 'Not checked: the file is not readable as Parquet.');
  }
  const results = acc.results();
  return {
    label,
    kind: 'unknown',
    unreadable: error,
    verdict: verdictOf(results),
    numRows: 0,
    numRowGroups: 0,
    createdBy: null,
    columns: [],
    footerText: null,
    results,
    collections: [],
    qualityProfile,
    durationMs: 0,
  };
}

/**
 * Check one Parquet file: a collection file against F1–F13, R1–R5 (and
 * Q1–Q4 with the quality profile), or an index file against I1 and, through
 * `resolveCompanion`, every collection file it lists.
 *
 * @param query  runs SQL against a DuckDB that can read `ref`
 * @param ref    the file as DuckDB sees it (registered name, path or URL)
 * @param label  display name
 * @throws CheckAbortedError if `signal` aborts
 */
export async function checkFile(
  query: QueryFn,
  ref: string,
  label: string,
  options: CheckOptions = {},
): Promise<ConformanceReport> {
  const { qualityProfile = false, onProgress, signal } = options;
  const started = Date.now();
  const progress = (phase: string, fraction: number | null, extra: Partial<Progress> = {}): void => {
    throwIfAborted(signal);
    onProgress?.({ phase, fraction, ...extra });
  };
  // Errors DuckDB raises are findings about the file (F1); tag them so any
  // other exception surfaces as a checker failure instead of a verdict.
  const q: QueryFn = async (sql) => {
    throwIfAborted(signal);
    try {
      return await query(sql);
    } catch (e) {
      if (signal?.aborted) throw new CheckAbortedError('Check cancelled');
      throw e instanceof QueryError ? e : new QueryError(e);
    }
  };

  progress('Opening file', 0);
  let numRows: number;
  let numRowGroups: number;
  let createdBy: string | null;
  let kv: Map<string, Uint8Array>;
  let columns: FileColumn[];
  try {
    const [meta] = await q(
      `SELECT num_rows::BIGINT AS num_rows, num_row_groups::BIGINT AS num_row_groups, created_by FROM parquet_file_metadata(${lit(ref)})`,
    );
    numRows = num(meta?.num_rows);
    numRowGroups = num(meta?.num_row_groups);
    createdBy = meta?.created_by == null ? null : String(meta.created_by);
    progress('Reading footer', 0.02);
    kv = await readKeyValues(q, ref);
    columns = await readColumns(q, ref, kv);
  } catch (e) {
    if (e instanceof CheckAbortedError || signal?.aborted) throw new CheckAbortedError('Check cancelled');
    if (!(e instanceof QueryError)) throw checkerFailure(e);
    return unreadableReport(label, cleanError(e, ref, label), qualityProfile);
  }

  // A file with only the index key is an index file; anything else
  // (including files with neither key, or both) is checked as a collection.
  const kind: FileKind = options.asIndex
    ? 'index'
    : kv.has(FOOTER_KEY)
      ? 'collection'
      : kv.has(INDEX_FOOTER_KEY)
        ? 'index'
        : 'unknown';
  const footerRaw = kv.get(kind === 'index' ? INDEX_FOOTER_KEY : FOOTER_KEY) ?? null;
  const base = {
    label,
    kind,
    unreadable: null,
    numRows,
    numRowGroups,
    createdBy,
    columns,
    footerText: footerRaw ? decodeUtf8(footerRaw) : null,
    qualityProfile,
  };

  if (kind === 'index') {
    const acc = new RuleAccumulator();
    const share = options.share ?? new ShareTally();
    const collections = await checkIndexFile({
      query: q,
      ref,
      columns,
      footerRaw,
      alsoCollectionKey: kv.has(FOOTER_KEY),
      acc,
      options: { ...options, share },
      progress,
    });
    if (!options.share) applyShareWarnings(collections, share);
    const results = acc.results();
    let verdict = verdictOf(results);
    if (verdict === 'conformant' && collections.some((c) => c.verdict === 'not-conformant')) verdict = 'not-conformant';
    else if (verdict === 'conformant' && collections.some((c) => c.verdict === 'incomplete')) verdict = 'incomplete';
    progress('Done', 1);
    return { ...base, results, collections, verdict, durationMs: Date.now() - started };
  }

  const acc = new RuleAccumulator();
  acc.skip('I1', 'na', 'Not applicable: this is a collection file, not an index file.');

  // F1: footer.
  const footer = checkFooter(footerRaw, 'collection', FOOTER_KEY);
  const footerProblems = [...footer.problems];
  if (kv.has(FOOTER_KEY) && kv.has(INDEX_FOOTER_KEY)) {
    footerProblems.push(`the file also carries the '${INDEX_FOOTER_KEY}' footer key`);
  }
  for (const p of footerProblems) acc.issue('F1', 1, p, [], 'problems');
  for (const w of footer.warnings) {
    acc.warn('F1', 1, `${w} (a warning: the file follows format version ${String((footer.value as { format_version?: unknown }).format_version)}, newer than this checker's ${IMPLEMENTED_VERSION})`, [], 'findings');
  }
  const ctx = footerContext(footer.value);
  if (!ctx.readable) {
    for (const r of ['F5', 'F6', 'F11', 'F12']) {
      acc.skip(r, 'blocked', 'Not checked: the footer metadata is unavailable (see F1).');
    }
  }

  // F2–F4: columns.
  progress('Checking columns', 0.04);
  const { usable, allNull, present } = checkCollectionColumns(columns, ctx.readable ? ctx.extensions : null, acc, footer.newerMinor);

  let rowGroups: number[] = [];
  /** Text columns holding values that are not valid UTF-8 (found when a scan fails on one). */
  const badUtf8 = new Set<string>();
  try {
    // Layout: R1 statistics, R2, R3.
    const layout = await readLayout(q, ref);
    rowGroups = layout.rowGroups;
    checkLayout(layout.rowGroups, layout.chunks, acc, usable.has('id'));
    // DuckDB stops at the first text value that is not UTF-8. Find the
    // columns holding such values, report them, and check the rows again
    // without them.
    for (;;) {
      try {
        await checkRows(badUtf8);
        break;
      } catch (e) {
        if (signal?.aborted || !isInvalidUtf8Error(e)) throw e;
        const found = await invalidUtf8Columns(q, ref, columns, badUtf8);
        if (found.length === 0) throw e;
        for (const name of found) {
          badUtf8.add(name);
          usable.delete(name);
        }
      }
    }
    for (const name of [...badUtf8].sort()) {
      const spec = COLUMN_BY_NAME.get(name);
      const rule = !spec || spec.extension === 'index' ? 'F3' : spec.extension === null ? 'F2' : 'F4';
      acc.issue(rule, 1, `Column ${name} holds text that is not valid UTF-8`, [name], 'columns');
    }
  } catch (e) {
    if (e instanceof CheckAbortedError || signal?.aborted) throw new CheckAbortedError('Check cancelled');
    if (!(e instanceof QueryError)) throw checkerFailure(e);
    // Undecodable data pages make the file invalid Parquet (F1); the rules
    // that need the row data are not checked.
    acc.issue('F1', 1, `Not a valid Parquet file: the row data cannot be read (${cleanError(e, ref, label)})`, [], 'problems');
    for (const r of ROW_RULES) if (!r.startsWith('Q')) acc.skip(r, 'blocked', 'Not checked: the row data cannot be read (see F1).');
    // The quality profile reads only its own columns (as `openspeclib
    // validate`), so it may still run.
    try {
      const quality = await startQuality(badUtf8);
      if (quality) {
        for (const [start, end] of batchRanges(rowGroups, batchTarget(numRows))) {
          for (const r of await q(qualityCandidatesSql(scan(ref), start, end, quality.withExtent, quality.nulled))) quality.tally.addCandidate(r);
        }
        quality.tally.record(acc);
      }
    } catch (qe) {
      if (qe instanceof CheckAbortedError || signal?.aborted) throw new CheckAbortedError('Check cancelled');
      if (!(qe instanceof QueryError)) throw checkerFailure(qe);
      for (const r of ['Q1', 'Q2', 'Q3', 'Q4']) acc.skip(r, 'blocked', 'Quality profile not evaluated: its columns cannot be read (see F1).');
    }
  }

  progress('Done', 1, { rowsChecked: numRows, rowsTotal: numRows });
  const results = acc.results();
  return { ...base, collectionId: ctx.collectionId ?? label, results, collections: [], verdict: verdictOf(results), durationMs: Date.now() - started };

  /**
   * Columns holding text that is not valid UTF-8: DuckDB cannot read such a
   * column at all where a bad value sits, so it is tried range by range. A
   * range that cannot be decoded fails the rule the column's values must
   * satisfy (F5, F6, F7 or F11; a non-UTF-8 value never can); the ranges
   * that decode are checked as usual. Returns, per column, an SQL condition
   * selecting its decodable rows (used for F11).
   */
  async function checkUndecodable(badUtf8: Set<string>, warnOnly: ReadonlySet<string>): Promise<Map<string, string>> {
    const decodable = new Map<string, string>();
    const ranges = numRows > 0 ? batchRanges(rowGroups, batchTarget(numRows)) : [];
    for (const col of [...badUtf8].sort()) {
      const rule = utf8DependentRule(col);
      if (rule === null) continue;
      const good: Array<[number, number]> = [];
      const bad: Array<[number, number]> = [];
      for (const [start, end] of ranges) {
        try {
          await q(`SELECT count(*) FILTER (WHERE ${ident(col)} IS NOT NULL)::BIGINT AS n FROM ${scan(ref)} WHERE file_row_number >= ${start} AND file_row_number < ${end}`);
          good.push([start, end]);
        } catch (e) {
          if (!isInvalidUtf8Error(e)) throw e;
          bad.push([start, end]);
        }
      }
      const where = (list: Array<[number, number]>): string =>
        list.length ? list.map(([a, b]) => `(file_row_number >= ${a} AND file_row_number < ${b})`).join(' OR ') : 'false';
      const report = warnOnly.has(rule) ? acc.warn.bind(acc) : acc.issue.bind(acc);
      report(
        rule,
        bad.length,
        `${col} holds text that is not valid UTF-8 in rows ${bad.map(([a, b]) => `${a}–${b - 1}`).join(', ')}; such a value can never satisfy ${rule}, and the other ${col} values of those rows could not be read`,
        [col],
        'row ranges',
      );
      decodable.set(col, where(good));
      // F11 and F12 use the decodable rows through `decodable`.
      if (rule === 'F11' || rule === 'F12' || good.length === 0) continue;
      // The column's own checks (its F2 null check, its dependent rule, and
      // R5 for geometry) on the rows that decode.
      const rules = new Set([rule, 'F2', ...(own(UTF8_ALSO, col) ?? [])]);
      const sub = planRowChecks({ usable: new Set([col]), present: new Set([col]), collectionId: ctx.collectionId });
      const subPlan = { checks: sub.checks.filter((c) => rules.has(c.rule) && c.sql.includes(ident(col))), label: sub.label, skips: [], notes: [] };
      if (subPlan.checks.length === 0) continue;
      const subTotals = emptyTotals();
      for (const [start, end] of good) {
        const [row] = await q(batchSql(ref, subPlan, start, end));
        if (row) addBatch(subTotals, subPlan, row);
      }
      // Shares (R5) are of all rows: the undecodable ones are not 'unknown'.
      subTotals.rows = numRows;
      recordRowFindings(subTotals, subPlan, acc, warnOnly);
    }
    return decodable;
  }

  /**
   * The quality profile (Q1–Q4), as `openspeclib validate --profile
   * openspeclib`: not evaluated when a column it needs is unusable (Q4 alone
   * when the extent columns are), otherwise spectra are counted for the
   * bounds-share warning and candidates are judged batch by batch.
   */
  async function startQuality(badUtf8: ReadonlySet<string> = new Set()): Promise<{ tally: QualityTally; withExtent: boolean; nulled: Set<string> } | null> {
    const qaRules = ['Q1', 'Q2', 'Q3', 'Q4'];
    if (!qualityProfile) {
      for (const r of qaRules) acc.skip(r, 'na', 'Not checked: the OpenSpecLib quality profile is off.');
      return null;
    }
    // A text column with values that are not UTF-8 is read as null, so the
    // other rows are still judged (ids then label rows by number).
    const nulled = new Set(QUALITY_COLUMNS.filter((c) => badUtf8.has(c)));
    const missing = QUALITY_COLUMNS.filter((c) => !usable.has(c) && !(c === 'dataset_id' && allNull.has(c)) && !nulled.has(c));
    if (missing.length) {
      for (const r of qaRules) {
        acc.skip(r, 'blocked', `Quality profile not evaluated: ${missing.join(', ')} ${missing.length === 1 ? 'is' : 'are'} missing or of the wrong type (see F2).`);
      }
      return null;
    }
    const withExtent = EXTENT_COLUMNS.every((c) => usable.has(c));
    if (!withExtent) acc.skip('Q4', 'blocked', 'wavelength_min_nm or wavelength_max_nm is missing or of the wrong type (see F2).');
    const tally = new QualityTally(ctx.collectionId ?? label, options.share);
    const col = (c: string): string => (nulled.has(c) ? 'NULL::VARCHAR' : ident(c));
    tally.addCounts(await q(`SELECT ${col('quantity')} AS quantity, ${col('dataset_id')} AS dataset_id, count(*)::BIGINT AS n FROM ${scan(ref)} GROUP BY ALL`));
    return { tally, withExtent, nulled };
  }

  async function checkRows(badUtf8: Set<string>): Promise<void> {
    // Row rules, batch by batch. Every column is decoded, not only those
    // the rules read, so a corrupt page in any column is found (F1).
    const decode = [...present].filter((name) => !badUtf8.has(name));
    const plan = planRowChecks({ usable, present: new Set(decode), allNull, collectionId: ctx.collectionId });
    // Rules that a column with invalid UTF-8 depends on are checked below,
    // range by range, instead of being skipped.
    const bad = [...badUtf8];
    const handled = new Set(
      bad.flatMap((c) => [utf8DependentRule(c), ...(own(UTF8_ALSO, c) ?? [])]).filter((r): r is string => r !== null),
    );
    plan.skips = plan.skips.filter((sk) => !(handled.has(sk.rule) && bad.some((c) => sk.reason.includes(c))));
    plan.notes = plan.notes.filter((n) => !(handled.has(n.rule) && bad.some((c) => n.note.startsWith(`${c} `))));
    const quality = await startQuality(badUtf8);
    const totals = emptyTotals();
    const extraBad = new Map<string, { n: number; examples: string[] }>();
    if (numRows > 0) {
      const ranges = batchRanges(rowGroups, batchTarget(numRows));
      for (const [start, end] of ranges) {
        progress('Checking rows', 0.05 + 0.85 * (start / numRows), { rowsChecked: start, rowsTotal: numRows });
        const [row] = await q(batchSql(ref, plan, start, end, decode));
        if (row) addBatch(totals, plan, row);
        if (quality) {
          for (const r of await q(qualityCandidatesSql(scan(ref), start, end, quality.withExtent, quality.nulled))) quality.tally.addCandidate(r);
        }
        // F4: each non-null `extra` is a JSON object. Parsed here, batch by
        // batch, rather than with DuckDB's json extension (another download).
        if (usable.has('extra')) {
          const rows = await q(
            `SELECT ${plan.label} AS label, ${ident('extra')} AS extra FROM ${scan(ref)}
             WHERE file_row_number >= ${start} AND file_row_number < ${end} AND ${ident('extra')} IS NOT NULL`,
          );
          for (const r of rows) {
            const problem = extraProblem(String(r.extra));
            if (problem === null) continue;
            const entry = extraBad.get(problem) ?? { n: 0, examples: [] };
            entry.n++;
            if (entry.examples.length < MAX_EXAMPLES) entry.examples.push(String(r.label));
            extraBad.set(problem, entry);
          }
        }
      }
    }
    const warnOnly: ReadonlySet<string> = footer.newerMinor ? new Set(['F7']) : new Set();
    recordRowFindings(totals, plan, acc, warnOnly);
    quality?.tally.record(acc);
    const decodable = await checkUndecodable(badUtf8, warnOnly);
    for (const [problem, { n, examples }] of extraBad) {
      acc.issue('F4', n, `${plural(n, 'row')}: ${problem}`, examples, 'rows');
    }
    progress('Checking ids', 0.9, { rowsChecked: numRows, rowsTotal: numRows });

    // F5 uniqueness and R1 order (files with no rows are exempt from R1).
    if (usable.has('id')) {
      const [dup] = await q(
        `WITH d AS (SELECT id, count(*)::BIGINT AS c FROM ${scan(ref)} WHERE id IS NOT NULL GROUP BY id HAVING count(*) > 1)
         SELECT count(*)::BIGINT AS n_ids, coalesce(sum(c), 0)::BIGINT AS n_rows, (list(id ORDER BY id))[1:${MAX_EXAMPLES}] AS ex FROM d`,
      );
      acc.issue(
        'F5',
        num(dup?.n_rows),
        `${plural(num(dup?.n_ids), 'id')} used more than once (${plural(num(dup?.n_rows), 'row')})`,
        Array.isArray(dup?.ex) ? dup.ex.map(String) : [],
        'rows',
      );
      if (numRows > 0) {
        // DuckDB compares VARCHAR by bytes, i.e. UTF-8 code unit order (R1).
        // Each id is compared with the row above; a null id (F2) compares
        // with nothing, as in the reference checker.
        const [order] = await q(
          `SELECT count(*) FILTER (WHERE prev > id)::BIGINT AS n,
                  (list(id ORDER BY file_row_number) FILTER (WHERE prev > id))[1:${MAX_EXAMPLES}] AS ex
           FROM (SELECT id, file_row_number, lag(id) OVER (ORDER BY file_row_number) AS prev FROM ${scan(ref)})`,
        );
        acc.issue(
          'R1',
          num(order?.n),
          `${plural(num(order?.n), 'row')} out of id order (each listed id sorts before the id above it)`,
          Array.isArray(order?.ex) ? order.ex.map(String) : [],
          'rows',
        );
      }
    } else if (badUtf8.has('id')) {
      acc.skip('F5', 'partial', 'id uniqueness was not checked: id holds text that is not valid UTF-8.');
      acc.skip('R1', 'blocked', 'id holds text that is not valid UTF-8 (see F2).');
    } else {
      acc.skip('F5', 'blocked', 'id is missing or of the wrong type (see F2).');
      acc.skip('R1', 'blocked', 'id is missing or of the wrong type (see F2).');
    }

    // F11: licences in rows and in the footer. Empty and whitespace-only
    // row values are F2's.
    progress('Checking licences', 0.95);
    if (ctx.readable) {
      for (const [path, value] of ctx.footerLicenses) {
        const problems = checkLicense(value, ctx.licenseRefs);
        acc.issue('F11', problems.length ? 1 : 0, `Footer ${path} ${JSON.stringify(value)}: ${problems.join('; ')}`, [path], 'values');
      }
      if (usable.has('license') || decodable.has('license')) {
        const values = await distinctCounts(q, ref, 'license', decodable.get('license'));
        let reported = 0;
        const rest = { values: 0, rows: 0 };
        for (const { value, n } of values) {
          if (isBlank(value)) continue;
          const problems = checkLicense(value, ctx.licenseRefs);
          if (problems.length === 0) continue;
          // One message per bad value, up to a point; the rest are summed.
          if (reported++ < MAX_LICENCE_MESSAGES) {
            acc.issue('F11', n, `license ${JSON.stringify(value)} on ${plural(n, 'row')}: ${problems.join('; ')}`, [value], 'rows');
          } else {
            rest.values++;
            rest.rows += n;
          }
        }
        acc.issue('F11', rest.rows, `${plural(rest.values, 'more licence value')} fail (${plural(rest.rows, 'row')})`, [], 'rows');
      } else {
        acc.skip('F11', 'blocked', 'license is missing or of the wrong type (see F2).');
      }
    }

    // F12: dataset references, unused datasets, spectra_included.
    if (ctx.readable) {
      if (ctx.datasetIds === null) {
        acc.skip('F12', 'blocked', 'The footer datasets entry is not an object (see F1).');
      } else if (!usable.has('dataset_id') && !allNull.has('dataset_id') && !decodable.has('dataset_id')) {
        acc.skip('F12', 'blocked', 'dataset_id is missing or of the wrong type (see F2).');
      } else {
        const known = ctx.datasetIds;
        const values =
          usable.has('dataset_id') || decodable.has('dataset_id') ? await distinctCounts(q, ref, 'dataset_id', decodable.get('dataset_id')) : [];
        const counts = new Map(values.map((v) => [v.value, v.n]));
        const unknown = values.filter((v) => !known.has(v.value));
        const n = unknown.reduce((s, v) => s + v.n, 0);
        acc.issue(
          'F12',
          n,
          `${plural(n, 'row')} reference ${plural(unknown.length, 'dataset')} missing from the footer datasets: ${unknown
            .slice(0, MAX_EXAMPLES)
            .map((v) => JSON.stringify(v.value))
            .join(', ')}${unknown.length > MAX_EXAMPLES ? ', …' : ''}`,
          unknown.map((v) => v.value),
          'rows',
        );
        const datasets = (footer.value as { datasets?: Record<string, { spectra_included?: unknown }> }).datasets ?? {};
        for (const key of [...known].sort()) {
          const used = counts.get(key) ?? 0;
          if (used === 0) {
            acc.issue('F12', 1, `datasets key ${JSON.stringify(key)} is not used by any row`, [key], 'datasets');
            continue;
          }
          const included = datasets[key]?.spectra_included;
          // Compared as a number: 99.0 equals 99 rows; 99.5 never does (and F1 rejects it).
          if (typeof included === 'number' && included !== used) {
            acc.issue('F12', 1, `datasets.${key}.spectra_included is ${included}, but ${plural(used, 'row')} have that dataset_id`, [key], 'datasets');
          }
        }
      }
    }
  }
}
