import { describe, expect, test } from 'vitest';
import { checkFooter, footerContext } from '../src/lib/conformance/footer';

const valid = {
  format_version: '1.0.0',
  collection: {
    id: 'usgs_splib07',
    title: 'USGS Spectral Library Version 7',
    license: 'LicenseRef-USGS-PublicDomain',
    license_refs: { 'LicenseRef-USGS-PublicDomain': { name: 'Public domain' } },
    citation: 'Kokaly et al. 2017',
    inclusion: 'complete',
  },
  datasets: { ds1: { title: 'Dataset 1', license: 'CC0-1.0' } },
  extensions: ['material', 'sample'],
  created: '2026-10-29T12:00:00Z',
  generator: 'openspeclib 0.2.0',
};
const clone = (): Record<string, any> => JSON.parse(JSON.stringify(valid)); // eslint-disable-line @typescript-eslint/no-explicit-any
const check = (v: unknown) => checkFooter(JSON.stringify(v), 'collection', 'openspeclib');

describe('checkFooter (F1)', () => {
  test('accepts a valid footer', () => {
    expect(check(valid).problems).toEqual([]);
  });

  test('missing key, bad UTF-8, bad JSON, non-object', () => {
    expect(checkFooter(null, 'collection', 'openspeclib').problems[0]).toContain("no 'openspeclib' key");
    expect(checkFooter(new Uint8Array([0xff, 0xfe]), 'collection', 'openspeclib').problems[0]).toContain('UTF-8');
    expect(checkFooter('{nope', 'collection', 'openspeclib').problems[0]).toContain('not valid JSON');
    expect(checkFooter('[1]', 'collection', 'openspeclib').problems[0]).toContain('JSON object');
  });

  test('reports schema errors with paths', () => {
    const f = clone();
    delete f.collection.citation;
    f.collection.extra_field = 1;
    f.extensions = ['material', 'spaceships'];
    const problems = check(f).problems;
    expect(problems).toContain("collection: missing required property 'citation'");
    expect(problems).toContain("collection: unknown property 'extra_field'");
    expect(problems.some((p) => p.startsWith('extensions.1: must be one of'))).toBe(true);
  });

  test('subset needs inclusion_notes', () => {
    const f = clone();
    f.collection.inclusion = 'subset';
    expect(check(f).problems).toEqual(["collection: missing required property 'inclusion_notes'"]);
    f.collection.inclusion_notes = 'Only VNIR spectra.';
    expect(check(f).problems).toEqual([]);
  });

  test('major version must be 1', () => {
    const f = clone();
    f.format_version = '2.0.0';
    expect(check(f).problems[0]).toContain('major version 2');
  });

  test('created must be an RFC 3339 timestamp with a time zone (schema pattern)', () => {
    const f = clone();
    f.created = '2026-10-29';
    expect(check(f).problems[0]).toMatch(/^created: must match pattern/);
    f.created = '2026-10-29T12:00:00';
    expect(check(f).problems[0]).toMatch(/^created: must match pattern/);
    f.created = '2026-10-29T12:00:00.5+02:00';
    expect(check(f).problems).toEqual([]);
  });

  test('created accepts a leap second but not second 61 or 30 February', () => {
    const f = clone();
    f.created = '2016-12-31T23:59:60Z';
    expect(check(f).problems).toEqual([]);
    f.created = '2016-12-31T23:59:61Z';
    expect(check(f).problems.some((p) => p.includes('not a real calendar date'))).toBe(true);
    f.created = '2026-02-30T12:00:00Z';
    expect(check(f).problems.some((p) => p.includes('not a real calendar date'))).toBe(true);
  });

  test('collection id grammar and LicenseRef keys', () => {
    const f = clone();
    f.collection.id = 'USGS';
    f.collection.license_refs = { 'Not-A-Ref': { name: 'x' } };
    const problems = check(f).problems;
    expect(problems.some((p) => p.startsWith('collection.id: must match pattern'))).toBe(true);
    expect(problems.some((p) => p.startsWith('collection.license_refs'))).toBe(true);
  });

  test('index footer', () => {
    const idx = {
      format_version: '1.0.0',
      created: '2026-10-29T12:00:00Z',
      generator: 'x',
      collections: [{ file: 'usgs_splib07.parquet', num_spectra: 3, metadata: valid }],
    };
    expect(checkFooter(JSON.stringify(idx), 'index', 'openspeclib_index').problems).toEqual([]);
    expect(checkFooter(JSON.stringify(valid), 'index', 'openspeclib_index').problems.length).toBeGreaterThan(0);
  });
});

describe('footerContext', () => {
  test('reads ids, extensions, datasets and licences', () => {
    const ctx = footerContext(valid);
    expect(ctx.collectionId).toBe('usgs_splib07');
    expect(ctx.extensions).toEqual(['material', 'sample']);
    expect([...ctx.datasetIds!]).toEqual(['ds1']);
    expect([...ctx.licenseRefs]).toEqual(['LicenseRef-USGS-PublicDomain']);
    expect(ctx.footerLicenses).toEqual([
      ['collection.license', 'LicenseRef-USGS-PublicDomain'],
      ['datasets.ds1.license', 'CC0-1.0'],
    ]);
  });
  test('copes with junk', () => {
    expect(footerContext(null).readable).toBe(false);
    expect(footerContext(null).extensions).toBeNull();
    expect(footerContext({ collection: 3 }).collectionId).toBeNull();
    expect(footerContext({ extensions: ['a', 1] }).extensions).toBeNull();
    expect(footerContext({ datasets: [] }).datasetIds).toBeNull();
    expect(footerContext({}).datasetIds?.size).toBe(0);
    expect(footerContext({ collection: { license: 3 } }).footerLicenses).toEqual([]);
  });
});
