import { fileURLToPath } from 'node:url'
import { defineConfig, searchForWorkspaceRoot } from 'vite'
import react from '@vitejs/plugin-react'
import { parquetRanges } from './dev/parquet-ranges'

// The OpenSpecLib Format v1 spec's machine-readable parts live outside the
// viewer package, in ../schemas/format-v1. They are imported at build time
// (the Format reference and the conformance checker are generated from
// them), so there is a single source of truth and nothing is copied.
const specDir = fileURLToPath(new URL('../schemas/format-v1', import.meta.url))
// The allowlist `openspeclib validate --profile openspeclib` ships with: the
// checker's quality profile applies the same waivers.
const allowlistFile = fileURLToPath(new URL('../src/openspeclib/validation_allowlist.json', import.meta.url))

// https://vite.dev/config/
export default defineConfig({
  base: '/openspeclib/',
  // Dev server: serve public/data/*.parquet with Range support like the deployed site.
  plugins: [react(), parquetRanges(fileURLToPath(new URL('./public/data', import.meta.url)))],
  resolve: {
    alias: {
      '@spec': specDir,
      '@allowlist': allowlistFile,
    },
  },
  server: {
    fs: {
      // Setting `allow` replaces Vite's default, so keep the workspace root.
      allow: [searchForWorkspaceRoot(process.cwd()), specDir, allowlistFile],
    },
  },
  optimizeDeps: {
    exclude: ['@duckdb/duckdb-wasm'],
  },
})
