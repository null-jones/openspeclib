import { describe, expect, test } from 'vitest';
import { columnsFromParquetSchema, decodeArrowSchema, type ParquetSchemaRow } from '../src/lib/conformance/arrow-schema';

// pyarrow: pa.schema([...]).serialize(), base64 — what writers store as ARROW:schema.
const PYARROW_SCHEMA =
  '/////wADAAAQAAAAAAAKAAwABgAFAAgACgAAAAABBAAEAAAA9P7//wQAAAAMAAAArAIAAHQCAAAYAgAAwAEAAJQBAABcAQAACAEAAMAAAACEAAAAXAAAACwAAAAEAAAAlP3//wAAAQgQAAAAFAAAAAQAAAAAAAAAAQAAAGwAAACE/f//uP3//wAAAQIQAAAAGAAAAAQAAAAAAAAAAQAAAGsABgAIAAQABgAAABAAAADk/f//AAABGBAAAAAUAAAABAAAAAAAAAABAAAAagAAANT9//8I/v//AAABAxAAAAAUAAAABAAAAAAAAAABAAAAaQAAAFr+//8AAAEAEAAYAAgABgAHAAwAEAAUABAAAAAAAAEFFAAAADQAAAAcAAAABAAAAAAAAAABAAAAaAAAAAgACAAAAAQACAAAAAQAAACA////AAAAASAAAABQ/v//hP7//wAAAQwUAAAAGAAAAAQAAAABAAAAEAAAAAEAAABnAAAAeP7//6z+//8AAAEFEAAAABgAAAAEAAAAAAAAAAQAAABpdGVtAAAAAKD+///U/v//AAABAhAAAAAcAAAABAAAAAAAAAABAAAAZgAAAAgADAAIAAcACAAAAAAAAAEgAAAACP///wAAAQgQAAAAFAAAAAQAAAAAAAAAAQAAAGUAAABa////AAAAADD///8AAAEVFAAAABgAAAAEAAAAAQAAABAAAAABAAAAZAAAACT///9Y////AAABAxAAAAAYAAAABAAAAAAAAAAEAAAAaXRlbQAAAACu////AAABAIT///8AAAEMFAAAABgAAAAEAAAAAQAAABAAAAABAAAAYwAAAHj///+s////AAABAxAAAAAcAAAABAAAAAAAAAAEAAAAaXRlbQAABgAIAAYABgAAAAAAAgDc////AAABFBAAAAAUAAAABAAAAAAAAAABAAAAYgAAAMz///8QABQACAAGAAcADAAAABAAEAAAAAAAAQUQAAAAGAAAAAQAAAAAAAAAAQAAAGEAAAAEAAQABAAAAAAAAAA=';

describe('decodeArrowSchema', () => {
  test('keeps the Arrow distinctions F2 needs', () => {
    expect(decodeArrowSchema(PYARROW_SCHEMA)).toEqual([
      { name: 'a', type: 'string' },
      { name: 'b', type: 'large_string' },
      { name: 'c', type: 'list<float64>' },
      { name: 'd', type: 'large_list<float32>' },
      { name: 'e', type: 'date32' },
      { name: 'f', type: 'int32' },
      { name: 'g', type: 'list<string>' },
      { name: 'h', type: 'dictionary<values=string>' },
      { name: 'i', type: 'float32' },
      { name: 'j', type: 'string_view' },
      { name: 'k', type: 'uint16' },
      { name: 'l', type: 'date64' },
    ]);
  });

  test('rejects garbage', () => {
    expect(() => decodeArrowSchema('AAAA')).toThrow();
    expect(() => decodeArrowSchema('not base64!')).toThrow();
  });
});

const row = (
  name: string,
  type: string | null,
  extra: Partial<ParquetSchemaRow> = {},
): ParquetSchemaRow => ({
  name,
  type,
  repetition_type: 'OPTIONAL',
  num_children: null,
  converted_type: null,
  logical_type: null,
  ...extra,
});

describe('columnsFromParquetSchema', () => {
  test('maps DuckDB parquet_schema rows to Arrow types', () => {
    const rows: ParquetSchemaRow[] = [
      row('duckdb_schema', null, { repetition_type: 'REQUIRED', num_children: 7 }),
      row('id', 'BYTE_ARRAY', { converted_type: 'UTF8', logical_type: 'StringType()' }),
      row('wavelengths', null, { num_children: 1, converted_type: 'LIST', logical_type: 'ListType()' }),
      row('list', null, { repetition_type: 'REPEATED', num_children: 1 }),
      row('element', 'DOUBLE'),
      row('values', null, { num_children: 1, converted_type: 'LIST' }),
      row('list', null, { repetition_type: 'REPEATED', num_children: 1 }),
      row('element', 'FLOAT'),
      row('measurement_date', 'INT32', { converted_type: 'DATE', logical_type: 'DateType()' }),
      row('num_points', 'INT32', { logical_type: 'IntType(bitWidth=32, isSigned=true)' }),
      row('blob', 'BYTE_ARRAY'),
      row('legacy', 'FLOAT', { repetition_type: 'REPEATED' }),
    ];
    expect(columnsFromParquetSchema(rows)).toEqual([
      { name: 'id', type: 'string' },
      { name: 'wavelengths', type: 'list<float64>' },
      { name: 'values', type: 'list<float32>' },
      { name: 'measurement_date', type: 'date32' },
      { name: 'num_points', type: 'int32' },
      { name: 'blob', type: 'binary' },
      { name: 'legacy', type: 'list<float32>' },
    ]);
  });

  test('handles 2-level legacy lists', () => {
    const rows: ParquetSchemaRow[] = [
      row('schema', null, { num_children: 1 }),
      row('values', null, { num_children: 1, converted_type: 'LIST' }),
      row('array', 'FLOAT', { repetition_type: 'REPEATED' }),
    ];
    expect(columnsFromParquetSchema(rows)).toEqual([{ name: 'values', type: 'list<float32>' }]);
  });
});
