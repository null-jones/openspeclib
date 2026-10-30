import { describe, expect, test } from 'vitest';
import { checkLicense, parseLicenseExpression } from '../src/lib/conformance/license';

describe('parseLicenseExpression', () => {
  test.each([
    'CC-BY-4.0',
    'MIT OR Apache-2.0',
    '(MIT OR Apache-2.0) AND CC0-1.0',
    'GPL-2.0+ WITH Classpath-exception-2.0',
    'GPL-2.0-or-later WITH Classpath-exception-2.0 OR MIT',
    'LicenseRef-USGS-PublicDomain',
    '((MIT))',
  ])('accepts %s', (expr) => {
    expect(parseLicenseExpression(expr).valid).toBe(true);
  });

  test.each([
    ['', 'empty'],
    ['CC BY 4.0', 'unexpected'],
    ['MIT OR', 'ends unexpectedly'],
    ['AND MIT', 'expected a licence id'],
    ['(MIT', "missing ')'"],
    ['MIT)', 'unexpected'],
    ['MIT WITH', 'exception'],
    ['(MIT) WITH Classpath-exception-2.0', 'unexpected'],
    ['LicenseRef-', 'invalid LicenseRef'],
    ['CC_BY', 'invalid licence id'],
    ['LicenseRef-foo+', 'invalid LicenseRef'],
    ['MIT  OR Apache-2.0', 'more than one space'],
    [' MIT', 'leading or trailing'],
    ['MIT ', 'leading or trailing'],
    ['MIT\u00a0OR Apache-2.0', 'whitespace U+00A0'],
    ['MIT\tOR Apache-2.0', 'whitespace U+0009'],
    ['GPL-2.0-only with Classpath-exception-2.0', 'unexpected'],
    ['('.repeat(33) + 'MIT' + ')'.repeat(33), 'nested deeper than 32'],
    ['MIT Or Apache-2.0', 'unexpected'],
    ['mit or apache-2.0', 'unexpected'],
    ['DocumentRef-spdx-tool-1.2:LicenseRef-MIT-Style-2', 'DocumentRef'],
    ['MIT WITH LicenseRef-x', 'exception'],
    ['NOASSERTION OR MIT', 'on its own'],
    ['licenseref-x', 'case-sensitive'],
    ['MIT OR LICENSEREF-x', 'case-sensitive'],
    ['documentref-x:LicenseRef-y', 'DocumentRef'],
    ['MIT WITH NOASSERTION', 'exception'],
    ['MIT WITH licenseref-x', 'exception'],
  ])('rejects %j', (expr, msg) => {
    const p = parseLicenseExpression(expr);
    expect(p.valid).toBe(false);
    expect(p.error).toContain(msg);
  });

  test('collects LicenseRef identifiers', () => {
    expect(parseLicenseExpression('LicenseRef-A AND (MIT OR LicenseRef-B OR LicenseRef-A)').licenseRefs).toEqual([
      'LicenseRef-A',
      'LicenseRef-B',
    ]);
  });
});

describe('checkLicense (F11)', () => {
  const defined = new Set(['LicenseRef-A']);
  test('accepts valid values', () => {
    expect(checkLicense('NOASSERTION', defined)).toEqual([]);
    expect(checkLicense('LicenseRef-A OR MIT', defined)).toEqual([]);
  });
  test('reports undefined LicenseRefs', () => {
    expect(checkLicense('LicenseRef-B', defined)).toEqual([
      'LicenseRef-B is not defined in collection.license_refs',
    ]);
  });
  test('leaves empty values to F2; whitespace is a syntax error', () => {
    expect(checkLicense('', defined)).toEqual([]);
    expect(checkLicense(null, defined)).toEqual([]);
    expect(checkLicense('MIT ', defined)[0]).toContain('leading or trailing whitespace');
  });
  test('parentheses need no spaces; 32 levels are fine', () => {
    expect(parseLicenseExpression('MIT OR(Apache-2.0)').valid).toBe(true);
    expect(parseLicenseExpression('('.repeat(32) + 'MIT' + ')'.repeat(32)).valid).toBe(true);
  });
});
