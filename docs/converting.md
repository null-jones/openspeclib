# Converting to and from ENVI and CSV

`openspeclib convert` turns an ENVI spectral library or a CSV table into a conformant Format v1 collection file. `openspeclib export` writes spectra from a collection file to ENVI or CSV, with their licences and citations. Run either with `--help` for every option.

The code is in `src/openspeclib/convert/`; the scale rules are in `convert/scale.py`. When this page and `--help` disagree, `--help` is right.

## Import: `openspeclib convert`

```bash
openspeclib convert INPUT OUTPUT.parquet --collection-id my_lab_2026 \
  --title "My lab spectral library" --license CC-BY-4.0 \
  --citation "My Lab (2026). Spectral library." --quantity reflectance
```

`INPUT` is an ENVI `.hdr`/`.sli` pair (name either file) or a `.csv`/`.tsv` table. `--from auto` (the default) decides by extension. Name the output `{collection_id}.parquet`, as the specification recommends.

Conversion is strict. Every problem the converter cannot resolve safely (an ambiguous scale, a conflicting long-CSV row, a repeated wavelength with two different values, a unit that contradicts the one the file states, an unrecognised quantity, a malformed ENVI file, a missing required field) stops it with a message that says what to fix. The output is checked for conformance before it is written, and written only if it conforms. On any failure the command exits 1 and no file is written.

Text files (CSV, ENVI headers) are read as UTF-8, falling back to Latin-1 with a warning. Use `--encoding` to name the encoding.

### Collection metadata

The footer needs a collection id, title, licence and citation. Give them as flags (`--collection-id`, `--title`, `--license`, `--citation`, `--description`, `--collection-version`, `--url`, `--doi`, `--publisher`, `--inclusion`, `--inclusion-notes`), or in a JSON file with `--metadata-json`. The file holds the footer `collection` object, or `{"collection": …, "datasets": …}`. Flags override the file. All missing required fields are reported at once.

- The collection id must match `^[a-z0-9][a-z0-9_]{1,62}$`. To avoid clashing with other publishers, pick one that is free in the [collection registry](spec/collections.md).
- `--license` takes an SPDX expression (`CC-BY-4.0`), `NOASSERTION`, or a `LicenseRef-…` id. Define a `LicenseRef-` with `--license-name` and `--license-url`.
- `--inclusion subset` requires `--inclusion-notes` saying how the subset was chosen.

### Per-spectrum defaults

When the input does not say, these flags fill in: `--quantity` (required if the input never states it), `--geometry` (default `unknown`), `--category` (default `other`), `--wavelength-unit` (overrides ENVI's `wavelength units`). Set `--geometry` only when you know the measurement setup; the format asks for `unknown` rather than a guess.

A quantity, whether from `--quantity` or a column, must be `reflectance`, `transmittance`, `emissivity` or `absorbance`, or a recognised alias of one (`reflectance factor`, `transmission`, `optical density`, …), compared ignoring case, spaces and punctuation. A label that only contains one of these words, such as `first derivative reflectance` or `reflectance (log 1/R)`, is a different quantity and stops the import.

A per-spectrum `wavelength_unit` column labels axes that nothing else labels. When the file itself states the unit (a band-header suffix such as `350nm`, a `wavelength_nm` column in a long table, the ENVI `wavelength units` or `--wavelength-unit` for ENVI) and a spectrum's `wavelength_unit` disagrees, the import stops: one of them is wrong, and picking either could be off by a factor of 1000.

### ENVI

- `wavelength`, `fwhm`, `spectra names` and `wavelength units` are read from the header. Each spectrum shares the header's axis.
- The data file must be exactly as large as the header says (`header offset` plus `samples` × `lines` × the size of the data type). A shorter file is an error. A longer one is an error too, unless you pass `--allow-trailing-bytes`, which ignores the extra bytes with a warning.
- `data ignore value` and bands flagged 0 in `bbl` become NaN. The counts are recorded in `extra.masked_ignore_value_count` and `extra.masked_bad_band_count`.
- `reflectance scale factor` is divided out (see [Scale](#scale)).
- A repeated header key keeps its last value, with a warning.
- `header offset` must be 0 or positive.
- A wavelength listed twice keeps its first band, with a warning (the dropped band is listed in `extra.dropped_duplicate_bands`). If the two bands have different values, the import stops.
- `openspeclib <column>` header fields (written by `export`) restore per-spectrum metadata, so an exported library converts back without loss.

### CSV

- **Wide** (one row per spectrum, the EcoSIS upload layout): metadata columns plus one numeric column per wavelength, whose header is the wavelength (`350`, `X350`, `350nm`, `0.35 µm`). The band columns must form one contiguous, monotonic run; a numeric-looking header outside it (a `2019` column) is metadata. Two headers for the same wavelength (`500` and `500.0`) are treated as in ENVI: equal values keep the first band with a warning, different values stop the import. Headers without a unit are read as nanometres, unless a `wavelength_unit` column or `--wavelength-unit` says otherwise. EcoSIS field names such as `Common Name`, `Latin Genus`/`Latin Species`, `Target Type`, `Measurement Quantity`, `Measurement Units`, `Instrument Manufacturer` and `Model` are recognised.
- **Long** (one row per spectrum and wavelength): a key column (`id`, `original_id` or `name`), `wavelength`, `value`, and optionally `bandpass_fwhm`/`fwhm` and `values_stddev`/`stddev`. Per-spectrum metadata can come from extra columns or from a separate table given with `--spectra-table`, joined on the key. Blank cells are skipped, but a metadata column with two different values within one key (a `quantity`, `license`, `wavelength_unit`, `name`, …) is an error naming the key, the column and both values. A key that repeats a wavelength with a *different* value is an error, because two spectra probably share the key; exact repeats are dropped with a warning.
- `--layout auto|wide|long` and `--delimiter` override detection.

Missing values:

- In **value cells**, empty cells and the tokens `NA`, `N/A`, `NaN`, `null`, `none` and `#N/A` are NaN. The sentinel numbers −9999 and −999 are masked to NaN and counted in `extra.masked_sentinel_count`; any finite value with magnitude above 1e10 is masked and counted in `extra.masked_fill_value_count`.
- In **metadata cells**, only empty text is missing. A value such as `NA` (sodium) or `none` (no fore-optic) is kept as text.

### Mapping file

Columns are matched to Format v1 fields by name, ignoring case and punctuation (`Grain Size` → `sample_particle_size`). For other names, pass `--mapping FILE`: a JSON object from source column to target.

```json
{"sample": "id", "mineral": "material_name", "Grain": "sample_particle_size", "Operator": "extra.operator", "Junk": "ignore"}
```

A target is a Format v1 column name, `id`, `extra`, `extra.<key>` or `ignore`; long CSVs also accept `wavelength`, `value`, `bandpass_fwhm` and `values_stddev`. Columns that map to nothing are kept in `extra.source_metadata`, and the command lists them.

### Scale

Format v1 stores reflectance, transmittance and emissivity as fractions. A wrong scale silently changes every value by a factor of 100 or 10000, so the converter would rather stop with an error than guess.

Only reflectance, transmittance and emissivity spectra are ever divided. Absorbance is decadic and never rescaled (spec §4.1): even with `--value-scale` or an ENVI `reflectance scale factor`, absorbance spectra keep divisor 1, and the output warns how many spectra the stated scale was not applied to.

Each reflectance, transmittance or emissivity spectrum's divisor comes from the first of:

1. `--value-scale N` (for example `100` for percent);
2. a re-import of an `openspeclib export`, recognised by its `.collection.json` (values are already fractions: divisor 1);
3. a scale the file states: ENVI `reflectance scale factor`;
4. units declared per spectrum, such as a `Measurement Units` column (`%`, `0-1`);
5. detection from the data. A spectrum's level is the 90th percentile of its finite values:

   | Data | Level | Scale |
   |---|---|---|
   | float | up to 1.5 | fractions (divisor 1) |
   | float | 1.5 to 15 | ambiguous: bright fractions or dark percent |
   | float | 15 to 150 | percent (100) |
   | float or integer | 150 to 15000 | 0–10000 (10000) |
   | float or integer | above 15000 | fits no scale: the import stops |
   | integer | up to 150 | ambiguous: percent or dark 0–10000 |

The import **stops** and asks for `--value-scale` when:

- any spectrum's level falls in an ambiguous band, or above 15000 (no supported scale fits);
- declared units contradict the data: a level between 0.015 and 1.5 with declared percent or 0–10000 units looks like fractions;
- the detected spectra of one file do not agree (a mixed-scale file); the message names the rows;
- after scaling, a spectrum's median lies outside the hard quality bounds of its quantity (rule Q1, [validation.md](validation.md#quality-profile)), when the scale came from the file, declared units or detection. When you gave `--value-scale`, or the input is a re-import of an export, this is a warning instead, except for absorbance and other quantities that are never rescaled: `--value-scale` does not apply to them, so an implausible one still stops the import (a re-import of an export only warns).

Detected percent has one blind spot, accepted by design: a float file holding *only* dark 0–10000 spectra (reflectance below about 1.5 %, level 15–150) looks exactly like percent data. Divided by 100 instead of 10000, its values (0.15–1.5) still pass the quality bounds, so nothing stops the import. Whenever percent is detected, the output says so; if the file is such data, pass `--value-scale 10000`.

The divisor applied is recorded per spectrum in `extra.source_reflectance_divisor`, the key the loaders use. Files written by earlier versions of `convert` used `extra.source_value_divisor`; a re-import renames it.

### Examples

These use the test fixtures in `tests/fixtures/convert/`:

```bash
# ENVI, big-endian int16 with a reflectance scale factor of 10000 and a bad band
openspeclib convert tests/fixtures/convert/lab_int16.hdr test_lab.parquet \
  --collection-id test_lab --title "Test lab library" --license CC-BY-4.0 \
  --citation "Test Lab (2026). Test spectral library." --quantity reflectance --category mineral

# Wide CSV in EcoSIS style, percent values declared in "Measurement Units"
openspeclib convert tests/fixtures/convert/ecosis_wide.csv maple_lab.parquet \
  --collection-id maple_lab --title "Maple leaves" --license CC-BY-4.0 --citation "Maple Lab (2026)."

# Long CSV with a mapping file and the metadata in JSON
openspeclib convert tests/fixtures/convert/long.csv test_lab.parquet \
  --mapping tests/fixtures/convert/long_mapping.json \
  --metadata-json tests/fixtures/convert/metadata.json --quantity reflectance
```

The output reports what it did: the divisor applied, bands set to NaN, unmapped columns, and the conformance result.

## Export: `openspeclib export`

```bash
openspeclib export usgs_splib07.parquet out/ --to envi \
  --search material=olivine --search wavelength_range_nm=400:2500 \
  --resample-to 400:2500:10 --name olivine
```

Select spectra with `--ids` (repeatable or comma-separated; local ids are accepted), `--search` (repeatable: `material=…`, `category=…`, `quantity=…`, `geometry=…`, `dataset_id=…`, `wavelength_range_nm=LO:HI`; a bare word searches material) and `--limit`. With no selection, every spectrum is exported.

### What is written

- **ENVI** (`--to envi`): `NAME.sli` and `NAME.hdr`. NaN is written as `data ignore value = -9999`, and `reflectance scale factor = 1` says the values are fractions. The header carries a licence summary in `description`; per-spectrum `openspeclib license` and `openspeclib attribution` (citation plus licence); the definitions of the `LicenseRef-` licences used, in `openspeclib license refs`; and the other per-spectrum metadata in `openspeclib <column>` fields.
- **CSV, wide** (`--to csv`, the default layout): `NAME.csv`, one row per spectrum and one column per wavelength, with `license` and `attribution` columns.
- **CSV, long** (`--layout long`): `NAME.csv`, one row per point, with `license` on every point; and `NAME.spectra.csv`, one row per spectrum with its metadata, `license` and `attribution`.
- Missing values are empty cells in CSV.
- **Always:** `NAME.README.md`, which says who to cite, under which licences, which datasets, and what processing was applied; and `NAME.collection.json`, the collection and dataset metadata, ready for `openspeclib convert --metadata-json`. Datasets that were only partly exported are marked `subset` there, and an `openspeclib_export` key tells `convert` that the values are already fractions.

Keep the README with the data when you share it. The licences of the source spectra still apply to exported copies.

### Resampling

ENVI and wide CSV need every exported spectrum on one axis. When the selected spectra have different axes, pass `--resample-to`:

- `START:STOP:STEP` in nanometres (for example `400:2500:10`);
- any ENVI `.hdr` with a `wavelength` list (a spectral library, an image, or a sensor definition), read in its stated `wavelength units`;
- a text file of band centres in nanometres.

Resampling is linear interpolation. It does not apply a spectral response function, and grid points outside a spectrum's range become missing values. Resampled spectra get `resampled` in `measurement_processing`. A `bandpass_fwhm` that differs between spectra, or that was resampled, is not written. `extra` fields that hold one item per band of the original axis (`source_band_order`, `source_band_index`) no longer match the new axis, so they are dropped; the README says so.

### Round trips

An export converts back with its own metadata file:

```bash
openspeclib convert out/olivine.hdr olivine.parquet --metadata-json out/olivine.collection.json
openspeclib convert out/usgs_splib07.csv back.parquet --layout long \
  --spectra-table out/usgs_splib07.spectra.csv --metadata-json out/usgs_splib07.collection.json
```

## Limitations

- **One axis per ENVI library.** ENVI cannot hold spectra with different axes; resample, or export each group separately. Resampling changes the data and is recorded as `resampled`.
- **ENVI has no NaN.** Missing values become −9999 on export and NaN again on import, through `data ignore value`.
- **Scale is not always knowable.** Ambiguous inputs stop the import until you pass `--value-scale`.
- **Geometry is not in ENVI or most CSVs.** Imported spectra are `unknown` unless you pass `--geometry` or the input has a `geometry` column.
- **One collection per call.** A `dataset_id` column with matching `datasets` in `--metadata-json` is supported. A spectrum's licence is its own `license` value; if it has none, its dataset's licence; if that is missing too, the collection's.
