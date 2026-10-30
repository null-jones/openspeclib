/**
 * Loading search rows and collection metadata from a Format v1 file:
 * an index file (the OpenSpecLib release, or one the user opens) or a
 * single collection file. Only non-array columns are read here; spectra are
 * fetched per id later (see spectra.ts).
 */
import { ARRAY_COLUMNS, FOOTER_KEY, INDEX_FOOTER_KEY } from '../../format/spec-keys';
import { ident, lit } from '../conformance/sql';
import type { DataEngine } from './engine';
import type { CollectionFooter, CollectionMeta, DataSource, SourceKind, SpectrumRecord } from './types';

export interface LoadSourceOptions {
  key: string;
  label: string;
  kind: SourceKind;
  /** The file as DuckDB sees it (registered name, path or URL). */
  ref: string;
  /**
   * For index files: map a `file` entry to a DuckDB-readable reference.
   * Collection files point at themselves.
   */
  resolveFile?: (file: string) => string;
}

export interface LoadedSource {
  source: DataSource;
  collections: CollectionMeta[];
  records: SpectrumRecord[];
}

let tableCounter = 0;

/** Read the OpenSpecLib footer keys of a file. */
async function readFooters(engine: DataEngine, ref: string): Promise<Map<string, string>> {
  const rows = await engine.rows(
    `SELECT decode(key) AS k, decode(value) AS v FROM parquet_kv_metadata(${lit(ref)})
     WHERE decode(key) IN (${lit(FOOTER_KEY)}, ${lit(INDEX_FOOTER_KEY)})`,
  );
  return new Map(rows.map((r) => [String(r.k), String(r.v)]));
}

function parseJson<T>(text: string, what: string): T {
  try {
    return JSON.parse(text) as T;
  } catch (e) {
    throw new Error(`The ${what} footer is not valid JSON: ${(e as Error).message}`);
  }
}

interface IndexFooter {
  release?: string;
  created?: string;
  generator?: string;
  collections: Array<{ file: string; num_spectra: number; sha256?: string; metadata: CollectionFooter }>;
}

/** SQL for the search-row projection, given the relation's columns. */
function recordsSql(table: string, columns: Set<string>, selfFile: string | null): string {
  const col = (name: string, fallback: string): string => (columns.has(name) ? ident(name) : fallback);
  const searchParts = ['id', 'name', 'material_name', 'material_formula', 'material_subcategory']
    .filter((c) => columns.has(c))
    .map(ident);
  if (columns.has('material_keywords')) searchParts.push(`array_to_string(${ident('material_keywords')}, ' ')`);
  return `SELECT
    id, collection, name, material_name, material_category,
    ${col('material_formula', 'NULL::VARCHAR')} AS material_formula,
    ${col('sample_description', 'NULL::VARCHAR')} AS sample_description,
    quantity, geometry, wavelength_unit, wavelength_min_nm, wavelength_max_nm,
    ${col('num_points', 'NULL::INTEGER')} AS num_points,
    license, dataset_id,
    ${columns.has('measurement_processing') ? `coalesce(list_contains(${ident('measurement_processing')}, 'simulated'), false)` : 'false'} AS simulated,
    ${selfFile !== null ? lit(selfFile) : 'file'} AS file,
    lower(concat_ws(' ', ${searchParts.join(', ')})) AS search
  FROM ${table}
  ORDER BY id`;
}

/** Turn query columns into records. */
function columnsToRecords(cols: Record<string, unknown[]>, sourceKey: string): SpectrumRecord[] {
  const n = cols.id?.length ?? 0;
  const out: SpectrumRecord[] = new Array(n);
  const str = (k: string, i: number) => {
    const v = cols[k][i];
    return v == null ? null : String(v);
  };
  for (let i = 0; i < n; i++) {
    out[i] = {
      id: String(cols.id[i]),
      sourceKey,
      collection: String(cols.collection[i]),
      name: String(cols.name[i] ?? ''),
      material_name: String(cols.material_name[i] ?? ''),
      material_category: String(cols.material_category[i] ?? 'other'),
      material_formula: str('material_formula', i),
      sample_description: str('sample_description', i),
      quantity: String(cols.quantity[i] ?? ''),
      geometry: String(cols.geometry[i] ?? 'unknown'),
      wavelength_unit: String(cols.wavelength_unit[i] ?? ''),
      wavelength_min_nm: Number(cols.wavelength_min_nm[i]),
      wavelength_max_nm: Number(cols.wavelength_max_nm[i]),
      num_points: cols.num_points[i] == null ? null : Number(cols.num_points[i]),
      license: String(cols.license[i] ?? ''),
      dataset_id: str('dataset_id', i),
      simulated: cols.simulated[i] === true,
      file: String(cols.file[i]),
      search: String(cols.search[i] ?? ''),
    };
  }
  return out;
}

/**
 * Load an index or collection file: its collections' metadata from the
 * footer, its non-array rows into a DuckDB table (for details), and its
 * search rows into memory.
 *
 * @throws Error if the file carries no OpenSpecLib footer
 */
export async function loadSource(engine: DataEngine, opts: LoadSourceOptions): Promise<LoadedSource> {
  const { key, label, kind, ref } = opts;
  const footers = await readFooters(engine, ref);
  const describe = await engine.rows(`DESCRIBE SELECT * FROM read_parquet(${lit(ref)})`);
  const allColumns = describe.map((r) => String(r.column_name));
  const arrays = allColumns.filter((c) => ARRAY_COLUMNS.has(c));
  const columns = allColumns.filter((c) => !ARRAY_COLUMNS.has(c));

  const collections: CollectionMeta[] = [];
  const fileRefs: Record<string, string> = {};
  let selfFile: string | null = null;
  let indexFooter: IndexFooter | null = null;

  if (footers.has(INDEX_FOOTER_KEY) && !footers.has(FOOTER_KEY)) {
    indexFooter = parseJson<IndexFooter>(footers.get(INDEX_FOOTER_KEY)!, INDEX_FOOTER_KEY);
    if (!Array.isArray(indexFooter.collections)) throw new Error('The index footer lists no collections.');
    for (const c of indexFooter.collections) {
      collections.push({
        id: c.metadata.collection.id,
        footer: c.metadata,
        sourceKey: key,
        file: c.file,
        numSpectra: c.num_spectra,
        sha256: c.sha256,
      });
      fileRefs[c.file] = opts.resolveFile ? opts.resolveFile(c.file) : c.file;
    }
  } else if (footers.has(FOOTER_KEY)) {
    const footer = parseJson<CollectionFooter>(footers.get(FOOTER_KEY)!, FOOTER_KEY);
    selfFile = label;
    fileRefs[label] = ref;
    collections.push({ id: footer.collection.id, footer, sourceKey: key, file: label, numSpectra: 0 });
  } else {
    throw new Error(`${label} has no '${FOOTER_KEY}' or '${INDEX_FOOTER_KEY}' footer key, so it is not an OpenSpecLib Format file.`);
  }

  const table = `osl_src_${++tableCounter}`;
  const exclude = arrays.length ? ` EXCLUDE (${arrays.map(ident).join(', ')})` : '';
  await engine.rows(`CREATE OR REPLACE TABLE ${table} AS SELECT *${exclude} FROM read_parquet(${lit(ref)})`);
  const cols = await engine.columns(recordsSql(table, new Set(columns), selfFile));
  const records = columnsToRecords(cols, key);
  if (selfFile !== null) collections[0].numSpectra = records.length;

  return {
    source: {
      key,
      label,
      kind,
      collections: collections.map((c) => c.id),
      table,
      columns,
      fileRefs,
      release: indexFooter?.release,
      created: indexFooter?.created,
      generator: indexFooter?.generator,
    },
    collections,
    records,
  };
}
