/**
 * Row rules as DuckDB SQL. Each check is a boolean SQL expression over one
 * row; a batch query counts the rows that trip each check and collects a
 * few example ids, so no spectral arrays ever reach JavaScript.
 *
 * This module only builds SQL strings and folds query results: it is pure
 * and unit-tested without DuckDB.
 */
import {
  canonicalType,
  COLUMNS,
  CORE_COLUMNS,
  LOCAL_ID_PATTERN,
  VOCABULARIES,
  VOCABULARY_COLUMNS,
} from '../../format/spec';
import { MAX_EXAMPLES, plural, type RuleAccumulator, type SkipKind } from './results';
import type { Row } from './types';

export { ident, lit, reEscape } from './sql';
import { ident, lit, num, reEscape } from './sql';

/** `read_parquet(...)` over a registered file, with row numbers. */
export function scan(fileRef: string): string {
  return `read_parquet(${lit(fileRef)}, file_row_number=true)`;
}

/**
 * How each unit converts to nanometres. The factors are physics, not part of
 * the schema files; spec.test.ts checks every unit in the wavelength_unit
 * vocabulary has one.
 */
const NM_CONVERSIONS: Record<string, number | 'wavenumber'> = { nm: 1, um: 1000, 'cm-1': 'wavenumber' };

/** Nanometres per unit, for the units in the wavelength_unit vocabulary; `cm-1` (wavenumber) is 1e7 / x. */
export const UNIT_TO_NM: Record<string, number | 'wavenumber'> = Object.fromEntries(
  Object.keys(VOCABULARIES.wavelength_unit)
    .filter((u) => u in NM_CONVERSIONS)
    .map((u) => [u, NM_CONVERSIONS[u]]),
);

/** Core and extension columns of type list<string> (F2: no null elements), from columns.json. */
export const STRING_LIST_COLUMNS: string[] = COLUMNS.filter((c) => canonicalType(c.type) === 'list<string>').map((c) => c.name);

/**
 * Whitespace as Python's str.isspace() sees it (the reference checker trims
 * with Unicode whitespace), as an RE2 character class.
 */
const WHITESPACE_CLASS = '[\\s\\pZ\\x{0085}\\x{000B}\\x{001C}-\\x{001F}]';

/** F10 tolerance (relative), as in rules.json. */
const F10_REL_TOL = 1e-6;

/** F9: a finite value with larger magnitude is a sentinel. */
export const SENTINEL_MAGNITUDE = 1e10;

export interface RowCheck {
  /** SQL column alias, unique within the plan. */
  key: string;
  rule: string;
  /** Boolean SQL expression; NULL counts as "not flagged". */
  sql: string;
  /** Describe `n` flagged rows. */
  message: (n: number) => string;
}

export interface RowPlan {
  checks: RowCheck[];
  /** SQL expression that labels a row in examples. */
  label: string;
  skips: Array<{ rule: string; kind: SkipKind; reason: string }>;
  notes: Array<{ rule: string; note: string }>;
}

/** R5: at most this share of spectra may have geometry 'unknown'. */
const R5_UNKNOWN_FRACTION = 0.5;

export interface RowPlanInput {
  /** Columns present with the exact spec type. */
  usable: Set<string>;
  /** Columns present at all. */
  present: Set<string>;
  /** Nullable columns stored with the Arrow `null` type (nothing to check). */
  allNull?: Set<string>;
  collectionId: string | null;
}

const rows = (n: number): string => plural(n, 'row');

/** Build the row checks that the file's columns allow. */
export function planRowChecks(input: RowPlanInput): RowPlan {
  const { usable, present, collectionId } = input;
  const allNull = input.allNull ?? new Set<string>();
  const checks: RowCheck[] = [];
  const skips: RowPlan['skips'] = [];
  const notes: RowPlan['notes'] = [];
  const add = (rule: string, sql: string, message: (n: number) => string): void => {
    checks.push({ key: `k${checks.length}`, rule, sql, message });
  };
  /** True if all `cols` are usable; otherwise record why `rule` (or part of it) is skipped. */
  const need = (rule: string, cols: string[], what = '', kind: SkipKind = 'blocked'): boolean => {
    const bad = cols.filter((c) => !usable.has(c));
    if (bad.length === 0) return true;
    skips.push({
      rule,
      kind,
      reason: `${what ? `${what}: ` : ''}${bad.join(', ')} ${bad.length === 1 ? 'is' : 'are'} missing or of the wrong type (see F2/F4)`,
    });
    return false;
  };

  const label = usable.has('id')
    ? `coalesce(${ident('id')}, '(row index ' || file_row_number || ')')`
    : `'(row index ' || file_row_number || ')'`;

  // F2: non-nullable core columns contain no nulls; some no empty strings.
  for (const col of CORE_COLUMNS) {
    if (col.nullable || !present.has(col.name)) continue;
    add('F2', `${ident(col.name)} IS NULL`, (n) => `${rows(n)} with null ${col.name}`);
  }
  // String list columns hold no null elements (F2, spec section 4).
  for (const col of STRING_LIST_COLUMNS) {
    if (!usable.has(col)) continue;
    add('F2', `list_count(${ident(col)}) < len(${ident(col)})`, (n) => `${rows(n)} with null elements in ${col}`);
  }
  for (const col of CORE_COLUMNS) {
    if (!col.non_empty || !usable.has(col.name)) continue;
    add('F2', `regexp_full_match(${ident(col.name)}, ${lit(`${WHITESPACE_CLASS}*`)})`, (n) => `${rows(n)} with an empty or whitespace-only ${col.name}`);
  }

  // F5: id grammar (uniqueness is a separate query).
  if (collectionId === null) {
    skips.push({ rule: 'F5', kind: 'blocked', reason: 'The footer has no collection.id, so ids were checked for uniqueness only (see F1).' });
  } else if (need('F5', ['id'])) {
    const re = `^${reEscape(collectionId)}:${LOCAL_ID_PATTERN}$`;
    add(
      'F5',
      `NOT regexp_full_match(${ident('id')}, ${lit(re)})`,
      (n) => `${rows(n)} whose id does not match ${re}`,
    );
  }

  // F6: collection column equals the footer collection.id.
  if (collectionId === null) {
    skips.push({ rule: 'F6', kind: 'blocked', reason: 'The footer has no collection.id to compare with (see F1).' });
  } else if (need('F6', ['collection'])) {
    add('F6', `${ident('collection')} <> ${lit(collectionId)}`, (n) => `${rows(n)} where collection is not '${collectionId}'`);
  }

  // F7: vocabularies.
  if (VOCABULARY_COLUMNS.every((c) => !usable.has(c))) {
    skips.push({ rule: 'F7', kind: 'blocked', reason: 'No vocabulary column is usable (see F2).' });
  }
  for (const col of VOCABULARY_COLUMNS) {
    if (!usable.has(col)) {
      if (VOCABULARY_COLUMNS.some((c) => usable.has(c))) notes.push({ rule: 'F7', note: `${col} not checked (see F2).` });
      continue;
    }
    const values = Object.keys(VOCABULARIES[col]);
    add(
      'F7',
      `${ident(col)} NOT IN (${values.map(lit).join(', ')})`,
      (n) => `${rows(n)} with a ${col} outside the vocabulary`,
    );
  }

  const w = ident('wavelengths');
  const v = ident('values');
  const hasW = usable.has('wavelengths');
  const nonFinite = (list: string): string => `len(list_filter(${list}, lambda x: isnan(x) OR isinf(x))) > 0`;
  const hasNullElem = (list: string): string => `list_count(${list}) < len(${list})`;

  // F8: wavelength axis.
  if (need('F8', ['wavelengths'])) {
    add('F8', `len(${w}) < 2`, (n) => `${rows(n)} with fewer than 2 wavelengths`);
    add('F8', hasNullElem(w), (n) => `${rows(n)} with null wavelengths`);
    add('F8', nonFinite(w), (n) => `${rows(n)} with NaN or infinite wavelengths`);
    add('F8', `len(list_filter(${w}, lambda x: x <= 0)) > 0`, (n) => `${rows(n)} with wavelengths of 0 or less`);
    add(
      'F8',
      `len(${w}) >= 2 AND list_count(${w}) = len(${w}) AND NOT (${nonFinite(w)}) AND ` +
        `(len(list_distinct(${w})) < len(${w}) OR NOT (list_sort(${w}) = ${w} OR list_reverse_sort(${w}) = ${w}))`,
      (n) => `${rows(n)} whose wavelengths are not strictly increasing or strictly decreasing`,
    );
  }

  // F9: values.
  if (need('F9', ['values'])) {
    if (need('F9', ['wavelengths'], 'length check', 'partial')) {
      add('F9', `len(${v}) <> len(${w})`, (n) => `${rows(n)} where values and wavelengths differ in length`);
    }
    add('F9', hasNullElem(v), (n) => `${rows(n)} with null elements in values (use NaN for missing bands)`);
    add('F9', `len(list_filter(${v}, lambda x: isinf(x))) > 0`, (n) => `${rows(n)} with infinite values`);
    add(
      'F9',
      `len(list_filter(${v}, lambda x: isfinite(x) AND abs(x) > ${SENTINEL_MAGNITUDE})) > 0`,
      (n) => `${rows(n)} with values of magnitude above 1e10 (sentinels; use NaN)`,
    );
  }

  // F10: wavelength_min_nm / wavelength_max_nm against the extent of the
  // axis converted to nm, ignoring NaN (F8 reports those), on every row with
  // a non-empty axis, a known unit and both extents present.
  if (need('F10', ['wavelengths', 'wavelength_unit', 'wavelength_min_nm', 'wavelength_max_nm'])) {
    const unit = ident('wavelength_unit');
    // Non-finite wavelengths are F8's: the extent covers the finite ones.
    const finiteW = `list_filter(${w}, lambda x: isfinite(x))`;
    const nm = `CASE ${unit} ${Object.entries(UNIT_TO_NM)
      .map(([u, f]) => `WHEN ${lit(u)} THEN ${f === 'wavenumber' ? `list_transform(${finiteW}, lambda x: 1e7 / x)` : f === 1 ? finiteW : `list_transform(${finiteW}, lambda x: x * ${f})`}`)
      .join(' ')} END`;
    // |a - b| <= 1e-6 * max(|a|, |b|); false when either side is NaN or
    // null, and an infinite value never equals a finite one.
    const close = (a: string, b: string): string =>
      `coalesce(isfinite(${a}) AND isfinite(${b}) AND abs(${a} - (${b})) <= ${F10_REL_TOL} * greatest(abs(${a}), abs(${b})), false)`;
    const checkable =
      `len(${w}) > 0 AND ${unit} IN (${Object.keys(UNIT_TO_NM).map(lit).join(', ')}) AND ` +
      `${ident('wavelength_min_nm')} IS NOT NULL AND ${ident('wavelength_max_nm')} IS NOT NULL`;
    add(
      'F10',
      `(${checkable}) AND NOT (${close(ident('wavelength_min_nm'), `list_min(${nm})`)} AND ${close(ident('wavelength_max_nm'), `list_max(${nm})`)})`,
      (n) => `${rows(n)} where wavelength_min_nm/wavelength_max_nm do not match the axis converted to nm`,
    );
  }

  // F13: per-band arrays.
  if (!hasW) {
    skips.push({ rule: 'F13', kind: 'blocked', reason: 'wavelengths is missing or of the wrong type (see F2).' });
  }
  for (const col of ['bandpass_fwhm', 'values_stddev']) {
    if (!present.has(col) || !hasW || allNull.has(col)) continue;
    if (!usable.has(col)) {
      notes.push({ rule: 'F13', note: `${col} not checked (see F4).` });
      continue;
    }
    const c = ident(col);
    add('F13', `len(${c}) <> len(${w})`, (n) => `${rows(n)} where ${col} and wavelengths differ in length`);
    add(
      'F13',
      `len(list_filter(${c}, lambda x: x IS NULL OR isinf(x) OR (NOT isnan(x) AND x < 0))) > 0`,
      (n) => `${rows(n)} with null, infinite or negative ${col} elements`,
    );
  }

  // R4: at least one non-NaN value.
  if (usable.has('values')) {
    add(
      'R4',
      `len(list_filter(${v}, lambda x: x IS NOT NULL AND NOT isnan(x))) = 0`,
      (n) => `${rows(n)} with no valid (non-NaN) values`,
    );
  } else {
    skips.push({ rule: 'R4', kind: 'blocked', reason: 'values is missing or of the wrong type (see F2).' });
  }

  // R5: at most half of the spectra have geometry 'unknown'.
  if (usable.has('geometry')) {
    add('R5', `${ident('geometry')} = 'unknown'`, (n) => `${rows(n)} with geometry 'unknown'`);
  } else {
    skips.push({ rule: 'R5', kind: 'blocked', reason: 'geometry is missing or of the wrong type (see F2).' });
  }

  return { checks, label, skips, notes };
}

/** Rules that have at least one check in the plan. */
export function plannedRules(plan: RowPlan): string[] {
  return [...new Set(plan.checks.map((c) => c.rule))];
}

/** Aggregate query for rows `[start, end)` of the file. */
export function batchSql(fileRef: string, plan: RowPlan, start: number, end: number, decode: string[] = []): string {
  const rules = plannedRules(plan);
  const inner = [
    `${plan.label} AS label`,
    ...plan.checks.map((c) => `coalesce((${c.sql}), false) AS ${c.key}`),
    // Touch every column so each page is decoded (a corrupt page anywhere is
    // F1). DuckDB answers count(col) from metadata; IS NOT NULL decodes.
    ...decode.map((name, i) => `${ident(name)} IS NOT NULL AS decoded_${i}`),
  ].join(',\n    ');
  const ruleFlag = (rule: string): string =>
    plan.checks
      .filter((c) => c.rule === rule)
      .map((c) => c.key)
      .join(' OR ');
  const outer = [
    'count(*)::BIGINT AS n_rows',
    ...plan.checks.map((c) => `count(*) FILTER (WHERE ${c.key})::BIGINT AS ${c.key}`),
    ...decode.map((_, i) => `count(*) FILTER (WHERE decoded_${i})::BIGINT AS decoded_${i}`),
    ...rules.flatMap((r) => [
      `count(*) FILTER (WHERE ${ruleFlag(r)})::BIGINT AS rule_${r}`,
      `(list(label) FILTER (WHERE ${ruleFlag(r)}))[1:${MAX_EXAMPLES}] AS ex_${r}`,
    ]),
  ].join(',\n  ');
  return (
    `SELECT\n  ${outer}\nFROM (\n  SELECT\n    ${inner}\n  FROM ${scan(fileRef)}\n` +
    `  WHERE file_row_number >= ${start} AND file_row_number < ${end}\n) AS t`
  );
}

export interface RowTotals {
  rows: number;
  checks: Record<string, number>;
  rules: Record<string, number>;
  examples: Record<string, string[]>;
}

export function emptyTotals(): RowTotals {
  return { rows: 0, checks: {}, rules: {}, examples: {} };
}


/** Fold one batch result row into the running totals. */
export function addBatch(totals: RowTotals, plan: RowPlan, row: Row): void {
  totals.rows += num(row.n_rows);
  for (const c of plan.checks) totals.checks[c.key] = (totals.checks[c.key] ?? 0) + num(row[c.key]);
  for (const r of plannedRules(plan)) {
    totals.rules[r] = (totals.rules[r] ?? 0) + num(row[`rule_${r}`]);
    const ex = totals.examples[r] ?? (totals.examples[r] = []);
    const batchEx = row[`ex_${r}`];
    if (Array.isArray(batchEx)) {
      for (const e of batchEx) {
        if (ex.length >= MAX_EXAMPLES) break;
        ex.push(String(e));
      }
    }
  }
}

/**
 * Record the row findings under their rules.
 *
 * @param warnOnly rules whose findings are warnings here (F7 for a file of
 *                 a newer 1.x minor version, spec section 11)
 */
export function recordRowFindings(totals: RowTotals, plan: RowPlan, acc: RuleAccumulator, warnOnly: ReadonlySet<string> = new Set()): void {
  for (const s of plan.skips) acc.skip(s.rule, s.kind, s.reason);
  for (const n of plan.notes) acc.note(n.rule, n.note);
  for (const rule of plannedRules(plan)) {
    const n = totals.rules[rule] ?? 0;
    if (n === 0) continue;
    if (rule === 'R5') {
      const share = totals.rows ? n / totals.rows : 0;
      const pct = `${Math.round(share * 100)} %`;
      if (share > R5_UNKNOWN_FRACTION) {
        acc.issue('R5', n, `${plural(n, 'row')} of ${totals.rows.toLocaleString('en-US')} (${pct}) ${n === 1 ? 'has' : 'have'} geometry 'unknown'; R5 allows at most ${R5_UNKNOWN_FRACTION * 100} %. Use 'unknown' only when the source does not document the setup.`, totals.examples.R5 ?? [], 'rows');
      } else {
        acc.note('R5', `${plural(n, 'row')} (${pct}) ${n === 1 ? 'has' : 'have'} geometry 'unknown'; R5 allows up to ${R5_UNKNOWN_FRACTION * 100} %.`);
      }
      continue;
    }
    const parts = plan.checks
      .filter((c) => c.rule === rule && (totals.checks[c.key] ?? 0) > 0)
      .map((c) => c.message(totals.checks[c.key]));
    // Count distinct rows (a row can trip several checks of one rule).
    if (warnOnly.has(rule)) {
      acc.warn(rule, n, `${parts.join('; ')} (a warning: the file follows a newer 1.x version, which may add values)`, totals.examples[rule] ?? [], 'rows');
    } else {
      acc.issue(rule, n, parts.join('; '), totals.examples[rule] ?? [], 'rows');
    }
  }
}
