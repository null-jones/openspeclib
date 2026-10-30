/**
 * Old shared links: spectrum ids changed between the v0 releases and Format
 * v1. The release ships `id-migration.csv` (old_id,new_id); the viewer
 * fetches it only when a link holds ids the index does not know.
 */

/** Parse `old_id,new_id` lines. New ids never contain commas (spec rule F5). */
export function parseMigrationCsv(text: string): Map<string, string> {
  const map = new Map<string, string>();
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line || (i === 0 && line.startsWith('old_id'))) continue;
    const comma = line.lastIndexOf(',');
    if (comma <= 0) continue;
    map.set(line.slice(0, comma), line.slice(comma + 1));
  }
  return map;
}

export interface MigrationResult {
  /** The id list with old ids replaced, unknown ids dropped, order kept, no duplicates. */
  ids: string[];
  migrated: number;
  missing: string[];
}

/** Replace ids `known` does not contain, using the migration map. */
export function migrateIds(
  ids: string[],
  known: (id: string) => boolean,
  map: Map<string, string> | null,
): MigrationResult {
  const out: string[] = [];
  const seen = new Set<string>();
  let migrated = 0;
  const missing: string[] = [];
  for (const id of ids) {
    let target: string | undefined = id;
    // Old links built by hand may carry a raw '+', which URL decoding turns
    // into a space (e.g. USGS "…Fe3+…").
    const plus = id.includes(' ') ? id.replace(/ /g, '+') : null;
    if (!known(id) && plus && known(plus)) {
      target = plus;
    } else if (!known(id)) {
      const next = map?.get(id) ?? (plus ? map?.get(plus) : undefined);
      if (next && known(next)) {
        target = next;
        migrated++;
      } else {
        missing.push(id);
        target = undefined;
      }
    }
    if (target && !seen.has(target)) {
      seen.add(target);
      out.push(target);
    }
  }
  return { ids: out, migrated, missing };
}
