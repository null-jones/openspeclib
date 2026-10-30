/**
 * The quality profile (Q1–Q4) against the Python validator: runs the
 * checker with the quality profile on every corpus fixture (and the release
 * collection with an allowlisted spectrum) and compares the failed, warned
 * and waived quality rules, and whether the profile was skipped, with
 * expected.json, written by generate_expected.py from
 * `openspeclib validate --profile openspeclib`.
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { checkFile } from '../../src/lib/conformance/engine';
import type { ConformanceReport } from '../../src/lib/conformance/types';
import { openDuckDB, type NodeDuckDB } from '../helpers/duckdb-node';

interface Expectation {
  failed: string[];
  warned: string[];
  waived: string[];
  skipped: boolean;
  sha256?: string;
}

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../../..');
const expected: Record<string, Expectation> = JSON.parse(readFileSync(join(here, 'expected.json'), 'utf8'));
const Q = ['Q1', 'Q2', 'Q3', 'Q4'];

let duck: NodeDuckDB;
beforeAll(async () => {
  duck = await openDuckDB();
});
afterAll(() => duck?.close());

/**
 * Quality results of a report and the collection reports nested in it. As
 * in the Python validator, only readable collection files are evaluated, so
 * "skipped" means the profile could not run on one of them.
 */
function summarise(report: ConformanceReport): Expectation {
  const files = [report, ...report.collections].filter((r) => r.kind === 'collection' && r.unreadable === null);
  const all = files.flatMap((r) => r.results.filter((x) => Q.includes(x.id)));
  const ids = (pred: (x: (typeof all)[number]) => boolean) => [...new Set(all.filter(pred).map((x) => x.id))].sort();
  return {
    failed: ids((x) => x.status === 'fail'),
    warned: ids((x) => x.status === 'warn'),
    waived: ids((x) => x.waived.length > 0),
    skipped: all.some((x) => x.status === 'skip'),
  };
}

describe('quality profile matches openspeclib validate --profile openspeclib', () => {
  test.each(Object.entries(expected))('%s', async (file, want) => {
    const path = join(root, file);
    if (!existsSync(path)) return;
    const bytes = readFileSync(path);
    if (want.sha256 && createHash('sha256').update(bytes).digest('hex') !== want.sha256) {
      console.warn(`${file} changed since expected.json was written; run generate_expected.py`);
      return;
    }
    const name = file.replace(/[^A-Za-z0-9._-]/g, '_');
    duck.register(name, path);
    const report = await checkFile(duck.query, name, name, {
      qualityProfile: true,
      resolveCompanion: async (f) => {
        const p = join(dirname(path), f);
        if (!existsSync(p)) return { status: 'missing', reason: `${p} does not exist` };
        const ref = `${name}::${f}`.replace(/\\/g, '_');
        duck.register(ref, p);
        return { status: 'found', ref, sha256: async () => createHash('sha256').update(readFileSync(p)).digest('hex') };
      },
    });
    const { sha256: _ignored, ...rest } = want;
    void _ignored;
    expect(summarise(report)).toEqual(rest);
  });
});
