# Adding a source library

A loader turns one source library into one OpenSpecLib collection. This page is the step-by-step guide; [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-source-loader) has the checklist for the pull request.

If you only want to publish your own library in the format, you may not need a loader: `openspeclib convert` reads ENVI spectral libraries and CSV tables ([converting.md](converting.md)).

## 1. Check the source

Before writing code, find out:

- **Licence.** It must allow redistribution. Note its SPDX id, or the exact wording if it has none.
- **Citation and DOI** the source asks for.
- **What was measured.** The quantity (reflectance, transmittance, emissivity, absorbance) and the value scale (fraction, percent, scaled integer), and where the source states them.
- **How it was measured.** Instrument, illumination, fore-optic or sphere. This decides `geometry`; collect the documents that say so.
- **Missing-value markers** the source uses.
- **Size**, and whether you will include everything (`complete`) or a selection (`subset`).

## 2. Claim a collection id

Pick an id that matches `^[a-z0-9][a-z0-9_]{1,62}$` and names the library, not the topic (`usgs_splib07`, not `minerals`). Add a row to the [collection registry](spec/collections.md) with status *reserved*, in your pull request or in a small one of its own. Ids are first come, first served, and are never reused.

## 3. Write the loader

Create `src/openspeclib/loaders/{name}.py` with a class that implements `BaseLoader` (`src/openspeclib/loaders/base.py`):

```python
from collections.abc import Iterator
from pathlib import Path

from openspeclib.loaders.base import BaseLoader
from openspeclib.models import CollectionInfo, SpectrumRecord


class NewSourceLoader(BaseLoader):
    """Loader for the New Source spectral library."""

    def source_name(self) -> str:
        """Return the collection id."""
        return "new_source"

    def collection_info(self) -> CollectionInfo:
        """Return the footer ``collection`` object."""
        return NEW_SOURCE_COLLECTION

    def download(self, target_dir: Path) -> Path:
        """Download and extract the source into ``target_dir``."""
        ...

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse the source files and yield one record per spectrum."""
        ...
```

- `collection_info()` returns a `CollectionInfo`: id, title, description, version, URL, DOI, publisher, licence (with `license_refs` for any `LicenseRef-`), citation, `inclusion` and `inclusion_notes`. See the existing loaders for examples.
- An aggregated source (one whose datasets have their own licences or citations) also implements `datasets()`, returning a `DatasetInfo` per dataset, and sets `dataset_id` on each record.
- Override these `BaseLoader` properties where they apply:
  - `supports_auto_download`: return `False` for a source that must be downloaded by hand; `download` then prints where to get the files.
  - `fatal_error_count`: the number of files rejected in a way that leaves the collection incomplete (valid after `load` has been consumed). Ingest fails when it is above zero. The default is 0.
- Register the loader in `LOADERS` in `src/openspeclib/cli.py`.

### Rules for records

Use the helpers in `openspeclib.loaders.base`:

| Rule | Helper |
|---|---|
| `id` is `{collection}:{local_id}`, with characters outside `[A-Za-z0-9._~-]` replaced; `original_id` keeps the upstream id unchanged | `spectrum_id`, `make_local_id` |
| Missing bands are NaN: replace the source's markers, non-finite values and anything with magnitude above 1e10 | `mask_invalid_values` (pass the source's own markers) |
| The axis is strictly monotonic; overlapping detectors are sorted and duplicate wavelengths dropped, and `extra` records it | `sort_by_wavelength`, `drop_invalid_wavelengths` |

And:

- **Scale.** Convert reflectance, transmittance and emissivity to fractions. Read the scale from the source's own metadata (as ECOSTRESS states it in `Y Units`) rather than guessing from the data. Record the divisor in `extra.source_reflectance_divisor`. Do not rescale absorbance; record its basis in `measurement_processing` (`absorbance_from_reflectance` or `absorbance_from_transmittance`).
- **Units.** Keep wavelengths in the source's unit.
- **Source-specific metadata** goes in `extra`, not in new columns.
- **Fail loudly.** If a file cannot be interpreted (for example its scale is unknown), log an error and count it in `fatal_error_count`. Ingest then fails instead of publishing an incomplete collection.
- **Generators.** Yield records one at a time.

## 4. Map quantity and geometry, with evidence

`quantity` and `geometry` decide whether two spectra can be compared, so they are set from evidence, never by default.

- Use the geometry vocabulary of the [specification](spec/format-v1.md) (section 4.2, after Schaepman-Strub et al. 2006). A laboratory lamp-and-probe or FTIR-attachment measurement is `biconical`, even when the source calls it "bidirectional"; keep the source's word in `extra.source_geometry_term`. An integrating sphere is `directional_hemispherical`. A sunlit field measurement with a fore-optic is `hemispherical_conical`.
- Set a geometry only when the source documents the setup: a header, a filename code explained in the source's documentation, or a description. Write down the evidence next to the mapping (in a code comment, a policy file, or `extra`), the way `usgs_geometry.py` records `extra.geometry_rule` and quotes the USGS report it rests on.
- When nothing establishes the setup, use `unknown`. Rule R5 warns when more than half of a collection is `unknown`; that is a prompt to look for documentation, not to guess.
- Flag modelled or computed spectra with `simulated` in `measurement_processing`.

## 5. Test

Put a few small source files in `tests/fixtures/{name}/` and write `tests/test_{name}_loader.py`. Test parsed values, units, scale, quantity, geometry, ids, missing values, and that bad files are rejected.

## 6. Validate a full build

```bash
openspeclib download --source new_source --target raw/new_source
openspeclib ingest --source new_source --input raw/new_source --output processed/
openspeclib combine --input processed/ --output release/
openspeclib validate release/ --profile openspeclib
```

The writer refuses to write a file that breaks a conformance rule, so `combine` fails with the rule ids if the loader produces invalid records. The quality profile then checks value scale and bounds. Fix loader bugs rather than allowlisting them. If a spectrum is genuinely unusual, add an entry to `src/openspeclib/validation_allowlist.json` with a reason ([validation.md](validation.md#allowlist)).

## 7. Document

- Add the source to [provenance.md](provenance.md), [licensing.md](licensing.md) and [processing-pipeline.md](processing-pipeline.md).
- If it ships in releases: add it to the release workflow's source list, add a row to the README counts table, and change its registry status to *released*.
- Add a line to [CHANGELOG.md](../CHANGELOG.md).
