# EcoSIS curation policy

[EcoSIS](https://ecosis.org) hosts hundreds of spectral datasets ("packages") uploaded by individual research groups. OpenSpecLib does not mirror EcoSIS. It publishes a curated selection as one aggregated collection, `ecosis`, in which each package is a dataset with its own licence and citation.

## Where the policy lives

Every decision is in [`src/openspeclib/loaders/ecosis_datasets.json`](../src/openspeclib/loaders/ecosis_datasets.json), one entry per package. That file is the source of truth, including counts and sample sizes; this page explains the rules and does not repeat the numbers. Each entry's `reason` explains the decisions for that package, and each `setup` block names its `evidence`.

| Field | Meaning |
|---|---|
| `id`, `name` | EcoSIS package id and title |
| `inclusion` | `full` (every eligible spectrum), `sample` (a deterministic sample) or `exclude` (never downloaded or ingested) |
| `reason` | Why. Required. |
| `available_spectra` | Spectra in the package upstream, at curation time |
| `sampling` | For `sample`: `stratify_by` (a per-spectrum field), caps `max_total` and/or `max_per_stratum`, `min_per_stratum`, and optionally `one_per` (at most one replicate per value of a field, such as a leaf id) |
| `identity` | How to name materials and set categories where EcoSIS's own fields (Common Name, Latin name, Target Type) do not work |
| `setup` | The measurement setup: geometry, instrument, instrument type, light source, fore-optic, acquisition method, venue. Required `evidence`. See below. |
| `reference` | The reference standard, where the package states it (e.g. `spectralon`) |
| `resource_techniques` | Regex on the uploaded file name → quantity, for packages that upload reflectance and transmittance as separate files |
| `exclude_resources` | Regexes on the uploaded file name; spectra from matching files are not ingested |
| `units_field`, `units_divisors` | A per-spectrum field whose value fixes the scale (e.g. `Percent_Reflectance_0_100` → 100) |
| `scale_divisor` | Fixes the package's scale divisor instead of inferring it |
| `dedupe_key` | What counts as a duplicate upload: identical datapoints from the same file (`datapoints`, default) or datapoints plus all non-id metadata (`content`) |
| `conflicting_duplicates` | What to do when duplicate copies carry different labels: `drop` (default) or `keep_one` |
| `drop_spectra` | Individual spectra to drop, each with a reason |
| `simulated` | The spectra are modelled, not measured |
| `carry_fields` | Per-spectrum fields copied into `extra.ecosis_metadata` |

The downloader fetches every non-excluded package in full. The policy is applied at ingest, so changing it needs no new download.

## Why curate

The curated packages hold well over 100,000 spectra upstream. Most are replicates: repeated measurements of the same leaf, plot or treatment, with no per-spectrum material identity. In OpenSpecLib they would share one generic name, so search could not tell them apart, and they would outweigh whole reference libraries in every category count. OpenSpecLib aims to be a searchable reference collection in which each spectrum says what it measures. If you need the bulk replicate data, download the package from EcoSIS: `https://ecosis.org/package/<package id>`.

## Inclusion rules

1. **Packages whose spectra carry identity are included in full.** Each spectrum records what it measures: species, material or target type. Packages that represent one land-cover or material class with no per-spectrum names (arctic tundra canopy) are also kept in full, with a fixed name from `identity`.
2. **Anonymous replicate or experimental packages are sampled.** The loader keeps a deterministic, stratified sample. Strata and sampling units are ranked by the SHA-256 of their key, so the sample does not depend on API or file order. Spectra with no value in the stratification field are not eligible. `extra.ecosis_sampling` records how each kept spectrum was sampled. Current cases:
   - *Leaf level spectra and LMA … Madison, WI*: stratified by functional type, one replicate per leaf, named by functional type (e.g. "tree leaf").
   - *Multiscale Mapping of Species Diversity …* (airborne canopy pixels): `Tree Species` becomes the material name, with a cap per species.
   - *Leaf and canopy level detection of Fusarium virguliforme … in soybean*: stratified by inoculation status; every spectrum is named soybean.
3. **Packages without material identity are excluded.** *2009 to 2016 Cedar Creek Enemy Removal Experiment Canopy Reflectance (MSR)* is a plot-level time series whose metadata names no material. It is not downloaded.
4. **Rows that are not measurements are excluded.** In *Hyperspectral data for measuring vegetation nutrients and canopy photosynthesis in a salt marsh*, most rows are PLSR regression coefficients uploaded beside the data. `exclude_resources` removes those files; the package's measured canopy spectra are kept.
5. **Quantities the format cannot represent are excluded.** *CABO 2018-2019 Leaf-Level Spectra v2* uploads reflectance, transmittance and absorptance as separate files. `resource_techniques` sets reflectance or transmittance per file. Absorptance (1 − R − T) is derived from the other two and is not a Format v1 quantity (it is not decadic `absorbance`), so it is dropped.
6. **Simulated data is kept only when flagged.** *EMIT Manually Adjusted Water Reflectance Spectra* is modelled (a parametric water optics model), not measured. It is kept as the collection's water spectra, with `simulated` in `measurement_processing` and geometry `unknown`. *Fractional Cover Simulated VSWIR Dataset v2*, despite its title, holds measured field spectra that were only resampled to 10 nm bands; it is not flagged simulated (the format reserves `simulated` for synthetic spectra).
7. **Failed measurements are dropped individually** through `drop_spectra`, each with a reason.
8. **Spectra that are not measurements are dropped by generic checks**, counted in each dataset's `inclusion_notes`: constant no-data fills (every value identical), saturated or clipped spectra in which more than half of the finite bands hold one value (in *2010-2017 Atqasuk Alaska CALM Plots Canopy Reflectance Spectra*, canopy spectra pinned at exactly 1.0 over most bands), and the scale checks below.

## Measurement setup and geometry

EcoSIS records the setup in package metadata (Acquisition Method, Foreoptic Type, Light Source, Measurement Venue, instrument) and sometimes per spectrum. A package's `setup` block turns that into Format v1 values:

- `default`: values for every spectrum of the package;
- `resources`: values per uploaded file (for example a leaf-clip file and a benchtop-probe file in one package);
- `field` and `field_map`: values chosen by a per-spectrum field;
- `instrument_field` and `instrument_map`: the instrument chosen by a per-spectrum field;
- `evidence`: the metadata the block rests on. Required.

Typical readings: a lamp-lit leaf clip or contact probe is `biconical`; an integrating sphere is `directional_hemispherical`; a sunlit field measurement with a fore-optic or bare fibre is `hemispherical_conical`; airborne image pixels and modelled spectra are `unknown`. A package with no `setup` block gets geometry `unknown`. Its other attributes fall back to the package-level metadata only when the package lists exactly one value.

## Duplicates

Some packages upload the same spectrum more than once. Copies are grouped under `dedupe_key` and collapsed to one record before scale inference and sampling. The kept copy is the one whose id ranks lowest by SHA-256; the other ids are listed in `extra.duplicate_ids`.

When the copies carry different material names or strata, no label can be trusted, and by default the whole group is dropped. *NGEE Arctic 2017 Canopy Spectral Reflectance* uses `keep_one` instead: each of its conflicting groups is one canopy measurement linked to several trait samples harvested inside the field of view. One copy is kept under a fixed name (arctic tundra canopy), with the species as keywords, the labels in `extra.linked_material_names`, and the linked sample ids in `extra.linked_sample_ids`; its `sample_id` is left empty, since the measurement belongs to all of the linked samples.

When the copies agree on the material but disagree on another label the record carries (the sample id, sample description, USDA symbol or a `carry_fields` field, e.g. the coordinates of adjacent image pixels), the kept copy's own value would be an arbitrary choice. That field is left out of the record, and every value the group carried is listed in `extra.duplicate_conflicting_fields`.

A large group (10 rows or more) whose rows present themselves as different samples is a fill-down or copy error in the upload: one spectrum was pasted onto many sample rows, and the labels those rows share may be filled down as well. As with conflicting labels, the whole group is dropped. Rows present themselves as different samples when they disagree on a label field (sample id, sample description, USDA symbol, a `carry_fields` field) or an identity field (the fields the naming chain, target type, keywords, category and sampling read). Rows that differ only in other metadata, such as a measurement time, are ordinary duplicates. The one case is a 190-row group in *Leaf and canopy level detection of Fusarium virguliforme … in soybean* (one plant, consecutive sample numbers); the package carries `SampleNumber` for this reason.

## Wavelength units

EcoSIS axes are kept in their own unit. A spectrum's "Wavelength Units" field wins over the package's. A declared unit is used when the axis agrees with it: a nanometre axis ends at 100 or above, a micrometre axis below 100. When nothing is declared, or the declaration contradicts the axis, the unit is inferred from the axis, and `extra.wavelength_unit_inferred` records the declared value and the reason. Two packages are in micrometres: *Urban Materials Spectral Library* and *Fresh and Dry Plant Tissue Spectra*.

## Scale and masking

EcoSIS packages do not share a value scale: some use 0–1, some percent, some 0–10000. Declared units are often missing or wrong, so the loader works from the data unless `units_field` or `scale_divisor` says otherwise. The code is `src/openspeclib/loaders/ecosis_scales.py`; its constants are the source of truth. **This section is the only place in the documentation that describes the masking window; update it here if the window changes.**

1. **Package divisor.** A spectrum's *level* is the 90th percentile of its finite values, so water-band and detector-edge spikes do not count. The package divisor is the smallest of 1, 100 and 10000 that brings the median level across the package to 1.5 or below.
2. **Per-spectrum guard.** A spectrum whose level does not fit the package divisor (a mixed-scale upload) moves to the next larger divisor that puts its level between 0.02 and 1.5. Spectra are never rescaled downward. A spectrum that fits no divisor is dropped, and so is any spectrum whose scaled level is more than 30 times below the package median: a 0–1 spectrum inside a percent package, or a spectrum the uploader had already divided by 100 inside a 0–1 package (the Madison PSR copies). The check runs before sampling, so such a copy is never chosen as a sampled replicate. A package of dark targets (water) is judged against its own median and is unaffected.
3. **Masking.** After scaling, values outside **[−0.05, 1.2]** are set to NaN. This window is the validator's warning band for reflectance: it removes detector-edge and water-band junk but keeps near-zero noise and genuine values slightly above 1, so the kept values are not biased by one-sided clipping.
4. **Coverage.** Spectra left with fewer than 50% valid bands are dropped.

## What `extra` records

| Key | Meaning |
|---|---|
| `source_reflectance_divisor` | The divisor applied, when not 1 |
| `dataset_reflectance_divisor` | The package divisor, when the per-spectrum guard used another |
| `source_missing_band_count` | Bands missing or non-numeric in the source |
| `masked_band_count`, `masked_bands` | Bands masked by the window, as `[wavelength, source value]` pairs (the unscaled value) |
| `duplicate_ids` | Ids of collapsed duplicate copies |
| `duplicate_conflicting_fields` | Label fields on which the collapsed copies disagreed, with every value they carried (the field is then left out of the record) |
| `linked_material_names`, `linked_sample_ids` | For `keep_one` groups |
| `usda_symbol` | The USDA PLANTS symbol of the species, where the upload gives one (it names the species, not the sample, so it is not the `sample_id`). Only a value shaped like a symbol (an upper-case letter, then upper-case letters and digits, 2 to 8 characters) is stored here |
| `source_plant_code` | The upload's `USDA Symbol` value when it is not shaped like a symbol: a common name (HyspIRI's "Grape", "Orange"), a genus ("Salix sp.") or two joined symbols ("SPAL-SPCY") |
| `wavelength_unit_inferred` | How the axis unit was decided, when not by declaration |
| `ecosis_sampling` | How a sampled spectrum was chosen |
| `ecosis_metadata` | The package's `carry_fields` |
| `bands_reordered_by_wavelength`, `source_band_order`, `duplicate_wavelength_bands_dropped`, `duplicate_wavelength_band_count` | Axis repairs (see [processing-pipeline.md](processing-pipeline.md)) |

## Field names, names and sample ids

EcoSIS uploads spell the same field in several ways (`Latin Genus`, `Latin genus`, `latin_genus`; `Sample ID`, `SampleID`, `sample_id`). The loader looks a field up by its exact name first, then by any spelling that differs only in case, whitespace, underscores or hyphens. A record's `sample_id` is the upload's own sample identifier from such a field, or empty when there is none. A spectrum's `name` is its material name followed by the full package title in parentheses.

## Provenance and licences

Each package is a dataset in the collection footer, keyed by its EcoSIS id (the row's `dataset_id`), with its title, description, URL, DOI, authors, organisation, licence, citation, `inclusion` (`complete` or `subset`) and spectrum counts. EcoSIS licence labels map to SPDX identifiers (`ODbL-1.0`, `ODC-By-1.0`) or to `LicenseRef-EcoSIS-*` definitions when the label has no version (for example "Creative Commons Attribution"). An unknown label becomes `NOASSERTION`, with the original text in the dataset's `license_note`. The collection's own `license` is `NOASSERTION`, with a note that the per-row `license` is authoritative. See [licensing.md](licensing.md).

## Download

- Rate limits (HTTP 429) and server errors (5xx) are retried with exponential backoff, honouring `Retry-After`.
- An empty page before the end of a package is treated as a temporary failure and retried.
- Each package is streamed to a `.part` file and renamed only when complete, so a failure never leaves a partial bundle. Complete bundles are skipped on a re-run.
- The download fails if any package cannot be fetched. Ingest fails if a curated, non-excluded package has no bundle or an unreadable one.

Releases v0.0.6 and v0.0.7 were built with an older downloader that treated the first temporary failure as the end of a package. Their EcoSIS data is a random, truncated subset.

## Changing the policy

1. Edit the package's entry in `ecosis_datasets.json`, including its `reason` (and `evidence` for a `setup` change).
2. Re-run `openspeclib ingest --source ecosis`, then `combine` and `validate --profile openspeclib`. A new download is needed only for a package that was excluded before.
3. Before a release, update the README counts table (marked `<!-- counts: updated at release build -->`).
