# Data structure

This page is an orientation. The [OpenSpecLib Format specification](spec/format-v1.md) is the normative definition, with its machine-readable parts in [`schemas/format-v1/`](../schemas/format-v1/). When this page and the specification differ, the specification is right.

## Files

| File | What it is | Defined in |
|---|---|---|
| `{collection_id}.parquet` | A **collection file**: one row per spectrum, with its wavelength axis, values and metadata. The footer key `openspeclib` holds the collection's metadata as JSON. | Spec sections 2–6 |
| `openspeclib-index.parquet` | An **index file**: every non-array column of the listed collection files, plus `num_points` and `file`. Its footer key `openspeclib_index` lists the files and copies each one's footer. Optional; it can always be rebuilt from the collection files. | Spec section 7 |
| `id-migration.csv` | `old_id,new_id` for spectra whose id changed from the v0 layout (releases up to v0.0.7). Not part of the format. | `src/openspeclib/combine.py` |
| `validation-report.json` | The validator's JSON report for the release. Not part of the format. | [validation.md](validation.md) |
| `checksums.txt` | SHA-256 of each file, `sha256sum` format | — |

A release and the output of `openspeclib combine` have the same flat layout: all files side by side in one directory. The index refers to collection files by relative path (its `file` column), so the directory can be moved or served over HTTP as a whole.

## Inside a collection file

Each row is one spectrum. Columns are flat, snake_case, and fall into four groups:

- **Core columns**, present in every file: `id`, `collection`, `original_id`, `name`, `material_name`, `material_category`, `quantity`, `geometry`, `wavelength_unit`, `wavelengths`, `values`, `wavelength_min_nm`, `wavelength_max_nm`, `license`, `dataset_id`.
- **Extension columns**, present when the footer lists the extension: `material_*`, `sample_*`, `measurement_*`, `bandpass_fwhm`, `values_stddev`, `quality_notes`.
- **`extra`**: a JSON object of producer-specific metadata, such as what a loader masked, or the rule that set the geometry.
- **`x_` columns**: producer-specific columns that readers ignore.

The column list with types is [`columns.json`](../schemas/format-v1/columns.json). The allowed values of `material_category`, `quantity`, `geometry` and `wavelength_unit` are in [`vocabularies.json`](../schemas/format-v1/vocabularies.json).

The footer object holds `format_version`, the `collection` (id, title, version, URL, DOI, licence, licence definitions, citation, inclusion), the contributing `datasets` of an aggregated collection, the declared `extensions`, `created` and `generator`. Its schema is [`footer.schema.json`](../schemas/format-v1/footer.schema.json).

## Things to know before you use the data

- **Ids.** `id` is `{collection}:{local_id}` and is unique within a release. Do not parse meaning out of the local part; `original_id` holds the source's own identifier.
- **Units.** `wavelengths` is in the source's unit (`wavelength_unit`). `wavelength_min_nm` and `wavelength_max_nm` are the same extent in nanometres.
- **Scale.** Reflectance, transmittance and emissivity are fractions; absorbance is decadic.
- **Missing values** are NaN, and nothing else.
- **Quantity and geometry** say what was measured and how. Two reflectance spectra with different geometries (for example `directional_hemispherical` and `biconical`) are not directly comparable. `unknown` means the source does not document the setup.
- **Licences** are per row (`license`) and per dataset. `LicenseRef-…` identifiers are defined in the footer's `collection.license_refs`.

## How the OpenSpecLib collection fills the format

What each loader puts in each column, and the evidence it uses for `quantity` and `geometry`, is in [processing-pipeline.md](processing-pipeline.md). The EcoSIS policy is in [ecosis-curation.md](ecosis-curation.md). Licences and citations are in [provenance.md](provenance.md) and [licensing.md](licensing.md).

In the Python package, `openspeclib.models.SpectrumRecord` is the in-memory form of one row, `openspeclib.storage` reads and writes collection and index files, and `openspeclib.reader` is the fast read-only interface ([reading.md](reading.md)).

## The v0 layout

Releases v0.0.1 to v0.0.7 used older layouts, which changed between releases. All had a JSON catalog and Parquet spectra with dotted column names (`material.name`). v0.0.1 had one Parquet file per source and material category; from v0.0.2 there was one file per source (`spectra/{source}.parquet`); from v0.0.6 the wavelength axes moved to a separate `wavelengths.parquet` registry joined by `grid_id`. The current code does not read any of them. [RFC 0001](rfcs/0001-format-v1.md) explains the change and maps the old columns to the new ones.
