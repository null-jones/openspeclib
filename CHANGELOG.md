# Changelog

All notable changes to OpenSpecLib are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org).

Two version numbers are involved:

- **Release version** (this file): the `openspeclib` Python package and the data releases built with it share one version, starting with 0.2.0. Releases v0.0.1 to v0.0.7 were data releases tagged in git.
- **Specification version**: the [OpenSpecLib Format](docs/spec/format-v1.md) has its own version (1.0.0), which changes only through the [RFC process](docs/rfcs/README.md).

## [Unreleased]

## [0.2.0] - 2026-10-30

The first release in [OpenSpecLib Format v1](docs/spec/format-v1.md) (specification 1.0.0, [RFC 0001](docs/rfcs/0001-format-v1.md)). It also fixes data errors present in every earlier release, so users of v0.0.x releases should switch rather than keep the old files. The release holds 29,669 spectra: USGS Spectral Library Version 7 (2,457), ECOSTRESS (3,451) and a curated EcoSIS collection (23,761).

### Added

- **OpenSpecLib Format v1**: one self-describing Parquet file per collection, with collection metadata as JSON under the `openspeclib` footer key (GeoParquet-style), a normative specification with machine-readable schemas, vocabularies and rules (`schemas/format-v1/`), and a shared conformance corpus (`tests/conformance/`).
- Required `quantity` and `geometry` columns. Geometry uses the Schaepman-Strub et al. (2006) nomenclature and describes the physical setup, with `unknown` when the source does not document it.
- Measurement geometry mapped from source evidence: USGS from Data Series 1035 and the per-spectrum metadata (`extra.geometry_rule` names the rule used), ECOSTRESS from the `Measurement:` header (`extra.source_geometry_term` keeps the source's wording), EcoSIS per dataset and per spectrum from package metadata. 88% of spectra have a known geometry.
- Optional extensions (`material`, `sample`, `measurement`, `bandpass`, `uncertainty`, `quality`) declared in the footer.
- SPDX licence expressions and documented `LicenseRef-` definitions, per row and in the footer.
- URL-safe spectrum ids (`{collection}:{local_id}`, rule F5) and `id-migration.csv`, which maps every id that changed since releases up to v0.0.7 to its current id.
- Index file (`openspeclib-index.parquet`) for searching all collections without reading spectral arrays.
- `openspeclib validate` with two profiles: `format` (conformance rules F1–F13, I1, recommendations R1–R5; the default) and `openspeclib` (adds quality rules Q1–Q4). Releases ship the validation report. The checks are strict where the specification is: `format_version` must be a semantic version without leading zeros, SPDX special values such as `NOASSERTION` are case-sensitive, schema patterns follow JSON Schema (ECMA-262) semantics, and a column may appear only once. Files written under a newer 1.x minor version get warnings, not errors, for what this version does not know.
- `openspeclib convert` and `openspeclib export`: ENVI spectral libraries (`.sli`/`.hdr`) and CSV (wide and long) into and out of Format v1, with explicit scale handling and licence attribution carried with every export.
- A fast reader: `openspeclib.open` and `openspeclib.open_index` (Arrow, numpy, pandas; remote files with `openspeclib[remote]`).
- Viewer: a Format area that documents the standard and checks any Parquet file for conformance in the browser; opening any conforming collection, local or by URL, alongside the OpenSpecLib data; quantity and geometry filters and columns, with a notice when plotted spectra mix geometries; automatic migration of old shared links.
- Written EcoSIS inclusion policy ([docs/ecosis-curation.md](docs/ecosis-curation.md)): datasets whose spectra carry material identity are included in full; anonymous replicate datasets are sampled deterministically; Cedar Creek is excluded; the salt-marsh package's 7,000 PLSR-coefficient rows are excluded while its measured spectra are kept.
- Validation allowlist: a spectrum can be exempted from a quality check only through an entry with a written reason.
- Project governance and publishing: [CONTRIBUTING.md](CONTRIBUTING.md), the [RFC process](docs/rfcs/README.md), the [collection id registry](docs/spec/collections.md), [CITATION.cff](CITATION.cff), Zenodo metadata, [docs/publishing.md](docs/publishing.md), and `scripts/publish_huggingface.py` for mirroring releases to Hugging Face under immutable versioned paths.

### Changed

- Wavelength axes are stored inline in each row, in the source's native unit. The separate `wavelengths.parquet` registry is gone; this costs about 3.6% in file size.
- Columns are flat snake_case (`material_name`, `values`) instead of dotted names (`material.name`, `spectral_data.values`).
- Spectral values are float32 with BYTE_STREAM_SPLIT encoding; row groups hold 100 spectra, so a single spectrum can be fetched over HTTP with range requests.
- The index file replaces the catalog JSON (27–105 MB in earlier releases).
- The writer refuses to produce a file that fails conformance, and validation is strict: errors fail the release build.
- The release version and the `openspeclib` package version are now the same number.

### Removed

- `wavelengths.parquet` and the catalog JSON (`openspeclib-catalog-<version>.json`) as release assets, and `licenses.json` as an output of `openspeclib combine` (it was never a release asset); their content is in each collection file's footer and in the index file.
- `schemas/library.parquet-schema.md` and the v0 JSON schemas; the [specification](docs/spec/format-v1.md) and `schemas/format-v1/` replace them.
- The hand-written per-source Hugging Face cards (`huggingface_cards/`); `scripts/publish_huggingface.py` generates the dataset card from the collection footers.

### Fixed

- **ECOSTRESS scale.** Values were stored in percent while labelled 0–1. They are now divided by 100 using each file's `Y Units` header, except for 36 USGS rock spectra whose header says percent but whose values are already fractions; those keep their values, and the decision is recorded in `extra`.
- **ECOSTRESS categories.** A specific `Class` now wins over the general `Type`: 17 lunar soils are `lunar` and 5 snow, ice and frost spectra are `snow_ice`. The source `Type` and `Class` are kept in `extra`.
- **USGS categories and names.** Chapter S (mixtures) spectra were all labelled `soil`; they are now `mixture`, `rock`, `soil` or `mineral` from each spectrum's title and description, and laboratory mixtures filed in other chapters (basalt with organic compounds, clay with solvents, mineral with dry grass) are `mixture`; the rule is in `extra.category_rule`. Material names and sample ids are split as the SPECPR record title gives them, refined by the sample's metadata page, instead of taking only the title's first word; measurement temperatures move from the sample id to `extra.sample_temperature_k`.
- **ECOSTRESS descriptions.** Parts the source appended to a sample description ("Sample was …", "Collected by …") are now separated from it by a full stop instead of running on.
- **ECOSTRESS labels.** 128 transmittance spectra were labelled reflectance.
- **USGS labels.** The spectrum with the `TRAN` data-type token in its filename (a Mylar transmittance standard) was labelled reflectance; it is now transmittance.
- **ECOSTRESS licence.** Earlier releases called the library public domain. JPL is operated by Caltech for NASA, so its works are not automatically U.S. Government public domain; the licence now points to the provider's terms (`LicenseRef-ECOSTRESS-Terms`).
- **USGS missing bands.** 706,826 values in 1,512 spectra held the sentinel −1.23e34; they are now NaN.
- **USGS computed spectra.** 32 mathematically computed spectra are now flagged `simulated`; computed mixtures use the `mixture` category.
- **EcoSIS truncation.** Releases v0.0.6 and v0.0.7 shipped randomly truncated downloads, holding only 16–21% of the curated packages' 130,931 spectra, because the downloader treated transient failures as the end of the data. The downloader now retries and fails loudly.
- **EcoSIS units.** 134 spectra in two datasets have micrometre axes and were labelled nanometres; they are now stored as `um`.
- **EcoSIS labels.** All 7,821 CABO spectra were labelled reflectance; 1,971 are transmittance and are now labelled so, and CABO's derived absorptance is excluded. Instruments and setups now follow each spectrum's own metadata in multi-instrument packages. The EMIT water spectra are model output and are flagged `simulated`.
- **EcoSIS duplicates and noise.** Exact duplicate spectra are removed; spectra that the source recorded under conflicting material names are dropped or, where they are one canopy measurement linked to several samples, kept once. Out-of-range noise bands are masked to NaN, and the masking is recorded in the spectrum's provenance. Spectra far darker than the rest of their dataset (mis-scaled copies) and saturated spectra (more than half the bands at one value) are dropped before sampling, and the drops are listed in each dataset's `inclusion_notes`.

## [0.0.7] - 2026-05-20

### Added

- Additional metadata fields and descriptions for spectra.
- Hugging Face dataset cards for USGS Speclib 07 and ECOSTRESS (`huggingface_cards/`).
- Viewer information dialog; `scripts/deploy_local.sh`.

### Changed

- Improvements to data loading and viewer layout.

### Fixed

- Stale release-version handling in the viewer.

## [0.0.6] - 2026-04-27

### Added

- OSSL (Open Soil Spectral Library) loader (not included in the release data).
- Additional EcoSIS datasets, with per-dataset scale handling.
- Shared wavelength registry (`wavelengths.parquet`).

### Changed

- Faster querying and DuckDB loading in the viewer, with a loading spinner.

## [0.0.5] - 2026-04-16

### Added

- ECOSTRESS in the release build.
- Better filtering and spectrum counts in the viewer; the release version is shown in the viewer header.

### Fixed

- EcoSIS ingest and combine steps, NaN handling, and recursive file discovery.
- Viewer queries.

## [0.0.4] - 2026-04-16

### Added

- Web viewer (React, DuckDB-WASM), deployed with GitHub Actions.
- A subset of EcoSIS.
- Sensor resampling using published relative spectral response curves, and more sensors.
- Licensing documentation.

### Fixed

- Build and plot-rendering issues.

## [0.0.3] - 2026-04-15

### Fixed

- USGS parsing issue.

## [0.0.2] - 2026-04-15

### Changed

- One Parquet file per source instead of one per source and material category.

### Fixed

- USGS ingestion; variable substitution in the release workflow's README.

## [0.0.1] - 2026-04-15

### Added

- First release: Pydantic data model, loaders for USGS Speclib 07, ECOSTRESS, RELAB, ASU TES and Bishop, a JSON catalog with Parquet spectra, the `openspeclib` CLI (download, ingest, combine, validate), CI and a release workflow.

[Unreleased]: https://github.com/null-jones/openspeclib/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/null-jones/openspeclib/compare/v0.0.7...v0.2.0
[0.0.7]: https://github.com/null-jones/openspeclib/compare/v0.0.6...v0.0.7
[0.0.6]: https://github.com/null-jones/openspeclib/compare/v0.0.5...v0.0.6
[0.0.5]: https://github.com/null-jones/openspeclib/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/null-jones/openspeclib/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/null-jones/openspeclib/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/null-jones/openspeclib/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/null-jones/openspeclib/releases/tag/v0.0.1
