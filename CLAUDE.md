# OpenSpecLib — Claude Code Guide

## Project Overview

OpenSpecLib is an open file format for spectral libraries (the OpenSpecLib Format, spec version 1.0.0), a curated collection of public spectral libraries published in it, the `openspeclib` Python toolkit, and a browser viewer (`viewer/`).

In Format v1 each collection is one self-describing Apache Parquet file: one row per spectrum, inline `wavelengths` and `values`, flat snake_case columns, and the collection's provenance, licences and citations as JSON under the `openspeclib` footer key. An optional index file (`openspeclib-index.parquet`) lists every spectrum's metadata without the arrays. A release directory (= `openspeclib combine` output) is flat: `{collection}.parquet` files, the index, `id-migration.csv`, `checksums.txt` (plus `validation-report.json` from the release workflow). Releases bundle USGS (`usgs_splib07`), ECOSTRESS and EcoSIS; loaders also exist for RELAB, ASU TES, Bishop and OSSL.

## Repository Structure

- `docs/spec/format-v1.md` + `schemas/format-v1/*.json` — the normative specification (columns, vocabularies, footer schema, rules and quality bounds). Changed only through an RFC (`docs/rfcs/`); never edit them to match the code — fix the code
- `docs/spec/collections.md` — collection id registry
- `src/openspeclib/models.py` — Pydantic models: `SpectrumRecord` (one row), `CollectionInfo`, `DatasetInfo`, vocabularies as enums
- `src/openspeclib/storage.py` — all collection/index file I/O (`write_collection`, `write_rows`, `write_index`, `read_footer`, `read_index_footer`, `iter_records`). The writer runs the conformance checker on every file and refuses to keep a non-conformant one
- `src/openspeclib/conformance.py` — reference conformance checker (rules F1–F13, I1, R1–R5)
- `src/openspeclib/validate.py` — `openspeclib validate`: profiles `format` and `openspeclib` (adds quality rules Q1–Q4), allowlist, report. `validation_allowlist.json` is the shipped allowlist
- `src/openspeclib/reader.py` — fast read API: `openspeclib.open`, `openspeclib.open_index` (local via pyarrow, http(s) via DuckDB)
- `src/openspeclib/combine.py` — builds the release directory from ingested `{collection}.jsonl` + `{collection}.collection.json`
- `src/openspeclib/convert/` — `openspeclib convert` (ENVI/CSV → Format v1) and `openspeclib export` (→ ENVI/CSV)
- `src/openspeclib/loaders/` — one loader per source (usgs.py, ecostress.py, ecosis.py, ossl.py, relab.py, asu_tes.py, bishop.py), shared helpers in base.py. `usgs_geometry.py` holds the USGS geometry rules and their DS1035 evidence; `ecosis_datasets.json` is the EcoSIS inclusion/setup policy; `ecosis_scales.py` the EcoSIS scale inference and masking
- `src/openspeclib/cli.py` — Click CLI: download, ingest, combine, validate, convert, export
- `scripts/generate_schemas.py` — checks the code against `schemas/format-v1/` (writes nothing); `scripts/publish_huggingface.py` — Hugging Face mirror
- `tests/` — pytest, fixtures in `tests/fixtures/`, conformance fixtures in `tests/conformance/`. `tests/test_readme.py` runs the code blocks in README.md and docs/reading.md marked `<!-- readme-test: python… -->` / `<!-- readme-test: sql… -->` against a tiny built release
- `docs/` — reading, data-structure, processing-pipeline, validation, converting, ecosis-curation, provenance, licensing, adding-sources, publishing

## Key Commands

```bash
pip install -e ".[dev]"              # Install with all dependencies (includes duckdb)
pytest tests/ -v                     # Run tests
black src/ tests/ scripts/           # Format code
ruff check src/ tests/               # Lint (style, docstrings, type annotations)
mypy src/openspeclib/                # Strict type checking
python scripts/generate_schemas.py   # Check code against schemas/format-v1 (exits non-zero on drift)
openspeclib validate PATH --profile openspeclib   # Conformance + quality profile
```

## Development Standards

### Formatting

- **Black** is the formatter. Line length is 100 characters.
- Run `black --check src/ tests/ scripts/` to verify; CI will reject unformatted code.

### Linting

- **Ruff** enforces pycodestyle (E/W), pyflakes (F), isort (I), pydocstyle (D), and flake8-annotations (ANN).
- Tests and scripts are exempt from docstring and annotation rules.

### Docstrings

- Use **Google-style** docstrings on all public and private functions/methods.
- Include `Args:`, `Returns:`, `Yields:`, and `Raises:` sections where applicable.
- Example:
  ```python
  def parse_file(filepath: Path) -> SpectrumRecord:
      """Parse a single spectrum file.

      Args:
          filepath: Path to the spectrum text file.

      Returns:
          A ``SpectrumRecord`` populated with parsed data.

      Raises:
          ValueError: If no spectral data is found.
      """
  ```

### Type Hints

- All function arguments and return values must have type annotations.
- Use `X | None` (Python 3.10+ union syntax) for optional types in non-Pydantic code.
- Pydantic models use `Optional[X]` with `Field()` to avoid annotation conflicts.

### CI

- CI runs on every push/PR to `main` via `.github/workflows/ci.yml`.
- Three jobs: **lint** (black + ruff), **typecheck** (mypy), **test** (pytest on 3.11 + 3.12).
- All checks must pass before merging.

## Conventions

- The specification (`docs/spec/format-v1.md`, `schemas/format-v1/`) is normative; the code follows it
- All collection and index file I/O goes through `openspeclib.storage`; never write the Parquet files directly. The writer refuses non-conformant output (`NonConformantFileError`) — fix the records, do not bypass the check
- Loaders yield `SpectrumRecord` via generators; ids are `{collection}:{local_id}` built with `loaders.base.spectrum_id`, and `original_id` keeps the upstream id unchanged
- Wavelengths are stored in native units (`nm`, `um`, `cm-1`) — never convert or resample during ingest
- Missing or invalid bands are NaN in `values` — never keep source sentinels (e.g. `-1.23e34`); use `loaders.base.mask_invalid_values`
- Reflectance, transmittance and emissivity are fractions (0–1 nominal); loaders divide at ingest using the scale the source states and record the divisor in `extra["source_reflectance_divisor"]` (`openspeclib convert` writes the same key and renames the legacy `source_value_divisor` on re-import). Absorbance is decadic and not rescaled
- `quantity` and `geometry` are set only from documented evidence (headers, source documentation, package metadata), and the evidence is recorded (e.g. `extra["geometry_rule"]` in USGS, `setup.evidence` in `ecosis_datasets.json`). Otherwise geometry is `unknown` — never guess. Lab lamp-and-probe "bidirectional" measurements are `biconical`
- Modelled or computed spectra get `simulated` in `measurement_processing`
- Source-specific metadata goes in `extra`, not new columns
- Validation is strict. Quality issues (Q1–Q4) can be waived only by an entry in `src/openspeclib/validation_allowlist.json` with a written reason; Q1 (`value_scale`) and Q4 (`wavelength_extent`) only by exact id; an `id_prefix` must reach past `{collection}:`; conformance rules are never waivable
- Loaders report skips that make output incomplete via `BaseLoader.fatal_error_count`; `openspeclib ingest` then removes its output and exits 1
- Spectrum counts appear in exactly one README table (marked `<!-- counts: updated at release build -->`); do not repeat counts in other docs. CHANGELOG.md is exempt: its entries record counts as of each release
- All tests, formatting, lint and type checks should pass before any changes are committed
