<p align="center">
  <img src="assets/logo.svg" alt="OpenSpecLib" width="480">
</p>

<p align="center">
  An open file format for spectral libraries, a curated collection of public spectra in that format, tools to read, write, check and convert it, and a browser viewer.
</p>

## What OpenSpecLib is

OpenSpecLib has four parts:

1. **The OpenSpecLib Format** ([specification](docs/spec/format-v1.md), version 1.0.0). One Apache Parquet file holds one *collection* of spectra. Each row is a spectrum with its own wavelength axis. The file footer holds the collection's provenance, licences and citations. Any Parquet reader can open it. Anyone can publish in the format; it is not tied to this project's data.
2. **The OpenSpecLib collection.** Public spectral libraries (USGS Spectral Library Version 7, ECOSTRESS, a curated selection of EcoSIS) converted to the format, checked, and published as versioned releases.
3. **The `openspeclib` Python package.** Loaders for each source, a writer that refuses to write a non-conformant file, a validator, a fast reader, and converters to and from ENVI and CSV.
4. **The [viewer](https://null-jones.github.io/openspeclib/).** Search, plot and export spectra in the browser. It runs on DuckDB-WASM, with no server and no account.

## Release contents

### Collections

<!-- counts: updated at release build -->
| Collection id | Source | Materials | Wavelength range | Spectra (0.2.0) |
|---|---|---|---|---|
| `usgs_splib07` | [USGS Spectral Library Version 7](https://doi.org/10.5066/F7RR1WDJ) | Minerals, rocks, organic compounds, man-made materials, vegetation, soils, mixtures, snow and ice, water | about 0.2 – 216 µm | 2,457 |
| `ecostress` | [ECOSTRESS Spectral Library](https://speclib.jpl.nasa.gov) | Minerals, rocks, soils, lunar soils, vegetation, non-photosynthetic vegetation, man-made materials, meteorites, snow and ice, water | about 0.3 – 25 µm | 3,451 |
| `ecosis` | [EcoSIS](https://ecosis.org), curated packages | Leaves, canopies, litter, soil, water, urban materials | 0.35 – 2.5 µm | 23,761 |
| **Total** | | | | **29,669** |

Loaders also exist for RELAB, the ASU Thermal Emission Spectral Library, the Bishop Spectral Library and the Open Soil Spectral Library (OSSL). Their collection ids are reserved in the [collection registry](docs/spec/collections.md), but their data is not in releases yet.

### Release assets

Every [GitHub Release](https://github.com/null-jones/openspeclib/releases) has these files, side by side. `openspeclib combine` writes the same flat layout.

| File | Contents |
|---|---|
| `{collection_id}.parquet` | One Format v1 collection file per collection: spectra, wavelengths, metadata, licences and citations |
| `openspeclib-index.parquet` | Every spectrum's metadata without the arrays, plus the file that holds it. Use it to search all collections at once. |
| `id-migration.csv` | `old_id,new_id` for every spectrum whose id changed since releases up to v0.0.7 |
| `validation-report.json` | The output of `openspeclib validate --profile openspeclib` for this release |
| `checksums.txt` | SHA-256 of every other file, in `sha256sum` format |

## Quick start

### Install

```bash
pip install "openspeclib[remote] @ git+https://github.com/null-jones/openspeclib.git"
```

The `remote` extra adds DuckDB, which the reader needs to read files over HTTP. Without it you can still read local files. For development, clone the repository and run `pip install -e ".[dev]"`.

### Read a release in Python

Download the release assets into one directory and run this from it.

<!-- readme-test: python -->
```python
import openspeclib

# 1. Open the index: one row per spectrum across all collections, no arrays.
index = openspeclib.open_index("openspeclib-index.parquet")
print(f"{len(index)} spectra in {sorted(index.collections)}")

# 2. Search. Criteria combine with AND; the result is a pyarrow table.
hits = index.search(material="olivine", quantity="reflectance", wavelength_range_nm=(400, 2500))
print(f"{hits.num_rows} olivine reflectance spectra cover 400-2500 nm")

# 3. Fetch one spectrum. Only the row group that holds it is read.
spectrum = index.get(hits["id"][0].as_py())
meta = spectrum.metadata
print(spectrum.id, meta["quantity"], meta["geometry"], spectrum.wavelength_unit)
print(f"{len(spectrum.values)} bands, {spectrum.wavelengths_nm().min():.0f} nm and up")
# spectrum.wavelengths and spectrum.values are numpy arrays; NaN marks a missing band.

# 4. Licence and citation, from the footer of the spectrum's collection file.
footer = index.collections[meta["collection"]]
license_refs = footer["collection"].get("license_refs", {})
terms = license_refs.get(meta["license"], {}).get("name", meta["license"])
cite = footer["datasets"][meta["dataset_id"]] if meta["dataset_id"] else footer["collection"]
print(f"License: {meta['license']} ({terms})")
print(f"Cite: {cite.get('citation')}")
```

To work with one collection, open its file directly: `lib = openspeclib.open("usgs_splib07.parquet")`. It has the same `search` and `get` methods, and `lib.metadata` is its footer. Both functions also accept an `https://` URL; the reader then fetches only the footer and the row groups it needs. See [docs/reading.md](docs/reading.md).

### Read with pyarrow only

The files are plain Parquet. You do not need the `openspeclib` package to read them.

<!-- readme-test: python-pyarrow -->
```python
import json

import pyarrow.parquet as pq

footer = json.loads(pq.read_metadata("usgs_splib07.parquet").metadata[b"openspeclib"])
table = pq.read_table(
    "usgs_splib07.parquet",
    columns=["id", "material_name", "wavelength_unit", "wavelengths", "values", "license"],
    filters=[("material_category", "=", "mineral"), ("quantity", "=", "reflectance")],
)
row = table.slice(0, 1).to_pylist()[0]
print(footer["collection"]["title"], "-", table.num_rows, "mineral reflectance spectra")
print(row["id"], row["wavelength_unit"], len(row["values"]), "bands")
```

### Query with DuckDB

Columns are flat snake_case names. Quote `values`, because it is an SQL keyword.

<!-- readme-test: sql -->
```sql
SELECT id, material_name, quantity, geometry, wavelength_unit, wavelengths, "values"
FROM 'usgs_splib07.parquet'
WHERE material_category = 'mineral'
  AND wavelength_min_nm <= 400 AND wavelength_max_nm >= 2500
ORDER BY id
LIMIT 5;
```

The collection metadata is a JSON string in the footer:

<!-- readme-test: sql-footer -->
```sql
SELECT decode(value) AS footer FROM parquet_kv_metadata('usgs_splib07.parquet') WHERE key = 'openspeclib';
```

Over HTTP, query one spectrum per `WHERE id = '…'`. DuckDB skips row groups for a single equality but not for an `IN (…)` list. [docs/reading.md](docs/reading.md) has more examples, including R and JavaScript.

## Command-line tools

| Command | What it does |
|---|---|
| `openspeclib download --source S --target DIR` | Fetch a source library. USGS, ECOSTRESS, EcoSIS and OSSL download automatically; RELAB, ASU TES and Bishop need their files placed by hand. |
| `openspeclib ingest --source S --input DIR --output DIR` | Parse a source into `{collection}.jsonl` plus `{collection}.collection.json` (footer metadata). Exits 1, and keeps no partial output, if the loader reports a fatal skip (a file it rejects that would leave the collection incomplete). |
| `openspeclib combine --input DIR --output DIR` | Write the release directory: one collection file per source, the index, `id-migration.csv`, `checksums.txt`. |
| `openspeclib validate PATH [--profile format\|openspeclib]` | Check a collection file, an index file or a directory. `format` checks conformance; `openspeclib` adds the quality profile. |
| `openspeclib convert INPUT OUTPUT.parquet` | Turn an ENVI spectral library or a CSV table into a conformant collection file. |
| `openspeclib export FILE OUTDIR --to envi\|csv` | Export spectra to ENVI or CSV, with a README of their licences and citations. |

Run any command with `--help` for its options. To build the collection yourself:

```bash
for source in usgs ecostress ecosis; do
  openspeclib download --source "$source" --target "raw/$source"
  openspeclib ingest --source "$source" --input "raw/$source" --output processed/
done
openspeclib combine --input processed/ --output release/
openspeclib validate release/ --profile openspeclib
```

See [docs/processing-pipeline.md](docs/processing-pipeline.md), [docs/validation.md](docs/validation.md) and [docs/converting.md](docs/converting.md).

## Data conventions

The [specification](docs/spec/format-v1.md) is normative. In short:

- **Missing values are NaN.** No sentinel numbers (such as −1.23e34 or −9999). A spectrum's NaN bands are its missing bands.
- **Fractions, not percent.** Reflectance, transmittance and emissivity are stored as fractions with a nominal range of 0 to 1. Loaders divide percent or scaled-integer sources at ingest and record the divisor in `extra.source_reflectance_divisor`, as does `openspeclib convert`. Absorbance is decadic and is not rescaled.
- **Native wavelength units.** Each spectrum keeps its source's axis in `nm`, `um` or `cm-1`; nothing is resampled. `wavelength_min_nm` and `wavelength_max_nm` give every axis's extent in nanometres, so you can filter across collections.
- **Quantity and geometry.** Every spectrum states its `quantity` (reflectance, transmittance, emissivity, absorbance) and its `geometry` (Schaepman-Strub et al. 2006 terms, such as `biconical` or `directional_hemispherical`). Geometry is `unknown` when the source does not document the setup; loaders do not guess.
- **Simulated spectra are flagged.** Modelled rather than measured spectra have `simulated` in `measurement_processing`.
- **Licences are SPDX.** Each row's `license` is an SPDX expression, `NOASSERTION`, or a `LicenseRef-` defined in the footer. For EcoSIS, each dataset has its own licence.

## Known issues in releases up to v0.0.7

Releases v0.0.1 to v0.0.7 used older layouts (a JSON catalog with Parquet files of dotted column names: one file per source and category in v0.0.1, one per source from v0.0.2, and a separate `wavelengths.parquet` registry from v0.0.6). Release 0.2.0 replaces them. The old releases also have these data errors, all fixed in 0.2.0:

- ECOSTRESS values are in percent although labelled 0–1. Divide by 100, except for 36 of the USGS-contributed rock spectra (`*.usgs.perknic` files whose `Measurement:` header is plain `Reflectance`), which already hold fractions; in 0.2.0 these carry `extra.value_scale_rule`.
- USGS missing bands hold the sentinel −1.23e34 instead of NaN.
- 128 ECOSTRESS transmittance spectra, the USGS `TRAN` spectrum and the CABO transmittance spectra are labelled reflectance.
- EcoSIS holds a random, truncated subset of each package, because the downloader stopped at the first temporary API failure.

Spectrum ids also changed; `id-migration.csv` maps old ids to new ones. See [CHANGELOG.md](CHANGELOG.md).

## Documentation

- [Format specification](docs/spec/format-v1.md) and its [schemas](schemas/format-v1/) — normative
- [Reading the files](docs/reading.md) — Python, pyarrow, DuckDB, R, JavaScript
- [Data structure](docs/data-structure.md) — orientation: files, layout, where to look
- [Processing pipeline](docs/processing-pipeline.md) — download, ingest, combine, validate; loader behaviour per source
- [Validation](docs/validation.md) — profiles, rules, allowlist, report
- [Converting](docs/converting.md) — ENVI and CSV import and export
- [EcoSIS curation](docs/ecosis-curation.md) — which EcoSIS packages are included and how
- [Provenance](docs/provenance.md) and [licensing](docs/licensing.md) — sources, citations, licences
- [Adding a source](docs/adding-sources.md) — writing a loader
- [Collection id registry](docs/spec/collections.md) — claimed collection ids
- [RFCs](docs/rfcs/README.md) — how the specification changes
- [Publishing a release](docs/publishing.md) — for maintainers
- [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md), [CITATION.cff](CITATION.cff)

## Citing

Cite OpenSpecLib using [CITATION.cff](CITATION.cff), and cite the source of every spectrum you use. Each collection file's footer holds the citation for the collection and, for EcoSIS, for each dataset.

## License

The code, specification and documentation are MIT licensed ([LICENSE](LICENSE)). The spectra keep their source's licence, stated per row in `license` and in each file's footer. See [docs/licensing.md](docs/licensing.md).
