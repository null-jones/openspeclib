<p align="center">
  <img src="assets/logo.svg" alt="OpenSpecLib" width="480">
</p>

<p align="center">
  An open-source amalgamated spectral library and processing toolkit that combines spectral measurements from multiple authoritative sources into a unified, schema-validated data structure.
</p>

## Overview

Spectral libraries are essential reference datasets for material identification, remote sensing, and geochemical analysis. However, the major publicly available libraries each employ different file formats, metadata schemas, and organizational conventions, creating barriers to cross-library search, comparison, and interoperability.

OpenSpecLib addresses this fragmentation by ingesting spectral data from multiple sources, normalizing it into a standard data structure defined by a formal JSON Schema, and producing a versioned master library suitable for downstream analysis and tool development.

## Source Libraries

| Source | Materials | Wavelength Range | Spectra |
|---|---|---|---|
| [USGS Spectral Library Version 7](https://doi.org/10.5066/F7RR1WDJ) | Minerals, rocks, soils, vegetation, water, man-made | 0.2 -- 200 um | ~2,500 |
| [ECOSTRESS Spectral Library](https://speclib.jpl.nasa.gov) | Minerals, rocks, soils, vegetation, man-made, meteorites | 0.35 -- 15.4 um | ~3,400 |
| [RELAB Spectral Database](https://sites.brown.edu/relab/) | Minerals, meteorites, lunar samples | 0.3 -- 26 um | ~3,000 |
| [ASU Thermal Emission Spectral Library](https://speclib.asu.edu) | Rock-forming minerals (thermal IR) | 5 -- 45 um (2000 -- 220 cm-1) | ~800 |
| [Bishop Spectral Library](https://dmp.seti.org/jbishop/spectral-library.html) | Carbonates, hydrated minerals, phyllosilicates | 0.3 -- 25 um | ~500 |

## Installation

```bash
pip install -e .

# With development tools
pip install -e ".[dev]"
```

## Quick Start

### Using a Published Release

Download the latest release from [GitHub Releases](https://github.com/null-jones/openspeclib/releases):

```python
import json
import pyarrow.parquet as pq

# Load the catalog (JSON; metadata index, no spectral arrays)
with open("catalog.json") as f:
    catalog = json.load(f)

print(f"Total spectra: {catalog['statistics']['total_spectra']}")
print(f"Sources: {list(catalog['sources'].keys())}")

# Find all olivine spectra
olivine = [s for s in catalog["spectra"] if "olivine" in s["name"].lower()]
print(f"Found {len(olivine)} olivine spectra")

# Per-source Parquet files — load the one the catalog points at and find the row
table = pq.read_table(olivine[0]["chunk_file"])
mask = [row_id == olivine[0]["id"] for row_id in table.column("id").to_pylist()]
row = table.filter(mask).to_pylist()[0]
print(f"Wavelengths: {row['spectral_data.wavelengths'][:5]}...")
```

For ad-hoc analytics, query the Parquet files directly with DuckDB without loading them into Python. Category filtering is a column predicate — no file-partitioning needed:

```sql
SELECT id, name, "material.formula"
FROM 'spectra/usgs_splib07.parquet'
WHERE "material.category" = 'mineral'
  AND "spectral_data.wavelength_min" < 0.4;
```

See [schemas/library.parquet-schema.md](schemas/library.parquet-schema.md) for the full column reference.

### Building the Library Locally

```bash
# Download source data
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

# Validate
openspeclib validate ./library/
```

## Data Structure

The master library uses a two-tier architecture:

- **Catalog** (`catalog.json`) — Complete metadata index for every spectrum. No spectral arrays. Small enough to load in memory for search and discovery.
- **Library chunks** (`spectra/{source}/{category}.parquet`) — Full spectrum records including wavelength and value arrays, partitioned by source and material category. Stored as Apache Parquet (zstd-compressed) for fast columnar queries via DuckDB / Polars / pandas.

Each spectrum record contains:

- **Source provenance** — library, version, DOI, license, citation
- **Material classification** — name, category, subcategory, chemical formula, searchable keywords
- **Sample information** — ID, description, particle size, origin, preparation
- **Measurement conditions** — instrument, technique, laboratory, geometry
- **Spectral data** — wavelength axis, values, bandpass, unit information
- **Quality indicators** — bad band detection, coverage fraction

See [docs/data-structure.md](docs/data-structure.md) for the full schema specification.

## Documentation

- [Data Structure Specification](docs/data-structure.md) — Formal schema definition with field descriptions
- [Processing Pipeline](docs/processing-pipeline.md) — Pipeline stages, loader details, CLI usage
- [Data Provenance](docs/provenance.md) — Source citations, DOIs, license terms
- [Adding Sources](docs/adding-sources.md) — Guide for integrating new spectral libraries

## GitHub Actions

The library is built and released via a manually triggered GitHub Actions workflow:

1. Go to **Actions** > **Build and Release Spectral Library**
2. Click **Run workflow**
3. Enter a semver version string (e.g., `1.0.0`)
4. Optionally select specific sources to include

The workflow downloads all source data, processes and combines it, validates the output, and creates a GitHub Release with the packaged library.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/

# Regenerate schemas (catalog/spectrum JSON Schemas + chunk Arrow schema dump)
python scripts/generate_schemas.py
```

## License

MIT License. See [LICENSE](LICENSE) for details.

Source spectral libraries retain their original licenses. See [docs/provenance.md](docs/provenance.md) for individual source library terms.
