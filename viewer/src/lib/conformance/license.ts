/**
 * SPDX license expression syntax as rule F11 defines it: check the syntax
 * and collect every `LicenseRef-` identifier, so the caller can check that
 * each one is defined in the footer's `license_refs`. Identifiers are not
 * checked against the SPDX licence list (F11 says they need not be).
 *
 * As in the spec and the reference checker (src/openspeclib/conformance.py):
 * tokens are separated by single U+0020 spaces or parentheses, with no other
 * whitespace and none leading or trailing; operators are upper case;
 * `NOASSERTION` only stands alone; `LicenseRef-` ids are whole terms (never
 * after WITH, never with `+`); `DocumentRef-` references are rejected; and
 * parentheses nest at most 32 deep.
 */

export interface LicenseParse {
  valid: boolean;
  error?: string;
  /** Every `LicenseRef-…` identifier used. */
  licenseRefs: string[];
}

/** F11: deepest parenthesis nesting accepted. */
export const MAX_LICENCE_NESTING = 32;

const IDSTRING = /^[A-Za-z0-9.-]+$/;
const OPERATORS = new Set(['AND', 'OR', 'WITH']);

/** Whitespace as Python's str.isspace() defines it. */
function isPySpace(ch: string): boolean {
  const c = ch.codePointAt(0)!;
  if (c === 0xfeff) return false;
  return (c >= 0x1c && c <= 0x1f) || c === 0x85 || /\s/.test(ch);
}

/** True when `s` is empty or only whitespace (Python's `not s.strip()`). */
export function isBlank(s: string): boolean {
  for (const ch of s) if (!isPySpace(ch)) return false;
  return true;
}

function tokenize(expr: string): string[] | string {
  const tokens: string[] = [];
  let word = '';
  let previousSpace = false;
  let offset = 0;
  for (const ch of expr) {
    if (ch === ' ' || ch === '(' || ch === ')') {
      if (word) {
        tokens.push(word);
        word = '';
      }
      if (ch === ' ') {
        if (previousSpace) return `more than one space at offset ${offset}`;
        previousSpace = true;
        offset++;
        continue;
      }
      tokens.push(ch);
    } else if (isPySpace(ch)) {
      return `whitespace U+${ch.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')} at offset ${offset}: tokens are separated by single spaces`;
    } else {
      word += ch;
    }
    previousSpace = false;
    offset++;
  }
  if (word) tokens.push(word);
  return tokens;
}

/** Parse an SPDX license expression. */
export function parseLicenseExpression(expr: string): LicenseParse {
  const licenseRefs: string[] = [];
  if (typeof expr !== 'string' || expr === '') return { valid: false, error: 'empty licence', licenseRefs };
  if (expr === 'NOASSERTION') return { valid: true, licenseRefs };
  if (isPySpace(expr[0]) || isPySpace(expr[expr.length - 1])) {
    return { valid: false, error: 'leading or trailing whitespace', licenseRefs };
  }
  const toks = tokenize(expr);
  if (typeof toks === 'string') return { valid: false, error: toks, licenseRefs };
  const tokens = toks;
  let pos = 0;
  let depth = 0;

  const fail = (msg: string): never => {
    throw new SyntaxError(msg);
  };

  const simple = (tok: string): void => {
    if (OPERATORS.has(tok) || tok === ')') fail(`expected a licence id, found '${tok}'`);
    if (tok.replace(/\+$/, '').toUpperCase() === 'NOASSERTION') {
      fail(tok === 'NOASSERTION' ? 'NOASSERTION must stand on its own' : `the special value NOASSERTION is case-sensitive ('${tok}')`);
    }
    const lower = tok.toLowerCase();
    if (lower.startsWith('documentref-')) fail(`DocumentRef- references are not allowed ('${tok}')`);
    if (lower.startsWith('licenseref-') && !tok.startsWith('LicenseRef-')) {
      fail(`the LicenseRef- prefix is case-sensitive ('${tok}')`);
    }
    if (tok.startsWith('LicenseRef-')) {
      if (!IDSTRING.test(tok.slice('LicenseRef-'.length))) fail(`invalid LicenseRef '${tok}'`);
      if (!licenseRefs.includes(tok)) licenseRefs.push(tok);
      return;
    }
    const id = tok.endsWith('+') ? tok.slice(0, -1) : tok;
    if (!IDSTRING.test(id)) fail(`invalid licence id '${tok}'`);
  };

  const term = (): void => {
    const tok = tokens[pos++];
    if (tok === undefined) fail('expression ends unexpectedly');
    if (tok === '(') {
      depth++;
      if (depth > MAX_LICENCE_NESTING) fail(`parentheses nested deeper than ${MAX_LICENCE_NESTING}`);
      expression();
      if (tokens[pos++] !== ')') fail("missing ')'");
      depth--;
      return;
    }
    simple(tok);
    if (tokens[pos] === 'WITH') {
      pos++;
      const exc = tokens[pos++];
      if (exc === undefined) fail('WITH must be followed by an exception id');
      const excLower = exc.toLowerCase();
      if (
        OPERATORS.has(exc) ||
        exc === '(' ||
        exc === ')' ||
        exc.toUpperCase() === 'NOASSERTION' ||
        excLower.startsWith('licenseref-') ||
        excLower.startsWith('documentref-')
      ) {
        fail(`WITH must be followed by an exception id, not '${exc}'`);
      }
      if (!IDSTRING.test(exc)) fail(`invalid exception id '${exc}'`);
    }
  };

  const expression = (): void => {
    term();
    while (tokens[pos] === 'AND' || tokens[pos] === 'OR') {
      pos++;
      term();
    }
  };

  try {
    expression();
    if (pos < tokens.length) fail(`unexpected '${tokens[pos]}'`);
  } catch (e) {
    return { valid: false, error: (e as Error).message, licenseRefs };
  }
  return { valid: true, licenseRefs };
}

/**
 * Check one non-empty licence value against rule F11: valid syntax, and
 * every `LicenseRef-` it uses is defined in `license_refs`. Returns a list of
 * problems (empty when the value is fine). Empty values are F2's (rows) or
 * the footer schema's (F1), so they return no problem here.
 */
export function checkLicense(value: unknown, definedRefs: ReadonlySet<string>): string[] {
  if (typeof value !== 'string' || value === '') return [];
  const parsed = parseLicenseExpression(value);
  if (!parsed.valid) return [`not a valid licence expression (${parsed.error})`];
  return parsed.licenseRefs
    .filter((ref) => !definedRefs.has(ref))
    .map((ref) => `${ref} is not defined in collection.license_refs`);
}
