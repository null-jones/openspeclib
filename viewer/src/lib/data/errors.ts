/**
 * Error text fit for users: DuckDB messages can carry SQL, internal file
 * names and multi-line hints. Keep the first meaningful line, without SQL.
 */
export function friendlyError(e: unknown, replacements: Array<[string, string]> = []): string {
  let msg = e instanceof Error ? e.message : String(e);
  for (const [from, to] of replacements) msg = msg.split(from).join(to);
  const first = msg.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? 'Unknown error';
  // "Invalid Input Error: No magic bytes…" → keep; drop anything from an SQL fragment on.
  let line = first.replace(/\s*(LINE \d+:|SELECT |WITH |CREATE |FROM read_parquet).*$/i, '');
  if (line.length > 240) line = `${line.slice(0, 237)}…`;
  return line || 'DuckDB could not read the file.';
}
