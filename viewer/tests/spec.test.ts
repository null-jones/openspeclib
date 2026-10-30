import { describe, expect, test } from 'vitest';
import {
  COLUMNS,
  CORE_COLUMNS,
  EXTENSIONS,
  EXTENSION_COLUMNS,
  FOOTER_SCHEMA,
  LOCAL_ID_PATTERN,
  RULES,
  VOCABULARIES,
  VOCABULARY_COLUMNS,
} from '../src/format/spec';
import { UNIT_TO_NM } from '../src/lib/conformance/row-checks';
import { INDEX_REQUIRED_COLUMNS } from '../src/lib/conformance/index-file';
import { QA_WAVELENGTH_NM } from '../src/format/spec';
import { PLAUSIBLE_NM } from '../src/lib/data/search';

// The checker hard-codes a few things the JSON only states in prose. These
// tests fail if the spec files change underneath it.
describe('spec files vs checker assumptions', () => {
  test('F5 local id grammar', () => {
    expect(RULES.find((r) => r.id === 'F5')?.text).toContain(`{collection}:${LOCAL_ID_PATTERN}`);
  });
  test('string-list columns and the implemented version come from the spec files', async () => {
    const { STRING_LIST_COLUMNS } = await import('../src/lib/conformance/row-checks');
    const { IMPLEMENTED_VERSION, IMPLEMENTED_MINOR, isNewerMinor } = await import('../src/lib/conformance/footer');
    expect(STRING_LIST_COLUMNS).toEqual(expect.arrayContaining(['material_keywords', 'measurement_processing']));
    expect(IMPLEMENTED_VERSION).toMatch(/^1\.\d+$/);
    expect(IMPLEMENTED_MINOR).toBe(Number(IMPLEMENTED_VERSION.split('.')[1]));
    expect(isNewerMinor(`1.${IMPLEMENTED_MINOR + 1}.0`)).toBe(true);
    expect(isNewerMinor(`1.0${IMPLEMENTED_MINOR + 1}.0`)).toBe(false);
  });
  test('every wavelength unit has a conversion to nm', () => {
    expect(Object.keys(VOCABULARIES.wavelength_unit).sort()).toEqual(Object.keys(UNIT_TO_NM).sort());
  });
  test('vocabulary columns are core string columns', () => {
    for (const v of VOCABULARY_COLUMNS) {
      expect(CORE_COLUMNS.find((c) => c.name === v)?.type).toBe('string');
    }
  });
  test('extensions agree across files', () => {
    const fromColumns = [...new Set(EXTENSION_COLUMNS.map((c) => c.extension))].sort();
    expect(fromColumns).toEqual([...EXTENSIONS].sort());
    const schemaEnum = (FOOTER_SCHEMA as { $defs: { extensions: { items: { enum: string[] } } } }).$defs.extensions.items.enum;
    expect([...schemaEnum].sort()).toEqual([...EXTENSIONS].sort());
  });
  test('the checker knows every rule id', () => {
    expect(RULES.map((r) => r.id)).toEqual([
      'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'F13',
      'I1', 'R1', 'R2', 'R3', 'R4', 'R5', 'Q1', 'Q2', 'Q3', 'Q4',
    ]);
  });
  test('the explorer flags implausible extents with the Q4 bounds', () => {
    expect(PLAUSIBLE_NM).toEqual(QA_WAVELENGTH_NM);
  });
  test('index columns', () => {
    expect(INDEX_REQUIRED_COLUMNS.map((c) => c.name)).not.toContain('values');
    expect(INDEX_REQUIRED_COLUMNS.map((c) => c.name)).toEqual(expect.arrayContaining(['id', 'num_points', 'file']));
    expect(COLUMNS.length).toBeGreaterThan(30);
  });
});

describe('links', () => {
  test('spec links point at the release tag, as footer.schema.json $id does', async () => {
    const { SPEC_URL, SCHEMAS_URL } = await import('../src/format/links');
    const { OPENSPECLIB_VERSION } = await import('../src/constants/urls');
    const tag = `/v${OPENSPECLIB_VERSION}/`;
    expect(String(FOOTER_SCHEMA.$id)).toContain(tag);
    expect(SPEC_URL).toContain(tag);
    expect(SCHEMAS_URL).toContain(tag);
  });
});
