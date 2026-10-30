// Static file server with HTTP Range support and a request log, run as a child process.
// Usage: node rangeserver.cjs <root>; prints "port <n>"; GET /__log returns the log as JSON.
// Paths under /gh-pages/ answer the way GitHub Pages answers a browser (which always sends
// Accept-Encoding: gzip): every response, a Range one too, is cut from a gzip copy of the
// file, with Content-Encoding: gzip and Content-Range counted in compressed bytes.
const http = require('http'), fs = require('fs'), path = require('path'), zlib = require('zlib');
const root = path.resolve(process.argv[2]);
const log = [];
const gzipped = new Map();
const GH_PAGES = '/gh-pages/';
const srv = http.createServer((req, res) => {
  if (req.url === '/__log') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify(log)); }
  let urlPath = decodeURIComponent(req.url.split('?')[0]);
  const ghPages = urlPath.startsWith(GH_PAGES);
  if (ghPages) urlPath = urlPath.slice(GH_PAGES.length - 1);
  const file = path.join(root, urlPath);
  if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404); return res.end(); }
  let body = null;
  if (ghPages) {
    if (!gzipped.has(file)) gzipped.set(file, zlib.gzipSync(fs.readFileSync(file)));
    body = gzipped.get(file);
  }
  const size = body ? body.length : fs.statSync(file).size;
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || '');
  let start = 0, end = size - 1, code = 200;
  const headers = { 'Accept-Ranges': 'bytes', 'Last-Modified': fs.statSync(file).mtime.toUTCString() };
  if (body) headers['Content-Encoding'] = 'gzip';
  if (m && (m[1] || m[2])) {
    if (m[1]) { start = +m[1]; end = m[2] ? Math.min(+m[2], size - 1) : size - 1; }
    else { start = Math.max(0, size - +m[2]); }
    code = 206; headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
  }
  headers['Content-Length'] = end - start + 1;
  res.writeHead(code, headers);
  const sent = req.method === 'HEAD' ? 0 : end - start + 1;
  log.push({ method: req.method, url: req.url, range: req.headers.range || null, status: code, bytes: sent });
  if (req.method === 'HEAD') return res.end();
  if (body) return res.end(body.subarray(start, end + 1));
  fs.createReadStream(file, { start, end }).pipe(res);
});
srv.listen(0, '127.0.0.1', () => console.log('port ' + srv.address().port));
