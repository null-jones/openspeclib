# Adding New Source Libraries

This guide describes the procedure for integrating a new spectral library source into OpenSpecLib.

## Prerequisites

Before beginning integration, gather the following information about the prospective source library:

1. **Data access method** — URL for bulk download, API endpoint, or manual acquisition procedure
2. **File format** — native format of spectral data files (ASCII text, CSV, binary, etc.)
3. **Metadata schema** — available metadata fields and their semantics
4. **License terms** — confirm that the source license permits redistribution and amalgamation
5. **Citation** — the recommended academic citation for the source library
6. **Scale** — approximate number of spectra and total data volume

## Implementation Steps

### 1. Register the Source

Add a new entry to the `SourceLibrary` enum in `src/openspeclib/models.py`:

```python
class SourceLibrary(str, Enum):
    USGS_SPLIB07 = "usgs_splib07"
    ECOSTRESS = "ecostress"
    OSSL = "ossl"
    NEW_SOURCE = "new_source"  # Add your source
```

### 2. Create the Loader Module

Create a new file at `src/openspeclib/loaders/{source_name}.py` implementing the `BaseLoader` interface:

```python
from openspeclib.loaders.base import BaseLoader
from openspeclib.models import SpectrumRecord

class NewSourceLoader(BaseLoader):
    def source_name(self) -> str:
        return "new_source"

    def download(self, target_dir: Path) -> Path:
        # Download and extract source data
        ...

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        # Parse native files and yield SpectrumRecord objects
        ...
```

### 3. Map Source Metadata to Standard Fields

The critical task in integration is mapping the source library's native metadata schema to the OpenSpecLib standard fields. Pay particular attention to:

- **Material classification:** Map source-specific categories to the `MaterialCategory` controlled vocabulary. If a source category does not map cleanly, use `other` and populate `material.subcategory` with the original value.
- **Measurement technique:** Determine whether spectral values represent reflectance, emissivity, absorbance, or transmittance.
- **Wavelength units:** Identify the native wavelength unit and set `spectral_data.wavelength_unit` accordingly. Do not convert units — store in the native domain.
- **Source-specific metadata:** Place any fields that do not map to standard schema fields into `additional_properties`.

### 4. Register the Loader in the CLI

Add the new loader to the `LOADERS` dictionary in `src/openspeclib/cli.py`:

```python
LOADERS = {
    "usgs": "openspeclib.loaders.usgs:UsgsLoader",
    "ecostress": "openspeclib.loaders.ecostress:EcostressLoader",
    "ossl": "openspeclib.loaders.ossl:OsslLoader",
    "new_source": "openspeclib.loaders.new_source:NewSourceLoader",
}
```

### 5. Create Test Fixtures

Place representative sample files in `tests/fixtures/{source_name}/`. Include at least:

- Two or more spectra covering different material categories present in the source
- Files that exercise edge cases (missing metadata, unusual formatting)

### 6. Write Tests

Create `tests/test_{source_name}_loader.py` covering:

- Individual file parsing with metadata verification
- End-to-end loader output (correct record count, category distribution)
- Source provenance fields (library name, citation, license)
- Quality metric computation

### 7. Update Documentation

- Add source information to `docs/provenance.md` (DOI, citation, license, description)
- Update `README.md` to list the new source
- Update the GitHub Actions workflow if the new source requires special download handling

### 8. Regenerate Schemas

After modifying the `SourceLibrary` enum:

```bash
python scripts/generate_schemas.py
```

## Design Guidelines

- **Use generators** for the `load()` method to support large datasets without excessive memory consumption.
- **Preserve source fidelity** — do not discard or transform source metadata that may be valuable for downstream analysis. Use `additional_properties` for fields that do not fit the standard schema.
- **Handle errors gracefully** — log warnings for individual files that fail to parse and continue processing the remaining files.
- **Document provenance** — include the full citation, DOI, and license in the `Source` object for every emitted record.
