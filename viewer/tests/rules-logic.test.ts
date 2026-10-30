import { describe, expect, test } from 'vitest';
import { checkCollectionColumns } from '../src/lib/conformance/columns';
import { batchRanges, batchTarget, checkLayout } from '../src/lib/conformance/engine';
import { RuleAccumulator, verdictOf } from '../src/lib/conformance/results';
import {
  addBatch,
  batchSql,
  emptyTotals,
  planRowChecks,
  plannedRules,
  recordRowFindings,
  reEscape,
  lit,
  ident,
} from '../src/lib/conformance/row-checks';
import { CORE_COLUMNS } from '../src/format/spec';
import type { FileColumn } from '../src/lib/conformance/types';

const core = (): FileColumn[] => CORE_COLUMNS.map((c) => ({ name: c.name, type: c.type }));
const byId = (acc: RuleAccumulator) => Object.fromEntries(acc.results().map((r) => [r.id, r]));

describe('RuleAccumulator and verdict', () => {
  test('a clean file is conformant', () => {
    const acc = new RuleAccumulator();
    acc.skip('I1', 'na', 'Not applicable');
    expect(verdictOf(acc.results())).toBe('conformant');
  });
  test('MUST issues fail, SHOULD issues warn, Q3 warns', () => {
    const acc = new RuleAccumulator();
    acc.issue('F8', 2, 'bad axis', ['a', 'b']);
    acc.issue('R2', 1, 'big row group');
    acc.issue('Q3', 4, 'suspect');
    const r = byId(acc);
    expect(r.F8.status).toBe('fail');
    expect(r.F8.examples).toEqual(['a', 'b']);
    expect(r.R2.status).toBe('warn');
    expect(r.Q3.status).toBe('warn');
    expect(verdictOf(acc.results())).toBe('not-conformant');
  });
  test('warnings alone keep a file conformant', () => {
    const acc = new RuleAccumulator();
    acc.issue('R1', 3, 'unsorted');
    expect(verdictOf(acc.results())).toBe('conformant');
  });
  test('an unchecked MUST rule makes the verdict incomplete', () => {
    const acc = new RuleAccumulator();
    acc.skip('I1', 'partial', 'files missing');
    const r = byId(acc);
    expect(r.I1.status).toBe('pass');
    expect(r.I1.complete).toBe(false);
    expect(verdictOf(acc.results())).toBe('incomplete');
  });
  test('examples are capped and deduplicated', () => {
    const acc = new RuleAccumulator();
    acc.issue('F5', 3, 'x', ['a', 'a', 'b']);
    acc.issue('F5', 9, 'y', ['c', 'd', 'e', 'f', 'g']);
    const r = byId(acc).F5;
    expect(r.count).toBe(12);
    expect(r.examples).toEqual(['a', 'b', 'c', 'd', 'e']);
  });
});

describe('checkCollectionColumns (F2–F4)', () => {
  test('all core columns with exact types', () => {
    const acc = new RuleAccumulator();
    const { usable } = checkCollectionColumns(core(), [], acc);
    expect(usable.size).toBe(CORE_COLUMNS.length);
    const r = byId(acc);
    expect([r.F2.status, r.F3.status, r.F4.status]).toEqual(['pass', 'pass', 'pass']);
  });
  test('missing and mistyped core columns fail F2', () => {
    const acc = new RuleAccumulator();
    const cols = core()
      .filter((c) => c.name !== 'material_name')
      .map((c) => (c.name === 'values' ? { ...c, type: 'large_list<float64>' } : c));
    const { usable } = checkCollectionColumns(cols, [], acc);
    expect(usable.has('values')).toBe(false);
    const r = byId(acc).F2;
    expect(r.status).toBe('fail');
    expect(r.messages.join(' ')).toContain('material_name');
    expect(r.messages.join(' ')).toContain('values: large_list<float64> (expected list<float32>)');
  });
  test('F3: unknown, index-only and undeclared extension columns; x_ and extra are fine', () => {
    const acc = new RuleAccumulator();
    const cols = [
      ...core(),
      { name: 'x_mine', type: 'int64' },
      { name: 'extra', type: 'string' },
      { name: 'notes', type: 'string' },
      { name: 'file', type: 'string' },
      { name: 'material_formula', type: 'string' },
      { name: 'sample_id', type: 'string' },
    ];
    checkCollectionColumns(cols, ['sample'], acc);
    const r = byId(acc).F3;
    expect(r.status).toBe('fail');
    expect(r.examples.sort()).toEqual(['file', 'material_formula', 'notes']);
  });
  test('F3 without a readable extensions list: names still checked, declarations not', () => {
    const acc = new RuleAccumulator();
    checkCollectionColumns([...core(), { name: 'material_formula', type: 'string' }], null, acc);
    expect(byId(acc).F3.status).toBe('pass');
    const acc2 = new RuleAccumulator();
    checkCollectionColumns([...core(), { name: 'notes', type: 'string' }], null, acc2);
    expect(byId(acc2).F3.status).toBe('fail');
  });
  test('F3: x_ columns must be flat', () => {
    const acc = new RuleAccumulator();
    checkCollectionColumns(
      [...core(), { name: 'x_ok', type: 'list<float32>' }, { name: 'x_s', type: 'struct<a: int32>' }, { name: 'x_l', type: 'list<list<float32>>' }, { name: 'x_m', type: 'map' }],
      [],
      acc,
    );
    expect(byId(acc).F3.examples.sort()).toEqual(['x_l', 'x_m', 'x_s']);
  });
  test('F2/F4: the null type is fine for nullable columns only', () => {
    const acc = new RuleAccumulator();
    const { allNull, usable } = checkCollectionColumns(
      [...core().map((c) => (c.name === 'dataset_id' ? { ...c, type: 'null' } : c)), { name: 'material_formula', type: 'null' }],
      ['material'],
      acc,
    );
    expect(byId(acc).F2.status).toBe('pass');
    expect(byId(acc).F4.status).toBe('pass');
    expect([...allNull].sort()).toEqual(['dataset_id', 'material_formula']);
    expect(usable.has('dataset_id')).toBe(false);
    const acc2 = new RuleAccumulator();
    checkCollectionColumns(core().map((c) => (c.name === 'name' ? { ...c, type: 'null' } : c)), [], acc2);
    expect(byId(acc2).F2.status).toBe('fail');
  });
  test('F2: large, view and dictionary variants equal plain types', () => {
    const acc = new RuleAccumulator();
    const cols = core().map((c) =>
      c.name === 'name' ? { ...c, type: 'large_string' } : c.name === 'values' ? { ...c, type: 'large_list<float32>' } : c.name === 'id' ? { ...c, type: 'string_view' } : c.name === 'quantity' ? { ...c, type: 'dictionary<values=string>' } : c,
    );
    const { usable } = checkCollectionColumns(cols, [], acc);
    expect(byId(acc).F2.status).toBe('pass');
    expect(usable.has('values')).toBe(true);
  });
  test('F2: duplicate core columns', () => {
    const acc = new RuleAccumulator();
    checkCollectionColumns([...core(), { name: 'name', type: 'string' }], [], acc);
    expect(byId(acc).F2.messages[0]).toContain('name appears 2 times');
  });
  test('F4: extension types', () => {
    const acc = new RuleAccumulator();
    const { usable } = checkCollectionColumns(
      [...core(), { name: 'measurement_date', type: 'string' }, { name: 'bandpass_fwhm', type: 'list<float32>' }],
      ['measurement', 'bandpass'],
      acc,
    );
    expect(byId(acc).F4.status).toBe('fail');
    expect(usable.has('measurement_date')).toBe(false);
    expect(usable.has('bandpass_fwhm')).toBe(true);
  });
});

describe('planRowChecks', () => {
  const allCore = new Set(CORE_COLUMNS.map((c) => c.name));
  test('plans every row rule for a complete file', () => {
    const plan = planRowChecks({ usable: allCore, present: allCore, collectionId: 'demo' });
    expect(plannedRules(plan).sort()).toEqual(
      ['F2', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'R4', 'R5'].sort(),
    );
    expect(plan.skips).toEqual([]);
    const sql = batchSql('f.parquet', plan, 0, 250);
    expect(sql).toContain("read_parquet('f.parquet', file_row_number=true)");
    expect(sql).toContain('file_row_number >= 0 AND file_row_number < 250');
    expect(sql).toContain("'^demo:[A-Za-z0-9._~-]{1,200}$'");
    expect(sql).toContain('regexp_full_match("name"');
    expect(sql).toContain('"values"');
  });
  test('decodes every listed column, even those no rule reads (corrupt pages are F1)', () => {
    const plan = planRowChecks({ usable: allCore, present: allCore, collectionId: 'demo' });
    const sql = batchSql('f.parquet', plan, 0, 250, ['x_label', 'quality_notes']);
    expect(sql).toContain('"x_label" IS NOT NULL AS decoded_0');
    expect(sql).toContain('count(*) FILTER (WHERE decoded_1)');
  });
  test('skips rules whose columns are missing', () => {
    const usable = new Set([...allCore].filter((c) => c !== 'wavelengths'));
    const plan = planRowChecks({ usable, present: usable, collectionId: null });
    const rules = plannedRules(plan);
    for (const r of ['F5', 'F6', 'F8', 'F10', 'F13']) expect(rules).not.toContain(r);
    expect(plan.skips.map((s) => s.rule)).toEqual(expect.arrayContaining(['F5', 'F6', 'F8', 'F9', 'F10', 'F13']));
    expect(plan.skips.find((s) => s.rule === 'F8')?.kind).toBe('blocked');
  });
  test('F13 only when the per-band columns exist', () => {
    const usable = new Set([...allCore, 'bandpass_fwhm']);
    const plan = planRowChecks({ usable, present: usable, collectionId: 'demo' });
    expect(plannedRules(plan)).toContain('F13');
    const typed = planRowChecks({
      usable: allCore,
      present: new Set([...allCore, 'values_stddev']),
      collectionId: 'demo',
    });
    expect(typed.notes.some((n) => n.rule === 'F13' && n.note.includes('values_stddev'))).toBe(true);
  });
  test('folds batches and records findings', () => {
    const plan = planRowChecks({ usable: allCore, present: allCore, collectionId: 'demo' });
    const f8 = plan.checks.filter((c) => c.rule === 'F8');
    const totals = emptyTotals();
    const batch = (n: number, short: number, ex: string[]) => ({
      n_rows: n,
      [f8[0].key]: short,
      rule_F8: short,
      ex_F8: ex,
    });
    addBatch(totals, plan, batch(250, 2, ['a', 'b']));
    addBatch(totals, plan, batch(100, 1, ['c']));
    expect(totals.rows).toBe(350);
    const acc = new RuleAccumulator();
    recordRowFindings(totals, plan, acc);
    const r = byId(acc).F8;
    expect(r.status).toBe('fail');
    expect(r.count).toBe(3);
    expect(r.examples).toEqual(['a', 'b', 'c']);
    expect(r.messages[0]).toBe('3 rows with fewer than 2 wavelengths');
  });
});

describe('SQL quoting', () => {
  test('identifiers, literals and regex', () => {
    expect(ident('va"l')).toBe('"va""l"');
    expect(lit("it's")).toBe("'it''s'");
    expect(reEscape('a.b+c')).toBe('a\\.b\\+c');
  });
});

describe('batching', () => {
  test('ranges follow row groups', () => {
    expect(batchRanges([250, 250, 250, 100], 500)).toEqual([
      [0, 500],
      [500, 850],
    ]);
    expect(batchRanges([], 500)).toEqual([]);
    expect(batchRanges([10_000], 500)).toEqual([[0, 10_000]]);
  });
  test('target size', () => {
    expect(batchTarget(100)).toBe(500);
    expect(batchTarget(100_000)).toBe(2500);
    expect(batchTarget(10_000_000)).toBe(10_000);
  });
});

describe('checkLayout (R1 statistics, R2, R3)', () => {
  const chunk = (rowGroup: number, column: string, encodings: string, compression = 'ZSTD', hasStats = true) => ({
    rowGroup,
    rowGroupRows: 250,
    column,
    encodings,
    compression,
    hasStats,
  });
  test('good layout', () => {
    const acc = new RuleAccumulator();
    checkLayout([250, 100], [chunk(0, 'id', 'PLAIN'), chunk(0, 'values', 'RLE, BYTE_STREAM_SPLIT'), chunk(0, 'wavelengths', 'BYTE_STREAM_SPLIT')], acc);
    const r = byId(acc);
    expect([r.R1.status, r.R2.status, r.R3.status]).toEqual(['pass', 'pass', 'pass']);
  });
  test('bad layout', () => {
    const acc = new RuleAccumulator();
    checkLayout([600], [chunk(0, 'id', 'PLAIN', 'ZSTD', false), chunk(0, 'values', 'PLAIN', 'SNAPPY'), chunk(0, 'wavelengths', 'BYTE_STREAM_SPLIT, RLE_DICTIONARY')], acc);
    const r = byId(acc);
    expect(r.R1.status).toBe('warn');
    expect(r.R2.status).toBe('warn');
    expect(r.R2.messages[0]).toContain('largest: 600');
    expect(r.R3.status).toBe('warn');
    expect(r.R3.count).toBe(2);
    expect(r.R3.messages.join(' ')).toContain('dictionary encoded');
  });
});

describe('search and migration edge cases', () => {
  test('a NaN extent never passes a wavelength filter', async () => {
    const { filterRecords } = await import('../src/lib/data/search');
    const r = { wavelength_min_nm: NaN, wavelength_max_nm: NaN, search: '', quantity: 'reflectance' } as never;
    const f = { text: '', categories: [], collections: [], quantity: null, geometries: [], hideSimulated: false, wavelengthMin: 0.4, wavelengthMax: 1, sensorWavelengthMin: null, sensorWavelengthMax: null, sort: null };
    expect(filterRecords([r], f)).toHaveLength(0);
    expect(filterRecords([r], { ...f, wavelengthMin: null, wavelengthMax: null })).toHaveLength(1);
  });
  test("old links with a raw '+' (decoded as a space) still resolve", async () => {
    const { migrateIds } = await import('../src/lib/data/migration');
    const known = new Set(['usgs:Fe3__x', 'usgs:a+b']);
    const map = new Map([['usgs:Fe3+_x', 'usgs:Fe3__x']]);
    expect(migrateIds(['usgs:Fe3 _x', 'usgs:a b'], (id) => known.has(id), map).ids).toEqual(['usgs:Fe3__x', 'usgs:a+b']);
  });
});

describe('index columns and UTF-8 errors', () => {
  test('index columns follow the reference checker', async () => {
    const { checkIndexColumns, INDEX_REQUIRED_COLUMNS } = await import('../src/lib/conformance/index-file');
    const cols: FileColumn[] = INDEX_REQUIRED_COLUMNS.map((c) => ({ name: c.name, type: c.type }));
    const check = (extra: FileColumn[], from = new Set<string>(), forward = false, forwardColumns = new Set<string>()) =>
      checkIndexColumns([...cols, ...extra], from, forward, forwardColumns);
    expect(check([{ name: 'x_note', type: 'string' }])).toEqual({ problems: [], warnings: [] });
    expect(check([{ name: 'x_tags', type: 'list<string>' }])).toEqual({ problems: [], warnings: [] });
    expect(check([{ name: 'x_s', type: 'struct<a: int32>' }]).problems).toEqual(['column x_s is not flat (only primitives or lists of primitives)']);
    // An extension column no listed collection file has.
    expect(check([{ name: 'sample_origin', type: 'string' }]).problems[0]).toContain('no listed collection file has it');
    expect(check([{ name: 'sample_origin', type: 'string' }], new Set(['sample_origin'])).problems).toEqual([]);
    // Unknown columns: an error in a 1.0 index, a warning in a newer minor (or from a newer collection file).
    expect(check([{ name: 'foo', type: 'string' }]).problems).toHaveLength(1);
    expect(check([{ name: 'foo', type: 'string' }], new Set(), true)).toEqual({ problems: [], warnings: [expect.stringContaining('column foo is not defined')] });
    expect(check([{ name: 'foo', type: 'string' }], new Set(['foo']), false, new Set(['foo'])).warnings).toHaveLength(1);
    expect(check([{ name: 'foo', type: 'struct<a: int32>' }], new Set(), true).problems).toHaveLength(1);
  });

  test('recognises DuckDB invalid UTF-8 errors only', async () => {
    const { isInvalidUtf8Error } = await import('../src/lib/conformance/engine');
    expect(isInvalidUtf8Error(new Error('Invalid Input Error: Invalid string encoding found in Parquet file "f": value "a" is not valid UTF8!'))).toBe(true);
    expect(isInvalidUtf8Error(new Error("don't know what type: "))).toBe(false);
  });
});

describe('extra (F4)', () => {
  test('must be a JSON object; NaN and Infinity are not JSON', async () => {
    const { extraProblem } = await import('../src/lib/conformance/engine');
    expect(extraProblem('{"a": 1}')).toBeNull();
    expect(extraProblem('"text"')).toBe('extra is a string, not a JSON object');
    expect(extraProblem('[1, 2]')).toBe('extra is an array, not a JSON object');
    expect(extraProblem('{"a": NaN}')).toBe('extra is not valid JSON');
    expect(extraProblem('Infinity')).toBe('extra is not valid JSON');
    expect(extraProblem('{oops')).toBe('extra is not valid JSON');
  });
});

describe('cleanError', () => {
  test('keeps the first line, names the file by its label and escapes control characters', async () => {
    const { cleanError } = await import('../src/lib/conformance/engine');
    expect(cleanError(new Error("don't know what type: \u000f in check-1-a.parquet\nLINE 1: SELECT"), 'check-1-a.parquet', 'a.parquet')).toBe(
      "don't know what type: \\x0F in a.parquet",
    );
  });
});

describe('allowlist (quality profile)', () => {
  test('selector rules as the Python validator', async () => {
    const { allowlistEntryError } = await import('../src/lib/conformance/quality');
    const base = { checks: ['value_out_of_bounds'], reason: 'why' };
    expect(allowlistEntryError({ ...base, id: 'c1:x' })).toBeNull();
    expect(allowlistEntryError({ ...base, id_prefix: 'c1:' })).toContain('nothing after the collection id');
    expect(allowlistEntryError({ ...base, id_prefix: 'c1:splib07a_' })).toBeNull();
    // Q1 and Q4 checks by exact id only.
    expect(allowlistEntryError({ ...base, id_prefix: 'c1:a', checks: ['value_scale'] })).toContain('cannot be waived');
    expect(allowlistEntryError({ ...base, id: 'c1:a', checks: ['value_scale', 'wavelength_extent'] })).toBeNull();
    expect(allowlistEntryError({ ...base, id: 'c1:a', reason: ' ' })).toContain('reason');
    expect(allowlistEntryError({ ...base, id: 'c1:a', id_prefix: 'c1:b' })).toContain('exactly one');
  });
  test('the most specific entry wins: id, then the longest prefix, then dataset', async () => {
    const { Allowlist } = await import('../src/lib/conformance/quality');
    const list = new Allowlist([
      { dataset: 'c1:d', checks: ['value_out_of_bounds'], reason: 'dataset' },
      { id_prefix: 'c1:ab', checks: ['value_out_of_bounds'], reason: 'short prefix' },
      { id_prefix: 'c1:abc', checks: ['value_out_of_bounds'], reason: 'long prefix' },
      { id: 'c1:abcd', checks: ['value_out_of_bounds'], reason: 'id' },
      { id: 'c1:q', quantity: 'emissivity', checks: ['value_out_of_bounds'], reason: 'emissivity only' },
    ]);
    expect(list.match('c1:abcd', 'c1:d', 'reflectance', 'value_out_of_bounds')?.reason).toBe('id');
    expect(list.match('c1:abcz', 'c1:d', 'reflectance', 'value_out_of_bounds')?.reason).toBe('long prefix');
    expect(list.match('c1:zzz', 'c1:d', 'reflectance', 'value_out_of_bounds')?.reason).toBe('dataset');
    expect(list.match('c1:zzz', null, 'reflectance', 'value_out_of_bounds')).toBeNull();
    expect(list.match('c1:q', null, 'reflectance', 'value_out_of_bounds')).toBeNull();
    expect(list.match('c1:abcd', null, 'reflectance', 'value_range_suspect')).toBeNull();
  });
  test('the shipped allowlist loads without invalid entries', async () => {
    const { Allowlist } = await import('../src/lib/conformance/quality');
    const doc = (await import('@allowlist')).default as unknown as { entries: never[] };
    expect(new Allowlist(doc.entries).problems).toEqual([]);
  });
});

describe('index file references', () => {
  test('relative paths and http(s) URLs; a backslash is part of a name', async () => {
    const { fileReferenceError, resolvedFileKey } = await import('../src/lib/conformance/index-file');
    expect(fileReferenceError('a.parquet')).toBeNull();
    expect(fileReferenceError('sub/a.parquet')).toBeNull();
    expect(fileReferenceError('\\c2.parquet')).toBeNull();
    expect(fileReferenceError('https://example.org/a.parquet')).toBeNull();
    // Bracketed hosts, as Python's urlsplit judges them.
    expect(fileReferenceError('http://[::1]/x.parquet')).toBeNull();
    expect(fileReferenceError('a[1].parquet')).toBeNull();
    for (const bad of ['http://[abc/x.parquet', 'http://a]/x.parquet', 'http://[zz]/x.parquet', '//[x/y.parquet']) {
      expect(fileReferenceError(bad)).not.toBeNull();
    }
    expect(fileReferenceError('/abs/a.parquet')).toContain('absolute');
    expect(fileReferenceError('C:\\data\\a.parquet')).toContain('absolute');
    expect(fileReferenceError('\\\\server\\share\\a.parquet')).toContain('absolute');
    expect(fileReferenceError('C:a.parquet')).toContain('only relative');
    expect(fileReferenceError('file:///a.parquet')).toContain('only relative');
    expect(fileReferenceError('s3://bucket/a.parquet')).toContain('only relative');
    expect(resolvedFileKey('\\c2.parquet')).toBe('http://index.invalid/dir/%5Cc2.parquet');
    // Python's urlsplit and PureWindowsPath decide these (reviewer's reference cases).
    const ABS = 'absolute';
    const REL = 'only relative';
    const cases: Array<[string, string | null]> = [
      [' mailto:c.parquet', REL],
      [' C:\\x.parquet', REL],
      ['\\\\server', REL],
      ['\\\\server\\share', ABS],
      ['//server', ABS],
      ['\t/abs.parquet', null],
      ['http:\\\\h\\x', REL],
      ['\\\\?\\C:\\x', ABS],
      ['\u00e9:x', REL],
      ['_x:y', null],
      ['https://', REL],
      [' https://h/x', null],
      ['\u00a0C:\\x', null],
      ['\u001fC:\\x', REL],
      ['\\', null],
      ['h\ttp://x', REL],
      ['ht\ntp:x', REL],
    ];
    for (const [ref, want] of cases) {
      const got = fileReferenceError(ref);
      expect(got === null ? null : got.startsWith('absolute') ? ABS : REL, JSON.stringify(ref)).toBe(want);
    }
    expect(resolvedFileKey('./a.parquet')).toBe(resolvedFileKey('a.parquet'));
  });
});

describe('exact JSON equality (as Python json.loads and ==)', () => {
  test('bools equal ints, big ints compare exactly, key order ignored', async () => {
    const { parseJsonExact: p, pyJsonEqual: eq } = await import('../src/lib/conformance/json-exact');
    expect(eq(p('true'), p('1'))).toBe(true);
    expect(eq(p('false'), p('0.0'))).toBe(true);
    expect(eq(p('true'), p('false'))).toBe(false);
    expect(eq(p('9007199254740993'), p('9007199254740992'))).toBe(false);
    expect(eq(p('9007199254740993'), p('9007199254740993'))).toBe(true);
    expect(eq(p('9007199254740993'), p('9007199254740992.0'))).toBe(false);
    expect(eq(p('2'), p('2.0'))).toBe(true);
    expect(eq(p('1.5'), p('15e-1'))).toBe(true);
    expect(eq(p('{"a": [1, "x"], "b": null}'), p('{"b": null, "a": [true, "x"]}'))).toBe(true);
    expect(eq(p('{"constructor": 1}'), p('{"toString": 1}'))).toBe(false);
    expect(eq(p('{"a": 1, "a": 2}'), p('{"a": 2}'))).toBe(true);
    expect(eq(p('"1"'), p('1'))).toBe(false);
    expect(eq(p('null'), p('0'))).toBe(false);
    expect(() => p('{"a": 1,}')).toThrow(SyntaxError);
    expect(() => p('"a\\u00"')).toThrow();
  });
});
