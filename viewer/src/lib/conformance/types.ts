import type { RuleLevel, RuleAppliesTo } from '../../format/spec';

/**
 * `pass`: the rule was checked and holds.
 * `fail`: a MUST rule (or a quality-profile hard rule) is violated.
 * `warn`: a SHOULD recommendation (or Q3) is not met.
 * `skip`: not checked — not applicable to this kind of file, or a
 *         prerequisite failed (that failure is reported under its own rule).
 */
export type RuleStatus = 'pass' | 'fail' | 'warn' | 'skip';

export interface RuleResult {
  id: string;
  level: RuleLevel;
  appliesTo: RuleAppliesTo;
  title: string;
  text: string;
  status: RuleStatus;
  /** False when part of the rule could not be checked. */
  complete: boolean;
  /** Number of offending items (rows, columns, row groups …), 0 when passing. */
  count: number;
  /** What `count` counts, e.g. "rows". */
  unit: string;
  /** A few offending spectrum ids, column names or values. */
  examples: string[];
  /** Human-readable detail lines. */
  messages: string[];
  /**
   * Quality issues an allowlist entry waives (quality profile only), with
   * the entry's reason. They do not affect the status.
   */
  waived: Array<{ message: string; reason: string }>;
}

export type Verdict = 'conformant' | 'not-conformant' | 'incomplete';

export type FileKind = 'collection' | 'index' | 'unknown';

/** A top-level column as found in the file. */
export interface FileColumn {
  name: string;
  /**
   * The Arrow type implied by the column's Parquet logical type and
   * repetition (spec section 4), e.g. `string`, `list<float64>`, `null`.
   * Rules F2–F4 compare this.
   */
  type: string;
  /** The type the writer's `ARROW:schema` hint gives, when it differs (display only). */
  arrowType?: string;
}

export interface ConformanceReport {
  label: string;
  kind: FileKind;
  verdict: Verdict;
  numRows: number;
  numRowGroups: number;
  createdBy: string | null;
  columns: FileColumn[];
  /** The footer's collection.id (collection files), for run-wide quality counts. */
  collectionId?: string | null;
  /** The raw footer JSON text, if the key exists. */
  footerText: string | null;
  /** Why DuckDB could not read the file as Parquet, or null. */
  unreadable: string | null;
  results: RuleResult[];
  /** For index files: reports of the listed collection files that were checked. */
  collections: ConformanceReport[];
  qualityProfile: boolean;
  durationMs: number;
}

export interface Progress {
  phase: string;
  /** 0..1, or null when indeterminate. */
  fraction: number | null;
  rowsChecked?: number;
  rowsTotal?: number;
}

/** A plain row as returned by the query runner (BigInts already converted). */
export type Row = Record<string, unknown>;

/**
 * Runs one SQL statement and returns plain JS rows. Implemented over
 * DuckDB-WASM in the browser and over DuckDB-WASM's Node build in tests;
 * the conformance engine depends only on this.
 */
export type QueryFn = (sql: string) => Promise<Row[]>;

/**
 * An error DuckDB raised while reading the file: a finding about the file
 * (F1, I1), unlike any other exception, which is a fault in the checker.
 */
export class QueryError extends Error {
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
    this.name = 'QueryError';
  }
}

/** The checker itself failed: not a verdict about the file. */
export function checkerFailure(e: unknown): Error {
  const msg = e instanceof Error ? e.message : String(e);
  return new Error(`The checker failed: ${msg}. This is a fault in the checker, not a finding about the file.`, { cause: e });
}

/** Thrown for problems that stop the check altogether. */
export class CheckAbortedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CheckAbortedError';
  }
}
