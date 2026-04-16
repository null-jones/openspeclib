# Processing Pipeline

## Overview

The OpenSpecLib processing pipeline transforms spectral data from multiple heterogeneous source libraries into a unified, validated master library. The pipeline operates in four sequential stages: **download**, **ingest**, **combine**, and **validate**.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Download   │────>│   Ingest    │────>│   Combine   │────>│  Validate   │
│              │     │             │     │             │     │             │
│ Fetch source │     │ Parse into  │     │ Merge into  │     │ Schema +    │
│ archives     │     │ standard    │     │ master      │     │ semantic    │
│              │     │ records     │     │ library     │     │ checks      │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
```

Each stage is accessible via the `openspeclib` CLI.

## Stage 1: Download

```bash
openspeclib download --source {usgs,ecostress,relab,asu_tes,bishop} --target ./raw/{source}
```

Downloads and extracts the source library archive into the target directory. Each loader manages its own download logic, handling archive formats (ZIP, tar) and any required authentication.

### Source Download Details

| Source | Format | Approximate Size | Notes |
|---|---|---|---|
| USGS Speclib 07 | ZIP archive of ASCII files | ~500 MB | Public, no authentication |
| ECOSTRESS | ZIP archive of text files | ~200 MB | Public, from JPL |
| RELAB | Text files (header + two-column data) | ~300 MB | Public, from Brown University |
| ASU TES | Text files (header + two-column data) | ~50 MB | Public, from ASU |
| Bishop | Text files (header + two-column data) | ~30 MB | Public, from SETI Institute |

## Stage 2: Ingest

```bash
openspeclib ingest --source {usgs,ecostress,relab,asu_tes,bishop} --input ./raw/{source} --output ./processed/
```

Each source library has a dedicated loader that parses native file formats into the unified `SpectrumRecord` data structure. Loaders implement the `BaseLoader` interface defined in `src/openspeclib/loaders/base.py`.

The ingest stage writes one JSONL (JSON Lines) file per source to the output directory, where each line is a complete serialized `SpectrumRecord`.

### USGS Speclib 07 Loader

**Input format:** Single-column ASCII text files containing reflectance values. Wavelength axes are stored in separate shared files per spectrometer.

**Processing steps:**
1. Scan the directory tree for spectrum files matching the USGS naming convention (`s07*.txt`).
2. Identify and cache wavelength/bandpass files for each spectrometer code.
3. For each spectrum file:
   - Read reflectance values from the single-column file.
   - Map the parent directory name to a material category (e.g., `ChapterM_Minerals` to `mineral`).
   - Parse the filename to extract material name and sample identifier.
   - Detect bad bands using the sentinel value `-1.23e34`.
   - Assemble the `SpectrumRecord` with matched wavelength data.

### ECOSTRESS Loader

**Input format:** Individual text files with a metadata header section followed by two-column (wavelength, value) spectral data.

**Processing steps:**
1. Locate all `.spectrum.txt` files (or `.txt` files) in the source directory.
2. For each file:
   - Parse the header section as key-value pairs until reaching numeric data.
   - Extract metadata fields: Name, Class, SubClass, ParticleSize, SampleNo, Origin, Description, Measurement type, etc.
   - Read the two-column data section into wavelength and value arrays.
   - Map the ECOSTRESS Class field to the canonical material category.
   - Determine the measurement technique from the Measurement or YUnits header field (reflectance for VIS/SWIR, emissivity for TIR).

### RELAB Loader

**Input format:** Individual text files with `#`-prefixed header lines containing key-value metadata, followed by two-column data (wavelength in micrometers, reflectance).

**Processing steps:**
1. Locate all `.txt` files in the source directory, excluding readme and index files.
2. For each file:
   - Parse `#`-prefixed header lines as key-value pairs (stripping the `#` prefix before extracting keys).
   - Read the two-column data section into wavelength (micrometers) and reflectance value arrays.
   - Classify the material category from the Sample Type and Name fields (mineral, meteorite, lunar, rock, soil).
   - Extract sample metadata: sample ID, particle size, geographic origin, and measurement geometry.
   - All spectra are recorded as bidirectional reflectance.

### ASU TES Loader

**Input format:** Individual text files with optional `#`- or `!`-prefixed header lines, followed by two-column data (wavenumber in cm-1, emissivity).

**Processing steps:**
1. Locate all `.txt`, `.asc`, and `.csv` files in the source directory.
2. For each file:
   - Parse header lines (stripping comment prefixes) as key-value pairs.
   - Read two-column data into wavenumber and emissivity arrays.
   - Detect the mineral group from the sample name (e.g., "quartz" maps to tectosilicate).
   - Wavelength unit is wavenumbers (cm-1); measurement type is emissivity.
   - All spectra are attributed to the ASU Thermal Emission Spectroscopy Laboratory.

### Bishop Loader

**Input format:** Individual text files with `#`-prefixed header lines containing key-value metadata, followed by two-column data (wavelength, reflectance).

**Processing steps:**
1. Locate all `.txt`, `.asc`, and `.csv` files in the source directory.
2. For each file:
   - Parse `#`-prefixed header lines as key-value pairs.
   - Read two-column data into wavelength and reflectance value arrays.
   - Infer wavelength unit from the data range (values > 100 are nanometers, otherwise micrometers).
   - Classify the material category from the Type header, falling back to whole-word keyword matching in the sample name.
   - All spectra are recorded as reflectance.

## Stage 3: Combine

```bash
openspeclib combine --input ./processed/ --output ./library/
```

The combiner reads all JSONL files from the processed directory and constructs the master library:

1. **Emit one Parquet file per source** at `spectra/{source}.parquet`. Records are streamed through `pyarrow.parquet.ParquetWriter` so memory usage is bounded regardless of source size. Inside each file, Parquet row groups (default 1,000 spectra per group) give readers partial-read granularity — category filtering is a column predicate, not a file selection.
2. **Build the catalog index** — for each spectrum, create a catalog entry (metadata without spectral arrays) with a `chunk_file` reference (`spectra/{source}.parquet`).
3. **Compute aggregate statistics** — total spectra, per-source counts, per-category counts.
4. **Write output files** — `catalog.json` (JSON), per-source `.parquet` files under `spectra/` (zstd-compressed, one row per spectrum), and `VERSION`.

File-level metadata (`openspeclib_version`, `source`) is written into the Parquet footer key-value metadata; the spectrum count is read from Parquet's native `num_rows`. See `schemas/library.parquet-schema.md` for the full column reference.

## Stage 4: Validate

```bash
openspeclib validate ./library/
```

Validation comprises two layers:

### Schema Validation

`catalog.json` is validated against `schemas/catalog.schema.json` (JSON Schema Draft 2020-12).

Each Parquet chunk file is validated against the canonical Arrow schema (`openspeclib.storage.ARROW_SCHEMA`): column names, types, and nullability are compared against the spec, and any drift is reported. The footer must also carry the four required metadata keys (`openspeclib_version`, `source`, `category`, `spectrum_count`).

### Semantic Validation

Cross-referencing checks that go beyond schema conformance:

- **Referential integrity:** Every `chunk_file` referenced in the catalog exists on disk.
- **Bidirectional consistency:** Every spectrum in a chunk file has a corresponding catalog entry.
- **Array length consistency:** `wavelengths` and `values` array lengths match the declared `num_points`.
- **Boundary accuracy:** `wavelength_min` and `wavelength_max` match the actual data extrema.
- **Uniqueness:** No duplicate spectrum IDs exist across the entire library.
- **Quality metrics:** `bad_band_count` and `coverage_fraction` are consistent with the actual data values.
- **Statistics accuracy:** Aggregate counts match the actual entry counts.

## Running the Full Pipeline

```bash
# Download all sources
openspeclib download --source usgs --target ./raw/usgs
openspeclib download --source ecostress --target ./raw/ecostress
openspeclib download --source relab --target ./raw/relab
openspeclib download --source asu_tes --target ./raw/asu_tes
openspeclib download --source bishop --target ./raw/bishop

# Ingest each source
openspeclib ingest --source usgs --input ./raw/usgs --output ./processed/
openspeclib ingest --source ecostress --input ./raw/ecostress --output ./processed/
openspeclib ingest --source relab --input ./raw/relab --output ./processed/
openspeclib ingest --source asu_tes --input ./raw/asu_tes --output ./processed/
openspeclib ingest --source bishop --input ./raw/bishop --output ./processed/

# Combine into master library
openspeclib combine --input ./processed/ --output ./library/

# Validate the result
openspeclib validate ./library/
```

The GitHub Actions workflow (`build-release.yml`) automates this entire sequence and packages the output as a versioned GitHub Release.
