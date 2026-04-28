# OpenSpecLib Source Library File — Parquet Schema

Each source library is stored as a single
[Apache Parquet](https://parquet.apache.org/) file at
`spectra/{source}.parquet` with [zstd](https://facebook.github.io/zstd/)
compression, one row per spectrum. Nested Pydantic models are flattened to
dot-separated columns for clean queryability in DuckDB / Polars / pandas /
pyarrow. Wavelength axes are deduplicated into a sibling
`spectra/wavelengths.parquet` registry so the largest column doesn't repeat
across thousands of rows.

Per-source files are sorted by `id` and written with Parquet column statistics
enabled, so `WHERE id IN (...)` lookups served over HTTP Range requests prune
to one or two row groups per id rather than scanning the file. Row groups
default to 250 spectra (small enough that a successful prune fetches only a
few hundred KB).

The machine-readable schema is `library.arrow.schema.json` (auto-generated
from `openspeclib.storage.ARROW_SCHEMA` and `WAVELENGTHS_ARROW_SCHEMA`). This
document is the human-readable companion.

---

## File-level (footer) metadata

Every source file carries the following key-value pairs in its Parquet footer
metadata. The spectrum count is intentionally *not* in the footer — Parquet's
native `num_rows` is authoritative and can never drift from the actual row
count.

| Key                  | Value                                       |
| -------------------- | ------------------------------------------- |
| `openspeclib_version` | OpenSpecLib version that produced the file |
| `source`              | Source library identifier (e.g. `usgs_splib07`) |

Read with:

```python
import pyarrow.parquet as pq
pf = pq.ParquetFile("speclib/spectra/usgs_splib07.parquet")
print(pf.schema_arrow.metadata[b"source"])  # b"usgs_splib07"
print(pf.metadata.num_rows)                  # total spectra in this source
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
| `material.category`               | utf8            | no       | Material category enum value — filter on this for category queries |
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
| `spectral_data.num_points`        | int32           | no       | Equals length of `values` and the resolved wavelength axis |
| `spectral_data.wavelength_grid_id` | int32          | no       | Reference into `spectra/wavelengths.parquet` (see below)   |
| `spectral_data.values`            | list&lt;float64&gt;    | no       | Variable length per spectrum                               |
| `spectral_data.bandpass`          | list&lt;float64&gt;    | yes      | FWHM at each wavelength, when reported                     |
| `spectral_data.reflectance_scale` | utf8            | no       | `unit` (0–1) for combined libraries; loaders normalise on ingest |
| `quality.has_bad_bands`           | bool            | no       |                                                            |
| `quality.bad_band_count`          | int32           | no       |                                                            |
| `quality.coverage_fraction`       | float64         | no       | 0.0–1.0                                                    |
| `quality.notes`                   | utf8            | yes      |                                                            |
| `additional_properties`           | utf8            | no       | JSON-serialised `dict[str, Any]` — parse on read           |

---

## Wavelength grid registry — `spectra/wavelengths.parquet`

The wavelength axis is heavily redundant in practice (USGS uses ~3 unique
grids across 2.5k spectra; ECOSIS ~10 across 17k), so we store each unique
`(wavelength_unit, wavelengths)` pair once in this side file and reference it
from per-source rows by `spectral_data.wavelength_grid_id`. This typically
shrinks the per-source files by ~20–30% and lets HTTP clients (e.g. the
viewer) load the registry once at init.

| Column            | Arrow type           | Nullable | Notes                                            |
| ----------------- | -------------------- | -------- | ------------------------------------------------ |
| `grid_id`         | int32                | no       | Globally unique; row index within this file      |
| `source`          | utf8                 | no       | First source seen contributing this grid (provenance only) |
| `wavelength_unit` | utf8                 | no       | One of `um`, `nm`, `cm-1`                        |
| `n_points`        | int32                | no       |                                                  |
| `wavelengths`     | list&lt;float64&gt;  | no       |                                                  |

```python
from pathlib import Path
from openspeclib.storage import read_wavelengths

registry = read_wavelengths(Path("speclib/spectra/wavelengths.parquet"))
wavelengths, unit = registry.get(0)
```

`read_chunk` and `iter_records` automatically locate this sibling file and
rehydrate `SpectralData.wavelengths` for callers, so consumers using the
Pydantic surface never see the `grid_id` indirection.

---

## Reflectance scale convention

Every record carries `spectral_data.reflectance_scale`, which in a combined
library is always `"unit"` (values on [0, 1]). Loaders normalise sources that
upload as `"percent"` (0–100) at ingest; the original source scale, when
non-unit, is preserved in `additional_properties.source_reflectance_scale`
for provenance. ECOSIS scale handling is configured per-dataset in
`openspeclib/loaders/ecosis_scales.py`.

---

## Querying source files

### DuckDB (recommended for ad-hoc analytics)

```sql
-- Count spectra by category in one source
SELECT "material.category", COUNT(*) AS n
FROM 'speclib/spectra/usgs_splib07.parquet'
GROUP BY "material.category"
ORDER BY n DESC;

-- Only minerals in the visible range
SELECT id, name, "spectral_data.wavelength_min", "spectral_data.wavelength_max"
FROM 'speclib/spectra/usgs_splib07.parquet'
WHERE "material.category" = 'mineral'
  AND "spectral_data.wavelength_min" <= 0.4
  AND "spectral_data.wavelength_max" >= 0.7;

-- Union across sources with a glob
SELECT id, "source.library", "material.name"
FROM 'speclib/spectra/*.parquet'
WHERE "material.category" = 'rock';
```

DuckDB pushes predicates down to Parquet so only the matching row groups are
read — even when the source is served over HTTP (via `INSTALL httpfs; LOAD
httpfs;`).

### Polars

```python
import polars as pl

lf = pl.scan_parquet("speclib/spectra/usgs_splib07.parquet")
df = (
    lf.filter(pl.col("material.category") == "mineral")
      .filter(pl.col("spectral_data.wavelength_min") < 0.4)
      .collect()
)
print(df.select(["id", "name", "material.formula"]))
```

### pandas (via pyarrow)

```python
import pandas as pd

df = pd.read_parquet(
    "speclib/spectra/usgs_splib07.parquet",
    columns=["id", "name", "material.category", "spectral_data.wavelengths",
             "spectral_data.values"],
    filters=[("material.category", "=", "mineral")],
)
```

### pyarrow (lowest-level, fastest for full-source reads)

```python
import pyarrow.parquet as pq

pf = pq.ParquetFile("speclib/spectra/usgs_splib07.parquet")
print(pf.metadata.num_rows, "spectra in", pf.schema_arrow.metadata[b"source"])
# Read only the first row group
table = pf.read_row_group(0)
# Verify column statistics are present (used for row-group skipping on id)
print(pf.metadata.row_group(0).column(0).statistics)
```

### Streaming row-by-row (any source size)

```python
from pathlib import Path
from openspeclib.storage import iter_records

for spectrum in iter_records(Path("speclib/spectra/usgs_splib07.parquet")):
    print(spectrum.id, spectrum.material.name)
```

### Reconstructing the whole source as Pydantic records

```python
from pathlib import Path
from openspeclib.storage import read_chunk

chunk = read_chunk(Path("speclib/spectra/usgs_splib07.parquet"))
print(chunk.spectrum_count, "spectra from", chunk.source)
for spectrum in chunk.spectra[:3]:
    print(spectrum.id, spectrum.material.name)
```
