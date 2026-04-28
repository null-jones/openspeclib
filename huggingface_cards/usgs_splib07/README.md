---
license: cc0-1.0
language:
- en
pretty_name: USGS Spectral Library Version 7 (OpenSpecLib re-upload)
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
- minerals
- rocks
- soils
- vegetation
- usgs
- speclib
- openspeclib
configs:
- config_name: default
  data_files:
  - split: train
    path: usgs_splib07.parquet
---

# USGS Spectral Library Version 7 — OpenSpecLib re-upload

This dataset is a re-upload of **USGS Spectral Library Version 7**
(Speclib07), packaged in the unified
[OpenSpecLib](https://github.com/null-jones/openspeclib) Parquet schema.
It is the same schema used by the OpenSpecLib GitHub releases — one row
per spectrum, dot-separated columns for nested metadata, zstd
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
| **Original archive** | https://www.usgs.gov/labs/spectroscopy-lab |
| **Publisher** | U.S. Geological Survey (USGS) Spectroscopy Laboratory, Denver |
| **Original DOI** | [10.3133/ds1035](https://doi.org/10.3133/ds1035) |
| **License** | Public domain (U.S. Government work) |
| **Materials** | Minerals, rocks, soils, vegetation, water/ice, organic compounds, man-made materials |
| **Wavelength range** | 0.2 – 200 µm (UV through far-IR; per-spectrum range varies by spectrometer) |
| **Measurement technique** | Reflectance (laboratory and field spectrometers) |
| **Approximate size** | ~2,500 spectra |

Speclib07 consolidates measurements made by USGS over decades using
five different spectrometers (ASD Fieldspec, Beckman 5270, Nicolet
FTIR, etc.). Per-spectrum metadata records the spectrometer, sample
preparation, particle size, and source publication.

## Citation

Please cite the original publication when using this data:

> Kokaly, R. F., Clark, R. N., Swayze, G. A., Livo, K. E., Hoefen,
> T. M., Pearson, N. C., … Klein, A. J. (2017). *USGS Spectral Library
> Version 7* (Data Series 1035). U.S. Geological Survey.
> <https://doi.org/10.3133/ds1035>

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
unit interval (one of 0–1, 0–100, or 0–10000); USGS Speclib07 is
uniformly 0–1 in the source data and passes through with no scaling.

USGS-specific bad-band detection is preserved: `quality.has_bad_bands`
flags spectra with sentinel values (`-1.23e34`) and
`quality.bad_band_count` records how many points are invalid.

### Wavelength axis

Wavelengths are referenced by an integer ``spectral_data.wavelength_grid_id``
into a sibling `wavelengths.parquet` registry (also published with this
dataset). USGS uses ~3 distinct wavelength grids across all 2,500
spectra (one per spectrometer family), so deduplication is dramatic.
To recover the explicit axis for a spectrum, join on `grid_id`:

```python
import pandas as pd
spectra = pd.read_parquet("usgs_splib07.parquet")
grids = pd.read_parquet("wavelengths.parquet").set_index("grid_id")

s = spectra.iloc[0]
wl = grids.loc[s["spectral_data.wavelength_grid_id"], "wavelengths"]
y = s["spectral_data.values"]
```

## Quick load

```python
from datasets import load_dataset
ds = load_dataset("<org>/openspeclib-usgs-splib07", split="train")
print(ds[0]["material.name"], ds[0]["material.formula"])
```

Or directly with pandas / pyarrow:

```python
import pandas as pd
df = pd.read_parquet(
    "hf://datasets/<org>/openspeclib-usgs-splib07/usgs_splib07.parquet"
)
```

## License

USGS Speclib07 is a U.S. Government work and is in the public domain
(no copyright restrictions). No additional restrictions are imposed by
this re-upload. We distribute this dataset under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) to make
that explicit.

The OpenSpecLib ingestion *code* (the loader and tooling that produced
this Parquet file) is licensed under MIT — see the
[OpenSpecLib repository](https://github.com/null-jones/openspeclib).
