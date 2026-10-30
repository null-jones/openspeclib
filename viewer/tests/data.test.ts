/**
 * The explorer's data layer against the real release files in public/data,
 * on DuckDB-WASM's Node build.
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { loadSource, type LoadedSource } from '../src/lib/data/loader';
import { fetchDetails, fetchSpectra, parseExtra } from '../src/lib/data/spectra';
import { searchRecords, type SearchFilters } from '../src/lib/data/search';
import { migrateIds, parseMigrationCsv } from '../src/lib/data/migration';
import { attributionLines, buildAttribution } from '../src/lib/data/attribution';
import type { CollectionMeta, DataSource } from '../src/lib/data/types';
import { buildCsv, buildEnvi, exportAttribution, NODATA } from '../src/lib/export';
import { openDuckDB, type NodeDuckDB } from './helpers/duckdb-node';

const DATA = join(dirname(fileURLToPath(import.meta.url)), '../public/data');
const INDEX = join(DATA, 'openspeclib-index.parquet');
const available = existsSync(INDEX);

const NO_FILTERS: SearchFilters = {
  text: '',
  categories: [],
  collections: [],
  quantity: null,
  geometries: [],
  hideSimulated: false,
  wavelengthMin: null,
  wavelengthMax: null,
  sensorWavelengthMin: null,
  sensorWavelengthMax: null,
  sort: null,
};

describe.skipIf(!available)('release data (public/data)', () => {
  let duck: NodeDuckDB;
  let loaded: LoadedSource;
  let sources: Map<string, DataSource>;
  let collections: Map<string, CollectionMeta>;

  beforeAll(async () => {
    duck = await openDuckDB();
    duck.register('openspeclib-index.parquet', INDEX);
    for (const f of ['usgs_splib07', 'ecostress', 'ecosis']) duck.register(`data/${f}.parquet`, join(DATA, `${f}.parquet`));
    loaded = await loadSource(duck.engine, {
      key: 'release',
      label: 'OpenSpecLib',
      kind: 'release',
      ref: 'openspeclib-index.parquet',
      resolveFile: (f) => `data/${f}`,
    });
    sources = new Map([[loaded.source.key, loaded.source]]);
    collections = new Map(loaded.collections.map((c) => [c.id, c]));
  }, 60_000);
  afterAll(() => duck?.close());

  test('loads every index row and the collection footers', () => {
    const total = loaded.collections.reduce((s, c) => s + c.numSpectra, 0);
    expect(loaded.records).toHaveLength(total);
    expect(loaded.records.length).toBeGreaterThan(20_000);
    expect(loaded.source.release).toMatch(/^\d+\.\d+\.\d+$/);
    expect(loaded.collections.map((c) => c.id).sort()).toEqual(['ecosis', 'ecostress', 'usgs_splib07']);
    const r = loaded.records.find((x) => x.collection === 'usgs_splib07')!;
    expect(r.num_points).toBeGreaterThan(0);
    expect(r.search).toContain(r.material_name.toLowerCase());
    expect(loaded.records.some((x) => x.simulated)).toBe(true);
  });

  test('fetches spectra from each collection file with wavelengths inline', async () => {
    const picks = ['usgs_splib07', 'ecostress', 'ecosis'].map((c) => loaded.records.find((r) => r.collection === c)!);
    const spectra = await fetchSpectra(duck.engine, picks, sources);
    expect(spectra.map((s) => s.id)).toEqual(picks.map((p) => p.id));
    for (const s of spectra) {
      expect(s.wavelengths.length).toBe(s.num_points);
      expect(s.values.length).toBe(s.wavelengths.length);
    }
  });

  test('details hold every non-array column', async () => {
    const r = loaded.records.find((x) => x.dataset_id)!;
    const d = await fetchDetails(duck.engine, r, loaded.source);
    expect(d?.id).toBe(r.id);
    expect(d).toHaveProperty('measurement_processing');
    expect(d).not.toHaveProperty('values');
  });

  test('search, filters and sort', () => {
    const all = searchRecords(loaded.records, NO_FILTERS, 0, 50);
    expect(all.total).toBe(loaded.records.length);
    const quartz = searchRecords(loaded.records, { ...NO_FILTERS, text: 'quartz' }, 0, 1000);
    expect(quartz.total).toBeGreaterThan(0);
    expect(quartz.results.every((r) => r.search.includes('quartz'))).toBe(true);
    const trans = searchRecords(loaded.records, { ...NO_FILTERS, quantity: 'transmittance' }, 0, 10);
    expect(trans.results.every((r) => r.quantity === 'transmittance')).toBe(true);
    const noSim = searchRecords(loaded.records, { ...NO_FILTERS, hideSimulated: true }, 0, 10);
    expect(noSim.total).toBe(loaded.records.filter((r) => !r.simulated).length);
    const tir = searchRecords(loaded.records, { ...NO_FILTERS, wavelengthMin: 8, wavelengthMax: 14 }, 0, 10);
    expect(tir.results.every((r) => r.wavelength_max_nm >= 8000 && r.wavelength_min_nm <= 14000)).toBe(true);
    const sorted = searchRecords(loaded.records, { ...NO_FILTERS, sort: { key: 'range', dir: 'desc' } }, 0, 5);
    expect(sorted.results[0].wavelength_min_nm).toBeGreaterThanOrEqual(sorted.results[4].wavelength_min_nm);
  });

  test('old ids from id-migration.csv map onto index ids', () => {
    const map = parseMigrationCsv(readFileSync(join(DATA, 'id-migration.csv'), 'utf8'));
    expect(map.size).toBeGreaterThan(1000);
    const known = new Set(loaded.records.map((r) => r.id));
    const [oldId, newId] = [...map.entries()].find(([, n]) => known.has(n))!;
    const res = migrateIds([oldId, 'nope:gone', newId], (id) => known.has(id), map);
    expect(res.ids).toEqual([newId]);
    expect(res.migrated).toBe(1);
    expect(res.missing).toEqual(['nope:gone']);
  });

  test('CSV and ENVI exports carry attribution, native units and NaN', async () => {
    const picks = [
      loaded.records.find((r) => r.collection === 'usgs_splib07' && r.wavelength_unit === 'um')!,
      loaded.records.find((r) => r.collection === 'ecosis' && r.dataset_id)!,
    ];
    const spectra = await fetchSpectra(duck.engine, picks, sources);
    const items = spectra.map((spectrum) => ({ spectrum, downsampled: null }));
    const attribution = exportAttribution(items, collections);
    const csv = buildCsv(items, false, attribution);
    expect(csv.startsWith('# Attribution:')).toBe(true);
    expect(csv).toContain('# Collection: USGS Spectral Library Version 7');
    expect(csv).toContain('#   Dataset: ');
    expect(csv).toContain(`# id: ${picks[0].id}`);
    for (const s of spectra) expect(csv).toContain(`wavelength_${s.wavelength_unit.replace('-', '')},${s.quantity}`);
    const dataLines = csv.split('\n').filter((l) => /^[0-9.eE+-]+,/.test(l));
    expect(dataLines.length).toBe(spectra[0].wavelengths.length + spectra[1].wavelengths.length);

    const envi = buildEnvi(items, false, attribution);
    const samples = Number(/samples = (\d+)/.exec(envi.hdr)![1]);
    expect(envi.sli.byteLength).toBe(samples * 2 * 4);
    expect(envi.hdr).toContain('attribution.txt');
    expect(envi.attribution).toContain(picks[1].id);
    expect(envi.attribution).toContain(picks[0].license);
    // Values outside the second spectrum's axis are no-data, not extrapolated.
    const v = new Float32Array(envi.sli);
    expect(Array.from(v).some((x) => x === NODATA)).toBe(true);
  });

  test('attribution names collections, licences and EcoSIS datasets', () => {
    const picks = [
      loaded.records.find((r) => r.collection === 'usgs_splib07')!,
      ...loaded.records.filter((r) => r.collection === 'ecosis' && r.dataset_id).slice(0, 3),
    ];
    const uses = buildAttribution(picks, collections);
    const ecosis = uses.find((u) => u.id === 'ecosis')!;
    expect(ecosis.datasets.length).toBeGreaterThan(0);
    expect(ecosis.datasets[0].title).not.toBe(ecosis.datasets[0].id);
    const usgs = uses.find((u) => u.id === 'usgs_splib07')!;
    expect(usgs.licenses[0].name).toBeTruthy();
    const text = attributionLines(uses, 'test').join('\n');
    expect(text).toContain('USGS Spectral Library Version 7');
    expect(text).toContain('Citation:');
  });
});

const CORPUS = join(dirname(fileURLToPath(import.meta.url)), '../../tests/conformance/fixtures');

describe.skipIf(!existsSync(join(CORPUS, 'valid_full.parquet')))('opening a single collection file', () => {
  test('rows, footer and spectra come from the file itself', async () => {
    const duck = await openDuckDB();
    duck.register('mine.parquet', join(CORPUS, 'valid_full.parquet'));
    const loaded = await loadSource(duck.engine, { key: 'open-1', label: 'valid_full.parquet', kind: 'local', ref: 'mine.parquet' });
    expect(loaded.collections).toHaveLength(1);
    expect(loaded.collections[0].numSpectra).toBe(loaded.records.length);
    expect(new Set(loaded.records.map((r) => r.geometry)).size).toBeGreaterThan(1);
    expect(loaded.records.every((r) => r.file === 'valid_full.parquet' && r.num_points === null)).toBe(true);
    const spectra = await fetchSpectra(duck.engine, loaded.records.slice(0, 2), new Map([[loaded.source.key, loaded.source]]));
    expect(spectra).toHaveLength(2);
    expect(spectra[0].values.length).toBe(spectra[0].wavelengths.length);
    duck.close();
  });

  test('a Parquet file without the footer is refused', async () => {
    const duck = await openDuckDB();
    duck.register('nofooter.parquet', join(CORPUS, 'f1_missing_footer.parquet'));
    await expect(loadSource(duck.engine, { key: 'x', label: 'nofooter.parquet', kind: 'local', ref: 'nofooter.parquet' })).rejects.toThrow(/not an OpenSpecLib Format file/);
    duck.close();
  });
});

describe('parseExtra', () => {
  test('masked-band provenance', () => {
    const { mask, json } = parseExtra('{"masked_band_count":2,"source_missing_band_count":1,"masked_bands":[[1414.87,1.41],[1500,9]]}');
    expect(json).not.toBeNull();
    expect(mask.maskedBandCount).toBe(2);
    expect(mask.sourceMissingBandCount).toBe(1);
    expect(mask.maskedBands).toEqual([
      [1414.87, 1.41],
      [1500, 9],
    ]);
  });
  test('junk', () => {
    expect(parseExtra('nope').json).toBeNull();
    expect(parseExtra(null).mask.maskedBandCount).toBeNull();
    expect(parseExtra('[1]').json).toBeNull();
  });
});
