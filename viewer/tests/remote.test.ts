import { describe, expect, test, vi } from 'vitest';
import { rangeReadsWork, remoteFileResolver } from '../src/lib/data/remote';

const PAR1 = new Uint8Array([0x50, 0x41, 0x52, 0x31]);
const URL_A = 'https://example.org/data/a.parquet?v=1';

function response(status: number, body: Uint8Array<ArrayBuffer>): Response {
  return new Response(body, { status });
}

describe('rangeReadsWork', () => {
  test('a 206 with the Parquet magic passes', async () => {
    const fetchFn = vi.fn(async () => response(206, PAR1));
    expect(await rangeReadsWork(URL_A, fetchFn)).toBe(true);
    expect(fetchFn).toHaveBeenCalledWith(URL_A, { headers: { Range: 'bytes=-4' } });
  });

  test('other bytes, a full 200 response, an empty body or a network error fail', async () => {
    expect(await rangeReadsWork(URL_A, async () => response(206, new Uint8Array([0x1f, 0x8b, 8, 0])))).toBe(false);
    expect(await rangeReadsWork(URL_A, async () => response(200, PAR1))).toBe(false);
    expect(await rangeReadsWork(URL_A, async () => response(206, new Uint8Array()))).toBe(false);
    expect(
      await rangeReadsWork(URL_A, async () => {
        throw new TypeError('net::ERR_CONTENT_DECODING_FAILED');
      }),
    ).toBe(false);
  });
});

describe('remoteFileResolver', () => {
  test('local and registered refs pass through without requests', async () => {
    const fetchFn = vi.fn();
    const resolve = remoteFileResolver(vi.fn(), fetchFn);
    expect(await resolve('release/openspeclib-index.parquet')).toBe('release/openspeclib-index.parquet');
    expect(fetchFn).not.toHaveBeenCalled();
  });

  test('a URL whose Range reads work is kept, and probed once', async () => {
    const fetchFn = vi.fn(async () => response(206, PAR1));
    const register = vi.fn();
    const resolve = remoteFileResolver(register, fetchFn);
    expect(await resolve(URL_A)).toBe(URL_A);
    expect(await resolve(URL_A)).toBe(URL_A);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(register).not.toHaveBeenCalled();
  });

  test('otherwise the file is downloaded once, even for concurrent callers', async () => {
    const file = new Uint8Array([...PAR1, 1, 2, 3, ...PAR1]);
    const fetchFn = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) =>
      init?.headers ? response(206, new Uint8Array([9, 9, 9, 9])) : response(200, file),
    );
    const register = vi.fn(async () => undefined);
    const resolve = remoteFileResolver(register, fetchFn);
    const [a, b] = await Promise.all([resolve(URL_A), resolve(URL_A)]);
    expect(a).toBe(b);
    expect(a).not.toBe(URL_A);
    expect(fetchFn).toHaveBeenCalledTimes(2);
    expect(register).toHaveBeenCalledTimes(1);
    expect(register).toHaveBeenCalledWith(a, file);
  });

  test('a failed download is reported and not cached', async () => {
    let fail = true;
    const fetchFn = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.headers) return response(200, new Uint8Array());
      return fail ? response(503, new Uint8Array()) : response(200, PAR1);
    });
    const resolve = remoteFileResolver(async () => undefined, fetchFn);
    await expect(resolve(URL_A)).rejects.toThrow(/HTTP 503/);
    fail = false;
    await expect(resolve(URL_A)).resolves.toMatch(/^remote-download\//);
  });
});
