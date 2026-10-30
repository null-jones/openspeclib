import { describe, expect, test } from 'vitest';
import { toMicrometersWithValues } from '../src/lib/wavelength-utils';
import { downsampleSpectrum, MIN_VALID_SRF_SHARE } from '../src/lib/downsampling';
import { buildEnvi, NODATA } from '../src/lib/export';
import type { SpectrumFull } from '../src/lib/data/types';

const spectrum = (over: Partial<SpectrumFull>): SpectrumFull => ({
  id: 'c:a', sourceKey: 'release', collection: 'c', name: 'n', material_name: 'm', material_category: 'mineral',
  material_formula: null, sample_description: null, quantity: 'reflectance', geometry: 'unknown', wavelength_unit: 'nm',
  wavelength_min_nm: 400, wavelength_max_nm: 700, num_points: 4, license: 'CC-BY-4.0', dataset_id: null, simulated: false,
  file: 'c.parquet', search: '', wavelengths: [400, 500, 600, 700], values: [0.1, 0.2, 0.3, 0.4], ...over,
});

describe('toMicrometersWithValues', () => {
  test.each([
    ['nm ascending', [400, 500, 600], 'nm'],
    ['nm descending', [600, 500, 400], 'nm'],
    ['um descending', [0.6, 0.5, 0.4], 'um'],
    ['cm-1 descending (wavelength ascending)', [25000, 20000, 16666.666666666668], 'cm-1'],
    ['cm-1 ascending (wavelength descending)', [16666.666666666668, 20000, 25000], 'cm-1'],
  ] as const)('%s comes out ascending with values moved along', (_, wl, unit) => {
    const values = wl.map((w) => w * 10); // tag each value with its own wavelength
    const out = toMicrometersWithValues([...wl], values, unit);
    expect(out.wavelengths.map((w) => Number(w.toFixed(6)))).toEqual([0.4, 0.5, 0.6]);
    out.values.forEach((v, i) => {
      const original = wl[values.indexOf(v)];
      const um = unit === 'nm' ? original / 1000 : unit === 'cm-1' ? 10000 / original : original;
      expect(um).toBeCloseTo(out.wavelengths[i], 9);
    });
  });
});

describe('downsampleSpectrum', () => {
  const flat = (n: number, lo: number, hi: number, v = 0.5) => ({
    wl: Array.from({ length: n }, (_, i) => lo + ((hi - lo) * i) / (n - 1)),
    val: Array.from({ length: n }, () => v),
  });
  const band = { name: 'b', centerWavelength: 0.55, fwhm: 0.04 } as never;

  test('a flat spectrum gives its value', () => {
    const { wl, val } = flat(401, 0.4, 0.8);
    expect(downsampleSpectrum(wl, val, [band])[0].value).toBeCloseTo(0.5, 6);
  });
  test('the same spectrum stored descending gives the same value', () => {
    const { wl, val } = flat(401, 0.4, 0.8);
    const desc = toMicrometersWithValues(wl.map((w) => w * 1000).reverse(), [...val].reverse(), 'nm');
    expect(downsampleSpectrum(desc.wavelengths, desc.values, [band])[0].value).toBeCloseTo(0.5, 6);
  });
  test('a band hanging off the end of the axis, or over a gap, is null', () => {
    const { wl, val } = flat(401, 0.4, 0.56);
    expect(downsampleSpectrum(wl, val, [band])[0].value).toBeNull();
    const { wl: w2, val: v2 } = flat(401, 0.4, 0.8);
    for (let i = 0; i < w2.length; i++) if (Math.abs(w2[i] - 0.55) < 0.015) v2[i] = NaN;
    expect(downsampleSpectrum(w2, v2, [band])[0].value).toBeNull();
    expect(MIN_VALID_SRF_SHARE).toBeGreaterThan(0.5);
  });
  test('a small gap in the wings still counts', () => {
    const { wl, val } = flat(401, 0.4, 0.8);
    val[0] = NaN;
    expect(downsampleSpectrum(wl, val, [band])[0].value).toBeCloseTo(0.5, 6);
  });
});

describe('ENVI export with descending axes', () => {
  test('a descending µm axis lands on the same grid', () => {
    const a = spectrum({});
    const b = spectrum({ id: 'c:b', wavelength_unit: 'um', wavelengths: [0.7, 0.6, 0.5, 0.4], values: [0.4, 0.3, 0.2, 0.1] });
    const lines = new Float32Array(buildEnvi([{ spectrum: a, downsampled: null }, { spectrum: b, downsampled: null }], false, []).sli);
    expect(Array.from(lines.slice(4, 8)).map((x) => Number(x.toFixed(4)))).toEqual([0.1, 0.2, 0.3, 0.4]);
  });
  test('a descending cm-1 axis interpolates, NODATA outside it', () => {
    const a = spectrum({ wavelengths: [400, 500, 600, 700] });
    const b = spectrum({ id: 'c:b', wavelength_unit: 'cm-1', wavelengths: [25000, 20000, 16666.666666666668], values: [0.1, 0.2, 0.3] });
    const lines = new Float32Array(buildEnvi([{ spectrum: a, downsampled: null }, { spectrum: b, downsampled: null }], false, []).sli);
    const line2 = Array.from(lines.slice(4, 8));
    expect(line2.slice(0, 3).map((x) => Number(x.toFixed(4)))).toEqual([0.1, 0.2, 0.3]);
    expect(line2[3]).toBe(NODATA);
  });
});

describe('export attribution and formatting', () => {
  test('LicenseRef text, licence note, dataset DOI and URL; CSV comment lines; float32 digits', async () => {
    const { attributionLines, buildAttribution } = await import('../src/lib/data/attribution');
    const { buildCsv } = await import('../src/lib/export');
    const meta = {
      id: 'c', sourceKey: 'release', file: 'c.parquet', numSpectra: 1,
      footer: {
        format_version: '1.0.0', extensions: [], created: '', generator: '',
        collection: {
          id: 'c', title: 'C', license: 'NOASSERTION', license_note: 'Per-row licence is authoritative.', citation: 'Cite C\nsecond line', inclusion: 'complete',
          license_refs: { 'LicenseRef-X': { name: 'X terms', text: 'Only for research.', url: 'https://example.org/x' } },
        },
        datasets: { d1: { title: 'D1', citation: 'Cite D1', url: 'https://example.org/d1', doi: '10.1/d1' } },
      },
    };
    const s = spectrum({ license: 'LicenseRef-X', dataset_id: 'd1', values: [Math.fround(0.1), NaN, Math.fround(0.3), Math.fround(0.4)] });
    const lines = attributionLines(buildAttribution([s], new Map([['c', meta as never]])), 'test');
    const text = lines.join('\n');
    expect(text).toContain('LicenseRef-X: Only for research. (https://example.org/x)');
    expect(text).toContain('Licence note: Per-row licence is authoritative.');
    expect(text).toContain('DOI: https://doi.org/10.1/d1');
    expect(text).toContain('URL: https://example.org/d1');
    const csv = buildCsv([{ spectrum: s, downsampled: null }], false, lines);
    const header = csv.split('\n').filter((l) => !/^[0-9wN]/.test(l));
    expect(header.every((l) => l === '' || l.startsWith('#'))).toBe(true);
    expect(csv).toContain('# second line');
    expect(csv).toContain('400,0.1\n500,NaN\n600,0.3');
  });
});

describe('downsampled exports keep every spectrum', () => {
  test('spectra without downsampled values are written as missing and named', async () => {
    const { buildCsv } = await import('../src/lib/export');
    const a = spectrum({});
    const b = spectrum({ id: 'c:b' });
    const ds = [{ bandName: 'B1', centerWavelength: 0.5, value: 0.25 }] as never;
    const csv = buildCsv([{ spectrum: a, downsampled: ds }, { spectrum: b, downsampled: null }], true, []);
    expect(csv).toContain('# Not downsampled');
    expect(csv).toContain('c:b');
    expect(csv.trim().split('\n').at(-1)).toMatch(/,NaN$/);
    const env = buildEnvi([{ spectrum: a, downsampled: ds }, { spectrum: b, downsampled: null }], true, []);
    expect(Array.from(new Float32Array(env.sli))).toEqual([0.25, NODATA]);
    expect(env.attribution).toContain('Not downsampled');
  });
  test('the export date is the local date', async () => {
    const { localDate } = await import('../src/lib/data/attribution');
    expect(localDate(new Date(2026, 0, 2, 23, 30))).toBe('2026-01-02');
  });
});
