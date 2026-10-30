# Processing pipeline

This page describes how the OpenSpecLib collection is built from the source libraries: **download → ingest → combine → validate → release**. Each step is an `openspeclib` command. The release workflow (`.github/workflows/build-release.yml`) runs them in order; [publishing.md](publishing.md) covers the publishing steps after the build.

```bash
for source in usgs ecostress ecosis; do
  openspeclib download --source "$source" --target "raw/$source"
  openspeclib ingest --source "$source" --input "raw/$source" --output processed/
done
openspeclib combine --input processed/ --output release/
openspeclib validate release/ --profile openspeclib --report release/validation-report.json
```

## 1. Download

```bash
openspeclib download --source <source> --target raw/<source>
```

| `--source` | Collection id | Fetched from | Automatic |
|---|---|---|---|
| `usgs` | `usgs_splib07` | Hugging Face mirror of the unmodified USGS ScienceBase archive | yes |
| `ecostress` | `ecostress` | Hugging Face mirror of the JPL archive | yes |
| `ecosis` | `ecosis` | EcoSIS API, one JSON bundle per curated package | yes |
| `ossl` | `ossl` | Three compressed CSV tables on Google Cloud Storage | yes |
| `relab` | `relab` | PDS Geosciences Node | no: place the files in the target directory |
| `asu_tes` | `asu_tes` | ASU spectral library | no: place the files in the target directory |
| `bishop` | `bishop` | SETI Institute | no: place the files in the target directory |

Downloads skip files that are already complete, so a re-run fetches only what is missing. The EcoSIS downloader retries rate limits and server errors, and never leaves a partial bundle; see [ecosis-curation.md](ecosis-curation.md#download).

## 2. Ingest

```bash
openspeclib ingest --source <source> --input raw/<source> --output processed/
```

Each source has a loader in `src/openspeclib/loaders/` that implements `BaseLoader` and yields `SpectrumRecord` objects. Ingest writes two files:

- `{collection}.jsonl`: one record per line;
- `{collection}.collection.json`: the footer metadata (the `collection` object and, for aggregated collections, the `datasets`).

Both are written as `.part` files and renamed only when the run succeeds. A loader may skip a file it cannot interpret, but it counts skips that would leave the collection incomplete in `fatal_error_count`. If that count is above zero, ingest deletes its output and exits 1. So does an EcoSIS ingest that is missing a curated, non-excluded package. Deleting the output includes the `{collection}.jsonl` and `{collection}.collection.json` of any previous run, so `combine` cannot pick up a stale collection; an ingest that finds no spectra at all does the same, but warns and exits 0. An ingest interrupted by an exception leaves a previous run's pair untouched.

### Rules every loader follows

- **Ids.** `id` is `{collection}:{local_id}`. The local id is the upstream id with characters outside `[A-Za-z0-9._~-]` replaced by `_` (`loaders.base.make_local_id`). `original_id` keeps the upstream id unchanged.
- **Missing values** become NaN (`loaders.base.mask_invalid_values`): source sentinels, non-numeric values, and any finite value with magnitude above 1e10.
- **Scale.** Reflectance, transmittance and emissivity are divided to fractions using the scale the source states. The divisor is recorded in `extra.source_reflectance_divisor`.
- **Units.** Wavelengths stay in the source's unit.
- **Axes.** Format v1 needs a strictly monotonic axis (rule F8). A source axis with overlapping detectors is sorted, with values moved along (`loaders.base.sort_by_wavelength`); `extra` records `bands_reordered_by_wavelength` and `source_band_order`. When two bands have the same wavelength one is kept (the first with a finite value, so a NaN band never displaces a measured one) and `extra` records `duplicate_wavelength_bands_dropped` and `duplicate_wavelength_band_count`.
- **Geometry from evidence.** `geometry` is set only from something the source states: a header, a filename code backed by the source's documentation, or package metadata. The evidence is recorded with the rule (see below). Otherwise it is `unknown`.
- **Generators.** Loaders yield records one at a time, so large sources need not fit in memory.

### USGS Spectral Library Version 7 (`usgs_splib07`)

- **Included:** the measured spectra of splib07a. The oversampled splib07b copies, the spectra convolved or resampled to other instruments, and the error-bar files are derived from the same measurements and are left out (`inclusion: subset`).
- **Values:** single-column ASCII files; the wavelength and bandpass axes are in shared files per spectrometer. Values are already fractions. The deleted-channel marker −1.23e34 becomes NaN, and `extra.source_missing_band_count` records how many.
- **Quantity:** from the filename's data-type token. `AREF`, `RREF` and `RTGC` are reflectance; `TRAN` is transmittance.
- **Geometry:** `src/openspeclib/loaders/usgs_geometry.py` applies named rules based on the spectrometer code and, for the Beckman and ASD instruments, the spectrum's own description in the archive's `HTMLmetadata/` folder. The evidence is Kokaly et al. (2017), USGS Data Series 1035, which ships in the archive:
  - Nicolet (`NIC4`): biconical (DS1035 p. 7).
  - AVIRIS: hemispherical-conical (p. 7).
  - ASD: biconical in the laboratory (lamp and fibre probe, p. 4); hemispherical-conical in the field under natural light (p. 7). Laboratory or field is read from the description, the chapter, or a powder or sieved size fraction in the title.
  - Beckman: biconical for powders in a sample cup; bihemispherical for rock surfaces placed over the integrating-sphere port (p. 4).
  - A spectrum measured with a field spectrometer and convolved to AVIRIS channels (its description says so) follows the field-spectrometer rules, not the AVIRIS one, and has `measurement_instrument_type` `field_spectrometer`.
  - `unknown` when none of these establishes the setup, for `TRAN` spectra, and for computed spectra.
  - A few named exceptions override these rules, each with the evidence quoted in `usgs_geometry.py`. One gives `hemispherical_conical` (a Beckman-coded spectrum that its description says is an average of field spectra); the others give `unknown`.
  - The rule that fired is in `extra.geometry_rule` (for example `nicolet_bcrf`, `beckman_surface_over_port`, `asd_setup_not_stated`), so each value can be traced to its evidence.
- **Processing flags:** `simulated` for spectra the description says were calculated or computed (computed mixtures); `interpolated` where the description says the original data were interpolated; `resampled` where it says the spectrum was convolved to another instrument's channels (`extra.resampled_to_channels_of` names the channel set); `scaled_to_reference_level` for Nicolet reflectance spectra.
- **Names:** from the SPECPR record title on the first line of each data file (`Topaz Little_3_Mine_#41  BECKb AREF`). DS1035 describes it as "a short sample name and a unique sample identifier number/code", separated by a space; the filename writes that space as `_`, so it cannot show where the name ends. The first title word is `material_name` and the rest is `sample_id`: "Topaz" with sample id `Little_3_Mine_#41`, not "Topaz Little". The spectrum's HTMLmetadata page refines this. Its `TITLE` joins a name the record title splits ("Samarium Oxide"). Its `SAMPLE_ID` moves an id written onto the name into `sample_id` (`Vermillion_GDS779`) and supplies the id when the title has none (`1-2-3-Trimethylbenzene 85K` gets `SA-T73202`). Lower-case words between the name and that id belong to the name ("Iron oxide #4820"); when the id is not in the title, title words that `SAMPLE_ID` gives as material rather than id belong to the name (`BHVO-2F+7% Decane` with `SAMPLE_ID` `BHVO-2F, Decane SA-30550` is "BHVO-2F + 7% Decane" with sample id `SA-30550`). A supplier catalogue number written onto the name is the sample id (`Iron_Powder_SA-267953 <10 um` is "Iron Powder", `SA-267953_<10_um`). A measurement temperature goes to `extra.sample_temperature_k` rather than `sample_id` when it is a title word of its own (`85K`, `T~77K`) or is written onto a frost or grain-size descriptor (`MedFrost85K`, `fine fr86K`, `<75um85K`, whose descriptor stays in `sample_id`). A heating temperature (`Htd16hr 480K`) stays in `sample_id`, and so does a temperature glued to any other word. Names the USGS abbreviated to fit the 40-character title (DS1035 appendix 2) are kept as written.
- **Category:** from the chapter directory. The Liquids chapter is split into `water` and `snow_ice`. Chapter S ("Soils and Mixtures", whose HTMLmetadata document almost every sample as a mixture) is classified per spectrum from the title and description, with the rule in `extra.category_rule`. Laboratory mixtures (`AMX` series, samples made from pure phases) and coatings or precipitates on a substrate are `mixture`. A rock named in the title, or a description that says the sample is or was taken from a rock, is `rock`. Sands and playa surfaces are `soil`. A title naming one mineral whose description says the sample is that mineral ("Weathered Pyrite from …") or phases of that one mineral group ("contains two alunite phases") is `mineral` (`chapter_s_single_mineral`). Anything else stays `mixture`. In the other chapters, computed mixtures (`computed_mixture`) and measured laboratory mixtures (`laboratory_mixture`: an `AMX` title token, or a description of a sample prepared by mixing, e.g. antigorite with dry grass in chapter V, basalt BHVO-2F with an organic compound in chapter O, montmorillonite with a solvent in chapter A) get `mixture`, and a title that names one rock and nothing else (the unmixed basalt BHVO-2F reference in chapter O) gets `rock` (`rock_in_title`). The rule is kept in `extra.category_rule` and the chapter's category in `extra.original_category`. The 0% member of a laboratory mixture series (every component after a `+` is 0%, e.g. `Vermiculite+0wt%amph._GDS327`, the pure vermiculite of the vermiculite + amphibole series) is not a mixture: it keeps its chapter's category, with `extra.category_rule` `zero_fraction_reference`.

### ECOSTRESS (`ecostress`)

- **Values and quantity:** every file states its quantity and scale in the `Y Units` header, for example `Reflectance (percent)`. The loader accepts one quantity followed by `(percent)`, `(percentage)` or `(%)`, tolerating the archive's misspellings, and divides by 100. The same header sets `quantity`. A file whose `Y Units` is missing or unrecognised is rejected, and ingest fails.
- **Percent headers on fraction data:** some USGS Perkin-Elmer/Nicolet rock spectra say `percent` but hold fractions. A "percent" spectrum whose axis reaches below 2 µm but whose 90th-percentile level is 1.5 or less is taken to be fractions and not divided: no genuine percent VSWIR spectrum in the archive is that dark, while thermal-only spectra (dark meteorites near 1 %) are exempt. `extra` records `source_reflectance_divisor` 1, `declared_value_scale`, `value_scale_rule` and `source_value_level_p90`, and `quality_notes` says why.
- **Category:** from `Type`, unless `Class` itself names a more specific category (`Soil` / `Lunar` is `lunar`; `Water` / `Snow`, `Ice` or `Frost` is `snow_ice`), in which case `extra.original_category` keeps the `Type` category. `Type` and `Class` are kept verbatim in `extra.source_type` and `extra.source_class`; `material_subcategory` is the `Subclass` (or the `Class` when there is no `Subclass` key).
- **Geometry:** from the `Measurement` header, for reflectance only:
  - "Hemispherical reflectance" and "Directional hemispherical reflectance": `directional_hemispherical` (integrating-sphere laboratory spectrometers).
  - "Bidirectional reflectance": `biconical`. These are laboratory lamp-and-probe or FTIR-attachment measurements, which older libraries call bidirectional (spec section 5). The header's own wording is kept in `extra.source_geometry_term`.
  - Plain "Reflectance", transmittance spectra, and spectra spliced from segments with different geometries: `unknown`.
- **Sample description:** the `Description` header. The ECOSTRESS conversion appended parts from other ASTER fields to it ("Sample was …", "Particle size was …", "Collected by …", "Original ASTER Spectral Library name was …"), often with no separator ("white limestoneSample was Whole rock chips"). Each appended part is separated by ". ", or by a space after text already ending in a full stop. The rest of the text is kept as written.
- **Instrument and laboratory:** from the filename tokens (`<…>.<lab>.<instrument>.spectrum.txt`). Spectra joined from a dispersive VSWIR and a Nicolet TIR measurement have the `spliced` processing flag.

### EcoSIS (`ecosis`)

An aggregated collection: each EcoSIS package is a dataset with its own licence and citation. Which packages are included, and how, is set per package in `src/openspeclib/loaders/ecosis_datasets.json` and explained in [ecosis-curation.md](ecosis-curation.md). That page also covers the scale inference, the masking window, duplicates, and the `setup` blocks that give each package's geometry and instrument with their evidence.

### RELAB (`relab`, not in releases)

- Files placed by hand. `inclusion` is `subset`, because completeness depends on what was downloaded.
- Spectra from RELAB's bidirectional spectrometer (BD-VNIR) are `bidirectional`, with the incidence and emission angles in `extra` (`incidence_angle_deg`, `emission_angle_deg`; RELAB's standard angles as `nominal_*` keys when the header lacks them).
- FTIR spectra, and spectra spliced from BD-VNIR and FTIR segments (flagged `spliced`), are `unknown`.

### ASU Thermal Emission Spectral Library (`asu_tes`, not in releases)

- Emissivity on a wavenumber axis (`cm-1`). Geometry is `directional` (emission viewed from above, ratioed to a blackbody, with no illumination geometry), and `measurement_reference` is `blackbody`.

### Bishop Spectral Library (`bishop`, not in releases)

- Reflectance. Geometry is `unknown`: the spectra come from more than one setup, and the files name neither instrument nor geometry.
- The wavelength unit is inferred from the axis range (values above 100 mean nm).

### OSSL (`ossl`, not in releases)

- One record per sample and technique: VisNIR reflectance (`nm`, local id `{uuid}_vnir`) and MIR absorbance (`cm-1`, `{uuid}_mir`). MIR absorbance is decadic, derived from reflectance, and not rescaled; its records carry `absorbance_from_reflectance` in `measurement_processing`.
- Each contributing soil library (KSSL, AfSIS, ICRAF-ISRIC, NEON, LUCAS, …) is a dataset. Licence CC-BY-4.0.
- Geometry is `unknown`: the contributing laboratories used different setups, and the tables carry no per-spectrum geometry.

## 3. Combine

```bash
openspeclib combine --input processed/ --output release/ [--release VERSION]
```

Combine reads every `{collection}.jsonl` with its `{collection}.collection.json` and writes the release directory:

1. **One collection file per collection**, through `openspeclib.storage`. Rows are sorted by `id`, in row groups of 100, with BYTE_STREAM_SPLIT and zstd on the array columns (recommendations R1–R3). Only extension columns that hold values are written, and only their extensions are declared. Datasets with no rows are left out of the footer.
2. **The writer checks its own output.** Each file is written to `{name}.part` and checked with the conformance checker (`openspeclib.conformance`). If any must-rule fails, the file is deleted and combine stops with the rule ids and examples. A non-conformant file is never written.
3. **Id collisions.** If sanitising two upstream ids gives the same local id, the later one (in a fixed order) gets a `~2`, `~3`, … suffix. The result depends only on the records, so a rebuild gives the same ids.
4. **The index**, `openspeclib-index.parquet`, with each file's SHA-256 and the release version in its footer.
5. **`id-migration.csv`**: `old_id,new_id` for every spectrum whose id differs from its v0 id.
6. **`checksums.txt`** for every file above.

## 4. Validate

```bash
openspeclib validate release/ --profile openspeclib --report release/validation-report.json
```

The `format` profile (the default) checks conformance to the specification. The `openspeclib` profile adds the quality rules Q1–Q4, with the allowlist. The release build uses `openspeclib`, and any error stops it. See [validation.md](validation.md).

## 5. Release

The release workflow then:

1. adds `validation-report.json` to the directory and to `checksums.txt`, and verifies every checksum;
2. writes release notes from the index footer;
3. creates the git tag `v<version>` and a GitHub Release with the collection files, the index, `id-migration.csv`, `validation-report.json` and `checksums.txt`.

Zenodo and the Hugging Face mirror are manual steps; see [publishing.md](publishing.md).
