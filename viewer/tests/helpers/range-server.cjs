// Static file server with HTTP Range support and a request log, run as a child process.
// Usage: node rangeserver.cjs <root>; prints "port <n>"; GET /__log returns the log as JSON.
const http = require('http'), fs = require('fs'), path = require('path');
const root = path.resolve(process.argv[2]);
const log = [];
const srv = http.createServer((req, res) => {
  if (req.url === '/__log') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify(log)); }
  const file = path.join(root, decodeURIComponent(req.url.split('?')[0]));
  if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404); return res.end(); }
  const size = fs.statSync(file).size;
  const m = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range || '');
  let start = 0, end = size - 1, code = 200;
  const headers = { 'Accept-Ranges': 'bytes', 'Last-Modified': fs.statSync(file).mtime.toUTCString() };
  if (m) { start = +m[1]; end = m[2] ? Math.min(+m[2], size - 1) : size - 1; code = 206; headers['Content-Range'] = `bytes ${start}-${end}/${size}`; }
  headers['Content-Length'] = end - start + 1;
  res.writeHead(code, headers);
  const sent = req.method === 'HEAD' ? 0 : end - start + 1;
  log.push({ method: req.method, url: req.url, range: req.headers.range || null, status: code, bytes: sent });
  if (req.method === 'HEAD') return res.end();
  fs.createReadStream(file, { start, end }).pipe(res);
});
srv.listen(0, '127.0.0.1', () => console.log('port ' + srv.address().port));
