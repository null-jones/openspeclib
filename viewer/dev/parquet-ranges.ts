/**
 * Dev server only: serve public/data/*.parquet as the production host does
 * for HTTP range reads (Accept-Ranges and a content type on HEAD as well as
 * GET, 206 / 416 for ranges), so `npm run dev` reads single spectra with
 * Range requests exactly like the deployed site. The last requests are
 * listed at /openspeclib/__data-requests for checking byte counts.
 */
import { createReadStream, statSync } from 'node:fs'
import { resolve, sep } from 'node:path'
import type { Plugin } from 'vite'

/** Requests kept for /__data-requests. */
const LOG_LIMIT = 1000

/**
 * A single `Range: bytes=…` header against a file of `size` bytes, per RFC
 * 9110 section 14: `{ start, end }` (inclusive) to serve 206, `'unsatisfiable'`
 * for a well-formed range that starts beyond the end (416), or null to
 * ignore the header and serve the whole file (200): no header, invalid
 * syntax such as `bytes=-` or `bytes=5-3`, or several ranges.
 */
export function parseRange(header: string | undefined, size: number): { start: number; end: number } | 'unsatisfiable' | null {
  // Range units are case-insensitive (RFC 9110 section 14.1).
  const m = /^bytes=(\d*)-(\d*)$/i.exec((header ?? '').trim())
  if (!m || (m[1] === '' && m[2] === '')) return null
  if (m[1] === '') {
    // Suffix range: the last n bytes.
    const n = Number(m[2])
    if (n === 0 || size === 0) return 'unsatisfiable'
    return { start: Math.max(0, size - n), end: size - 1 }
  }
  const start = Number(m[1])
  if (m[2] !== '' && Number(m[2]) < start) return null
  if (start >= size) return 'unsatisfiable'
  return { start, end: m[2] === '' ? size - 1 : Math.min(Number(m[2]), size - 1) }
}

export function parquetRanges(dataDir: string): Plugin {
  const root = resolve(dataDir)
  const log: Array<{ method: string; file: string; range: string | null; status: number; bytes: number }> = []
  return {
    name: 'openspeclib-parquet-ranges',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = (req.url ?? '').split('?')[0]
        if (path.endsWith('/__data-requests')) {
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify(log))
          return
        }
        const m = /\/data\/([^/]+\.parquet)$/.exec(path)
        if (!m || (req.method !== 'GET' && req.method !== 'HEAD')) return next()
        let name: string
        try {
          name = decodeURIComponent(m[1])
        } catch {
          res.statusCode = 400
          res.end()
          return
        }
        // An encoded `/` (`%2F`) must not lead out of public/data.
        const file = resolve(root, name)
        if (!file.startsWith(root + sep)) {
          res.statusCode = 400
          res.end()
          return
        }
        let size: number
        try {
          size = statSync(file).size
        } catch {
          return next()
        }
        res.setHeader('Accept-Ranges', 'bytes')
        res.setHeader('Content-Type', 'application/octet-stream')
        res.setHeader('Cache-Control', 'no-cache')
        const range = parseRange(req.headers.range, size)
        if (range === 'unsatisfiable') {
          res.statusCode = 416
          res.setHeader('Content-Range', `bytes */${size}`)
          res.end()
          return
        }
        const start = range?.start ?? 0
        const end = range?.end ?? size - 1
        if (range) {
          res.statusCode = 206
          res.setHeader('Content-Range', `bytes ${start}-${end}/${size}`)
        }
        res.setHeader('Content-Length', String(Math.max(0, end - start + 1)))
        log.push({ method: req.method, file: name, range: req.headers.range ?? null, status: res.statusCode, bytes: req.method === 'HEAD' ? 0 : Math.max(0, end - start + 1) })
        if (log.length > LOG_LIMIT) log.splice(0, log.length - LOG_LIMIT)
        if (req.method === 'HEAD' || size === 0) {
          res.end()
          return
        }
        createReadStream(file, { start, end }).pipe(res)
      })
    },
  }
}
