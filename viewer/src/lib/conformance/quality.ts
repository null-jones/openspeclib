/**
 * The OpenSpecLib quality profile (rules Q1–Q4, spec section 12), as
 * `openspeclib validate --profile openspeclib` applies it: bounds from
 * rules.json rounded to float32, at most one of Q1/Q2/Q3 per spectrum, Q4 on
 * the stated extent, the shipped allowlist's waivers, and the bounds-share
 * warning (reported under Q2).
 *
 * DuckDB selects the candidate spectra (a valid value outside the bounds, or
 * an implausible extent); the decisions are made here, per spectrum, so they
 * follow the reference validator exactly.
 */
import allowlistDoc from '@allowlist';
import { COLLECTION_ID_PATTERN, QA_BOUNDS, QA_WAVELENGTH_NM } from '../../format/spec';
import { MAX_EXAMPLES, plural, type RuleAccumulator } from './results';
import { ident, lit, num } from './sql';
import type { ConformanceReport, Row } from './types';

/** A valid value is finite and not a sentinel (rule F9). */
const SENTINEL_MAGNITUDE = 1e10;

/** Bounds-share warning: at least this many spectra of one quantity … */
const SHARE_MIN_SPECTRA = 10;
/** … of which at least this share fail Q1, Q2 or Q4. */
const SHARE_THRESHOLD = 0.1;

/**
 * Quality check names, as in the allowlist and the Python validator:
 * value_scale (Q1), value_out_of_bounds (Q2), value_range_suspect (Q3),
 * wavelength_extent (Q4); the bounds-share warning is reported under Q2.
 */
export type QualityCheck = 'value_scale' | 'value_out_of_bounds' | 'value_range_suspect' | 'wavelength_extent';

/** Waivable with any selector. */
const WAIVABLE = new Set(['value_out_of_bounds', 'value_range_suspect']);
/** Waivable only by exact id: a whole-spectrum scale or unit error is never waived wholesale. */
const ID_ONLY_WAIVABLE = new Set(['value_scale', 'wavelength_extent']);

/** Columns the quality profile needs (as the reference validator). */
export const QUALITY_COLUMNS = ['id', 'quantity', 'values', 'dataset_id'];
export const EXTENT_COLUMNS = ['wavelength_min_nm', 'wavelength_max_nm'];

export interface AllowlistEntry {
  id?: string | null;
  id_prefix?: string | null;
  dataset?: string | null;
  quantity?: string | null;
  checks: string[];
  bounds?: { min?: number | null; max?: number | null } | null;
  reason: string;
}

interface Bounds {
  hardMin: number;
  hardMax: number;
  warnMin: number;
  warnMax: number;
}

/** Bounds by quantity, rounded to float32 as stored values are, so a value exactly at a bound passes. */
const F32_BOUNDS: ReadonlyMap<string, Bounds> = new Map(
  Object.entries(QA_BOUNDS).map(([q, b]) => [
    q,
    { hardMin: Math.fround(b.hard[0]), hardMax: Math.fround(b.hard[1]), warnMin: Math.fround(b.warn[0]), warnMax: Math.fround(b.warn[1]) },
  ]),
);

const COLLECTION_ID = new RegExp(`^${COLLECTION_ID_PATTERN.replace(/^\^/, '').replace(/\$$/, '')}$`);

/** Why an allowlist entry is invalid (as the reference validator rejects it), or null. */
export function allowlistEntryError(e: AllowlistEntry): string | null {
  const selectors = [e.id, e.id_prefix, e.dataset].filter((s): s is string => !!s);
  if (selectors.length !== 1) return "exactly one of 'id', 'id_prefix' or 'dataset' must be set";
  const [collection, ...rest] = selectors[0].split(':');
  if (rest.length === 0 || !COLLECTION_ID.test(collection)) return `selector '${selectors[0]}' must start with a collection id and a colon`;
  if (rest.join(':') === '') return `selector '${selectors[0]}' has nothing after the collection id`;
  if (e.quantity != null && !F32_BOUNDS.has(e.quantity)) return `unknown quantity '${e.quantity}'`;
  if (!Array.isArray(e.checks) || e.checks.length === 0) return 'no checks listed';
  const bad = e.checks.filter((c) => !(WAIVABLE.has(c) || (e.id && ID_ONLY_WAIVABLE.has(c))));
  if (bad.length) return `checks ${bad.join(', ')} cannot be waived by this selector`;
  if (e.bounds != null) {
    if (!e.checks.includes('value_out_of_bounds')) return "'bounds' needs the value_out_of_bounds check";
    const { min, max } = e.bounds;
    if (min == null && max == null) return "'bounds' must set 'min' and/or 'max'";
    if (min != null && max != null && min > max) return "'bounds' min is greater than max";
  }
  if (typeof e.reason !== 'string' || e.reason.trim() === '') return "'reason' must be a non-empty explanation";
  return null;
}

/** Per-spectrum waivers, matched as the reference validator: exact id, then longest id prefix, then dataset. */
export class Allowlist {
  readonly entries: AllowlistEntry[];
  /** Entries left out because they are invalid. */
  readonly problems: string[] = [];

  constructor(entries: AllowlistEntry[]) {
    this.entries = [];
    for (const e of entries) {
      const err = allowlistEntryError(e);
      if (err) this.problems.push(err);
      else this.entries.push(e);
    }
  }

  match(spectrumId: string, datasetKey: string | null, quantity: string, check: QualityCheck): AllowlistEntry | null {
    const applies = (e: AllowlistEntry): boolean => e.checks.includes(check) && (e.quantity == null || e.quantity === quantity);
    const last = (list: AllowlistEntry[]): AllowlistEntry | null => (list.length ? list[list.length - 1] : null);
    const byId = this.entries.filter((e) => e.id === spectrumId && applies(e));
    if (byId.length) return last(byId);
    const prefixed = this.entries.filter((e) => e.id_prefix && spectrumId.startsWith(e.id_prefix) && applies(e));
    if (prefixed.length) {
      const longest = Math.max(...prefixed.map((e) => e.id_prefix!.length));
      return last(prefixed.filter((e) => e.id_prefix!.length === longest));
    }
    if (datasetKey !== null) return last(this.entries.filter((e) => e.dataset === datasetKey && applies(e)));
    return null;
  }
}

/** The allowlist `openspeclib validate` ships with (src/openspeclib/validation_allowlist.json). */
const SHIPPED_ALLOWLIST = new Allowlist((allowlistDoc as { entries: AllowlistEntry[] }).entries);

/** Shortest round-trip text of a number, for SQL literals. */
const sqlNum = (x: number): string => (Number.isFinite(x) ? String(x) : x > 0 ? "'inf'::DOUBLE" : "'-inf'::DOUBLE");

/**
 * SQL for the candidate spectra of rows `[start, end)`: a valid value
 * outside the warning or hard bounds of its quantity, or (with the extent
 * columns) an implausible stated extent. NaN extents are never candidates
 * (DuckDB orders NaN above every number; F10 reports them).
 */
export function qualityCandidatesSql(scanSql: string, start: number, end: number, withExtent: boolean, nulled: ReadonlySet<string> = new Set()): string {
  // Columns read as null (text that is not UTF-8 there).
  const col = (c: string): string => (nulled.has(c) ? 'NULL::VARCHAR' : ident(c));
  const v = ident('values');
  const q = col('quantity');
  const validX = `isfinite(x) AND abs(x) <= ${SENTINEL_MAGNITUDE}`;
  const valid = `list_filter(${v}, lambda x: ${validX})`;
  const outsideSelect = [...F32_BOUNDS]
    .map(
      ([quantity, b]) =>
        `(${q} = ${lit(quantity)} AND len(list_filter(${v}, lambda x: ${validX} AND (x < ${sqlNum(Math.max(b.warnMin, b.hardMin))} OR x > ${sqlNum(Math.min(b.warnMax, b.hardMax))}))) > 0)`,
    )
    .join(' OR ');
  const [lo, hi] = QA_WAVELENGTH_NM;
  const wmin = ident('wavelength_min_nm');
  const wmax = ident('wavelength_max_nm');
  const extent = withExtent ? `(NOT isnan(${wmin}) AND ${wmin} < ${lo}) OR (NOT isnan(${wmax}) AND ${wmax} > ${hi})` : 'false';
  const hard = (side: 'below' | 'above'): string =>
    [...F32_BOUNDS]
      .map(([quantity, b]) => `WHEN ${lit(quantity)} THEN len(list_filter(${valid}, lambda x: x ${side === 'below' ? `< ${sqlNum(b.hardMin)}` : `> ${sqlNum(b.hardMax)}`}))`)
      .join(' ');
  return `SELECT ${col('id')} AS id, file_row_number AS rn, ${q} AS quantity, ${col('dataset_id')} AS dataset_id,
      ${withExtent ? `${wmin} AS wmin, ${wmax} AS wmax` : 'NULL::DOUBLE AS wmin, NULL::DOUBLE AS wmax'},
      list_min(${valid}) AS lo, list_max(${valid}) AS hi, list_median(list_transform(${valid}, lambda x: x::DOUBLE)) AS median,
      len(${valid})::BIGINT AS n_valid,
      (CASE ${q} ${hard('below')} ELSE 0 END + CASE ${q} ${hard('above')} ELSE 0 END)::BIGINT AS n_out
    FROM ${scanSql}
    WHERE file_row_number >= ${start} AND file_row_number < ${end}
      AND (coalesce(${outsideSelect || 'false'}, false) OR coalesce(${extent}, false))`;
}

const g = (x: number): string => Number(x.toPrecision(6)).toString();

interface Finding {
  message: string;
  id: string;
}

/**
 * Bounds-share counts per (collection, dataset or the whole collection,
 * quantity) over a whole run, as `openspeclib validate` keeps them across
 * every file it checks: spectra, and spectra with an unwaived Q1, Q2 or Q4
 * error.
 */
export class ShareTally {
  private entries = new Map<string, { collection: string; scope: string; quantity: string; total: number; failed: number }>();

  private keys(collection: string, datasetKey: string | null, quantity: string): Array<[string, string]> {
    const out: Array<[string, string]> = [[`${collection}\u0000\u0000${quantity}`, `collection '${collection}'`]];
    if (datasetKey !== null) out.push([`${collection}\u0000${datasetKey}\u0000${quantity}`, `dataset '${datasetKey}'`]);
    return out;
  }

  add(collection: string, datasetKey: string | null, quantity: string, spectra: number, failed: number): void {
    if (!F32_BOUNDS.has(quantity)) return;
    for (const [key, scope] of this.keys(collection, datasetKey, quantity)) {
      const e = this.entries.get(key) ?? { collection, scope, quantity, total: 0, failed: 0 };
      e.total += spectra;
      e.failed += failed;
      this.entries.set(key, e);
    }
  }

  /** The bounds-share warnings for one collection. */
  warnings(collection: string): string[] {
    return [...this.entries.values()]
      .filter((s) => s.collection === collection && s.total >= SHARE_MIN_SPECTRA && s.failed / s.total >= SHARE_THRESHOLD)
      .sort((a, b) => a.scope.localeCompare(b.scope) || a.quantity.localeCompare(b.quantity))
      .map(
        (s) =>
          `${s.failed.toLocaleString('en-US')} of ${s.total.toLocaleString('en-US')} ${s.quantity} spectra of ${s.scope} (${Math.round((100 * s.failed) / s.total)} %) fail Q1, Q2 or Q4: suspect a set-wide scale or unit problem (reported when at least ${SHARE_MIN_SPECTRA} spectra of one quantity and at least ${SHARE_THRESHOLD * 100} % of them fail)`,
      );
  }
}

/**
 * Add the run's bounds-share warnings (under Q2) to the reports of the
 * collections they concern, once every file of the run has been checked.
 */
export function applyShareWarnings(reports: ConformanceReport[], share: ShareTally): void {
  for (const report of reports.flatMap((r) => [r, ...r.collections])) {
    if (!report.qualityProfile || !report.collectionId) continue;
    const warnings = share.warnings(report.collectionId);
    const q2 = report.results.find((r) => r.id === 'Q2');
    if (!q2 || warnings.length === 0 || q2.status === 'skip') continue;
    if (q2.status === 'pass') q2.status = 'warn';
    q2.count += warnings.length;
    q2.messages.push(...warnings);
  }
}

/** Accumulates quality findings for one collection file. */
export class QualityTally {
  private findings: Record<'Q1' | 'Q2' | 'Q2b' | 'Q3' | 'Q4', Finding[]> = { Q1: [], Q2: [], Q2b: [], Q3: [], Q4: [] };
  private waived: Array<{ rule: string; message: string; reason: string }> = [];

  private readonly collection: string;
  private readonly allowlist: Allowlist;
  private readonly share: ShareTally;
  /** The share tally is this file's alone: record() reports its warnings. */
  private readonly ownShare: boolean;

  /**
   * @param share the run's tally; without one, this file is the whole run
   *              and record() adds the bounds-share warnings itself
   */
  constructor(collection: string, share?: ShareTally, allowlist: Allowlist = SHIPPED_ALLOWLIST) {
    this.collection = collection;
    this.allowlist = allowlist;
    this.share = share ?? new ShareTally();
    this.ownShare = share === undefined;
  }

  /** Count spectra per quantity and dataset (rows of `SELECT quantity, dataset_id, count(*)`). */
  addCounts(rows: Row[]): void {
    for (const r of rows) {
      const quantity = r.quantity == null ? '' : String(r.quantity);
      const datasetKey = r.dataset_id ? `${this.collection}:${String(r.dataset_id)}` : null;
      this.share.add(this.collection, datasetKey, quantity, num(r.n), 0);
    }
  }

  /** Apply Q1–Q4 to one candidate spectrum (a row of {@link qualityCandidatesSql}). */
  addCandidate(r: Row): void {
    const id = r.id == null ? `row ${num(r.rn)}` : String(r.id);
    const quantity = r.quantity == null ? '' : String(r.quantity);
    const datasetKey = r.dataset_id ? `${this.collection}:${String(r.dataset_id)}` : null;
    let failed = false;

    // Q4: the stated extent (JS comparisons: NaN and null never trigger).
    const [low, high] = QA_WAVELENGTH_NM;
    const wmin = r.wmin == null ? NaN : Number(r.wmin);
    const wmax = r.wmax == null ? NaN : Number(r.wmax);
    if (wmin < low || wmax > high) {
      const message = `Spectrum '${id}': wavelength extent [${g(wmin)}, ${g(wmax)}] nm is outside the plausible range [${g(low)}, ${g(high)}] nm`;
      const entry = this.allowlist.match(id, null, quantity, 'wavelength_extent');
      if (entry) this.waived.push({ rule: 'Q4', message, reason: entry.reason });
      else {
        this.findings.Q4.push({ message, id });
        failed = true;
      }
    }

    const b = F32_BOUNDS.get(quantity);
    if (b && r.lo != null && r.hi != null) {
      if (this.checkValues(id, quantity, datasetKey, b, Number(r.lo), Number(r.hi), r.median == null ? null : Number(r.median), num(r.n_out), num(r.n_valid))) {
        failed = true;
      }
    }
    if (failed) this.share.add(this.collection, datasetKey, quantity, 0, 1);
  }

  /** Q1–Q3 on one spectrum; at most one is reported. Returns whether an unwaived Q1/Q2 error was recorded. */
  private checkValues(id: string, quantity: string, datasetKey: string | null, b: Bounds, lo: number, hi: number, median: number | null, nOut: number, nValid: number): boolean {
    const hard = `[${g(b.hardMin)}, ${g(b.hardMax)}]`;
    const span = `values span [${g(lo)}, ${g(hi)}]`;
    if (median !== null && (median > b.hardMax || median < b.hardMin)) {
      const message = `Spectrum '${id}': median ${quantity} value ${g(median)} is outside the hard bounds ${hard} (${span})`;
      const entry = this.allowlist.match(id, datasetKey, quantity, 'value_scale');
      if (entry) {
        this.waived.push({ rule: 'Q1', message, reason: entry.reason });
        return false;
      }
      this.findings.Q1.push({ message, id });
      return true;
    }
    const isError = lo < b.hardMin || hi > b.hardMax;
    const isWarning = lo < b.warnMin || hi > b.warnMax;
    if (isError) {
      const entry = this.allowlist.match(id, datasetKey, quantity, 'value_out_of_bounds');
      if (entry?.bounds) {
        const emin = entry.bounds.min != null ? Math.fround(entry.bounds.min) : b.hardMin;
        const emax = entry.bounds.max != null ? Math.fround(entry.bounds.max) : b.hardMax;
        if (lo < emin || hi > emax) {
          this.findings.Q2b.push({ message: `Spectrum '${id}': ${quantity} ${span}, outside even the allowlisted bounds [${g(emin)}, ${g(emax)}]`, id });
          return true;
        }
      }
      if (entry) {
        this.waived.push({ rule: 'Q2', message: `Spectrum '${id}': ${quantity} ${span}`, reason: entry.reason });
        return false;
      }
      this.findings.Q2.push({ message: `Spectrum '${id}': ${nOut} of ${nValid} valid ${quantity} values outside the hard bounds ${hard} (${span})`, id });
      return true;
    }
    if (isWarning) {
      const soft = `[${g(b.warnMin)}, ${g(b.warnMax)}]`;
      const message = `Spectrum '${id}': ${quantity} ${span}, outside the expected range ${soft} (within the hard bounds ${hard})`;
      const entry = this.allowlist.match(id, datasetKey, quantity, 'value_range_suspect');
      if (entry) this.waived.push({ rule: 'Q3', message, reason: entry.reason });
      else this.findings.Q3.push({ message, id });
    }
    return false;
  }

  /** Record everything under Q1–Q4. */
  record(acc: RuleAccumulator): void {
    const summary: Record<string, (n: number) => string> = {
      Q1: (n) => `${plural(n, 'spectrum', 'spectra')} whose median value is outside the hard bounds (a missed scale conversion, or not a spectrum of that quantity)`,
      Q2: (n) => `${plural(n, 'spectrum', 'spectra')} with valid values outside the hard bounds while the median is in range (spikes, clipped or corrupt bands)`,
      Q2b: (n) => `${plural(n, 'spectrum', 'spectra')} outside even the bounds their allowlist entry allows`,
      Q3: (n) => `${plural(n, 'spectrum', 'spectra')} with values outside the expected range (within the hard bounds)`,
      Q4: (n) => `${plural(n, 'spectrum', 'spectra')} whose stated extent is outside ${g(QA_WAVELENGTH_NM[0])} nm to ${QA_WAVELENGTH_NM[1].toLocaleString('en-US')} nm (a mislabelled wavelength_unit?)`,
    };
    for (const key of ['Q1', 'Q2', 'Q2b', 'Q3', 'Q4'] as const) {
      const list = this.findings[key];
      if (list.length === 0) continue;
      const rule = key === 'Q2b' ? 'Q2' : key;
      const shown = list.slice(0, MAX_EXAMPLES).map((f) => f.message);
      const more = list.length > MAX_EXAMPLES ? [`… and ${plural(list.length - MAX_EXAMPLES, 'more spectrum', 'more spectra')}`] : [];
      acc.issue(rule, list.length, [summary[key](list.length), ...shown, ...more].join('\n'), list.map((f) => f.id).slice(0, MAX_EXAMPLES), 'spectra');
    }
    for (const w of this.waived) acc.waive(w.rule, w.message, w.reason);
    if (this.ownShare) {
      for (const w of this.share.warnings(this.collection)) acc.warn('Q2', 1, w, [], 'findings');
    }
  }
}
