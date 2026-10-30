# OpenSpecLib Viewer

A browser app for the OpenSpecLib data release. It lets you search the
release's spectra and plot them. You can export a selection with its licences
and citations, and open your own files in the OpenSpecLib Format. The Format
area holds the format reference and a conformance checker that runs in the
browser.

Everything runs client-side. DuckDB-WASM reads the release index
(`openspeclib-index.parquet`). It then fetches single spectra from the
collection files with HTTP Range requests, about one row group per spectrum.
There is no server. Files you check or open are read in place and never
uploaded.

React 19, TypeScript, Vite, Tailwind CSS, DuckDB-WASM, Plotly.

## Develop

```bash
npm install
npm run dev        # http://localhost:5173/openspeclib/
npm run lint       # ESLint
npm test           # Vitest: checker, data layer, exports, HTTP Range reads
npm run build      # type-check and build into dist/
npm run preview    # serve dist/
```

The format reference and the conformance checker are built from the normative
spec files in `../schemas/format-v1/`, imported through the Vite alias
`@spec`. The quality profile uses the allowlist that `openspeclib validate`
ships with (`../src/openspeclib/validation_allowlist.json`, alias
`@allowlist`). Nothing is copied, so there is one source of truth.

## Data

The viewer serves one data release, set by `OPENSPECLIB_VERSION` in
`src/constants/urls.ts`. The release files go in `public/data/`: the
collection `.parquet` files, `openspeclib-index.parquet`, `id-migration.csv`
and `checksums.txt`. They come from the GitHub release and are not committed.

In development, a small middleware in `vite.config.ts` serves
`public/data/*.parquet` with HTTP Range support, as the deployed site does.
It lists the requests it served at `/openspeclib/__data-requests`.

## Deploy

`../scripts/deploy_local.sh` downloads the release data into `public/data/`,
builds the viewer and serves it at http://localhost:4173. Pass `--tag` to pick
a release, `--skip-data` to keep the data already there, or `--no-serve` to
build only. The `deploy-viewer.yml` GitHub Actions workflow does the same for
GitHub Pages.

## Tests

- `tests/corpus.test.ts` checks every file in `../tests/conformance/` and
  compares the verdicts with that corpus's `expected.json`. Set
  `CONFORMANCE_CORPUS` to use another directory.
- `tests/quality/` compares the quality profile (Q1 to Q4, including
  allowlist waivers) with `openspeclib validate --profile openspeclib`. Run
  `python viewer/tests/quality/generate_expected.py` from the repository root
  after changing the validator, the bounds or the allowlist.
- `tests/http-range.test.ts` checks that reading one spectrum over HTTP uses
  Range requests only.
