/**
 * Footer metadata (rule F1 for collection files, part of I1 for index
 * files): JSON parsing and JSON Schema validation with ajv against
 * schemas/format-v1/footer.schema.json.
 */
import Ajv2020, { type ErrorObject, type ValidateFunction } from 'ajv/dist/2020';
import { FOOTER_SCHEMA, FORMAT_VERSION } from '../../format/spec';

const DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|([+-])(\d{2}):(\d{2}))$/;

/**
 * Whether an RFC 3339 timestamp (already matching the schema pattern) names
 * a real date and time: no month 13, no 30 February, no hour 25, an offset
 * under 24 hours (spec section 3). Leap seconds (second 60) are accepted.
 */
function isRealDateTime(s: string): boolean {
  const m = DATE_TIME.exec(s);
  if (!m) return false;
  const [y, mo, d, h, mi, sec] = m.slice(1, 7).map(Number);
  if (y < 1 || mo < 1 || mo > 12 || d < 1) return false;
  const last = new Date(0);
  last.setUTCFullYear(y, mo, 0); // day 0 of the next month: the last day of this one
  if (d > last.getUTCDate()) return false;
  if (h > 23 || mi > 59 || sec > 60) return false;
  if (m[9] !== undefined && (Number(m[10]) > 23 || Number(m[11]) > 59)) return false;
  return true;
}

let validators: { collection: ValidateFunction; index: ValidateFunction } | null = null;

function getValidators(): { collection: ValidateFunction; index: ValidateFunction } {
  if (!validators) {
    // `format` is an annotation in JSON Schema 2020-12; the footer schema
    // enforces `created` with a pattern instead (spec section 3).
    const ajv = new Ajv2020({ allErrors: true, strict: false, validateFormats: false });
    ajv.addSchema(FOOTER_SCHEMA, 'footer');
    const get = (def: string): ValidateFunction => {
      const v = ajv.getSchema(`footer#/$defs/${def}`);
      if (!v) throw new Error(`footer schema has no $defs/${def}`);
      return v;
    };
    validators = { collection: get('collectionFooter'), index: get('indexFooter') };
  }
  return validators;
}

function formatError(e: ErrorObject): string {
  const where = e.instancePath ? e.instancePath.slice(1).replace(/\//g, '.') : '(top level)';
  const params = e.params as Record<string, unknown>;
  if (e.keyword === 'additionalProperties') {
    return `${where}: unknown property '${String(params.additionalProperty)}'`;
  }
  if (e.keyword === 'required') {
    return `${where}: missing required property '${String(params.missingProperty)}'`;
  }
  if (e.keyword === 'enum') {
    const allowed = (params.allowedValues as unknown[]).map((v) => JSON.stringify(v)).join(', ');
    return `${where}: must be one of ${allowed}`;
  }
  return `${where}: ${e.message ?? e.keyword}`;
}

/** Drop the noise ajv adds around a failing `if`/`then`. */
function relevantErrors(errors: ErrorObject[]): ErrorObject[] {
  return errors.filter((e) => e.keyword !== 'if');
}

export interface FooterCheck {
  /** Parsed JSON, or null when the key is missing or not JSON. */
  value: unknown;
  /** Problems found; empty means the footer is valid. */
  problems: string[];
  /**
   * For a file of a newer 1.x minor version: footer members and vocabulary
   * values this checker does not know, reported as warnings (spec section 11).
   */
  warnings: string[];
  /** The file names a newer 1.x minor version than this checker implements. */
  newerMinor: boolean;
}

/** A semantic version core (MAJOR.MINOR.PATCH): ASCII digits, no leading zeros. */
const SEMVER = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;

/** Major version this checker reads. */
const SUPPORTED_MAJOR = 1;

/** The format version this checker implements, from the schema files (e.g. `1.0`). */
export const IMPLEMENTED_VERSION = FORMAT_VERSION.split('.').slice(0, 2).join('.');

/** Minor version of the format this checker implements, from the schema files. */
export const IMPLEMENTED_MINOR = Number(SEMVER.exec(FORMAT_VERSION)?.[2] ?? 0);

/**
 * Whether `format_version` is a well-formed 1.x.y with x above the
 * implemented minor (spec section 11). A malformed version (e.g. `1.01.0`)
 * is never newer: F1 / I1 reject it.
 */
export function isNewerMinor(version: unknown): boolean {
  const m = typeof version === 'string' ? SEMVER.exec(version) : null;
  return !!m && Number(m[1]) === SUPPORTED_MAJOR && Number(m[2]) > IMPLEMENTED_MINOR;
}

/** Whether a footer object declares a newer minor version. */
function footerIsNewer(doc: unknown): boolean {
  return isObject(doc) && isNewerMinor(doc.format_version);
}

/**
 * A `format_version` problem stated once and clearly, instead of a schema
 * pattern mismatch: an unsupported major version, or leading zeros (which
 * the schema pattern admits but semantic versioning forbids).
 */
function versionProblem(version: unknown): string | null {
  if (typeof version !== 'string') return null;
  if (version.split('.')[0] !== String(SUPPORTED_MAJOR)) {
    return `format_version '${version}' has major version ${version.split('.')[0]}; this checker reads major version ${SUPPORTED_MAJOR}`;
  }
  if (/^[0-9]+\.[0-9]+\.[0-9]+$/.test(version) && !SEMVER.test(version)) {
    return `format_version '${version}' is not a semantic version (version numbers have no leading zeros)`;
  }
  return null;
}

/**
 * Decode footer bytes as UTF-8, or null if they are not UTF-8. A byte-order
 * mark is kept: JSON does not allow one (as in Python's json).
 */
export function decodeUtf8(raw: Uint8Array): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(raw);
  } catch {
    return null;
  }
}

/**
 * Parse and validate a footer value.
 *
 * @param raw  the footer value bytes or text, or null if the key is missing
 * @param kind which `$defs` entry to validate against
 * @param key  the footer key name, for messages
 */
export function checkFooter(
  raw: Uint8Array | string | null,
  kind: 'collection' | 'index',
  key: string,
): FooterCheck {
  const fail = (value: unknown, problem: string): FooterCheck => ({ value, problems: [problem], warnings: [], newerMinor: false });
  if (raw === null) return fail(null, `the Parquet footer has no '${key}' key`);
  let text: string;
  if (typeof raw === 'string') {
    text = raw;
  } else {
    const decoded = decodeUtf8(raw);
    if (decoded === null) return fail(null, `the '${key}' value is not valid UTF-8`);
    text = decoded;
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    return fail(null, `the '${key}' value is not valid JSON: ${(e as Error).message}`);
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return fail(value, `the '${key}' value must be a JSON object`);
  }

  const problems: string[] = [];
  const warnings: string[] = [];
  const doc = value as Record<string, unknown>;
  const newerMinor = footerIsNewer(doc);
  // Which footer governs a JSON path (spec section 11): an index's embedded
  // collections[N].metadata is a collection footer, judged by its own
  // format_version; everything else follows the file's format_version.
  const entries = kind === 'index' && Array.isArray(doc.collections) ? doc.collections : [];
  const forwardAt = (instancePath: string): boolean => {
    const m = /^\/collections\/(\d+)\/metadata(\/|$)/.exec(instancePath);
    if (m && Number(m[1]) < entries.length && isObject(entries[Number(m[1])])) {
      return footerIsNewer((entries[Number(m[1])] as Record<string, unknown>).metadata);
    }
    return newerMinor;
  };
  const version = versionProblem(doc.format_version);
  const validate = getValidators()[kind];
  if (!validate(value)) {
    const seen = new Set<string>();
    for (const err of relevantErrors(validate.errors ?? [])) {
      if (version && err.instancePath === '/format_version') continue;
      const msg = formatError(err);
      if (seen.has(msg)) continue;
      seen.add(msg);
      // A newer minor version may add footer members and vocabulary values.
      if ((err.keyword === 'additionalProperties' || err.keyword === 'enum') && forwardAt(err.instancePath)) warnings.push(msg);
      else problems.push(msg);
    }
  }
  if (version) problems.push(version);
  const created = (value as Record<string, unknown>).created;
  if (typeof created === 'string' && !problems.some((p) => p.startsWith('created:')) && !isRealDateTime(created)) {
    problems.push(`created: '${created}' is not a real calendar date and time`);
  }
  return { value, problems, warnings, newerMinor };
}

/** What the rest of the checks need from a (possibly invalid) collection footer. */
export interface FooterContext {
  /** False when the footer is missing or not a JSON object. */
  readable: boolean;
  collectionId: string | null;
  /** Declared extensions; null when `extensions` is not a list of strings. */
  extensions: string[] | null;
  /** Keys of `datasets` (empty if absent); null when `datasets` is not an object. */
  datasetIds: Set<string> | null;
  licenseRefs: Set<string>;
  /** String licence fields in the footer, as [path, value]. */
  footerLicenses: Array<[string, string]>;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Read what we can from a parsed footer, even if it failed validation. */
export function footerContext(value: unknown): FooterContext {
  const ctx: FooterContext = {
    readable: isObject(value),
    collectionId: null,
    extensions: null,
    datasetIds: null,
    licenseRefs: new Set(),
    footerLicenses: [],
  };
  if (!isObject(value)) return ctx;
  const collection = isObject(value.collection) ? value.collection : {};
  if (typeof collection.id === 'string') ctx.collectionId = collection.id;
  if (typeof collection.license === 'string') ctx.footerLicenses.push(['collection.license', collection.license]);
  if (isObject(collection.license_refs)) {
    for (const k of Object.keys(collection.license_refs)) ctx.licenseRefs.add(k);
  }
  if (Array.isArray(value.extensions) && value.extensions.every((e) => typeof e === 'string')) {
    ctx.extensions = value.extensions as string[];
  }
  const datasets = value.datasets === undefined ? {} : value.datasets;
  if (isObject(datasets)) {
    ctx.datasetIds = new Set(Object.keys(datasets));
    for (const k of Object.keys(datasets).sort()) {
      const ds = datasets[k];
      if (isObject(ds) && typeof ds.license === 'string') {
        ctx.footerLicenses.push([`datasets.${k}.license`, ds.license]);
      }
    }
  }
  return ctx;
}
