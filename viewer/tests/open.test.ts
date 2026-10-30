/**
 * "Open a collection": the decisions (already open, too big, not
 * conformant, warnings only) on DuckDB-WASM's Node build, and removing an
 * opened source from the app state.
 */
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { openEntries, peek, type OpenEntry } from '../src/lib/data/open';
import { friendlyError } from '../src/lib/data/errors';
import { appReducer, initialState, type AppState } from '../src/state/types';
import { openDuckDB, type NodeDuckDB } from './helpers/duckdb-node';

const CORPUS = join(dirname(fileURLToPath(import.meta.url)), '../../tests/conformance/fixtures');
const has = (f: string) => existsSync(join(CORPUS, f));

describe.skipIf(!has('valid_full.parquet') || !has('f9_sentinel.parquet') || !has('r5_unknown_geometry.parquet'))('openEntries', () => {
  let duck: NodeDuckDB;
  const entry = (file: string, label = file): OpenEntry => {
    duck.register(label, join(CORPUS, file));
    return {
      label,
      ref: label,
      kind: 'local',
      resolve: async () => ({ status: 'unavailable', reason: 'n/a' }),
      fileRef: (f) => f,
      files: [label],
    };
  };
  beforeAll(async () => {
    duck = await openDuckDB();
  });
  afterAll(() => duck?.close());

  test('opens conforming files, refuses failing ones and repeated collection ids', async () => {
    const loadedCollections = new Set<string>();
    const out = await openEntries(
      [entry('valid_full.parquet'), entry('f9_sentinel.parquet'), entry('valid_full.parquet', 'copy_of_valid_full.parquet'), entry('r5_unknown_geometry.parquet')],
      { check: true, loadedCollections, onProgress: () => undefined },
      duck.query,
      duck.engine,
    );
    expect(out.loaded.map((l) => l.source.label)).toEqual(['valid_full.parquet', 'r5_unknown_geometry.parquet']);
    expect(out.refused.map((r) => r.label)).toEqual(['f9_sentinel.parquet', 'copy_of_valid_full.parquet']);
    expect(out.refused[0].reason).toContain('F9');
    expect(out.refused[1].reason).toContain('already open');
    expect(out.warnings).toEqual([{ label: 'r5_unknown_geometry.parquet', rules: ['R5'] }]);
    // The repeated id was refused from the footer alone: no second report.
    expect(out.reports.map((r) => r.label)).toEqual(['valid_full.parquet', 'f9_sentinel.parquet', 'r5_unknown_geometry.parquet']);
    expect(loadedCollections.size).toBe(2);
  });

  test('peek reads collection ids and the row count from the footer', async () => {
    const e = entry('valid_full.parquet', 'peek.parquet');
    const p = await peek(duck.query, e.ref);
    expect(p.ids).toEqual(['conformance_full']);
    expect(p.rows).toBeGreaterThan(0);
  });

  test('a non-Parquet file is refused with a readable reason', async () => {
    const out = await openEntries([entry('f1_not_parquet.parquet', 'notes.parquet')], { check: true, loadedCollections: new Set(), onProgress: () => undefined }, duck.query, duck.engine);
    expect(out.refused[0].reason).toMatch(/not a Parquet file/);
    expect(out.refused[0].reason).not.toMatch(/SELECT|parquet_kv_metadata/);
  });
});

describe('friendlyError', () => {
  test('keeps the first line, drops SQL and internal names', () => {
    const e = new Error("Invalid Input Error: No magic bytes found at end of file 'open-3-x.parquet'\nLINE 1: SELECT * FROM read_parquet('open-3-x.parquet')");
    expect(friendlyError(e, [['open-3-x.parquet', 'x.parquet']])).toBe("Invalid Input Error: No magic bytes found at end of file 'x.parquet'");
  });
});

describe('REMOVE_SOURCE', () => {
  test('drops the source, its collections, rows, library entries and filters', () => {
    const rec = (id: string, sourceKey: string, collection: string) => ({ id, sourceKey, collection }) as never;
    let s: AppState = {
      ...initialState,
      sources: new Map([
        ['release', { key: 'release', kind: 'release', collections: ['a'] }],
        ['open-1', { key: 'open-1', kind: 'local', collections: ['b'] }],
      ]) as never,
      collections: new Map([['a', {}], ['b', {}]]) as never,
      records: [rec('a:1', 'release', 'a'), rec('b:1', 'open-1', 'b')] as AppState['records'],
      libraryIds: ['a:1', 'b:1'],
      librarySpectra: [{ id: 'b:1' }] as never,
      selectedSources: ['b'],
      dataLoaded: true,
      openState: { running: false, label: 'b.parquet', phase: '', fraction: null, error: null, messages: ['Opened b.parquet'] },
    };
    s = appReducer(s, { type: 'REMOVE_SOURCE', key: 'open-1' });
    expect(s.openState).toBeNull();
    expect([...s.sources.keys()]).toEqual(['release']);
    expect([...s.collections.keys()]).toEqual(['a']);
    expect(s.records.map((r) => r.id)).toEqual(['a:1']);
    expect(s.libraryIds).toEqual(['a:1']);
    expect(s.librarySpectra).toEqual([]);
    expect(s.selectedSources).toEqual([]);
    // The release cannot be removed.
    expect(appReducer(s, { type: 'REMOVE_SOURCE', key: 'release' })).toBe(s);
  });
});
