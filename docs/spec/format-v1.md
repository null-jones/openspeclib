# OpenSpecLib Format, version 1.0.0

Status: **1.0.0**, accepted through [RFC 0001](../rfcs/0001-format-v1.md). This document is normative. The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

The OpenSpecLib Format stores a spectral library (a *collection* of spectra) in a single [Apache Parquet](https://parquet.apache.org/) file. It is written for anyone who publishes spectra, whether or not their data is part of the OpenSpecLib collection. A file in this format:

- is self-describing: one file holds the spectra, their wavelength axes, and the collection's provenance and licensing;
- uses ordinary Parquet columns that any Parquet reader can query (DuckDB, pandas, Polars, pyarrow, R `arrow`, Spark, DuckDB-WASM);
- records what was measured (`quantity` and `geometry`) and keeps wavelengths in the source's native unit;
- marks missing values with NaN and nothing else.

The machine-readable parts of this specification live next to it and are normative too:

| File | Content |
|---|---|
| [`schemas/format-v1/columns.json`](../../schemas/format-v1/columns.json) | Every column: name, Arrow type, nullability, extension |
| [`schemas/format-v1/vocabularies.json`](../../schemas/format-v1/vocabularies.json) | Controlled vocabularies |
| [`schemas/format-v1/footer.schema.json`](../../schemas/format-v1/footer.schema.json) | JSON Schema for the footer metadata |
| [`schemas/format-v1/rules.json`](../../schemas/format-v1/rules.json) | Conformance rules (section 8) and quality-profile bounds (section 12) |

Where this document and those files disagree, the files win, and the disagreement is a bug to report.

## 1. Concepts

- A **spectrum** is one measurement: a wavelength axis and the values measured along it, plus metadata.
- A **collection** is a set of spectra published together under one identity, licence and citation. Examples: the USGS Spectral Library Version 7, a laboratory's in-house library.
- An **aggregated collection** gathers spectra from several contributing **datasets**, each with its own licence and citation (for example EcoSIS). Datasets are described in the footer and referenced from rows by `dataset_id`.
- A **collection file** holds one collection. An optional **index file** lists the spectra of several collection files without their arrays, for fast search (section 7).

## 2. File structure

A collection file MUST be a valid Apache Parquet file. It SHOULD use the extension `.parquet` and SHOULD be named `{collection_id}.parquet`. A file MUST NOT carry both the `openspeclib` and the `openspeclib_index` footer keys.

The file holds one row per spectrum. Columns are flat (no Parquet structs) and use snake_case names so that every tool can address them without quoting.

## 3. Footer metadata

The Parquet footer key-value metadata MUST contain the key `openspeclib`. Its value MUST be a UTF-8 JSON object that validates against `#/$defs/collectionFooter` in `footer.schema.json`. In outline:

```json
{
  "format_version": "1.0.0",
  "collection": {
    "id": "usgs_splib07",
    "title": "USGS Spectral Library Version 7",
    "version": "7a",
    "url": "https://doi.org/10.5066/F7RR1WDJ",
    "doi": "10.5066/F7RR1WDJ",
    "license": "LicenseRef-USGS-PublicDomain",
    "license_refs": {
      "LicenseRef-USGS-PublicDomain": {
        "name": "U.S. Government work, public domain",
        "url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits"
      }
    },
    "citation": "Kokaly, R.F., et al., 2017, USGS Spectral Library Version 7: U.S. Geological Survey Data Series 1035.",
    "inclusion": "subset",
    "inclusion_notes": "The measured spectra of splib07a only. The oversampled splib07b copies, the spectra convolved or resampled to other instruments and the per-band error-bar files are derived from the same measurements and are not included."
  },
  "datasets": {},
  "extensions": ["material", "sample", "measurement", "bandpass"],
  "created": "2026-10-29T12:00:00Z",
  "generator": "openspeclib 0.2.0"
}
```

- `format_version` is the version of this specification the file follows, written as `MAJOR.MINOR.PATCH` with no leading zeros (e.g. `1.0.0`).
- `collection.license` is the licence of the collection as a whole. When rows carry different licences (aggregated collections), it SHOULD be `NOASSERTION` with a `license_note` pointing to the per-row `license` column, which is always authoritative.
- Every `LicenseRef-` identifier used in any licence field (the `license` column, `collection.license` and each `datasets.*.license`) MUST be defined in `collection.license_refs`. When a source names a licence without saying which version (e.g. "Creative Commons Attribution"), producers SHOULD define a `LicenseRef-` with the source's wording rather than guess an SPDX version.
- `collection.inclusion` says whether the file holds everything the source publishes (`complete`) or a deliberate selection (`subset`). A subset MUST explain its selection in `inclusion_notes`. Datasets carry the same two fields, with the same requirement. A dataset's `spectra_available` is the number of spectra the source publishes in it; `spectra_included` is the number in this file. `datasets` lists only datasets with at least one spectrum in the file; a collection that leaves out whole datasets SHOULD name them and say why in `collection.inclusion_notes`.
- `extensions` lists the optional column groups the file may contain (section 5).
- `created` is an RFC 3339 timestamp with a time-zone designator that names a real calendar date and time, e.g. `2026-10-29T12:00:00Z`. The footer schema checks the shape with a pattern (JSON Schema treats `format` as an annotation only); validators MUST also reject impossible dates such as month 13 and offsets beyond `±23:59`. Second `60` is accepted on any date, as RFC 3339 allows for leap seconds; validators do not check it against the leap-second table.

## 4. Core columns

Every collection file MUST contain these columns with these Arrow types. None may contain nulls except `dataset_id`, and `id`, `collection`, `original_id`, `name`, `material_name`, `license` and non-null `dataset_id` MUST NOT contain empty or whitespace-only strings. List columns of strings (`material_keywords`, `measurement_processing`) MUST NOT contain null elements.

A column has the stated Arrow type when the type implied by its Parquet logical type and repetition is that type. Arrow-specific hints that some writers store in the `ARROW:schema` footer entry (dictionary encoding, `fixed_size_list`, large and view variants such as `large_string`, `string_view`, `large_list` and `list_view`) do not change the type, and the field names and nullability of list children are not significant. This way files written by different Parquet libraries conform, and every reader reaches the same verdict. A nullable column whose values are all null MAY be stored with the Arrow `null` type. `date64` is equivalent to `date32`, and both the standard 3-level and the legacy 2-level Parquet list encodings are accepted.

| Column | Type | Content |
|---|---|---|
| `id` | string | `{collection}:{local_id}`, unique (F5) |
| `collection` | string | Equals the footer `collection.id` |
| `original_id` | string | The upstream identifier, unchanged |
| `name` | string | Display label |
| `material_name` | string | What was measured |
| `material_category` | string | Vocabulary `material_category` |
| `quantity` | string | Vocabulary `quantity` |
| `geometry` | string | Vocabulary `geometry` |
| `wavelength_unit` | string | `nm`, `um` or `cm-1` |
| `wavelengths` | list&lt;float64&gt; | Spectral axis, native unit |
| `values` | list&lt;float32&gt; | Measured values; NaN = missing |
| `wavelength_min_nm` | float64 | Shortest wavelength, in nm |
| `wavelength_max_nm` | float64 | Longest wavelength, in nm |
| `license` | string | SPDX expression, `LicenseRef-…` or `NOASSERTION` |
| `dataset_id` | string, nullable | Key into footer `datasets` |

Local ids are opaque. Readers MUST NOT parse meaning out of them (producers may sanitise characters or add suffixes such as `~2` to resolve collisions); `original_id` is the authoritative upstream identifier.

### 4.1 Values

`values` holds the measured quantity on its natural scale:

- `reflectance`, `transmittance` and `emissivity` are dimensionless fractions with a nominal range of 0 to 1. Producers MUST convert percentages (0–100) and other scaled integers (e.g. 0–10000) to fractions. A reflectance factor may legitimately exceed 1, and noise may produce small negative values; producers SHOULD NOT clip them.
- `absorbance` is decadic, A = log10(1/x). The basis (reflectance or transmittance) SHOULD be recorded in `measurement_processing`.

A missing or invalid band MUST be stored as NaN. Producers MUST NOT use sentinel numbers such as −1.23e34, −9999 or 0 for missing bands. A producer that masks values it considers invalid SHOULD record what it masked in `extra` (section 6). Rule F9 can only detect sentinels above 1e10 in magnitude; the quality profile (section 12) catches most others, such as −9999 in a reflectance spectrum.

### 4.2 Geometry

`geometry` names the illumination and viewing geometry using the nomenclature of Schaepman-Strub et al. (2006), *Reflectance quantities in optical remote sensing — definitions and case studies*, Remote Sensing of Environment 103, 27–42. Together with `quantity` it tells a reader whether two reflectance spectra can be compared directly. Integrating-sphere laboratory spectra (`directional_hemispherical`) and contact-probe or field spectra (`biconical`, `hemispherical_conical`) are different quantities. Producers MUST use `unknown` when the source does not say, rather than guessing.

### 4.3 Wavelengths

Wavelengths stay in the unit the source published (`wavelength_unit`) so that no resampling or conversion error is introduced. Some instruments publish axes that are not monotonic, for example where detectors overlap. To satisfy F8, producers MAY reorder bands by wavelength, moving `values`, `bandpass_fwhm` and `values_stddev` with them, and SHOULD record in `extra` that they did so, including the original band order. Bands with exactly equal wavelengths cannot be reordered into a strictly monotonic axis; producers MUST resolve them (for example by dropping one) and SHOULD record that too. `wavelength_min_nm` and `wavelength_max_nm` give the axis extent in nanometres for every row, so readers can filter across collections without conversion. Wavenumber axes convert as λ[nm] = 10⁷ / ν[cm⁻¹].

## 5. Extensions

Optional column groups add detail without making every file wide. A column from an extension MAY appear only when the footer lists that extension in `extensions`. A file that declares an extension need not contain every column of it.

| Extension | Columns |
|---|---|
| `material` | `material_subcategory`, `material_formula`, `material_keywords` (list&lt;string&gt;) |
| `sample` | `sample_id`, `sample_description`, `sample_particle_size`, `sample_origin`, `sample_owner`, `sample_collection_date` (date32), `sample_preparation` |
| `measurement` | `measurement_instrument`, `measurement_instrument_type`, `measurement_laboratory`, `measurement_date` (date32), `measurement_processing` (list&lt;string&gt;), `measurement_light_source`, `measurement_venue`, `measurement_acquisition_method`, `measurement_foreoptic`, `measurement_reference` |
| `bandpass` | `bandpass_fwhm` (list&lt;float32&gt;, in `wavelength_unit`) |
| `uncertainty` | `values_stddev` (list&lt;float32&gt;) |
| `quality` | `quality_notes` |

All extension columns are nullable. Types are given in `columns.json`; columns without a stated type above are `string`.

`measurement_instrument_type` is free text. Producers SHOULD use one of these values where it fits: `laboratory_spectrometer` (a dispersive laboratory instrument, e.g. Beckman, Perkin Elmer), `field_spectrometer` (a portable spectroradiometer such as an ASD or SVC, in the field or laboratory), `ftir` (Fourier-transform infrared spectrometer), `imaging_spectrometer` (airborne or spaceborne imaging spectrometer), `goniometer`.

Geometry describes the physical setup, so that equal setups get equal values across sources. Older libraries often call laboratory measurements made with a lamp and fore-optic, contact probe or FTIR reflectance attachment "bidirectional"; in Schaepman-Strub terms these are biconical reflectance factors (Kokaly et al. 2017, USGS Data Series 1035, p. 4, says so explicitly). Producers SHOULD record such measurements as `biconical`, keep the source's verbatim term in `extra` (e.g. `"source_geometry_term": "Bidirectional reflectance"`), and reserve `bidirectional` for goniometric or near-collimated measurements with stated illumination and viewing angles. When the setup cannot be established, use `unknown`.

`measurement_reference` names the reference the values are relative to. Recommended values: `spectralon`, `halon`, `gold`, `stainless_steel_mirror`, `blackbody`, and `absolute` for values corrected to absolute reflectance (the reflectance of the physical standard removed); when the physical standard of an absolute-corrected spectrum is known, producers MAY record it in `extra`.

`measurement_processing` lists free-text flags in the order the processing was applied. These flags have defined meanings: `averaged`, `interpolated`, `resampled` (e.g. convolved to a coarser bandpass), `smoothed`, `continuum_removed`, `spliced` (joined from measurements by different instruments), `scaled_to_reference_level` (scaled to match the level of another measurement), `simulated` (the spectrum is synthetic or modelled rather than measured; a measured spectrum that was only resampled is not simulated), `absorbance_from_reflectance`, `absorbance_from_transmittance`.

## 6. Other columns

- `extra` (string, nullable) MAY appear in any file without being declared. Each non-null value MUST be a JSON object (rule F4) (its `extension` in `columns.json` is `"*"`). It holds a JSON object of producer-specific metadata, for example the original values of masked bands.
- Producers MAY add their own columns whose names start with `x_`. Like every column in the file, they MUST be flat: no structs or maps (lists of primitive types are allowed). Readers MUST ignore columns they do not recognise.
- Any other column name not defined by this specification makes the file non-conformant (F3). This keeps the namespace free for future versions.

## 7. Index files

An index file lets a reader search several collection files without reading their arrays. It is optional and fully derivable from the collection files.

An index file MUST:

- be a Parquet file whose footer key `openspeclib_index` holds a JSON object valid against `#/$defs/indexFooter` in `footer.schema.json`. That object lists each collection file (`file`, `num_spectra`, optional `sha256`) together with that file's complete `openspeclib` footer object;
- contain every non-array column of the collection files it indexes, with the same names and types (every column except the per-band arrays `wavelengths`, `values`, `bandpass_fwhm` and `values_stddev`; list columns such as `material_keywords` are included);
- add `num_points` (int32), the length of `values`, and `file` (string): the location of the collection file holding that spectrum: a relative URL reference with `/` separators, resolved against the location of the index file, or an absolute `http(s)` URL. Absolute local paths and `file:` URLs are not allowed, so an index stays valid when copied (their `extension` in `columns.json` is `"index"`);
- not contain other columns, except columns whose names start with `x_`. Producers MAY copy `x_` columns from collection files.

Every row's `file` MUST be listed in the footer. Every row MUST correspond to exactly one row of that collection file with the same `id`, and the shared columns MUST have equal values (`x_` columns are not compared). Where an extension column exists in the index but not in a given collection file, it MUST be null for that file's rows. Every row of each listed collection file MUST appear in the index exactly once. Each listed file's `num_spectra` MUST equal its row count and its `metadata` MUST equal that file's `openspeclib` footer; `sha256`, when given, MUST match the file. `id` MUST be unique across the whole index, so two collection files listed in one index cannot share a collection id or a spectrum id. Each collection file MUST be listed at most once, comparing resolved paths. Index rows SHOULD be sorted by `id`. The OpenSpecLib distribution names its index `openspeclib-index.parquet`.

## 8. Conformance rules

A collection file conforms to this specification when it meets every rule below. Rule identifiers are stable; validators report them.

| Rule | Requirement |
|---|---|
| F1 | The file is a valid Parquet file. The footer key `openspeclib` exists, holds valid JSON that validates against `#/$defs/collectionFooter`, `format_version` has major version 1, and `created` names a real calendar date and time. The file does not also carry `openspeclib_index`. |
| F2 | Every core column exists with its Arrow type (section 4). Columns marked non-nullable contain no nulls, and the columns listed in section 4 contain no empty or whitespace-only strings. |
| F3 | Every column is a core column, a column of a declared extension, `extra`, or starts with `x_`. Columns starting with `x_` are flat: primitive types or lists of primitive types, no structs or maps. |
| F4 | Every extension column, and `extra`, has its defined Arrow type, and each non-null `extra` value is a JSON object. |
| F5 | `id` is unique within the file and matches `^{collection}:[A-Za-z0-9._~-]{1,200}$`, where `{collection}` is the footer `collection.id`. (The footer schema restricts collection ids to `^[a-z0-9][a-z0-9_]{1,62}$`; F1 checks that.) |
| F6 | `collection` equals the footer `collection.id` on every row. |
| F7 | `material_category`, `quantity`, `geometry` and `wavelength_unit` hold values from their vocabularies. |
| F8 | `wavelengths` has at least 2 elements, no nulls, all finite and greater than 0, and is strictly increasing or strictly decreasing. |
| F9 | `values` has the same length as `wavelengths`. Its elements are finite or NaN: no nulls, no infinities, and no finite value with magnitude above 1e10 (such values are sentinels). |
| F10 | `wavelength_min_nm` and `wavelength_max_nm` equal the minimum and maximum of `wavelengths` converted to nanometres: \|a − b\| ≤ 1e-6 · max(\|a\|, \|b\|). |
| F11 | Every non-empty licence field (the per-row `license` column, footer `collection.license`, and each `datasets.*.license`) is an SPDX license expression, a `LicenseRef-` identifier defined in `collection.license_refs`, or `NOASSERTION` on its own. Operators (`AND`, `OR`, `WITH`) are upper case. Tokens are separated by single spaces (U+0020) or parentheses, and values have no leading or trailing whitespace. `DocumentRef-` references are not allowed. Validators check expression syntax and `LicenseRef-` definitions; they need not check identifiers against the SPDX licence list. |
| F12 | Every non-null `dataset_id` is a key of the footer `datasets` object, and every key of `datasets` is the `dataset_id` of at least one row. Where a dataset gives `spectra_included`, it equals the number of rows with that `dataset_id`. |
| F13 | `bandpass_fwhm` and `values_stddev`, where present and not null, have the same length as `wavelengths`; their elements are finite and non-negative, or NaN. |

An index file conforms when it meets rule I1 and its collection files conform:

| Rule | Requirement |
|---|---|
| I1 | The file is a valid Parquet file whose footer key `openspeclib_index` validates against `#/$defs/indexFooter` and which does not also carry `openspeclib`. It meets every requirement of section 7. |

### 8.1 Recommendations

These are not required for conformance; validators report them as warnings.

| Rule | Recommendation |
|---|---|
| R1 | Rows are sorted by `id` in ascending byte (UTF-8 code unit) order, and Parquet statistics are written for `id` (files with no rows are exempt), so readers can fetch a single spectrum with HTTP Range requests. |
| R2 | Row groups hold at most 250 spectra. |
| R3 | `values`, `wavelengths`, `bandpass_fwhm` and `values_stddev` use BYTE_STREAM_SPLIT encoding on their leaf columns (e.g. `values.list.element`), without dictionary encoding, compressed with zstd. In measured tests this made files about three times smaller than float64 with default encoding. |
| R4 | Every spectrum has at least one non-NaN value. |
| R5 | At most half of the spectra have `geometry` = `unknown`. (A validator cannot know whether a source documents its setup, so this proxy is the testable form of "use a known geometry where the source documents it".) |

## 9. Reading

Reading one spectrum needs nothing beyond a Parquet reader. With DuckDB:

```sql
SELECT id, material_name, quantity, geometry, wavelength_unit, wavelengths, "values"
FROM 'usgs_splib07.parquet'
WHERE id = 'usgs_splib07:splib07a_Olivine_GDS70.a_Fo89_165um_BECKb_AREF';
```

(`values` is quoted because it is an SQL keyword.) When reading over HTTP, query one id per `WHERE id = …` clause rather than `id IN (…)`: as of DuckDB 1.5, DuckDB prunes row groups for single equality predicates but not for `IN` lists. Servers that host collection files SHOULD support HTTP Range requests and send `ETag` or `Last-Modified` headers so clients can cache the footer. Readers that use Parquet statistics MUST locate them by column path, not by column position.

Collection metadata:

```sql
SELECT decode(value) FROM parquet_kv_metadata('usgs_splib07.parquet') WHERE key = 'openspeclib';
```

With Python and pyarrow:

```python
import json
import pyarrow.parquet as pq

pf = pq.ParquetFile("usgs_splib07.parquet")
meta = json.loads(pf.metadata.metadata[b"openspeclib"])  # the Parquet footer key-value metadata
table = pf.read(columns=["id", "material_name", "wavelengths", "values"])
```

## 10. Writing

To publish a library in this format:

1. Choose a collection id that is not already in use. The OpenSpecLib project lists known ids in [`docs/spec/collections.md`](collections.md).
2. Write one row per spectrum with the core columns and any extension columns you can fill.
3. Write the footer metadata.
4. Run `openspeclib validate your_file.parquet`. A file that passes is conformant. (The default profile checks conformance only; `--profile openspeclib` adds the quality rules of section 12.)

With pyarrow, set BYTE_STREAM_SPLIT (R3) on the Parquet leaf path of a list column, e.g. `column_encoding={"values.list.element": "BYTE_STREAM_SPLIT"}` and `use_dictionary=False` for it. Naming the top-level column (`"values"`) is silently ignored.

The `openspeclib` Python package writes conformant files (`openspeclib.storage.write_collection`) and converts from ENVI spectral libraries and CSV (`openspeclib convert`).

## 11. Versioning

This specification uses semantic versioning. Minor versions (1.1, 1.2, …) may add vocabulary values, extensions and optional columns; they never remove or redefine anything, so a 1.0 reader can read any 1.x file by ignoring what it does not know. A reader MUST reject a file whose `format_version` has a major version it does not support.

A validator checks a file against the rules of the highest 1.x version it implements. When `format_version` names a higher minor version, it MUST NOT report vocabulary values, columns, extensions or footer members that it does not know as errors; it SHOULD report them as warnings. An index whose listed collection file cannot be fetched is not conformant (I1), because the cross-file checks cannot be completed. Changes are proposed through the RFC process in [`docs/rfcs/`](../rfcs/).

The version of this specification is independent of OpenSpecLib data releases and of the `openspeclib` Python package.

## 12. The OpenSpecLib quality profile

The OpenSpecLib collection applies extra quality rules on top of format conformance. Other publishers may adopt them. `openspeclib validate --profile openspeclib` checks them:

| Rule | Requirement |
|---|---|
| Q1 | The median of a spectrum's valid values lies inside the hard bounds for its quantity. A median outside them usually means a missed scale conversion. |
| Q2 | No valid value lies outside the hard bounds for its quantity. |
| Q3 | Values outside the warning bounds are reported as warnings. |
| Q4 | `wavelength_min_nm` is at least 100 and `wavelength_max_nm` at most 1,000,000 (100 nm to 1 mm). An extent outside this range almost always means a mislabelled `wavelength_unit`. It can be exempted only by exact id. |

A *valid value* here is one that is finite and not a sentinel (F9); NaN bands are ignored. The hard and warning bounds per quantity are the `qa_bounds` in [`rules.json`](../../schemas/format-v1/rules.json). The median in Q1 may be computed approximately. Each spectrum is reported under at most one of these rules: a spectrum that fails Q1 is not also reported under Q2, and one that fails Q2 is not also reported under Q3. Validators SHOULD also report when a large share of a collection or dataset fails Q1, Q2 or Q4, which usually points to a systematic problem rather than isolated spikes. The threshold is validator-defined; `openspeclib validate` reports it when a collection or dataset has at least 10 spectra of one quantity and at least 10% of them fail. Spectra can be exempted from Q2 and Q3 only through a reviewed allowlist entry with a written reason. An entry selects an exact id, an id prefix (which MUST include at least one character after `{collection}:`, so an entry can never waive a whole collection) or a dataset; Q1 can be exempted only by exact id. [`docs/validation.md`](../validation.md) describes the allowlist file.
