# OpenSpecLib Library Chunk File — Parquet Schema

Library chunk files (`spectra/{source}/{category}.parquet`) are stored as
[Apache Parquet](https://parquet.apache.org/) with [zstd](https://facebook.github.io/zstd/)
compression. One row per spectrum. Nested Pydantic models are flattened to
dot-separated columns for clean queryability in DuckDB / Polars / pandas /
pyarrow.

The machine-readable schema is `library.arrow.schema.json` (auto-generated
from `openspeclib.storage.ARROW_SCHEMA`). This document is the human-readable
companion.

---

## File-level (footer) metadata

Every chunk file carries the following key-value pairs in its Parquet footer
metadata. They are the four chunk-level fields that previously lived at the
top of the JSON chunk file, and they round-trip into `LibraryChunkFile`
without per-row duplication.

| Key                  | Value                                       |
| -------------------- | ------------------------------------------- |
| `openspeclib_version` | OpenSpecLib version that produced the file |
| `source`              | Source library identifier (e.g. `usgs_splib07`) |
| `category`            | Material category or chunk label           |
| `spectrum_count`      | Number of rows / spectra in the chunk      |

Read with:

```python
import pyarrow.parquet as pq
metadata = pq.read_schema("speclib/spectra/usgs_splib07/mineral.parquet").metadata
print(metadata[b"source"])  # b"usgs_splib07"
```

---

## Columns

| Column                            | Arrow type      | Nullable | Notes                                                      |
| --------------------------------- | --------------- | -------- | ---------------------------------------------------------- |
| `id`                              | utf8            | no       | Globally unique (`{source}:{original_id}`)                 |
| `name`                            | utf8            | no       | Human-readable display name                                |
| `source.library`                  | utf8            | no       | Source library enum value                                  |
| `source.library_version`          | utf8            | no       |                                                            |
| `source.original_id`              | utf8            | no       |                                                            |
| `source.filename`                 | utf8            | yes      |                                                            |
| `source.url`                      | utf8            | no       |                                                            |
| `source.license`                  | utf8            | no       |                                                            |
| `source.citation`                 | utf8            | no       |                                                            |
| `material.name`                   | utf8            | no       |                                                            |
| `material.category`               | utf8            | no       | Material category enum value                               |
| `material.subcategory`            | utf8            | yes      |                                                            |
| `material.formula`                | utf8            | yes      |                                                            |
| `material.keywords`               | list&lt;utf8&gt;       | no       | Searchable terms                                           |
| `sample.id`                       | utf8            | yes      |                                                            |
| `sample.description`              | utf8            | yes      |                                                            |
| `sample.particle_size`            | utf8            | yes      |                                                            |
| `sample.origin`                   | utf8            | yes      |                                                            |
| `sample.owner`                    | utf8            | yes      |                                                            |
| `sample.collection_date`          | date32          | yes      | ISO 8601 calendar date                                     |
| `sample.preparation`              | utf8            | yes      |                                                            |
| `measurement.instrument`          | utf8            | yes      |                                                            |
| `measurement.instrument_type`     | utf8            | yes      |                                                            |
| `measurement.laboratory`          | utf8            | yes      |                                                            |
| `measurement.technique`           | utf8            | no       | Measurement technique enum value                           |
| `measurement.geometry`            | utf8            | yes      |                                                            |
| `measurement.date`                | date32          | yes      | ISO 8601 calendar date                                     |
| `spectral_data.type`              | utf8            | no       | Same vocabulary as `measurement.technique`                 |
| `spectral_data.wavelength_unit`   | utf8            | no       | One of `um`, `nm`, `cm-1`                                  |
| `spectral_data.wavelength_min`    | float64         | no       |                                                            |
| `spectral_data.wavelength_max`    | float64         | no       |                                                            |
| `spectral_data.num_points`        | int32           | no       | Equals length of `wavelengths` and `values`                |
| `spectral_data.wavelengths`       | list&lt;float64&gt;    | no       | Variable length per spectrum                               |
| `spectral_data.values`            | list&lt;float64&gt;    | no       | Variable length per spectrum                               |
| `spectral_data.bandpass`          | list&lt;float64&gt;    | yes      | FWHM at each wavelength, when reported                     |
| `quality.has_bad_bands`           | bool            | no       |                                                            |
| `quality.bad_band_count`          | int32           | no       |                                                            |
| `quality.coverage_fraction`       | float64         | no       | 0.0–1.0                                                    |
| `quality.notes`                   | utf8            | yes      |                                                            |
| `additional_properties`           | utf8            | no       | JSON-serialised `dict[str, Any]` — parse on read           |

---

## Querying chunks

### DuckDB (recommended for ad-hoc analytics)

```sql
-- Count spectra by category in a single chunk
SELECT "material.category", COUNT(*) AS n
FROM 'speclib/spectra/usgs_splib07/mineral.parquet'
GROUP BY "material.category"
ORDER BY n DESC;

-- Find spectra covering the visible range across all chunks
SELECT id, name, "spectral_data.wavelength_min", "spectral_data.wavelength_max"
FROM 'speclib/spectra/**/*.parquet'
WHERE "spectral_data.wavelength_min" <= 0.4
  AND "spectral_data.wavelength_max" >= 0.7;
```

DuckDB pushes predicates down to Parquet so only the matching row groups are
read.

### Polars

```python
import polars as pl

lf = pl.scan_parquet("speclib/spectra/usgs_splib07/mineral.parquet")
df = lf.filter(pl.col("spectral_data.wavelength_min") < 0.4).collect()
print(df.select(["id", "name", "material.formula"]))
```

### pandas (via pyarrow)

```python
import pandas as pd

df = pd.read_parquet(
    "speclib/spectra/usgs_splib07/mineral.parquet",
    columns=["id", "name", "material.category", "spectral_data.wavelengths",
             "spectral_data.values"],
)
```

### pyarrow (lowest-level, fastest for full-chunk reads)

```python
import pyarrow.parquet as pq

table = pq.read_table("speclib/spectra/usgs_splib07/mineral.parquet")
print(table.schema)
print(pq.read_schema(table.schema_arrow.metadata))
```

### Reconstructing Pydantic records

If you want strongly-typed `SpectrumRecord` / `LibraryChunkFile` objects in
Python, use the storage helper rather than rebuilding the deserialisation
yourself:

```python
from pathlib import Path
from openspeclib.storage import read_chunk

chunk = read_chunk(Path("speclib/spectra/usgs_splib07/mineral.parquet"))
print(chunk.spectrum_count, "spectra")
for spectrum in chunk.spectra[:3]:
    print(spectrum.id, spectrum.material.name)
```
