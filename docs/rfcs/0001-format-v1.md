# RFC 0001: OpenSpecLib Format v1

- **Status:** Accepted (implemented in release 0.2.0; becomes Implemented when 0.2.0 is published)
- **Author(s):** Ellie Jones
- **Created:** 2026-10-29
- **Discussion:** none: bootstrap RFC, accepted by the maintainer without the 14-day discussion window
- **Spec version:** 1.0.0
- **Amends / supersedes:** none. Replaces the unversioned layout used by releases v0.0.1 to v0.0.7 ("v0").

## Summary

Define a published, versioned file format for spectral libraries: one self-describing Apache Parquet file per collection, with flat snake_case columns, inline wavelength axes, a required `quantity` and `geometry`, NaN for missing values, SPDX licences, optional column groups (extensions), an optional index file for search, and a set of numbered conformance rules with a checker. The normative text is [`docs/spec/format-v1.md`](../spec/format-v1.md). OpenSpecLib release 0.2.0 is the first release in this format.

## Motivation

Releases v0.0.1 to v0.0.7 used a layout that grew out of the toolkit's internal data model. It had no specification of its own, and several data errors in those releases went undetected because nothing in the layout made them checkable.

**Wrong values that the layout could not catch.**

- ECOSTRESS spectra were stored in percent while documented as 0–1 fractions. 128 ECOSTRESS transmittance spectra were also labelled reflectance.
- USGS missing bands were stored as the sentinel −1.23e34: 706,826 values in 1,512 spectra.
- CABO (EcoSIS) labelled all 7,821 spectra reflectance, although 1,971 are transmittance.

The scale, the meaning of the values and the missing-value marker were conventions in prose, not rules a validator could apply to a file. Format v1 makes each of them a rule: values are fractions (section 4.1), missing bands are NaN and finite values above 1e10 are treated as sentinels (F9), and `quantity` is a required column from a closed vocabulary (F7).

**Files that could not be read on their own.** A v0 Parquet file did not carry what was needed to interpret it:

- wavelength axes lived in a separate `wavelengths.parquet` registry, joined by `grid_id`;
- licence, citation and dataset provenance lived in the catalog JSON (27 to 105 MB depending on the release) and later in `licenses.json`;
- a reader who downloaded one source file without its siblings had numbers with no axis and no licence.

**No way to tell whether two spectra are comparable.** v0 recorded a measurement technique, and geometry only as optional free text. A directional-hemispherical laboratory spectrum and a biconical contact-probe spectrum were both just "reflectance", although they are different physical quantities.

**Awkward to query.** v0 flattened nested models to dotted column names (`material.name`, `spectral_data.values`), which every SQL engine requires quoting, and the README had to warn about it.

**Licences that tools cannot read.** Licences were free text, so no tool could check whether a set of spectra may be combined or redistributed.

**Not adoptable by anyone else.** The layout was defined by the Pydantic models in one Python package and changed with it. A laboratory that wanted to publish its own library in a compatible way had nothing stable to target, no versioning promise, and no way to check its files.

**Selections that were not declared.** EcoSIS in releases v0.0.6 and v0.0.7 was a randomly truncated subset (it held only 16–21% of the curated packages' 130,931 spectra) because the downloader treated transient failures as end-of-data. Nothing in the files said whether a collection was complete or a selection. That bug is fixed separately; the format change is that a file now has to say which it is (`inclusion`, with `inclusion_notes` for a subset).

## Proposal

The full text is [`docs/spec/format-v1.md`](../spec/format-v1.md) with the machine-readable files in `schemas/format-v1/`. In outline:

1. **One self-describing Parquet file per collection.** One row per spectrum. Collection metadata (title, version, DOI, licence, citation, inclusion, contributing datasets, declared extensions, generator) is a JSON object under the Parquet footer key `openspeclib`, validated by `footer.schema.json`. This follows the GeoParquet approach of putting format metadata in the footer, so any Parquet reader can get it without reading data.
2. **Inline wavelength axes.** Each row holds its own `wavelengths` list next to `values`. The separate `wavelengths.parquet` registry is removed. Measured on the OpenSpecLib collections, this costs 3.6% in total size, which buys files that stand alone.
3. **Flat snake_case columns.** No structs and no dots, so every engine can address every column without quoting.
4. **Required `quantity` and `geometry`.** `quantity` is reflectance, transmittance, emissivity or absorbance. `geometry` uses the Schaepman-Strub et al. (2006) nomenclature, with `unknown` when the source does not say.
5. **Values on their natural scale; NaN for missing.** Fractions for reflectance, transmittance and emissivity; decadic absorbance. No sentinels.
6. **Native wavelength units, plus nanometre extents.** Wavelengths keep the source's unit (`nm`, `um`, `cm-1`); `wavelength_min_nm` and `wavelength_max_nm` let readers filter across collections without converting.
7. **Optional extensions.** Column groups (`material`, `sample`, `measurement`, `bandpass`, `uncertainty`, `quality`) that a file declares in its footer. A small core keeps simple files simple. Producer-specific metadata goes in `extra` (JSON) or `x_`-prefixed columns.
8. **URL-safe ids.** `{collection}:{local_id}`, with the local part restricted to unreserved URL characters (F5), so an id can be used in a URL or a file name without escaping. Collection ids are lowercase and registered in [`docs/spec/collections.md`](../spec/collections.md).
9. **SPDX licences.** A per-row `license` column (SPDX expression, `LicenseRef-` defined in the footer, or `NOASSERTION`). In aggregated collections each row carries its dataset's licence.
10. **Optional index file.** A Parquet file holding every non-array column of several collection files plus each file's footer, for search without reading arrays. In the OpenSpecLib distribution it replaces the 27–105 MB catalog JSON.
11. **Conformance rules and a checker.** Numbered rules (F1–F13, I1) and recommendations (R1–R5), stable across versions, checked by `openspeclib validate`.
12. **Semantic versioning, independent of the package.** Section 11 of the spec; changes go through this RFC process.

## Compatibility

This is the first versioned format, so there is no earlier specification to stay compatible with. v0 files are not Format v1 files: they have no `openspeclib` footer key (v0 used `openspeclib_version` and `source`), so a v1 reader rejects them under F1 rather than misreading them.

## Alternatives considered

- **Keep the v0 layout and only fix the data.** This would have fixed the known value errors but left the next one equally hard to catch, and left the files unusable without the catalog. Rejected.
- **Keep the shared wavelength registry.** It is smaller, but it splits every spectrum across two files and makes a single file meaningless on its own. The measured saving (3.6%) did not justify that.
- **Nested Parquet structs** (`material.name` as a struct field). Closer to the in-memory model, but struct support and quoting rules differ between engines. Flat columns work everywhere.
- **One column per wavelength (wide tables).** Natural for a single instrument, but the collections have many different axes and units; a wide table would be mostly empty or force resampling, which the project does not do at ingest.
- **One file per material category** (the layout before v0.0.2). Multiplies files without helping readers: category is a column predicate, and row-group statistics make it cheap.
- **HDF5 or NetCDF.** Well suited to gridded arrays, but less suited to a table of spectra with heterogeneous axes and many metadata columns, and not readable in the browser or by SQL engines without extra libraries. Parquet is read natively by DuckDB (including DuckDB-WASM in the viewer), pandas, Polars, pyarrow, R and Spark.
- **ENVI spectral libraries (`.sli`/`.hdr`) or CSV as the format.** Widely used, but they assume one shared wavelength axis per file and have no place for per-spectrum provenance or licences. Format v1 keeps them as interchange formats through converters (`openspeclib convert`).
- **Null instead of NaN for missing bands.** Nulls inside a list of floats are poorly supported by numeric libraries and do not survive conversion to numpy arrays. NaN is the convention those tools already use.
- **Metadata in a sidecar JSON file.** Simpler to write, but it can be separated from the data. The footer key keeps them in one file.

## Implementation

Release 0.2.0 of the `openspeclib` package:

- writes collection files and the index file through `openspeclib.storage`;
- validates them against the rules (`openspeclib validate`), with the OpenSpecLib quality profile on top;
- converts to and from ENVI `.sli`/`.hdr` and CSV;
- provides a fast Arrow/numpy reader;
- adds a viewer page documenting the format, with an in-browser conformance checker.

## Migration from the v0 layout

Releases v0.0.1 to v0.0.7 stay on GitHub unchanged. They are not converted in place, and they carry the data errors listed above, so users should move to 0.2.0 or later rather than convert old files.

| v0 (releases up to v0.0.7) | Format v1 |
|---|---|
| `openspeclib-catalog-<version>.json` (27–105 MB) | Optional `openspeclib-index.parquet`; metadata also in each file's footer |
| `wavelengths.parquet` + `spectral_data.wavelength_grid_id` | `wavelengths` column in each row |
| `licenses.json`, `source.license`, `source.citation` | Footer `collection` and `datasets`; per-row `license` (SPDX) |
| Footer keys `openspeclib_version`, `source` | Footer key `openspeclib` (JSON) |
| Dotted columns (`material.name`, `sample.id`, `measurement.instrument`, …) | Snake_case (`material_name`, `sample_id`, `measurement_instrument`, …) |
| `spectral_data.values` | `values` |
| `spectral_data.type`, `measurement.technique` | `quantity` |
| `measurement.geometry` (optional free text) | `geometry` (required, controlled vocabulary) |
| `spectral_data.wavelength_min` / `_max` (native unit) | `wavelength_min_nm` / `wavelength_max_nm` (nanometres) |
| `source.library` | `collection` |
| `source.original_id` | `original_id` |
| `source.dataset.*` | `dataset_id`, with the dataset's metadata in the footer `datasets` |
| `spectral_data.bandpass` | `bandpass_fwhm` (extension `bandpass`) |
| `spectral_data.num_points` | Removed from collection files: use the length of `values`; index files carry `num_points` |
| `quality.notes` | `quality_notes` (extension `quality`) |
| `quality.bad_band_count`, `has_bad_bands`, `coverage_fraction` | Removed: count the NaN values in `values` |
| `spectral_data.reflectance_scale` | Removed: values are always on their natural scale; the divisor applied at ingest is recorded in `extra` |
| `additional_properties` | `extra` |


For code that read v0 files:

- Replace joins against `wavelengths.parquet` with the `wavelengths` column.
- Replace lookups in the catalog JSON with a query on the index file, or on the collection file's columns and footer.
- Drop any sentinel handling (`abs(x) > 1e10`) and any division by 100 for ECOSTRESS; values are already fractions with NaN for missing bands.
- Read the licence from the row's `license`, and the citation from the footer (`datasets[dataset_id]` for an aggregated collection, otherwise `collection`).

## Unresolved questions

None blocking 1.0.0. Later additions need their own RFCs.

## Decision

Accepted by the maintainer on 2026-10-29 as the basis of specification 1.0.0 and OpenSpecLib release 0.2.0.

Rationale: this RFC creates the specification and the RFC process itself, so there was no earlier process or audience to run a discussion window with. It was accepted without the 14-day discussion that [`README.md`](README.md) requires of later RFCs, because the v0 releases had data errors (ECOSTRESS in percent, USGS sentinels, mislabelled transmittance, truncated EcoSIS) that users should not keep downloading, and fixing them meant a new layout anyway. Every later change to the specification goes through the full process.

### Changes before publication

Specification 1.0.0 was finalised after this RFC was accepted and before release 0.2.0 published it. Independent reviews of the reference checker found places where the draft was ambiguous or where the schema files and the text disagreed. They were settled as follows, in the specification, the schema files, the reference checker, the browser checker and the conformance corpus together. No file published in Format v1 existed yet, so none changed status.

- `format_version` is a semantic version without leading zeros (`1.01.0` fails F1/I1); the footer schema pattern says so.
- `created` accepts second `60` (a leap second, as RFC 3339 allows) on any date, and rejects offsets beyond `±23:59`.
- Schema `pattern`s use ECMA-262 semantics, as JSON Schema specifies: `$` matches only at the end of the string, and `\s` is ECMA-262 whitespace.
- SPDX special values are case-sensitive: `noassertion` and `licenseref-` fail F11, as does `WITH NOASSERTION`.
- A column appears at most once: a repeated `x_` column, or a repeated column of a newer minor version, fails F3 (collection) or I1 (index), as repeated core and extension columns already did.
- Section 11 in an index: each embedded `collections[].metadata` object is judged by its own `format_version`.

These are recorded here because they settle what 1.0.0 means, not as an amendment to it.
