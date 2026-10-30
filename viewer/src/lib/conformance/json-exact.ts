/**
 * JSON values compared as the reference checker compares them: Python's
 * `json.loads` followed by `==`. JavaScript's JSON.parse loses integers above
 * 2^53 and tells `true` from `1`, so I1's "metadata equals the collection
 * file's footer" check parses both texts here, keeping each number's text.
 */

/** A JSON number, as written. */
export interface JsonNumber {
  readonly num: string;
}

export type ExactJson = null | boolean | string | JsonNumber | ExactJson[] | Map<string, ExactJson>;

const isNumber = (v: ExactJson): v is JsonNumber => typeof v === 'object' && v !== null && !Array.isArray(v) && !(v instanceof Map);

/** Parse JSON text, keeping numbers as text. Throws SyntaxError on invalid JSON. Duplicate keys: the last wins (as in Python). */
export function parseJsonExact(text: string): ExactJson {
  let i = 0;
  const fail = (what: string): never => {
    throw new SyntaxError(`${what} at position ${i}`);
  };
  const ws = (): void => {
    while (i < text.length && ' \t\n\r'.includes(text[i])) i++;
  };
  const value = (): ExactJson => {
    ws();
    const c = text[i];
    if (c === '{') {
      i++;
      const obj = new Map<string, ExactJson>();
      ws();
      if (text[i] === '}') {
        i++;
        return obj;
      }
      for (;;) {
        ws();
        if (text[i] !== '"') fail('expected a key');
        const key = str();
        ws();
        if (text[i++] !== ':') fail("expected ':'");
        obj.set(key, value());
        ws();
        const d = text[i++];
        if (d === '}') return obj;
        if (d !== ',') fail("expected ',' or '}'");
      }
    }
    if (c === '[') {
      i++;
      const arr: ExactJson[] = [];
      ws();
      if (text[i] === ']') {
        i++;
        return arr;
      }
      for (;;) {
        arr.push(value());
        ws();
        const d = text[i++];
        if (d === ']') return arr;
        if (d !== ',') fail("expected ',' or ']'");
      }
    }
    if (c === '"') return str();
    for (const [word, v] of [['true', true], ['false', false], ['null', null]] as const) {
      if (text.startsWith(word, i)) {
        i += word.length;
        return v;
      }
    }
    const m = /^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i));
    if (!m) return fail('unexpected character');
    i += m[0].length;
    return { num: m[0] };
  };
  const str = (): string => {
    // Find the closing quote; JSON.parse then decodes (and validates) the escapes.
    const start = i;
    i++;
    while (i < text.length && text[i] !== '"') {
      if (text.charCodeAt(i) < 0x20) fail('control character in a string');
      i += text[i] === '\\' ? 2 : 1;
    }
    if (i >= text.length) fail('unterminated string');
    i++;
    return JSON.parse(text.slice(start, i)) as string;
  };
  const v = value();
  ws();
  if (i !== text.length) fail('unexpected text after the value');
  return v;
}

/** A JSON number (or a Python bool, which is an int) as an exact integer, or as a double when it is not an integer. */
function numeric(v: JsonNumber | boolean): { int: bigint } | { float: number } {
  if (typeof v === 'boolean') return { int: v ? 1n : 0n };
  if (!/[.eE]/.test(v.num)) return { int: BigInt(v.num) };
  return { float: Number(v.num) };
}

/** Python `==` between int and float is exact. */
function numbersEqual(a: JsonNumber | boolean, b: JsonNumber | boolean): boolean {
  const x = numeric(a);
  const y = numeric(b);
  if ('int' in x && 'int' in y) return x.int === y.int;
  if ('float' in x && 'float' in y) return x.float === y.float;
  const i = 'int' in x ? x.int : (y as { int: bigint }).int;
  const f = 'float' in x ? x.float : (y as { float: number }).float;
  return Number.isInteger(f) && BigInt(f) === i;
}

/** Python's `==` on the values `json.loads` would produce. */
export function pyJsonEqual(a: ExactJson, b: ExactJson): boolean {
  const scalarNum = (v: ExactJson): v is JsonNumber | boolean => typeof v === 'boolean' || isNumber(v);
  if (scalarNum(a) && scalarNum(b)) return typeof a === 'boolean' && typeof b === 'boolean' ? a === b : numbersEqual(a, b);
  if (a === null || b === null || typeof a === 'string' || typeof b === 'string') return a === b;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, k) => pyJsonEqual(x, b[k]));
  if (a instanceof Map && b instanceof Map) {
    if (a.size !== b.size) return false;
    for (const [k, v] of a) {
      if (!b.has(k) || !pyJsonEqual(v, b.get(k)!)) return false;
    }
    return true;
  }
  return false;
}
