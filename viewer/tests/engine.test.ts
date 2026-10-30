/**
 * End-to-end tests of the conformance engine on DuckDB-WASM's Node build,
 * with fixtures written by DuckDB itself (no ARROW:schema, so column types
 * come from the Parquet schema).
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { checkFile } from '../src/lib/conformance/engine';
import { lit } from '../src/lib/conformance/row-checks';
import { CheckAbortedError, type ConformanceReport, type Progress } from '../src/lib/conformance/types';
import type { Companion } from '../src/lib/conformance/index-file';
import { openDuckDB, type NodeDuckDB } from './helpers/duckdb-node';

const FOOTER = {
  format_version: '1.0.0',
  collection: { id: 'demo', title: 'Demo', license: 'CC-BY-4.0', citation: 'Demo 2026', inclusion: 'complete' },
  datasets: {},
  extensions: ['bandpass'],
  created: '2026-10-29T12:00:00Z',
  generator: 'engine.test.ts',
};

const CORE: Record<string, string> = {
  id: `'demo:s' || lpad(i::VARCHAR, 4, '0')`,
  collection: `'demo'`,
  original_id: `i::VARCHAR`,
  name: `'Sample ' || i`,
  material_name: `'Quartz'`,
  material_category: `'mineral'`,
  quantity: `'reflectance'`,
  geometry: `'directional_hemispherical'`,
  wavelength_unit: `'nm'`,
  wavelengths: `[400.0, 500.0, 600.0]::DOUBLE[]`,
  values: `[0.1, 0.2, 0.3]::FLOAT[]`,
  wavelength_min_nm: `400.0::DOUBLE`,
  wavelength_max_nm: `600.0::DOUBLE`,
  license: `'CC-BY-4.0'`,
  dataset_id: `NULL::VARCHAR`,
};

interface FixtureSpec {
  rows?: number;
  /** Column -> SQL for row 3 only. */
  row3?: Record<string, string>;
  /** Column -> SQL for every row (also adds columns). */
  set?: Record<string, string>;
  drop?: string[];
  footer?: unknown;
  key?: string;
}

let duck: NodeDuckDB;
let dir: string;

async function fixture(name: string, spec: FixtureSpec = {}): Promise<string> {
  const cols = { ...CORE, ...spec.set };
  for (const d of spec.drop ?? []) delete cols[d];
  const select = Object.entries(cols)
    .map(([c, sql]) => {
      const special = spec.row3?.[c];
      return `${special ? `CASE WHEN i = 3 THEN ${special} ELSE ${sql} END` : sql} AS "${c}"`;
    })
    .join(', ');
  const path = join(dir, name);
  const footer = spec.footer === undefined ? FOOTER : spec.footer;
  const kv = footer === null ? '' : `, KV_METADATA {${spec.key ?? 'openspeclib'}: ${lit(typeof footer === 'string' ? footer : JSON.stringify(footer))}}`;
  await duck.query(
    `COPY (SELECT ${select} FROM range(${spec.rows ?? 10}) t(i) ORDER BY i) TO ${lit(path)} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 250${kv})`,
  );
  duck.register(name, path);
  return name;
}

async function run(name: string, spec?: FixtureSpec, opts: Parameters<typeof checkFile>[3] = {}): Promise<ConformanceReport> {
  await fixture(name, spec);
  return checkFile(duck.query, name, name, opts);
}

const failed = (r: ConformanceReport) => r.results.filter((x) => x.status === 'fail').map((x) => x.id);
const result = (r: ConformanceReport, id: string) => r.results.find((x) => x.id === id)!;

beforeAll(async () => {
  duck = await openDuckDB();
  dir = mkdtempSync(join(tmpdir(), 'osl-engine-'));
});
afterAll(() => {
  duck?.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('collection files', () => {
  test('a valid file conforms', async () => {
    const r = await run('valid.parquet');
    expect(failed(r)).toEqual([]);
    expect(r.verdict).toBe('conformant');
    expect(r.kind).toBe('collection');
    expect(r.numRows).toBe(10);
    expect(result(r, 'I1').status).toBe('skip');
  });

  test.each<[string, FixtureSpec, string, string?]>([
    ['F2 null', { row3: { material_name: 'NULL' } }, 'F2', '1 row with null material_name'],
    ['F2 type', { set: { wavelength_min_nm: '400.0::FLOAT' } }, 'F2', 'wavelength_min_nm: float32 (expected float64)'],
    ['F2 empty string', { row3: { name: `''` } }, 'F2', '1 row with an empty or whitespace-only name'],
    ['F8 non-positive', { row3: { wavelengths: '[-1.0, 500.0, 600.0]::DOUBLE[]', wavelength_min_nm: '-1.0::DOUBLE' } }, 'F8', 'wavelengths of 0 or less'],
    ['F3', { set: { comment: `'x'` } }, 'F3'],
    ['F4', { set: { bandpass_fwhm: '[1, 1, 1]::DOUBLE[]' } }, 'F4'],
    ['F5 duplicate', { row3: { id: `'demo:s0002'` } }, 'F5', '1 id used more than once (2 rows)'],
    ['F5 grammar', { row3: { id: `'demo:a/b'` } }, 'F5'],
    ['F6', { row3: { collection: `'other'` } }, 'F6'],
    ['F7', { row3: { geometry: `'sideways'` } }, 'F7'],
    ['F8', { row3: { wavelengths: '[400.0, 600.0, 500.0]::DOUBLE[]' } }, 'F8'],
    ['F9 sentinel', { row3: { values: '[0.1, -9999999999999.0, 0.3]::FLOAT[]' } }, 'F9'],
    ['F9 NaN is fine but infinity is not', { row3: { values: `['NaN'::FLOAT, 'inf'::FLOAT, 0.3]` } }, 'F9', '1 row with infinite values'],
    ['F10', { row3: { wavelength_unit: `'um'` } }, 'F10'],
    ['F11', { row3: { license: `'CC BY'` } }, 'F11'],
    ['F12', { row3: { dataset_id: `'ds2'` } }, 'F12'],
    ['F13', { set: { bandpass_fwhm: '[1, -1, 1]::FLOAT[]' } }, 'F13'],
  ])('%s', async (label, spec, rule, message) => {
    const r = await run(`${label.replace(/\W+/g, '_')}.parquet`, spec);
    expect(failed(r)).toEqual([rule]);
    expect(r.verdict).toBe('not-conformant');
    if (message) expect(result(r, rule).messages.join('\n')).toContain(message);
  });

  test('row failures carry example ids', async () => {
    const r = await run('examples.parquet', { row3: { quantity: `'radiance'` } });
    expect(result(r, 'F7').examples).toEqual(['demo:s0003']);
  });

  test('wavenumber axes convert as 1e7 / x', async () => {
    const r = await run('wavenumber.parquet', {
      set: {
        wavelength_unit: `'cm-1'`,
        wavelengths: '[4000.0, 3000.0, 2000.0]::DOUBLE[]',
        wavelength_min_nm: '2500.0::DOUBLE',
        wavelength_max_nm: '5000.0::DOUBLE',
      },
    });
    expect(failed(r)).toEqual([]);
  });

  test('a missing footer fails F1 and blocks F6', async () => {
    const r = await run('nofooter.parquet', { footer: null });
    expect(r.kind).toBe('unknown');
    expect(failed(r)).toEqual(['F1']);
    expect(result(r, 'F6').status).toBe('skip');
  });

  test('a footer that is not JSON fails F1', async () => {
    const r = await run('badjson.parquet', { footer: '{"format_version": ' });
    expect(failed(r)).toEqual(['F1']);
    expect(result(r, 'F1').messages[0]).toContain('not valid JSON');
  });

  test('warnings: all-NaN spectra; unknown geometry only above half the rows', async () => {
    const r = await run('warn.parquet', {
      row3: { geometry: `'unknown'`, values: `['NaN'::FLOAT, 'NaN'::FLOAT, 'NaN'::FLOAT]` },
    });
    expect(r.verdict).toBe('conformant');
    expect(result(r, 'R4').status).toBe('warn');
    expect(result(r, 'R5').status).toBe('pass');
    expect(result(r, 'R5').messages.join(' ')).toContain('1 row (10 %) has');
    const most = await run('warn-r5.parquet', { set: { geometry: `CASE WHEN i < 6 THEN 'unknown' ELSE 'biconical' END` } });
    expect(result(most, 'R5').status).toBe('warn');
  });

  test('both footer keys fail F1', async () => {
    await fixture('both-keys.parquet');
    const path = join(dir, 'both-keys2.parquet');
    await duck.query(
      `COPY (SELECT * FROM 'both-keys.parquet') TO ${lit(path)} (FORMAT parquet, KV_METADATA {openspeclib: ${lit(JSON.stringify(FOOTER))}, openspeclib_index: '{}'})`,
    );
    duck.register('both-keys2.parquet', path);
    const r = await checkFile(duck.query, 'both-keys2.parquet', 'x');
    expect(failed(r)).toEqual(['F1']);
    expect(result(r, 'F1').messages.join(' ')).toContain('also carries');
  });

  test('quality profile', async () => {
    const spec = { row3: { values: '[10.0, 20.0, 30.0]::FLOAT[]' } };
    const off = await run('qa-off.parquet', spec);
    expect(result(off, 'Q1').status).toBe('skip');
    const on = await run('qa-on.parquet', spec, { qualityProfile: true });
    // Each spectrum is reported under at most one of Q1–Q3. 1 of the 10
    // spectra failing reaches the bounds-share threshold: a Q2 warning.
    expect(['Q1', 'Q2', 'Q3'].map((id) => result(on, id).status)).toEqual(['fail', 'warn', 'pass']);
    expect(result(on, 'Q2').messages.join(' ')).toContain('fail Q1, Q2 or Q4');
    const spike = await run('qa-spike.parquet', { row3: { values: '[0.1, 1.6, 0.3]::FLOAT[]' } }, { qualityProfile: true });
    expect(['Q1', 'Q2', 'Q3'].map((id) => result(spike, id).status)).toEqual(['pass', 'fail', 'pass']);
    const suspect = await run('qa-suspect.parquet', { row3: { values: `[0.1, 1.3, 'NaN'::FLOAT]::FLOAT[]` } }, { qualityProfile: true });
    expect(['Q1', 'Q2', 'Q3'].map((id) => result(suspect, id).status)).toEqual(['pass', 'pass', 'warn']);
    // Q4: an extent in µm stored under wavelength_unit 'nm'.
    const mislabelled = await run('qa-extent.parquet', { set: { wavelengths: '[0.4, 0.5, 0.6]::DOUBLE[]', wavelength_min_nm: '0.4::DOUBLE', wavelength_max_nm: '0.6::DOUBLE' } }, { qualityProfile: true });
    expect(result(mislabelled, 'Q4').status).toBe('fail');
    expect(result(mislabelled, 'Q4').count).toBe(10);
    // The quality profile does not change format conformance.
    expect(on.verdict).toBe('conformant');
  });

  test('vocabulary values that are Object.prototype names do not break the checker', async () => {
    for (const quantity of ['constructor', '__proto__', 'toString']) {
      const r = await run(`proto-${quantity}.parquet`, { set: { quantity: `'${quantity}'` } }, { qualityProfile: true });
      expect(failed(r)).toEqual(['F7']);
      expect(r.verdict).toBe('not-conformant');
    }
  });

  test('a missing core column blocks the rules that need it', async () => {
    const r = await run('nowl.parquet', { drop: ['wavelengths'] });
    expect(failed(r)).toEqual(['F2']);
    expect(result(r, 'F8').status).toBe('skip');
  });

  test('progress is reported in order and ends at 1', async () => {
    const seen: Progress[] = [];
    await run('progress.parquet', { rows: 1200 }, { onProgress: (p) => seen.push(p) });
    const fractions = seen.map((p) => p.fraction ?? 0);
    expect(fractions).toEqual([...fractions].sort((a, b) => a - b));
    expect(fractions.at(-1)).toBe(1);
    expect(seen.some((p) => p.phase === 'Checking rows')).toBe(true);
  });

  test('can be cancelled', async () => {
    await fixture('cancel.parquet');
    const ctl = new AbortController();
    ctl.abort();
    await expect(checkFile(duck.query, 'cancel.parquet', 'x', { signal: ctl.signal })).rejects.toBeInstanceOf(CheckAbortedError);
  });

  test('non-Parquet input', async () => {
    const path = join(dir, 'notes.parquet');
    writeFileSync(path, 'id,values\nfoo,1\n');
    duck.register('notes.parquet', path);
    const r = await checkFile(duck.query, 'notes.parquet', 'notes.parquet');
    expect(r.unreadable).toBeTruthy();
    expect(failed(r)).toEqual(['F1']);
    expect(result(r, 'F2').status).toBe('skip');
  });
});

describe('index files', () => {
  const coreNoArrays = Object.keys(CORE).filter((c) => c !== 'wavelengths' && c !== 'values');

  async function writeIndex(name: string, rowsSql: string, footer: unknown): Promise<string> {
    const path = join(dir, name);
    await duck.query(
      `COPY (${rowsSql}) TO ${lit(path)} (FORMAT parquet, KV_METADATA {openspeclib_index: ${lit(JSON.stringify(footer))}})`,
    );
    duck.register(name, path);
    return name;
  }

  const indexFooter = {
    format_version: '1.0.0',
    created: '2026-10-29T12:00:00Z',
    generator: 'engine.test.ts',
    collections: [{ file: 'idx-coll.parquet', num_spectra: 10, metadata: FOOTER }],
  };
  const select = (name = '"name"', where = '') =>
    `SELECT ${coreNoArrays.map((c) => (c === 'name' ? `${name} AS "name"` : `"${c}"`)).join(', ')},
            len("values")::INTEGER AS num_points, 'idx-coll.parquet' AS file
     FROM 'idx-coll.parquet' ${where}`;
  const resolver = async (f: string): Promise<Companion> =>
    f === 'idx-coll.parquet' ? { status: 'found', ref: f } : { status: 'missing', reason: 'nope' };

  test('a matching index conforms', async () => {
    await fixture('idx-coll.parquet');
    await writeIndex('idx-good.parquet', select(), indexFooter);
    const r = await checkFile(duck.query, 'idx-good.parquet', 'index', { resolveCompanion: resolver });
    expect(r.kind).toBe('index');
    expect(result(r, 'I1').status).toBe('pass');
    expect(result(r, 'F1').status).toBe('skip');
    expect(r.verdict).toBe('conformant');
    expect(r.collections.map((c) => c.verdict)).toEqual(['conformant']);
  });

  test('without its collection files an index does not conform (they cannot be fetched)', async () => {
    await fixture('idx-coll.parquet');
    await writeIndex('idx-alone.parquet', select(), indexFooter);
    const r = await checkFile(duck.query, 'idx-alone.parquet', 'index');
    expect(r.verdict).toBe('not-conformant');
    expect(result(r, 'I1').messages.join(' ')).toContain('could not be read');
    expect(r.collections).toEqual([]);
  });

  test('missing and changed rows fail I1', async () => {
    await fixture('idx-coll.parquet');
    const rows = select(`CASE WHEN "id" = 'demo:s0005' THEN 'changed' ELSE "name" END`, `WHERE "id" <> 'demo:s0002'`);
    await writeIndex('idx-bad.parquet', rows, indexFooter);
    const r = await checkFile(duck.query, 'idx-bad.parquet', 'index', { resolveCompanion: resolver });
    const i1 = result(r, 'I1');
    expect(i1.status).toBe('fail');
    expect(i1.messages.join('\n')).toContain('1 row missing from the index');
    expect(i1.messages.join('\n')).toContain('1 index row whose values differ');
    expect(i1.examples).toEqual(expect.arrayContaining(['demo:s0002', 'demo:s0005']));
  });
});

describe('format additions', () => {
  test('F4: a non-null extra must be a JSON object', async () => {
    const r = await run('extra.parquet', { set: { extra: `CASE WHEN i = 3 THEN '[1, 2]' WHEN i = 4 THEN '{"a": 1}' ELSE NULL END` } });
    expect(failed(r)).toEqual(['F4']);
    expect(result(r, 'F4').examples).toEqual(['demo:s0003']);
  });
  test('F2: string lists hold no null elements', async () => {
    const footer = { ...FOOTER, extensions: ['material'] };
    const r = await run('kw.parquet', { footer, set: { material_keywords: `CASE WHEN i = 3 THEN ['a', NULL] ELSE ['a'] END` } });
    expect(failed(r)).toEqual(['F2']);
    expect(result(r, 'F2').messages.join(' ')).toContain('null elements in material_keywords');
  });
  test('a newer 1.x minor: unknown values, columns and footer members are warnings', async () => {
    const footer = { ...FOOTER, format_version: '1.1.0', future_member: true };
    const r = await run('newer.parquet', { footer, set: { x_ok: '1', future_column: `'x'` }, row3: { geometry: `'retroreflective'` } });
    expect(failed(r)).toEqual([]);
    expect(r.verdict).toBe('conformant');
    expect(['F1', 'F3', 'F7'].map((id) => result(r, id).status)).toEqual(['warn', 'warn', 'warn']);
    // In a 1.0 file the same things fail.
    const old = await run('old.parquet', { footer: { ...FOOTER, future_member: true }, set: { future_column: `'x'` }, row3: { geometry: `'retroreflective'` } });
    expect(failed(old).sort()).toEqual(['F1', 'F3', 'F7']);
  });
});
