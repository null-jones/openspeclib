/**
 * Remote collection files on hosts where HTTP Range reads do not work.
 *
 * DuckDB-WASM reads a remote Parquet file with Range requests: the footer,
 * then about one row group per spectrum. Some static hosts break this.
 * GitHub Pages answers a browser's Range request with a slice of its gzip
 * copy of the file (`Content-Encoding: gzip`, `Content-Range` counted in
 * compressed bytes), and a browser cannot opt out of gzip. DuckDB then reads
 * garbage ("too small to be a Parquet file").
 *
 * So before DuckDB reads a remote file, a 4-byte suffix Range request checks
 * that the file's last bytes come back as the Parquet magic `PAR1`. If they
 * do, DuckDB reads the URL with Range requests; if not, the whole file is
 * downloaded once (a plain GET, which the browser decompresses) and DuckDB
 * reads it from memory.
 */

const PARQUET_MAGIC = [0x50, 0x41, 0x52, 0x31]; // "PAR1"

type FetchFn = typeof fetch;

/**
 * Whether a Range request for the last 4 bytes of `url` returns them as they
 * are on disk. Any failure counts as no.
 */
export async function rangeReadsWork(url: string, fetchFn: FetchFn = fetch): Promise<boolean> {
  try {
    const res = await fetchFn(url, { headers: { Range: 'bytes=-4' } });
    if (res.status !== 206) {
      // The server ignored the Range header: do not download the whole body here.
      await res.body?.cancel().catch(() => undefined);
      return false;
    }
    const bytes = new Uint8Array(await res.arrayBuffer());
    return bytes.length === 4 && PARQUET_MAGIC.every((b, i) => bytes[i] === b);
  } catch {
    return false;
  }
}

/**
 * Make a resolver from a file reference to one DuckDB can read. Local and
 * registered refs pass through; an http(s) URL is probed once, and if Range
 * reads fail it is downloaded and registered under a new name with
 * `register`. Results are cached per URL; a failure is not, so a retry can
 * succeed.
 */
export function remoteFileResolver(
  register: (name: string, bytes: Uint8Array) => Promise<void>,
  fetchFn: FetchFn = fetch,
): (ref: string) => Promise<string> {
  const resolved = new Map<string, Promise<string>>();
  let counter = 0;
  return (ref) => {
    if (!/^https?:\/\//i.test(ref)) return Promise.resolve(ref);
    let pending = resolved.get(ref);
    if (!pending) {
      pending = (async () => {
        if (await rangeReadsWork(ref, fetchFn)) return ref;
        const res = await fetchFn(ref);
        if (!res.ok) throw new Error(`Could not download ${ref}: HTTP ${res.status}`);
        const name = `remote-download/${++counter}.parquet`;
        await register(name, new Uint8Array(await res.arrayBuffer()));
        return name;
      })().catch((e) => {
        resolved.delete(ref);
        throw e;
      });
      resolved.set(ref, pending);
    }
    return pending;
  };
}
