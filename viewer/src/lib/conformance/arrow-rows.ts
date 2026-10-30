import type { Row } from './types';

interface VectorLike {
  toArray(): unknown;
  [Symbol.iterator](): Iterator<unknown>;
}

function isVectorLike(v: object): v is VectorLike {
  return (
    typeof (v as { toArray?: unknown }).toArray === 'function' &&
    typeof (v as { [Symbol.iterator]?: unknown })[Symbol.iterator] === 'function' &&
    !ArrayBuffer.isView(v)
  );
}

/** Convert an Arrow cell to plain JS: BigInt to number, list vectors to arrays. */
function plainValue(v: unknown): unknown {
  if (typeof v === 'bigint') return Number(v);
  if (v === null || typeof v !== 'object') return v;
  if (ArrayBuffer.isView(v)) return v;
  // Arrow StructRow.toJSON() gives an object, Vector.toJSON() an array.
  if (typeof (v as { toJSON?: unknown }).toJSON === 'function') {
    return plainValue((v as { toJSON(): unknown }).toJSON());
  }
  if (isVectorLike(v)) return Array.from(v, plainValue);
  if (Array.isArray(v)) return v.map(plainValue);
  const out: Row = {};
  for (const [k, x] of Object.entries(v)) out[k] = plainValue(x);
  return out;
}

/** Rows of an Arrow table (as returned by DuckDB-WASM) as plain objects. */
export function tableToRows(table: { toArray(): unknown[] }): Row[] {
  return table.toArray().map((r) => plainValue(r) as Row);
}

interface ArrowLikeTable {
  numRows: number;
  schema: { fields: Array<{ name: string }> };
  getChild(name: string): { [Symbol.iterator](): Iterator<unknown> } | null;
}

/**
 * Columns of an Arrow table as plain arrays. Much faster than building one
 * object per row for large results (e.g. the ~30,000-row search index).
 */
export function tableToColumns(table: ArrowLikeTable): Record<string, unknown[]> {
  const out: Record<string, unknown[]> = {};
  for (const field of table.schema.fields) {
    const child = table.getChild(field.name);
    out[field.name] = child ? Array.from(child, plainValue) : new Array(table.numRows).fill(null);
  }
  return out;
}
