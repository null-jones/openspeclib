import { createHash } from 'node:crypto';
import { describe, expect, test } from 'vitest';
import { Sha256, sha256OfBlob } from '../src/lib/conformance/sha256';

const ref = (b: Uint8Array) => createHash('sha256').update(b).digest('hex');

describe('Sha256', () => {
  test.each([0, 1, 55, 56, 63, 64, 65, 119, 120, 1000, 100_003])('%i bytes, in uneven pieces', (n) => {
    const data = Uint8Array.from({ length: n }, (_, i) => (i * 31 + 7) & 0xff);
    const h = new Sha256();
    for (let off = 0, step = 1; off < n; off += step, step = (step * 3 + 1) % 97 || 1) h.update(data.subarray(off, off + step));
    expect(h.hex()).toBe(ref(data));
  });
  test('Blob in slices', async () => {
    const data = Uint8Array.from({ length: 300_000 }, (_, i) => i & 0xff);
    expect(await sha256OfBlob(new Blob([data]))).toBe(ref(data));
  });
});
