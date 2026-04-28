---
license: cc0-1.0
language:
- en
pretty_name: ECOSTRESS Spectral Library (OpenSpecLib re-upload)
size_categories:
- 1K<n<10K
task_categories:
- tabular-classification
- feature-extraction
tags:
- spectroscopy
- remote-sensing
- earth-observation
- hyperspectral
- vegetation
- minerals
- soil
- ecostress
- openspeclib
configs:
- config_name: default
  data_files:
  - split: train
    path: ecostress.parquet
---

# ECOSTRESS Spectral Library — OpenSpecLib re-upload

This dataset is a re-upload of the **ECOSTRESS Spectral Library** (a
re-publication of NASA JPL's ECOSTRESS / ASTER spectral library) in the
unified [OpenSpecLib](https://github.com/null-jones/openspeclib) Parquet
schema. It is the same schema used by the OpenSpecLib GitHub releases —
one row per spectrum, dot-separated columns for nested metadata, zstd
compression — packaged as a standalone HuggingFace dataset for easy
ingestion into Datasets / `pandas` / `polars` / DuckDB workflows.

The wavelength axis for each spectrum is referenced by an integer
``spectral_data.wavelength_grid_id`` that points into a sibling
`wavelengths.parquet` registry. See the OpenSpecLib
[parquet-schema reference](https://github.com/null-jones/openspeclib/blob/main/schemas/library.parquet-schema.md)
for the complete column list.

## Source library

| | |
| --- | --- |
| **Original archive** | https://speclib.jpl.nasa.gov/ |
| **Publisher** | Jet Propulsion Laboratory (JPL), California Institute of Technology |
| **Original DOI** | [10.1016/j.rse.2019.05.015](https://doi.org/10.1016/j.rse.2019.05.015) |
| **License** | Public domain (no copyright restrictions on the underlying spectra) |
| **Materials** | Minerals, rocks, soils, vegetation, water, snow/ice, man-made materials, meteorites |
| **Wavelength range** | 0.35 – 15.4 µm (visible through thermal infrared) |
| **Measurement techniques** | Reflectance (VSWIR) and emissivity (TIR), depending on band |
| **Approximate size** | ~3,400 spectra |

ECOSTRESS itself extends the earlier ASTER Spectral Library and
incorporates contributions from Johns Hopkins University (JHU), JPL, and
USGS. Visible / shortwave infrared bands are reported as **directional
hemispherical reflectance**; thermal infrared bands are reported as
**emissivity**.

## Citation

Please cite the original source publication when using this data:

> Meerdink, S. K., Hook, S. J., Roberts, D. A., & Abbott, E. A. (2019).
> The ECOSTRESS spectral library version 1.0. *Remote Sensing of
> Environment*, 230, 111196.
> <https://doi.org/10.1016/j.rse.2019.05.015>

If you use the OpenSpecLib re-upload (this HuggingFace dataset, the
unified schema, or the ingestion code), please also cite OpenSpecLib:

> Jones, E. (2026). *OpenSpecLib: an amalgamated spectral library
> toolkit*. <https://github.com/null-jones/openspeclib>

## Schema

Each row is one spectrum. Top-level fields (flattened with dot
separators):

| Group | Fields |
| --- | --- |
| Identification | `id`, `name` |
| Provenance | `source.library`, `source.library_version`, `source.original_id`, `source.filename`, `source.url`, `source.license`, `source.citation` |
| Material | `material.name`, `material.category`, `material.subcategory`, `material.formula`, `material.keywords` |
| Sample | `sample.id`, `sample.description`, `sample.particle_size`, `sample.origin`, `sample.owner`, `sample.collection_date`, `sample.preparation` |
| Measurement | `measurement.instrument`, `measurement.instrument_type`, `measurement.laboratory`, `measurement.technique`, `measurement.geometry`, `measurement.date` |
| Spectral data | `spectral_data.type`, `spectral_data.wavelength_unit`, `spectral_data.wavelength_min`, `spectral_data.wavelength_max`, `spectral_data.num_points`, `spectral_data.wavelength_grid_id`, `spectral_data.values`, `spectral_data.bandpass`, `spectral_data.reflectance_scale` |
| Quality | `quality.has_bad_bands`, `quality.bad_band_count`, `quality.coverage_fraction`, `quality.notes` |
| Extra | `additional_properties` (JSON-serialised string) |

### Reflectance scale

All values are normalised to the **0–1 unit interval**. OpenSpecLib
assumes every source reflectance scale is a power-of-10 multiplier of the
unit interval (one of 0–1, 0–100, or 0–10000); ECOSTRESS is uniformly
0–1 in the source data and passes through with no scaling.

### Wavelength axis

Wavelengths are referenced by an integer ``spectral_data.wavelength_grid_id``
into a sibling `wavelengths.parquet` registry (also published with this
dataset). To recover the explicit axis for a spectrum, join on `grid_id`:

```python
import pandas as pd
spectra = pd.read_parquet("ecostress.parquet")
grids = pd.read_parquet("wavelengths.parquet").set_index("grid_id")

s = spectra.iloc[0]
wl = grids.loc[s["spectral_data.wavelength_grid_id"], "wavelengths"]
y = s["spectral_data.values"]
```

## Quick load

```python
from datasets import load_dataset
ds = load_dataset("<org>/openspeclib-ecostress", split="train")
print(ds[0]["material.name"], ds[0]["measurement.technique"])
```

Or directly with pandas / pyarrow:

```python
import pandas as pd
df = pd.read_parquet(
    "hf://datasets/<org>/openspeclib-ecostress/ecostress.parquet"
)
```

## License

The underlying ECOSTRESS Spectral Library data is in the public domain.
No additional restrictions are imposed by this re-upload. We distribute
this dataset under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)
to make that explicit.

The OpenSpecLib ingestion *code* (the loader and tooling that produced
this Parquet file) is licensed under MIT — see the
[OpenSpecLib repository](https://github.com/null-jones/openspeclib).
