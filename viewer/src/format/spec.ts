/**
 * OpenSpecLib Format v1, as published in ../schemas/format-v1/*.json.
 *
 * The JSON files are imported at build time (Vite alias `@spec`), so the
 * Format reference and the conformance checker always reflect the normative
 * files; nothing here restates the spec by hand.
 */
import columnsDoc from '@spec/columns.json';
import vocabulariesDoc from '@spec/vocabularies.json';
import rulesDoc from '@spec/rules.json';
import footerSchemaDoc from '@spec/footer.schema.json';

export interface ColumnSpec {
  name: string;
  /** Arrow type name, e.g. `string`, `list<float64>`, `date32`. */
  type: string;
  nullable: boolean;
  /**
   * `null` = core column; an extension name; `'*'` = allowed in any file
   * without a declaration (`extra`); `'index'` = index files only.
   */
  extension: string | null;
  /** String column that must not hold empty strings (rule F2). */
  non_empty?: boolean;
  description: string;
}

export type RuleLevel = 'must' | 'should' | 'qa';
export type RuleAppliesTo = 'collection' | 'index';

export interface RuleSpec {
  id: string;
  level: RuleLevel;
  applies_to: RuleAppliesTo;
  title: string;
  text: string;
}

export interface QaBounds {
  hard: number[];
  warn: number[];
}

export type Vocabulary = Record<string, string>;

export const FORMAT_VERSION: string = columnsDoc.format_version;

export const COLUMNS: ColumnSpec[] = columnsDoc.columns;

/** Columns every collection file must contain. */
export const CORE_COLUMNS: ColumnSpec[] = COLUMNS.filter((c) => c.extension === null);

/** Columns that only appear in index files (`num_points`, `file`). */
export const INDEX_ONLY_COLUMNS: ColumnSpec[] = COLUMNS.filter((c) => c.extension === 'index');

/** The `extra` column: allowed on any file without a declaration. */
export const ANY_FILE_COLUMNS: ColumnSpec[] = COLUMNS.filter((c) => c.extension === '*');

/** Extension columns, in spec order. */
export const EXTENSION_COLUMNS: ColumnSpec[] = COLUMNS.filter(
  (c) => c.extension !== null && c.extension !== '*' && c.extension !== 'index',
);

export { ARRAY_COLUMNS } from './spec-keys';

/**
 * Canonical form of an Arrow type name for comparison with columns.json:
 * large, view and dictionary-encoded variants of string and list equal
 * their plain forms (spec section 4). Types read from the Parquet schema are
 * already plain; this matters for `ARROW:schema` hints shown to users. List child names and nullability are already dropped by
 * the type readers.
 */
export function canonicalType(type: string): string {
  const dict = /^dictionary<values=(.*)>$/.exec(type);
  if (dict) return canonicalType(dict[1]);
  const list = /^(?:large_list|list_view|large_list_view|fixed_size_list|list)<(.*)>$/.exec(type);
  if (list) return `list<${canonicalType(list[1])}>`;
  if (type === 'large_string' || type === 'string_view') return 'string';
  if (type === 'large_binary' || type === 'binary_view') return 'binary';
  if (type === 'date64') return 'date32';
  return type;
}

/**
 * Whether a column's type has the spec type. A nullable column whose values
 * are all null may be stored with the Arrow `null` type (spec section 4).
 */
export function hasSpecType(found: string, spec: string, nullable = false): boolean {
  if (nullable && found === 'null') return true;
  return canonicalType(found) === spec;
}

/** F3: a primitive or a list of primitives (no structs, maps or nested lists). */
export function isFlatType(type: string): boolean {
  const t = canonicalType(type);
  const inner = /^list<(.*)>$/.exec(t)?.[1] ?? t;
  return !/^(list|struct|map|union)\b/.test(inner);
}

export const COLUMN_BY_NAME: Map<string, ColumnSpec> = new Map(COLUMNS.map((c) => [c.name, c]));

export const VOCABULARIES = {
  description: vocabulariesDoc.description as string,
  quantity: vocabulariesDoc.quantity as Vocabulary,
  geometry: vocabulariesDoc.geometry as Vocabulary,
  wavelength_unit: vocabulariesDoc.wavelength_unit as Vocabulary,
  material_category: vocabulariesDoc.material_category as Vocabulary,
  extension: vocabulariesDoc.extension as Vocabulary,
  inclusion: vocabulariesDoc.inclusion as Vocabulary,
};

/** Core columns whose values must come from a vocabulary of the same name (rule F7). */
export const VOCABULARY_COLUMNS = [
  'material_category',
  'quantity',
  'geometry',
  'wavelength_unit',
] as const;
export type VocabularyColumn = (typeof VOCABULARY_COLUMNS)[number];

/** Extension names in vocabulary order. */
export const EXTENSIONS: string[] = Object.keys(VOCABULARIES.extension);

export const RULES: RuleSpec[] = rulesDoc.rules as RuleSpec[];
export const QA_BOUNDS: Record<string, QaBounds> = rulesDoc.qa_bounds;
/** Q4: plausible axis extent in nm, [min of wavelength_min_nm, max of wavelength_max_nm]. */
export const QA_WAVELENGTH_NM: number[] = rulesDoc.qa_wavelength_nm;

export const FOOTER_SCHEMA: Record<string, unknown> = footerSchemaDoc;

export { FOOTER_KEY, INDEX_FOOTER_KEY } from './spec-keys';

/** `id` grammar (rule F5): `{collection}:{local id}`. */
export const LOCAL_ID_PATTERN = '[A-Za-z0-9._~-]{1,200}';
/** Collection id grammar (rule F5 and the footer schema). */
export const COLLECTION_ID_PATTERN: string = footerSchemaDoc.$defs.collectionId.pattern;
