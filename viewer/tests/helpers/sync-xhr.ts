/**
 * A minimal synchronous XMLHttpRequest for Node, so DuckDB-WASM's browser
 * runtime (which reads remote files with sync XHR) can run in tests. Each
 * request runs in a child Node process; the server must be in another
 * process too, since this one blocks while waiting.
 *
 * Requests for DuckDB extensions are answered from DuckDB's local extension
 * cache (~/.duckdb/extensions) when the file is there.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const FETCH = `
const u = new URL(process.argv[1]);
const lib = require(u.protocol === 'https:' ? 'https' : 'http');
const r = lib.request(u, { method: process.argv[2], headers: JSON.parse(process.argv[3]) }, (res) => {
  const c = [];
  res.on('data', (d) => c.push(d));
  res.on('end', () => process.stdout.write(JSON.stringify({ s: res.statusCode, h: res.headers, b: Buffer.concat(c).toString('base64') })));
});
r.on('error', (e) => process.stdout.write(JSON.stringify({ s: 0, h: {}, b: '', e: String(e) })));
r.end();`;

function toArrayBuffer(b: Buffer): ArrayBuffer {
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer;
}

export class SyncXHR {
  method = 'GET';
  url = '';
  responseType = '';
  status = 0;
  response: ArrayBuffer = new ArrayBuffer(0);
  private headers: Record<string, string> = {};
  private responseHeaders: Record<string, string> = {};

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
    this.headers = {};
  }
  setRequestHeader(k: string, v: string): void {
    this.headers[k] = v;
  }
  send(): void {
    const ext = /^https:\/\/extensions\.duckdb\.org\/(.*)$/.exec(this.url);
    const cached = ext && join(homedir(), '.duckdb/extensions/extensions.duckdb.org', ext[1]);
    if (cached && existsSync(cached)) {
      const b = readFileSync(cached);
      this.status = 200;
      this.responseHeaders = { 'content-length': String(b.length) };
      this.response = toArrayBuffer(b);
      return;
    }
    const out = execFileSync(process.execPath, ['-e', FETCH, this.url, this.method, JSON.stringify(this.headers)], {
      maxBuffer: 1 << 30,
    });
    const j = JSON.parse(out.toString()) as { s: number; h: Record<string, string>; b: string };
    this.status = j.s;
    this.responseHeaders = j.h;
    this.response = toArrayBuffer(Buffer.from(j.b, 'base64'));
  }
  getResponseHeader(k: string): string | null {
    const v = this.responseHeaders[k.toLowerCase()];
    return v === undefined ? null : String(v);
  }
  getAllResponseHeaders(): string {
    return Object.entries(this.responseHeaders)
      .map(([k, v]) => `${k}: ${v}`)
      .join('\r\n');
  }
}
