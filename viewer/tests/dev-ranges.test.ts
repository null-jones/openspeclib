/** The dev server's Range middleware for public/data/*.parquet (dev/parquet-ranges.ts). */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, describe, expect, test } from 'vitest';
import { parquetRanges, parseRange } from '../dev/parquet-ranges';

describe('parseRange (RFC 9110)', () => {
  test('well-formed ranges', () => {
    expect(parseRange('bytes=0-9', 100)).toEqual({ start: 0, end: 9 });
    expect(parseRange('Bytes=0-9', 100)).toEqual({ start: 0, end: 9 });
    expect(parseRange('bytes=90-', 100)).toEqual({ start: 90, end: 99 });
    expect(parseRange('bytes=90-500', 100)).toEqual({ start: 90, end: 99 });
    expect(parseRange('bytes=-10', 100)).toEqual({ start: 90, end: 99 });
    expect(parseRange('bytes=-500', 100)).toEqual({ start: 0, end: 99 });
  });
  test('invalid syntax is ignored (200), a range beyond the end is unsatisfiable (416)', () => {
    expect(parseRange(undefined, 100)).toBeNull();
    expect(parseRange('bytes=-', 100)).toBeNull();
    expect(parseRange('bytes=5-3', 100)).toBeNull();
    expect(parseRange('bytes=0-1,5-6', 100)).toBeNull();
    expect(parseRange('items=0-1', 100)).toBeNull();
    expect(parseRange('bytes=100-', 100)).toBe('unsatisfiable');
    expect(parseRange('bytes=-0', 100)).toBe('unsatisfiable');
  });
});

describe('middleware', () => {
  const root = mkdtempSync(join(tmpdir(), 'osl-dev-'));
  const data = join(root, 'public', 'data');
  mkdirSync(data, { recursive: true });
  writeFileSync(join(data, 'a.parquet'), 'x'.repeat(100));
  writeFileSync(join(root, 'secret.parquet'), 'secret');
  afterAll(() => rmSync(root, { recursive: true, force: true }));

  let handler!: (req: unknown, res: unknown, next: () => void) => void;
  const plugin = parquetRanges(data);
  (plugin.configureServer as (s: unknown) => void)({ middlewares: { use: (h: typeof handler) => (handler = h) } });

  const call = async (url: string, range?: string) =>
    new Promise<{ status: number; headers: Record<string, string>; body: string; next: boolean }>((done) => {
      const headers: Record<string, string> = {};
      let body = '';
      const res = {
        statusCode: 200,
        setHeader: (k: string, v: string) => (headers[k.toLowerCase()] = v),
        write: (c: Buffer | string) => ((body += c.toString()), true),
        end: (c?: Buffer | string) => {
          if (c) body += c.toString();
          done({ status: res.statusCode, headers, body, next: false });
        },
        on: () => res,
        once: () => res,
        emit: () => true,
        removeListener: () => res,
      };
      handler({ url, method: 'GET', headers: range ? { range } : {} }, res, () => done({ status: 0, headers, body, next: true }));
    });

  test('a range of a data file', async () => {
    const r = await call('/openspeclib/data/a.parquet?v=1', 'bytes=10-19');
    expect(r.status).toBe(206);
    expect(r.headers['content-range']).toBe('bytes 10-19/100');
    expect(r.headers['accept-ranges']).toBe('bytes');
  });
  test('an encoded slash cannot leave public/data; bad escapes are 400', async () => {
    expect((await call('/openspeclib/data/..%2Fsecret.parquet')).status).toBe(400);
    expect((await call('/openspeclib/data/..%2F..%2Fsecret.parquet')).status).toBe(400);
    expect((await call('/openspeclib/data/%E0%A4%A.parquet')).status).toBe(400);
  });
  test('beyond the end is 416; other files pass through', async () => {
    expect((await call('/openspeclib/data/a.parquet', 'bytes=200-')).status).toBe(416);
    expect((await call('/openspeclib/data/missing.parquet')).next).toBe(true);
    expect((await call('/openspeclib/index.html')).next).toBe(true);
  });
});
