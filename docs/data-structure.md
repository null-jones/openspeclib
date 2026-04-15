# OpenSpecLib Data Structure Specification

## Overview

The OpenSpecLib unified data structure provides a standardized representation for spectral measurements originating from heterogeneous source libraries. The design prioritizes readability, interoperability, and discoverability while preserving the full fidelity of source data.

This document constitutes the formal specification for the OpenSpecLib schema. The catalog conforms to the JSON Schema definitions in `schemas/`; library chunk files conform to the canonical Arrow schema documented in `schemas/library.parquet-schema.md`.

## Architecture

The master library employs a two-tier architecture designed to balance accessibility with scalability:

### Tier 1: Catalog (`catalog.json`)

The catalog serves as the primary discovery and search index. It contains complete metadata for every spectrum in the library but excludes raw spectral data arrays (wavelengths, values, and bandpass). This keeps the catalog sufficiently compact (tens of megabytes) to load into memory for interactive querying.

Each catalog entry includes a `chunk_file` reference that identifies the library chunk file containing the corresponding full spectral data.

### Tier 2: Library Chunks (`spectra/{source}/{category}.parquet`)

Spectral data is partitioned into chunk files organized by source library and material category. Each chunk file contains complete spectrum records including raw data arrays. Chunks are stored as [Apache Parquet](https://parquet.apache.org/) with [zstd](https://facebook.github.io/zstd/) compression — one row per spectrum, with nested Pydantic models flattened to dot-separated columns (e.g. `material.name`, `spectral_data.wavelengths`). The full column reference and querying examples live in `schemas/library.parquet-schema.md`.

```
library/
├── catalog.json
├── VERSION
└── spectra/
    ├── usgs_splib07/
    │   ├── mineral.parquet
    │   ├── rock.parquet
    │   └── vegetation.parquet
    ├── ecostress/
    │   ├── mineral.parquet
    │   └── vegetation.parquet
    ├── relab/
    │   ├── mineral.parquet
    │   └── meteorite.parquet
    ├── asu_tes/
    │   └── mineral.parquet
    └── bishop/
        └── mineral.parquet
```

Chunk-level metadata (`openspeclib_version`, `source`, `category`, `spectrum_count`) is stored in the Parquet file's footer key-value metadata so it round-trips back into the in-memory `LibraryChunkFile` representation without per-row duplication.

## Spectrum Record

Each spectrum is represented as a JSON object with the following top-level fields:

### `id` (string, required)

A globally unique identifier following the pattern `{source_library}:{original_id}`. The source library prefix guarantees collision-free identifiers across all constituent libraries.

**Examples:** `usgs_splib07:s07AV95a_Olivine_GDS70_ASD`, `ecostress:calcite_ws272`, `relab:olivine_fo91`, `asu_tes:quartz_bur4120`, `bishop:calcite_iceland_spar`

### `name` (string, required)

A human-readable display name for the spectrum, suitable for use in search results and user interfaces.

### `source` (object, required)

Provenance metadata linking the spectrum to its origin library.

| Field | Type | Required | Description |
|---|---|---|---|
| `library` | enum | yes | Source library identifier: `usgs_splib07`, `ecostress`, `relab`, `asu_tes`, or `bishop` |
| `library_version` | string | yes | Version of the source library from which the spectrum was extracted |
| `original_id` | string | yes | The identifier used within the source library |
| `filename` | string | no | Original filename in the source archive |
| `url` | string | yes | DOI or URL for the source library |
| `license` | string | yes | License governing the use of the source data |
| `citation` | string | yes | Recommended citation string for academic attribution |

### `material` (object, required)

Classification of the material whose spectrum was measured.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Material name (e.g., "Olivine", "Quartz", "Oak leaf") |
| `category` | enum | yes | Top-level material category (see controlled vocabulary below) |
| `subcategory` | string | no | Source-specific subcategory (e.g., mineral group, vegetation type) |
| `formula` | string | no | Chemical formula where applicable |
| `keywords` | string[] | yes | Searchable terms for discovery |

**Material Category Vocabulary:**
`mineral`, `rock`, `soil`, `vegetation`, `npv` (non-photosynthetic vegetation), `water`, `snow_ice`, `man_made`, `meteorite`, `lunar`, `organic_compound`, `mixture`, `other`

### `sample` (object, required)

Information about the physical sample that was measured.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | no | Sample identifier from the source |
| `description` | string | no | Free-text description of the sample |
| `particle_size` | string | no | Particle size or size range as reported |
| `origin` | string | no | Geographic or geological provenance |
| `owner` | string | no | Institution or individual owning the sample |
| `collection_date` | date | no | Date the sample was collected (ISO 8601) |
| `preparation` | string | no | Description of sample preparation procedures |

### `measurement` (object, required)

Instrument configuration and conditions under which the spectrum was acquired.

| Field | Type | Required | Description |
|---|---|---|---|
| `instrument` | string | no | Instrument name and model |
| `instrument_type` | string | no | Category of instrument |
| `laboratory` | string | no | Facility where the measurement was performed |
| `technique` | enum | yes | `reflectance`, `emissivity`, `absorbance`, or `transmittance` |
| `geometry` | string | no | Measurement geometry |
| `date` | date | no | Date of measurement (ISO 8601) |

### `spectral_data` (object, required)

The spectral measurement data and associated axis information.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | enum | yes | Type of spectral values: `reflectance`, `emissivity`, `absorbance`, `transmittance` |
| `wavelength_unit` | enum | yes | Unit of the wavelength axis: `um` (micrometers), `nm` (nanometers), `cm-1` (wavenumbers) |
| `wavelength_min` | number | yes | Minimum value on the wavelength axis |
| `wavelength_max` | number | yes | Maximum value on the wavelength axis |
| `num_points` | integer | yes | Number of spectral data points (must be >= 1) |
| `wavelengths` | number[] | yes* | Wavelength (or wavenumber) axis values |
| `values` | number[] | yes* | Spectral measurement values |
| `bandpass` | number[] | no | Bandpass (FWHM) at each wavelength position |

*In catalog entries, `wavelengths`, `values`, and `bandpass` are omitted. In Parquet chunks, these fields appear as columns named `spectral_data.wavelengths`, `spectral_data.values`, and `spectral_data.bandpass` (`list<float64>`).

### `additional_properties` (object, required)

A flexible key-value store for source-specific metadata that does not map to the standard schema fields. This preserves information that is unique to individual source libraries without inflating the core schema.

**Examples:**
- USGS spectra may include X-ray diffraction (XRD) or electron probe micro-analysis (EPMA) results.
- RELAB spectra may include detailed sample preparation notes and viewing geometry parameters.

### `quality` (object, required)

Data quality indicators.

| Field | Type | Required | Description |
|---|---|---|---|
| `has_bad_bands` | boolean | yes | Whether invalid bands are present |
| `bad_band_count` | integer | yes | Number of invalid data points |
| `coverage_fraction` | number | yes | Fraction of valid data points (0.0 to 1.0) |
| `notes` | string | no | Additional quality annotations |

## Wavelength Unit Convention

Spectra are stored in their native wavelength domain to prevent interpolation artifacts that would arise from unit conversion:

| Source | Domain | Unit | Typical Range |
|---|---|---|---|
| USGS Speclib 07 | Optical | `um` (micrometers) | 0.2 -- 200 um |
| ECOSTRESS | Optical / Thermal | `um` (micrometers) | 0.35 -- 15.4 um |
| RELAB | Optical | `um` (micrometers) | 0.3 -- 26 um |
| ASU TES | Thermal Infrared | `cm-1` (wavenumbers) | 220 -- 2000 cm-1 |
| Bishop | Optical | `um` (micrometers) | 0.3 -- 25 um |

The `openspeclib.units` module provides conversion functions between all supported units for downstream analysis.

## Schemas

Schemas are provided in `schemas/`:

- **`catalog.schema.json`** — JSON Schema (Draft 2020-12) for `catalog.json`.
- **`spectrum.schema.json`** — JSON Schema (Draft 2020-12) for the in-memory `SpectrumRecord` shape (useful for validating any JSONified record, e.g. the `.jsonl` files emitted by `openspeclib ingest`).
- **`library.arrow.schema.json`** — Machine-readable dump of the canonical Arrow schema used for library chunk Parquet files (column names, types, nullability, footer metadata keys).
- **`library.parquet-schema.md`** — Human-readable column reference and querying examples (DuckDB / Polars / pandas / pyarrow) for chunk files.

The JSON Schemas and the Arrow schema dump are programmatically generated from the authoritative Pydantic models in `src/openspeclib/models.py` and the `ARROW_SCHEMA` defined in `src/openspeclib/storage.py` (see `scripts/generate_schemas.py`).

## Querying chunk files

Because chunks are Parquet, downstream consumers can issue predicate-pushdown queries directly without loading entire files into memory:

```sql
-- DuckDB: count spectra by category in a single chunk
SELECT "material.category", COUNT(*) AS n
FROM 'library/spectra/usgs_splib07/mineral.parquet'
GROUP BY "material.category";
```

```python
# Polars: scan and filter
import polars as pl
lf = pl.scan_parquet("library/spectra/usgs_splib07/mineral.parquet")
df = lf.filter(pl.col("spectral_data.wavelength_min") < 0.4).collect()
```

```python
# Round-trip back to Pydantic
from openspeclib.storage import read_chunk
chunk = read_chunk(Path("library/spectra/usgs_splib07/mineral.parquet"))
```

See `schemas/library.parquet-schema.md` for the full reference.
