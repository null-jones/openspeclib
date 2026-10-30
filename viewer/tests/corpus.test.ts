/**
 * Runs the browser checker's engine, on DuckDB-WASM's Node build, over the
 * shared conformance corpus and compares verdicts with expected.json.
 *
 * Default corpus: ../tests/conformance (fixtures/*.parquet + expected.json),
 * shared with the reference Python checker. Point CONFORMANCE_CORPUS at
 * another directory with an expected.json to run a different set.
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { checkFile } from '../src/lib/conformance/engine';
import { openDuckDB, type NodeDuckDB } from './helpers/duckdb-node';

interface Expectation {
  description?: string;
  /** `index`: run the index checker on this file whatever its footer keys. */
  check?: string;
  conformant: boolean;
  failed: string[];
  warned: string[];
}

const here = dirname(fileURLToPath(import.meta.url));
const corpusDir = resolve(process.env.CONFORMANCE_CORPUS ?? join(here, '../../tests/conformance'));
const expectedPath = join(corpusDir, 'expected.json');
const expected: Record<string, Expectation> = existsSync(expectedPath)
  ? JSON.parse(readFileSync(expectedPath, 'utf8'))
  : {};

function locate(file: string): string {
  for (const candidate of [join(corpusDir, file), join(corpusDir, 'fixtures', file)]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(`fixture ${file} not found under ${corpusDir}`);
}

describe.skipIf(Object.keys(expected).length === 0)(`conformance corpus (${corpusDir})`, () => {
  let duck: NodeDuckDB;
  beforeAll(async () => {
    duck = await openDuckDB();
  });
  afterAll(() => duck?.close());

  test.each(Object.entries(expected))('%s', async (file, want) => {
    const path = locate(file);
    duck.register(file, path);
    const qualityProfile = [...want.failed, ...want.warned].some((id) => id.startsWith('Q'));
    const report = await checkFile(duck.query, file, file, {
      qualityProfile,
      asIndex: want.check === 'index',
      resolveCompanion: async (f) => {
        const p = join(dirname(path), f);
        if (!existsSync(p)) return { status: 'missing', reason: `${p} does not exist` };
        duck.register(`${file}::${f}`, p);
        return { status: 'found', ref: `${file}::${f}`, sha256: async () => createHash('sha256').update(readFileSync(p)).digest('hex') };
      },
    });
    const got = {
      conformant: report.verdict === 'conformant',
      failed: report.results.filter((r) => r.status === 'fail').map((r) => r.id).sort(),
      warned: report.results.filter((r) => r.status === 'warn').map((r) => r.id).sort(),
    };
    const detail = report.results
      .filter((r) => r.status === 'fail' || r.status === 'warn')
      .map((r) => `${r.id}: ${r.messages.join(' | ')}`)
      .join('\n');
    expect(got, detail).toEqual({
      conformant: want.conformant,
      failed: [...want.failed].sort(),
      warned: [...want.warned].sort(),
    });
  });
});
