/**
 * Column types for rules F2–F4.
 *
 * The spec defines a column's type by its Parquet logical type and
 * repetition (section 4), so verdicts use types derived from the Parquet
 * schema with the standard Parquet-to-Arrow mapping. Arrow writers also
 * store an `ARROW:schema` hint (a base64 IPC Schema message); it is decoded
 * only to show users what their writer intended (large_string, dictionary,
 * fixed_size_list …), never to decide a rule, so a spoofed hint can neither
 * pass nor crash a check. We decode that flatbuffer directly because
 * apache-arrow's JS reader rejects types it does not implement.
 */
import type { FileColumn } from './types';

// --- Minimal flatbuffer reader ----------------------------------------------

class FlatBuffer {
  private view: DataView;
  private bytes: Uint8Array;

  constructor(bytes: Uint8Array) {
    this.bytes = bytes;
    this.view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  }

  private check(pos: number, size: number): void {
    if (pos < 0 || pos + size > this.bytes.byteLength) {
      throw new RangeError('flatbuffer offset out of range');
    }
  }

  u8(pos: number): number {
    this.check(pos, 1);
    return this.view.getUint8(pos);
  }
  i16(pos: number): number {
    this.check(pos, 2);
    return this.view.getInt16(pos, true);
  }
  u16(pos: number): number {
    this.check(pos, 2);
    return this.view.getUint16(pos, true);
  }
  i32(pos: number): number {
    this.check(pos, 4);
    return this.view.getInt32(pos, true);
  }
  u32(pos: number): number {
    this.check(pos, 4);
    return this.view.getUint32(pos, true);
  }

  root(): number {
    return this.u32(0);
  }

  /** Absolute position of field `id` of the table at `table`, or 0 if absent. */
  field(table: number, id: number): number {
    const vtable = table - this.i32(table);
    const vtableSize = this.u16(vtable);
    const slot = 4 + 2 * id;
    if (slot >= vtableSize) return 0;
    const off = this.u16(vtable + slot);
    return off === 0 ? 0 : table + off;
  }

  indirect(pos: number): number {
    return pos + this.u32(pos);
  }

  string(pos: number): string {
    const start = this.indirect(pos);
    const len = this.u32(start);
    this.check(start + 4, len);
    return new TextDecoder().decode(this.bytes.subarray(start + 4, start + 4 + len));
  }

  /** Positions of the tables in a vector of tables. */
  tableVector(pos: number): number[] {
    const start = this.indirect(pos);
    const len = this.u32(start);
    const out: number[] = [];
    for (let i = 0; i < len; i++) {
      const elem = start + 4 + i * 4;
      out.push(this.indirect(elem));
    }
    return out;
  }
}

// Arrow `Type` union ids (format/Schema.fbs).
const TYPE_NAMES: Record<number, string> = {
  1: 'null',
  4: 'binary',
  5: 'string',
  6: 'bool',
  7: 'decimal',
  9: 'time',
  10: 'timestamp',
  11: 'interval',
  14: 'union',
  15: 'fixed_size_binary',
  17: 'map',
  18: 'duration',
  19: 'large_binary',
  20: 'large_string',
  22: 'run_end_encoded',
  23: 'binary_view',
  24: 'string_view',
};

interface DecodedField {
  name: string;
  type: string;
}

function decodeField(fb: FlatBuffer, field: number): DecodedField {
  const namePos = fb.field(field, 0);
  const name = namePos ? fb.string(namePos) : '';
  const typeTypePos = fb.field(field, 2);
  const typeId = typeTypePos ? fb.u8(typeTypePos) : 0;
  const typePos = fb.field(field, 3);
  const typeTable = typePos ? fb.indirect(typePos) : 0;
  const childrenPos = fb.field(field, 5);
  const children = childrenPos ? fb.tableVector(childrenPos).map((c) => decodeField(fb, c)) : [];
  const child = (): string => children[0]?.type ?? '?';

  let type: string;
  switch (typeId) {
    case 2: {
      // Int { bitWidth: int; is_signed: bool }
      const bw = typeTable && fb.field(typeTable, 0) ? fb.i32(fb.field(typeTable, 0)) : 0;
      const signed = typeTable && fb.field(typeTable, 1) ? fb.u8(fb.field(typeTable, 1)) !== 0 : false;
      type = `${signed ? 'int' : 'uint'}${bw}`;
      break;
    }
    case 3: {
      // FloatingPoint { precision: short (HALF=0, SINGLE=1, DOUBLE=2) }
      const p = typeTable && fb.field(typeTable, 0) ? fb.i16(fb.field(typeTable, 0)) : 0;
      type = p === 2 ? 'float64' : p === 1 ? 'float32' : 'float16';
      break;
    }
    case 8: {
      // Date { unit: short = MILLISECOND (DAY=0, MILLISECOND=1) }
      const unit = typeTable && fb.field(typeTable, 0) ? fb.i16(fb.field(typeTable, 0)) : 1;
      type = unit === 0 ? 'date32' : 'date64';
      break;
    }
    case 12:
      type = `list<${child()}>`;
      break;
    case 13:
      type = `struct<${children.map((c) => `${c.name}: ${c.type}`).join(', ')}>`;
      break;
    case 16:
      type = `fixed_size_list<${child()}>`;
      break;
    case 21:
      type = `large_list<${child()}>`;
      break;
    case 25:
      type = `list_view<${child()}>`;
      break;
    case 26:
      type = `large_list_view<${child()}>`;
      break;
    default:
      type = TYPE_NAMES[typeId] ?? `unknown_type_${typeId}`;
  }

  // A dictionary-encoded field stores its value type in `type`.
  if (fb.field(field, 4)) type = `dictionary<values=${type}>`;
  return { name, type };
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64.trim());
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/**
 * Decode the `ARROW:schema` footer value (base64 IPC Schema message) into
 * top-level columns with normalised Arrow type names. Throws if malformed.
 */
export function decodeArrowSchema(base64: string): FileColumn[] {
  const raw = base64ToBytes(base64);
  const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  // Encapsulated IPC message: [0xFFFFFFFF continuation] int32 length, flatbuffer.
  let start = 4;
  let len = dv.getInt32(0, true);
  if (dv.getUint32(0, true) === 0xffffffff) {
    len = dv.getInt32(4, true);
    start = 8;
  }
  if (len <= 0 || start + len > raw.byteLength) throw new Error('bad IPC message length');
  const fb = new FlatBuffer(raw.subarray(start, start + len));
  const message = fb.root();
  const headerTypePos = fb.field(message, 1);
  if (!headerTypePos || fb.u8(headerTypePos) !== 1) throw new Error('not a Schema message');
  const schema = fb.indirect(fb.field(message, 2));
  const fieldsPos = fb.field(schema, 1);
  if (!fieldsPos) return [];
  return fb.tableVector(fieldsPos).map((f) => decodeField(fb, f));
}

// --- Parquet schema fallback --------------------------------------------------

/** One row of DuckDB's `parquet_schema()` table function. */
export interface ParquetSchemaRow {
  name: string;
  type: string | null;
  repetition_type: string | null;
  num_children: number | null;
  converted_type: string | null;
  logical_type: string | null;
}

interface SchemaNode {
  row: ParquetSchemaRow;
  children: SchemaNode[];
}

function buildTree(rows: ParquetSchemaRow[]): SchemaNode {
  let i = 0;
  const take = (): SchemaNode => {
    const row = rows[i++];
    if (!row) throw new Error('truncated Parquet schema');
    const n = row.num_children ?? 0;
    const children: SchemaNode[] = [];
    for (let k = 0; k < n; k++) children.push(take());
    return { row, children };
  };
  return take();
}

function has(s: string | null, needle: string): boolean {
  return !!s && s.toUpperCase().includes(needle.toUpperCase());
}

function leafType(row: ParquetSchemaRow): string {
  const { type, converted_type: ct, logical_type: lt } = row;
  // Arrow's `null` type is stored with the Parquet logical type Null/Unknown.
  if (has(lt, 'NullType') || has(lt, 'UnknownType')) return 'null';
  switch (type) {
    case 'BOOLEAN':
      return 'bool';
    case 'INT32': {
      if (has(lt, 'DateType') || ct === 'DATE') return 'date32';
      if (has(lt, 'DecimalType') || ct === 'DECIMAL') return 'decimal';
      if (has(lt, 'TimeType') || ct === 'TIME_MILLIS') return 'time';
      const m = /bitWidth=(\d+),\s*isSigned=(\w+)/i.exec(lt ?? '');
      if (m) return `${m[2] === 'true' ? 'int' : 'uint'}${m[1]}`;
      const c = /^(U?)INT_(\d+)$/.exec(ct ?? '');
      if (c) return `${c[1] ? 'uint' : 'int'}${c[2]}`;
      return 'int32';
    }
    case 'INT64': {
      if (has(lt, 'TimestampType') || (ct ?? '').startsWith('TIMESTAMP')) return 'timestamp';
      if (has(lt, 'TimeType') || ct === 'TIME_MICROS') return 'time';
      if (has(lt, 'DecimalType') || ct === 'DECIMAL') return 'decimal';
      if (has(lt, 'isSigned=false') || ct === 'UINT_64') return 'uint64';
      return 'int64';
    }
    case 'INT96':
      return 'timestamp';
    case 'FLOAT':
      return 'float32';
    case 'DOUBLE':
      return 'float64';
    case 'BYTE_ARRAY':
      // ENUM and JSON are their own logical types, not plain strings (as
      // pyarrow, and the reference checker, read them).
      if (has(lt, 'EnumType') || ct === 'ENUM') return 'enum';
      if (has(lt, 'JsonType') || ct === 'JSON') return 'json';
      if (has(lt, 'StringType') || ct === 'UTF8') return 'string';
      if (has(lt, 'DecimalType') || ct === 'DECIMAL') return 'decimal';
      return 'binary';
    case 'FIXED_LEN_BYTE_ARRAY':
      if (has(lt, 'Float16Type')) return 'float16';
      if (has(lt, 'DecimalType') || ct === 'DECIMAL') return 'decimal';
      return 'fixed_size_binary';
    default:
      return (type ?? 'unknown').toLowerCase();
  }
}

function nodeType(node: SchemaNode): string {
  const { row, children } = node;
  if (children.length === 0 && (row.num_children ?? 0) === 0 && row.type) {
    return leafType(row);
  }
  const isList = row.converted_type === 'LIST' || has(row.logical_type, 'ListType');
  const isMap =
    row.converted_type === 'MAP' ||
    row.converted_type === 'MAP_KEY_VALUE' ||
    has(row.logical_type, 'MapType');
  if (isList && children.length === 1) {
    const rep = children[0];
    // 2-level legacy list: the repeated child is the element itself.
    if (rep.children.length === 0) return `list<${leafType(rep.row)}>`;
    // 3-level list: repeated group with exactly one element child.
    if (rep.children.length === 1) return `list<${fieldType(rep.children[0])}>`;
    return `list<${structType(rep.children)}>`;
  }
  if (isMap) return 'map';
  return structType(children);
}

function structType(children: SchemaNode[]): string {
  return `struct<${children.map((c) => `${c.row.name}: ${fieldType(c)}`).join(', ')}>`;
}

/** Type of a field, wrapping legacy un-annotated repeated fields as lists. */
function fieldType(node: SchemaNode): string {
  const t = nodeType(node);
  const annotated = node.row.converted_type === 'LIST' || has(node.row.logical_type, 'ListType');
  return node.row.repetition_type === 'REPEATED' && !annotated ? `list<${t}>` : t;
}

/** Top-level columns with Arrow types derived from DuckDB `parquet_schema()` rows. */
export function columnsFromParquetSchema(rows: ParquetSchemaRow[]): FileColumn[] {
  if (rows.length === 0) return [];
  const root = buildTree(rows);
  return root.children.map((c) => ({ name: c.row.name, type: fieldType(c) }));
}
