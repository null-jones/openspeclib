/** SQL quoting helpers shared by the checker and the data layer. */

/** Quote an SQL identifier. */
export function ident(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

/** Quote an SQL string literal. */
export function lit(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/** Escape a string for use inside an RE2 regular expression. */
export function reEscape(s: string): string {
  return s.replace(/[\\^$.*+?()[\]{}|]/g, '\\$&');
}

/** A numeric query result (DuckDB BIGINTs may arrive as bigint) as a number. */
export function num(v: unknown): number {
  if (typeof v === 'number') return v;
  if (typeof v === 'bigint') return Number(v);
  if (typeof v === 'string' && v !== '') return Number(v);
  return 0;
}
