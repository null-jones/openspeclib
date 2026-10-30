import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { EXAMPLE_FOOTER, EXAMPLE_ID, READING_SNIPPETS } from '../src/format/examples';
import { checkFooter } from '../src/lib/conformance/footer';
import { checkFile } from '../src/lib/conformance/engine';
import { lit } from '../src/lib/conformance/row-checks';
import { openDuckDB, type NodeDuckDB } from './helpers/duckdb-node';

let duck: NodeDuckDB;
let dir: string;
let path: string;

beforeAll(async () => {
  duck = await openDuckDB();
  dir = mkdtempSync(join(tmpdir(), 'osl-examples-'));
  path = join(dir, 'usgs_splib07.parquet');
  await duck.query(`COPY (
    SELECT ${lit(EXAMPLE_ID)} AS id, 'usgs_splib07' AS collection, 'x' AS original_id, 'Olivine GDS70' AS name,
      'Olivine' AS material_name, 'mineral' AS material_category, 'reflectance' AS quantity,
      'biconical' AS geometry, 'um' AS wavelength_unit, [0.35, 1.0, 2.5]::DOUBLE[] AS wavelengths,
      [0.1::FLOAT, 'NaN'::FLOAT, 0.3::FLOAT] AS "values", 350.0::DOUBLE AS wavelength_min_nm, 2500.0::DOUBLE AS wavelength_max_nm,
      'LicenseRef-USGS-PublicDomain' AS license, NULL::VARCHAR AS dataset_id
  ) TO ${lit(path)} (FORMAT parquet, KV_METADATA {openspeclib: ${lit(JSON.stringify(EXAMPLE_FOOTER))}})`);
});
afterAll(() => {
  duck?.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('Format reference examples', () => {
  test('the example footer is valid', () => {
    expect(checkFooter(JSON.stringify(EXAMPLE_FOOTER), 'collection', 'openspeclib').problems).toEqual([]);
  });

  test('the example footer quotes the published USGS footer (when the release data is present)', async () => {
    const release = join(__dirname, '../public/data/usgs_splib07.parquet');
    if (!existsSync(release)) return;
    duck.register('release-usgs.parquet', release);
    const [row] = await duck.query(
      `SELECT decode(value) AS v FROM parquet_kv_metadata('release-usgs.parquet') WHERE decode(key) = 'openspeclib'`,
    );
    const real = JSON.parse(String(row.v)) as { collection: Record<string, unknown>; extensions: string[] };
    for (const key of ['id', 'title', 'doi', 'license', 'citation', 'inclusion', 'inclusion_notes'] as const) {
      expect(EXAMPLE_FOOTER.collection[key], key).toEqual(real.collection[key]);
    }
    expect(EXAMPLE_FOOTER.extensions).toEqual(real.extensions);
  });

  test('a file with the example footer conforms (core columns only, extensions declared)', async () => {
    duck.register('example.parquet', path);
    const r = await checkFile(duck.query, 'example.parquet', 'example');
    expect(r.results.filter((x) => x.status === 'fail').map((x) => x.id)).toEqual([]);
  });

  test('the DuckDB SQL snippet runs', async () => {
    const sql = READING_SNIPPETS.find((s) => s.id === 'sql')!.code(path, EXAMPLE_ID);
    const statements = sql
      .split(';')
      .map((s) => s.replace(/--.*$/gm, '').trim())
      .filter(Boolean);
    expect(statements).toHaveLength(3);
    const [meta, find, one] = await Promise.all(statements.map((s) => duck.query(s)));
    expect(JSON.parse(String(meta[0].footer)).collection.id).toBe('usgs_splib07');
    expect(find).toHaveLength(1);
    expect(one.map((r) => r.wavelength)).toEqual([0.35, 1.0, 2.5]);
    expect(Number.isNaN(one[1].value)).toBe(true);
  });

  test('the JavaScript snippet queries and accessors work', async () => {
    const js = READING_SNIPPETS.find((s) => s.id === 'js')!.code('x', EXAMPLE_ID);
    const queries = [...js.matchAll(/conn\.query\(\s*`([\s\S]*?)`\)/g)].map((m) => m[1].replace(/\$\{url\}/g, path));
    expect(queries).toHaveLength(2);
    const raw = duck.raw();
    const meta = raw.query(queries[0]);
    expect(JSON.parse(meta.get(0)!.footer).format_version).toBe('1.0.0');
    const row = raw.query(queries[1]).get(0)!;
    expect(row.wavelengths.toArray()).toBeInstanceOf(Float64Array);
    expect(row.values.toArray()).toBeInstanceOf(Float32Array);
  });
});
