/**
 * Rule I1 for index files (spec section 7): the `openspeclib_index` footer,
 * the columns, and agreement with every collection file the index lists.
 * An index conforms only if those collection files conform too, so each
 * available collection file is checked in full and its report nested.
 *
 * The cross-file checks need the collection files. In the browser they come
 * from files dropped together with the index, or from a URL next to the
 * index URL; when a file is not available, that part of I1 is reported as
 * not checked.
 */
import { ARRAY_COLUMNS, COLUMN_BY_NAME, CORE_COLUMNS, FOOTER_KEY, INDEX_FOOTER_KEY, INDEX_ONLY_COLUMNS, RULES, canonicalType, hasSpecType, isFlatType } from '../../format/spec';
import { checkFooter, decodeUtf8, IMPLEMENTED_VERSION, isNewerMinor } from './footer';
import { parseJsonExact, pyJsonEqual, type ExactJson } from './json-exact';
import { ident, lit, scan } from './row-checks';
import { MAX_EXAMPLES, plural, type RuleAccumulator } from './results';
import { QueryError, type ConformanceReport, type FileColumn, type Progress, type QueryFn } from './types';
import { checkFile, type CheckOptions } from './engine';

/** What a resolver knows about a collection file listed in an index. */
export type Companion =
  /** Readable by DuckDB as `ref`; `sha256` hashes its bytes, if possible. */
  | { status: 'found'; ref: string; sha256?: () => Promise<string> }
  /** Definitely does not exist (e.g. HTTP 404): I1 fails. */
  | { status: 'missing'; reason: string }
  /** Not provided or not reachable: that part of I1 is not checked. */
  | { status: 'unavailable'; reason: string };

/** Map an index `file` entry (relative path or URL) to a companion file. */
export type CompanionResolver = (file: string) => Promise<Companion>;

/** Columns every index file must have, with their types (before per-file extensions). */
export const INDEX_REQUIRED_COLUMNS = [
  ...CORE_COLUMNS.filter((c) => !ARRAY_COLUMNS.has(c.name)),
  ...INDEX_ONLY_COLUMNS,
];

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/**
 * Scheme and network location of a URL as Python's `urllib.parse.urlsplit`
 * (3.12) finds them: leading C0 controls and spaces are stripped, tabs and
 * newlines removed, and a scheme is an ASCII letter followed by letters,
 * digits, `+`, `-` or `.` before the first colon.
 */
function pyUrlSchemeNetloc(file: string): { scheme: string; netloc: string } {
  let start = 0;
  while (start < file.length && file.charCodeAt(start) <= 0x20) start++;
  let url = file
    .slice(start)
    .split('')
    .filter((c) => c !== '\t' && c !== '\r' && c !== '\n')
    .join('');
  let scheme = '';
  const colon = url.indexOf(':');
  if (colon > 0 && /^[A-Za-z]$/.test(url[0]) && /^[A-Za-z0-9+.-]+$/.test(url.slice(0, colon))) {
    scheme = url.slice(0, colon).toLowerCase();
    url = url.slice(colon + 1);
  }
  let netloc = '';
  if (url.startsWith('//')) {
    const rest = url.slice(2);
    const cut = [...'/?#'].map((c) => rest.indexOf(c)).filter((i) => i >= 0);
    netloc = cut.length ? rest.slice(0, Math.min(...cut)) : rest;
  }
  return { scheme, netloc };
}

/**
 * Drive and root of a path as Python's `PureWindowsPath` (3.12) parses it:
 * `C:` drives, UNC drives (`\\server\share`, which imply a root) and device
 * paths (`\\?\…`, `\\.\…`); `/` counts as `\`.
 */
function pyWindowsDriveRoot(path: string): { drive: string; root: string } {
  const sep = '\\';
  const p = path.split('/').join(sep);
  let drive = '';
  let root = '';
  if (p[0] === sep) {
    if (p[1] === sep) {
      const start = p.slice(0, 8).toUpperCase() === '\\\\?\\UNC\\' ? 8 : 2;
      const i = p.indexOf(sep, start);
      const j = i === -1 ? -1 : p.indexOf(sep, i + 1);
      if (j === -1) drive = p;
      else {
        drive = p.slice(0, j);
        root = sep;
      }
    } else {
      root = sep;
    }
  } else if (p[1] === ':') {
    drive = p.slice(0, 2);
    if (p[2] === sep) root = sep;
  }
  if (!root && drive.startsWith(sep) && !drive.endsWith(sep)) {
    const parts = drive.split(sep);
    // `'?.'.includes('')` is true, as Python's `'' in '?.'`.
    if ((parts.length === 4 && !'?.'.includes(parts[2])) || parts.length === 6) root = sep;
  }
  return { drive, root };
}

/**
 * Problem with an index `file` value, or null: it must be a path relative to
 * the index file or an http(s) URL; absolute paths and other schemes are not
 * allowed, so an index stays valid when copied (spec section 7). Decided as
 * the reference checker decides it, with Python's URL and path parsing: an
 * http(s) URL needs a host; a path is absolute when POSIX or Windows says so;
 * any other scheme or a Windows drive is not allowed. A backslash is
 * otherwise part of a file name.
 */
export function fileReferenceError(file: string): string | null {
  const { scheme, netloc } = pyUrlSchemeNetloc(file);
  // Python's urlsplit rejects an unbalanced or invalid [IPv6] host. WHATWG URL
  // parsing is used for the latter; it is stricter for exotic hosts (bad
  // ports, IPvFuture, zone ids), which no real collection reference uses.
  if (netloc.includes('[') || netloc.includes(']')) {
    const unbalanced = netloc.includes('[') !== netloc.includes(']');
    let invalid = unbalanced;
    if (!unbalanced) {
      try {
        new URL(`http://${netloc}/`);
      } catch {
        invalid = true;
      }
    }
    if (invalid) return 'not a valid URL (invalid [IPv6] host)';
  }
  if ((scheme === 'http' || scheme === 'https') && netloc) return null;
  const { drive, root } = pyWindowsDriveRoot(file);
  if (file.startsWith('/') || (drive && root)) {
    return 'absolute paths are not allowed (use a path relative to the index file)';
  }
  if (drive || scheme) return 'only relative paths and http(s) URLs are allowed';
  return null;
}

/**
 * Resolve an index `file` value against a base URL. A backslash is part of
 * a file name (not a separator, as URL parsing would make it), so it is
 * percent-encoded first.
 */
export function resolveFileRef(file: string, base: string): string {
  return new URL(file.replace(/\\/g, '%5C'), base).href;
}

/** Comparable form of a `file` value: `./a.parquet` and `a.parquet` are the same file. */
export function resolvedFileKey(file: string, base = 'http://index.invalid/dir/'): string {
  try {
    return resolveFileRef(file, base);
  } catch {
    return file;
  }
}

/**
 * Column problems and section 11 warnings of an index file (as the
 * reference checker): the core non-array columns, the index-only columns,
 * and the extension columns some listed collection file has are required;
 * x_ columns are allowed but must be flat. A column the spec does not define
 * is a warning (and must be flat) when the index, or a listed collection file
 * that has it, declares a newer minor version; any other column fails.
 *
 * @param columns         the index's columns
 * @param fromCollections column names found in the available collection files
 * @param forward         the index declares a newer minor version
 * @param forwardColumns  column names of listed collection files that declare a newer minor version
 */
export function checkIndexColumns(
  columns: FileColumn[],
  fromCollections: Set<string>,
  forward = false,
  forwardColumns: Set<string> = new Set(),
): { problems: string[]; warnings: string[] } {
  const problems: string[] = [];
  const warnings: string[] = [];
  const counts = new Map<string, number>();
  for (const c of columns) counts.set(c.name, (counts.get(c.name) ?? 0) + 1);
  const byName = new Map(columns.map((c) => [c.name, c.type]));
  const required = new Map(CORE_COLUMNS.filter((c) => !ARRAY_COLUMNS.has(c.name)).map((c) => [c.name, c]));
  for (const name of fromCollections) {
    const spec = COLUMN_BY_NAME.get(name);
    if (spec && spec.extension !== null && spec.extension !== 'index' && !ARRAY_COLUMNS.has(name)) required.set(name, spec);
  }
  for (const c of INDEX_ONLY_COLUMNS) required.set(c.name, c);
  for (const [name, spec] of required) {
    const n = counts.get(name) ?? 0;
    const found = byName.get(name);
    if (n === 0 || found === undefined) problems.push(`missing column ${name}`);
    else if (n > 1) problems.push(`column ${name} appears ${n} times`);
    else if (!hasSpecType(found, spec.type, spec.nullable)) problems.push(`${name}: ${found} (expected ${spec.type})`);
  }
  for (const name of counts.keys()) {
    if (required.has(name)) continue;
    const unknown = !COLUMN_BY_NAME.has(name) && !name.startsWith('x_');
    let why: string | null = null;
    if (unknown && forward) why = 'the index declares a newer minor version';
    else if (unknown && forwardColumns.has(name)) why = 'a listed collection file that declares a newer minor version has it';
    else if (!name.startsWith('x_')) {
      const spec = COLUMN_BY_NAME.get(name);
      problems.push(
        spec && ARRAY_COLUMNS.has(name)
          ? `${name} belongs in collection files only`
          : spec
            ? `column ${name} is not allowed in an index (no listed collection file has it)`
            : `column ${name} is not allowed in an index (use an x_ prefix)`,
      );
      continue;
    }
    if ((counts.get(name) ?? 0) > 1) {
      problems.push(`column '${name}' appears ${counts.get(name)} times`);
    } else if (columns.some((c) => c.name === name && !isFlatType(c.type))) {
      problems.push(`column ${name} is not flat (only primitives or lists of primitives)`);
    } else if (why !== null) {
      warnings.push(`column ${name} is not defined in format ${IMPLEMENTED_VERSION} (${why})`);
    }
  }
  return { problems, warnings };
}

interface IndexArgs {
  query: QueryFn;
  ref: string;
  columns: FileColumn[];
  footerRaw: Uint8Array | null;
  /** The file also carries the collection footer key (I1 forbids it). */
  alsoCollectionKey?: boolean;
  acc: RuleAccumulator;
  options: CheckOptions;
  progress: (phase: string, fraction: number | null, extra?: Partial<Progress>) => void;
}

interface Available {
  file: string;
  ref: string;
  columns: string[];
  /** The file's own footer declares a newer minor version. */
  newerMinor: boolean;
  /** Its `format_version`, for messages. */
  version: string;
  columnTypes: FileColumn[];
}

const examples = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

/** Run I1. Returns the reports of the collection files that could be checked. */
export async function checkIndexFile(args: IndexArgs): Promise<ConformanceReport[]> {
  const { query, ref, columns, footerRaw, acc, options, progress } = args;
  for (const rule of RULES) {
    if (rule.applies_to === 'collection') {
      acc.skip(rule.id, 'na', 'Not applicable to an index file. Its collection files are checked below.');
    }
  }

  const footer = checkFooter(footerRaw, 'index', INDEX_FOOTER_KEY);
  for (const p of footer.problems) acc.issue('I1', 1, `Footer: ${p}`, [], 'problems');
  for (const w of footer.warnings) acc.warn('I1', 1, `Footer: ${w} (a warning: the footer that governs it follows a newer 1.x version than ${IMPLEMENTED_VERSION})`, [], 'findings');
  if (args.alsoCollectionKey) acc.issue('I1', 1, `Footer: the file also carries the '${FOOTER_KEY}' footer key`, [], 'problems');

  // The same entries parsed exactly, for comparisons that must follow
  // Python's == (big integers, true == 1): by position in `collections`.
  const exactCollections = (() => {
    try {
      const text = footerRaw ? decodeUtf8(footerRaw) : null;
      const doc = text === null ? null : parseJsonExact(text);
      const list = doc instanceof Map ? doc.get('collections') : undefined;
      return Array.isArray(list) ? list : [];
    } catch {
      return [];
    }
  })();
  const exactField = (pos: number, name: string): ExactJson => {
    const e = exactCollections[pos];
    return e instanceof Map ? (e.get(name) ?? null) : null;
  };
  const entries = (isObject(footer.value) && Array.isArray(footer.value.collections) ? footer.value.collections : [])
    .map((e: unknown, pos: number) => ({ e, pos }))
    .filter((x): x is { e: Record<string, unknown>; pos: number } => isObject(x.e) && typeof x.e.file === 'string');
  const reports: ConformanceReport[] = [];
  const available: Available[] = [];
  const notProvided: string[] = [];
  const seen = new Set<string>();
  const seenKeys = new Set<string>();
  /** Which listed file holds each collection id. */
  const owners = new Map<string, string>();

  for (let i = 0; i < entries.length; i++) {
    const { e: entry, pos } = entries[i];
    const file = String(entry.file);
    const refError = fileReferenceError(file);
    if (refError) acc.issue('I1', 1, `Footer: file ${JSON.stringify(file)}: ${refError}`, [file], 'problems');
    const key = resolvedFileKey(file);
    if (seenKeys.has(key)) {
      acc.issue('I1', 1, `Footer: ${file} is listed more than once`, [file], 'problems');
      continue;
    }
    seenKeys.add(key);
    seen.add(file);
    const span = 0.8 / entries.length;
    progress(`Checking ${file}`, 0.05 + span * i);
    const companion: Companion = options.resolveCompanion
      ? await options.resolveCompanion(file)
      : { status: 'unavailable', reason: 'not provided' };
    if (companion.status === 'missing') {
      acc.issue('I1', 1, `${file}: file not found (${companion.reason})`, [file], 'problems');
      continue;
    }
    if (companion.status === 'unavailable') {
      notProvided.push(`${file} (${companion.reason})`);
      continue;
    }

    const report = await checkFile(query, companion.ref, file, {
      qualityProfile: options.qualityProfile,
      share: options.share,
      signal: options.signal,
      onProgress: (p) =>
        options.onProgress?.({ ...p, phase: `${file}: ${p.phase}`, fraction: 0.05 + span * (i + (p.fraction ?? 0)) }),
    });
    reports.push(report);
    if (report.unreadable) {
      acc.issue('I1', 1, `${file}: not a readable Parquet file`, [file], 'problems');
      continue;
    }
    let ownFooter: unknown = null;
    try {
      ownFooter = report.footerText === null ? null : JSON.parse(report.footerText);
    } catch {
      ownFooter = null;
    }
    const ownCollection = isObject(ownFooter) && isObject(ownFooter.collection) ? ownFooter.collection : null;
    const cid = ownCollection && typeof ownCollection.id === 'string' ? ownCollection.id : null;
    if (cid !== null) {
      const owner = owners.get(cid);
      if (owner !== undefined) {
        acc.issue('I1', 1, `${file}: collection id '${cid}' is also used by ${owner} (files in one index cannot share a collection id)`, [file], 'problems');
      } else {
        owners.set(cid, file);
      }
    }
    if (!pyJsonEqual(exactField(pos, 'num_spectra'), { num: String(report.numRows) })) {
      acc.issue('I1', 1, `${file}: num_spectra is ${JSON.stringify(entry.num_spectra)}, the file has ${report.numRows} rows`, [file], 'problems');
    }
    let ownExact: ExactJson | undefined;
    try {
      ownExact = report.footerText === null ? undefined : parseJsonExact(report.footerText);
    } catch {
      ownExact = undefined;
    }
    if (ownExact === undefined || !pyJsonEqual(exactField(pos, 'metadata'), ownExact)) {
      acc.issue('I1', 1, `${file}: metadata differs from the file's own '${FOOTER_KEY}' footer`, [file], 'problems');
    }
    if (typeof entry.sha256 === 'string') {
      if (companion.sha256) {
        progress(`Hashing ${file}`, null);
        const digest = await companion.sha256();
        if (digest !== entry.sha256) acc.issue('I1', 1, `${file}: sha256 does not match`, [file], 'problems');
      } else {
        acc.skip('I1', 'partial', `the sha256 of ${file} could not be computed here`);
      }
    }
    available.push({
      file,
      ref: companion.ref,
      columns: report.columns.map((c) => c.name),
      columnTypes: report.columns,
      newerMinor: isObject(ownFooter) && isNewerMinor(ownFooter.format_version),
      version: isObject(ownFooter) ? String(ownFooter.format_version) : '?',
    });
  }

  // Columns.
  const fromCollections = new Set(available.flatMap((a) => a.columns));
  const forwardColumns = new Set(available.filter((a) => a.newerMinor).flatMap((a) => a.columns));
  const cols = checkIndexColumns(columns, fromCollections, footer.newerMinor, forwardColumns);
  for (const p of cols.problems) acc.issue('I1', 1, `Columns: ${p}`, [], 'problems');
  for (const w of cols.warnings) acc.warn('I1', 1, `Columns: ${w}`, [], 'findings');
  // A column a newer minor version defines may be a per-band array, which an
  // index leaves out, so a missing one is only a warning; a scalar one is
  // almost certainly required by section 7 (as the reference checker).
  const indexColumnNames = new Set(columns.map((c) => c.name));
  for (const a of available) {
    if (!a.newerMinor) continue;
    for (const c of a.columnTypes) {
      if (COLUMN_BY_NAME.has(c.name) || c.name.startsWith('x_') || indexColumnNames.has(c.name)) continue;
      if (!isFlatType(c.type) || canonicalType(c.type).startsWith('list<')) continue;
      acc.warn('I1', 1, `${a.file}: column '${c.name}' (format ${a.version}) is not in the index; section 7 requires every non-array column`, [a.file], 'findings');
    }
  }

  const indexTypes = new Map(columns.map((c) => [c.name, c.type]));
  const stringCol = (name: string): boolean => hasSpecType(indexTypes.get(name) ?? '', 'string');
  if (!stringCol('file') || !stringCol('id')) {
    acc.skip('I1', 'partial', 'the index has no usable id or file column, so its rows were not matched');
    return reports;
  }

  // Rows whose file is not listed.
  progress('Matching rows', 0.88);
  const listed = [...seen];
  try {
    const [stray] = await query(
      `SELECT count(*)::BIGINT AS n, (list(DISTINCT coalesce(file, '(null)')))[1:${MAX_EXAMPLES}] AS ex FROM ${scan(ref)}
       WHERE file IS NULL OR file NOT IN (${listed.length ? listed.map(lit).join(', ') : `''`})`,
    );
    const nStray = Number(stray?.n ?? 0);
    acc.issue('I1', nStray, `${plural(nStray, 'row')} whose file is not listed in the index footer`, examples(stray?.ex), 'rows');
    // `id` is unique across the whole index (two listed files cannot share ids).
    const [dup] = await query(
      `WITH d AS (SELECT id, count(*) AS c FROM ${scan(ref)} WHERE id IS NOT NULL GROUP BY id HAVING count(*) > 1)
       SELECT coalesce(sum(c), 0)::BIGINT AS n, (list(id ORDER BY id))[1:${MAX_EXAMPLES}] AS ex FROM d`,
    );
    const nDup = Number(dup?.n ?? 0);
    acc.issue('I1', nDup, `${plural(nDup, 'index row')} whose id appears more than once in the index`, examples(dup?.ex), 'rows');
  } catch (e) {
    if (!(e instanceof QueryError)) throw e;
    acc.issue('I1', 1, `Not a valid Parquet file: the index rows cannot be read (${e instanceof Error ? e.message.split('\n')[0] : String(e)})`, [], 'problems');
    return reports;
  }

  // Row-by-row agreement with each available collection file.
  const indexNames = new Set(columns.map((c) => c.name));
  for (const a of available) {
    const collNames = new Set(a.columns);
    const skip = (n: string): boolean => n === 'id' || n === 'file' || n === 'num_points' || ARRAY_COLUMNS.has(n) || n.startsWith('x_');
    const shared = [...indexNames].filter((n) => !skip(n) && collNames.has(n));
    const lacking = [...indexNames].filter((n) => !skip(n) && !collNames.has(n) && COLUMN_BY_NAME.get(n)?.extension !== null && COLUMN_BY_NAME.has(n));
    const differs = [
      ...shared.map((n) => `i.${ident(n)} IS DISTINCT FROM c.${ident(n)}`),
      ...lacking.map((n) => `i.${ident(n)} IS NOT NULL`),
      ...(indexNames.has('num_points') && collNames.has('values') ? ['i.num_points IS DISTINCT FROM c._np'] : []),
    ];
    const npSel = collNames.has('values') ? `len(${ident('values')})::BIGINT` : 'NULL::BIGINT';
    const sql = `
      WITH i AS (SELECT * FROM ${scan(ref)} WHERE file = ${lit(a.file)}),
           c AS (SELECT ${['id', ...shared].map(ident).join(', ')}, ${npSel} AS _np FROM ${scan(a.ref)}),
           cn AS (SELECT id, count(*) AS n FROM c GROUP BY id),
           inn AS (SELECT id, count(*) AS n FROM i GROUP BY id)
      SELECT
        (SELECT count(*) FROM i ANTI JOIN c ON i.id = c.id)::BIGINT AS no_match,
        (SELECT (list(coalesce(i.id, '(null id)')))[1:${MAX_EXAMPLES}] FROM i ANTI JOIN c ON i.id = c.id) AS no_match_ex,
        (SELECT count(*) FROM i JOIN cn ON i.id = cn.id WHERE cn.n > 1)::BIGINT AS ambiguous,
        (SELECT count(*) FROM c ANTI JOIN i ON c.id = i.id)::BIGINT AS not_indexed,
        (SELECT (list(c.id))[1:${MAX_EXAMPLES}] FROM c ANTI JOIN i ON c.id = i.id) AS not_indexed_ex,
        (SELECT coalesce(sum(n), 0) FROM inn WHERE n > 1 AND id IS NOT NULL)::BIGINT AS repeated,
        (SELECT (list(id))[1:${MAX_EXAMPLES}] FROM inn WHERE n > 1 AND id IS NOT NULL) AS repeated_ex,
        (SELECT count(*) FROM i JOIN c ON i.id = c.id JOIN cn ON i.id = cn.id
          WHERE cn.n = 1 AND (${differs.length ? differs.join(' OR ') : 'false'}))::BIGINT AS differ,
        (SELECT (list(i.id))[1:${MAX_EXAMPLES}] FROM i JOIN c ON i.id = c.id JOIN cn ON i.id = cn.id
          WHERE cn.n = 1 AND (${differs.length ? differs.join(' OR ') : 'false'})) AS differ_ex`;
    let r;
    try {
      [r] = await query(sql);
    } catch (e) {
      if (!(e instanceof QueryError)) throw e;
      const msg = e instanceof Error ? e.message.split('\n')[0] : String(e);
      acc.issue('I1', 1, `${a.file}: rows could not be compared with the index (${msg})`, [a.file], 'problems');
      continue;
    }
    const n = (k: string): number => Number(r?.[k] ?? 0);
    acc.issue('I1', n('no_match'), `${a.file}: ${plural(n('no_match'), 'index row')} with no row of that id in the file`, examples(r?.no_match_ex), 'rows');
    acc.issue('I1', n('ambiguous'), `${a.file}: ${plural(n('ambiguous'), 'index row')} matching more than one row of the file`, [], 'rows');
    acc.issue('I1', n('not_indexed'), `${a.file}: ${plural(n('not_indexed'), 'row')} missing from the index`, examples(r?.not_indexed_ex), 'rows');
    acc.issue('I1', n('repeated'), `${a.file}: ${plural(n('repeated'), 'index row')} repeating an id`, examples(r?.repeated_ex), 'rows');
    acc.issue(
      'I1',
      n('differ'),
      `${a.file}: ${plural(n('differ'), 'index row')} whose values differ from the file (or num_points ≠ len(values), or an extension column the file lacks is not null)`,
      examples(r?.differ_ex),
      'rows',
    );
  }

  // An index whose listed collection files cannot be fetched is not
  // conformant: the cross-file checks cannot be completed (spec section 11).
  acc.issue(
    'I1',
    notProvided.length,
    `${plural(notProvided.length, 'listed collection file')} could not be read, so the index cannot be verified: ${notProvided
      .slice(0, MAX_EXAMPLES)
      .join(', ')}${notProvided.length > MAX_EXAMPLES ? ', …' : ''}. Choose them together with the index, or check the index by URL.`,
    notProvided.map((n) => n.split(' (')[0]),
    'files',
  );
  return reports;
}
