/**
 * Column-level rules: F2 (core columns and types), F3 (no unknown columns,
 * flat `x_` columns) and F4 (extension and `extra` column types). Pure:
 * takes the file's columns and the footer's declared extensions.
 */
import { COLUMN_BY_NAME, CORE_COLUMNS, hasSpecType, isFlatType } from '../../format/spec';
import { plural, type RuleAccumulator } from './results';
import type { FileColumn } from './types';

export interface ColumnFindings {
  /** Columns present once with the spec's type and real values: safe to query by row rules. */
  usable: Set<string>;
  /** Nullable columns stored with the Arrow `null` type: valid, but every value is null. */
  allNull: Set<string>;
  /** All column names present. */
  present: Set<string>;
}

function describe(col: FileColumn): string {
  return col.arrowType && col.arrowType !== col.type ? `${col.type} (Arrow hint ${col.arrowType})` : col.type;
}

/**
 * Check the columns of a collection file and record F2, F3 and F4 findings.
 *
 * @param declared extensions listed in the footer, or null if the footer (or
 *                 its `extensions` list) could not be read: extension
 *                 declarations are then not checked, but column names are
 */
export function checkCollectionColumns(
  columns: FileColumn[],
  declared: string[] | null,
  acc: RuleAccumulator,
  /** The file names a newer 1.x minor: unknown column names are warnings (spec section 11). */
  newerMinor = false,
): ColumnFindings {
  const present = new Set(columns.map((c) => c.name));
  const occurrences = new Map<string, number>();
  for (const c of columns) occurrences.set(c.name, (occurrences.get(c.name) ?? 0) + 1);
  const byName = new Map(columns.map((c) => [c.name, c]));
  const usable = new Set<string>();
  const allNull = new Set<string>();

  const accept = (name: string, col: FileColumn, spec: { type: string; nullable: boolean }): boolean => {
    if (!hasSpecType(col.type, spec.type, spec.nullable)) return false;
    if (col.type === 'null') allNull.add(name);
    else usable.add(name);
    return true;
  };

  // F2: every core column exists once with its type.
  const missing: string[] = [];
  const wrong: string[] = [];
  for (const spec of CORE_COLUMNS) {
    const n = occurrences.get(spec.name) ?? 0;
    const col = byName.get(spec.name);
    if (n === 0 || !col) missing.push(spec.name);
    else if (n > 1) wrong.push(`${spec.name} appears ${n} times`);
    else if (!accept(spec.name, col, spec)) wrong.push(`${spec.name}: ${describe(col)} (expected ${spec.type})`);
  }
  acc.issue('F2', missing.length, `Missing core ${missing.length === 1 ? 'column' : 'columns'}: ${missing.join(', ')}`, missing, 'columns');
  acc.issue('F2', wrong.length, `Wrong type: ${wrong.join('; ')}`, wrong.map((w) => w.split(/[: ]/)[0]), 'columns');

  // F3 and F4.
  if (declared === null) {
    acc.note('F3', 'Extension declarations were not checked: the footer or its extensions list could not be read (see F1).');
  }
  const declaredSet = new Set(declared ?? []);
  const unknown: string[] = [];
  const notFlat: string[] = [];
  const repeated: string[] = [];
  const undeclared: string[] = [];
  const wrongExt: string[] = [];
  for (const name of occurrences.keys()) {
    const spec = COLUMN_BY_NAME.get(name);
    if (spec && spec.extension === null) continue; // core: F2
    if (!spec || spec.extension === 'index') {
      // x_ columns, and in a newer minor version unknown columns, are allowed
      // but must be flat (spec sections 2, 6 and 11).
      const allowed = name.startsWith('x_') || (!spec && newerMinor);
      if (!allowed) unknown.push(spec ? `${name} (index files only)` : name);
      else if ((occurrences.get(name) ?? 0) > 1) repeated.push(`column '${name}' appears ${occurrences.get(name)} times`);
      else if (columns.some((c) => c.name === name && !isFlatType(c.type))) notFlat.push(`${name}: ${byName.get(name)!.type}`);
      else if (!name.startsWith('x_')) unknown.push(name);
      continue;
    }
    // Extension column or `extra`.
    if (declared !== null && spec.extension !== '*' && !declaredSet.has(spec.extension!)) {
      undeclared.push(`${name} (extension '${spec.extension}' not declared)`);
    }
    const n = occurrences.get(name)!;
    const col = byName.get(name)!;
    if (n > 1) wrongExt.push(`${name} appears ${n} times`);
    else if (!accept(name, col, spec)) wrongExt.push(`${name}: ${describe(col)} (expected ${spec.type})`);
  }
  const indexOnly = unknown.filter((u) => u.endsWith('(index files only)'));
  const unknownNames = unknown.filter((u) => !u.endsWith('(index files only)'));
  const report = newerMinor ? acc.warn.bind(acc) : acc.issue.bind(acc);
  report(
    'F3',
    unknownNames.length,
    newerMinor
      ? `${plural(unknownNames.length, 'column')} this checker does not know (allowed as a warning: the file follows a newer 1.x version): ${unknownNames.join(', ')}`
      : `${plural(unknownNames.length, 'column')} not defined by the spec (prefix your own columns with x_): ${unknownNames.join(', ')}`,
    unknownNames,
    'columns',
  );
  acc.issue('F3', indexOnly.length, `${plural(indexOnly.length, 'column')} belonging to index files only: ${indexOnly.join(', ')}`, indexOnly.map((u) => u.split(' ')[0]), 'columns');
  acc.issue('F3', repeated.length, repeated.join('; '), repeated.map((r) => r.split("'")[1]), 'columns');
  acc.issue(
    'F3',
    notFlat.length,
    `${plural(notFlat.length, 'column')} not flat (only primitives or lists of primitives; no structs or maps): ${notFlat.join('; ')}`,
    notFlat.map((u) => u.split(':')[0]),
    'columns',
  );
  acc.issue(
    'F3',
    undeclared.length,
    `${plural(undeclared.length, 'extension column')} without a footer declaration: ${undeclared.join(', ')}`,
    undeclared.map((u) => u.split(' ')[0]),
    'columns',
  );
  acc.issue('F4', wrongExt.length, `Wrong type: ${wrongExt.join('; ')}`, wrongExt.map((w) => w.split(/[: ]/)[0]), 'columns');

  return { usable, allNull, present };
}
