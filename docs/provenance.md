# Provenance and citations

Every OpenSpecLib collection file carries its own provenance in the footer: the collection's title, version, publisher, URL, DOI, licence and citation, and for aggregated collections the same for each contributing dataset. The footer is the authoritative, machine-readable record. This page summarises it for each source and describes what OpenSpecLib changed when converting the data.

To read the footer, see [reading.md](reading.md); for licence terms, see [licensing.md](licensing.md).

## What to cite

When you publish results that use OpenSpecLib data, cite:

1. **the source of each spectrum**: the collection's `citation`, and for EcoSIS and OSSL the dataset's `citation` (the row's `dataset_id` keys into the footer `datasets`);
2. **OpenSpecLib**, using [CITATION.cff](../CITATION.cff), if you used the format, the tools or the converted files.

`openspeclib export` writes a README next to exported data that lists the citations and licences of exactly the spectra exported.

## Collections in releases

### USGS Spectral Library Version 7 — `usgs_splib07`

| | |
|---|---|
| Publisher | U.S. Geological Survey, Spectroscopy Laboratory, Denver |
| Version | 7a |
| Data | [doi:10.5066/F7RR1WDJ](https://doi.org/10.5066/F7RR1WDJ) |
| Licence | `LicenseRef-USGS-PublicDomain`: U.S. Government work, public domain |
| Inclusion | `subset`: the measured splib07a spectra only |

> Kokaly, R.F., Clark, R.N., Swayze, G.A., Livo, K.E., Hoefen, T.M., Pearson, N.C., Wise, R.A., Benzel, W.M., Lowers, H.A., Driscoll, R.L., and Klein, A.J., 2017, USGS Spectral Library Version 7: U.S. Geological Survey Data Series 1035, 61 p., https://doi.org/10.3133/ds1035.

Laboratory and field spectra of minerals, rocks, soils, vegetation, man-made materials and other substances, about 0.2 to 216 µm, from several spectrometers (Beckman, ASD, Nicolet FTIR, AVIRIS).

What OpenSpecLib changed: the −1.23e34 deleted-channel marker became NaN; the quantity and geometry were set from the filename and the DS1035 report, with the rule recorded in `extra.geometry_rule`; computed mixtures were flagged `simulated`, and they and measured laboratory mixtures in any chapter were given the category `mixture`; a field spectrum convolved to AVIRIS channels was flagged `resampled`; the "Soils and Mixtures" chapter was split into soils, rocks, single minerals and mixtures from each sample's title and description, with the rule in `extra.category_rule`; the material name and sample id were read from each spectrum's record title and HTMLmetadata page, and a measurement temperature in the title was moved to `extra.sample_temperature_k`. The oversampled splib07b copies and the convolved and resampled products are left out because they derive from the same measurements. The USGS asks users who modify the data to describe their modifications; these notes and [processing-pipeline.md](processing-pipeline.md) do that.

### ECOSTRESS Spectral Library — `ecostress`

| | |
|---|---|
| Publisher | NASA Jet Propulsion Laboratory |
| Version | 1.0 |
| URL | <https://speclib.jpl.nasa.gov> |
| Licence | `LicenseRef-ECOSTRESS-Terms`: no standard licence; the provider's terms at <https://speclib.jpl.nasa.gov> apply. JPL is operated by Caltech for NASA, so its works are not automatically U.S. Government public domain. |
| Inclusion | `complete` |

> Meerdink, S.K., Hook, S.J., Roberts, D.A., Abbott, E.A., 2019. The ECOSTRESS spectral library version 1.0. Remote Sensing of Environment, 230, 111196. https://doi.org/10.1016/j.rse.2019.05.015.

Spectra of natural and man-made materials from about 0.3 to 25 µm, consolidating and extending the ASTER Spectral Library, with contributions from Johns Hopkins University, JPL, UCSB and the USGS. Most spectra, including the thermal infrared ones, are directional-hemispherical or biconical reflectance; some are transmittance.

What OpenSpecLib changed: values were divided by 100 using each file's `Y Units` header, which also set the quantity, except for spectra whose header says percent but whose values are already fractions (see [processing-pipeline.md](processing-pipeline.md#ecostress-ecostress)); geometry came from the `Measurement` header, with the original wording in `extra.source_geometry_term`; the category came from `Type`, or from `Class` where it is more specific (lunar soils, snow and ice), with both kept in `extra`; parts the ECOSTRESS conversion appended to the `Description` header without a separator ("white limestoneSample was Whole rock chips") were separated.

### EcoSIS — `ecosis`

| | |
|---|---|
| Publisher | Ecological Spectral Information System (EcoSIS) |
| URL | <https://ecosis.org> |
| Licence | `NOASSERTION` for the collection; each dataset has its own (see below) |
| Inclusion | `subset`: a curated selection of packages, some sampled |

EcoSIS aggregates datasets uploaded by research groups, each with its own authors, licence and citation. OpenSpecLib publishes each package as a dataset. The footer's `datasets` entry, keyed by the EcoSIS package id, holds the package's title, description, URL, DOI, authors, organisation, licence and citation, and whether it is included in full or as a sample. The collection-level citation only points to these:

> Ecological Spectral Information System (EcoSIS), https://ecosis.org. Cite each contributing dataset; per-dataset citations are listed in the footer `datasets`.

What OpenSpecLib changed, and why each package is included, sampled or excluded: [ecosis-curation.md](ecosis-curation.md).

## Collections with loaders, not yet in releases

| Collection id | Source | Licence | Citation |
|---|---|---|---|
| `relab` | RELAB Spectral Database, Brown University | `LicenseRef-RELAB-ResearchUse`: freely available for research use with citation | Pieters, C.M. and Hiroi, T., RELAB (Reflectance Experiment Laboratory): A NASA Multiuser Spectroscopy Facility. Lunar and Planetary Science Conference, Abstract #2196. |
| `asu_tes` | ASU Thermal Emission Spectral Library, Arizona State University | `LicenseRef-ASU-TES-ResearchUse`: freely available for research use with citation | Christensen, P.R., et al., 2000, A thermal emission spectral library of rock-forming minerals: Journal of Geophysical Research, 105(E4), 9735–9739. https://doi.org/10.1029/1999JE001138 |
| `bishop` | Bishop Spectral Library, SETI Institute | `LicenseRef-Bishop-NonCommercial`: non-commercial use with citation | Bishop, J.L., Lane, M.D., Dyar, M.D., and Brown, A.J., 2008, Clay Minerals, 43, 35–54. https://doi.org/10.1180/claymin.2008.043.1.03 |
| `ossl` | Open Soil Spectral Library, Soil Spectroscopy for Global Good (L0 v1.2) | `CC-BY-4.0` | Sanderman, J., et al. (2024). Open Soil Spectral Library (OSSL). PLOS ONE, 19(1): e0296545. https://doi.org/10.1371/journal.pone.0296545 |

RELAB, ASU TES and Bishop files are placed by hand, so their collections are marked `subset`. OSSL's contributing soil libraries (KSSL, AfSIS, ICRAF-ISRIC, NEON, LUCAS and others) are datasets of the collection. OSSL MIR spectra are decadic absorbance derived from reflectance, flagged `absorbance_from_reflectance` in `measurement_processing`. The full citations are in each loader's footer metadata in `src/openspeclib/loaders/`.

## Releases up to v0.0.7

The v0 releases stored provenance in a JSON catalog and `licenses.json`, with free-text licences. Their data has errors fixed in 0.2.0 (ECOSTRESS in percent, USGS sentinels, mislabelled transmittance, truncated EcoSIS); see the README's known issues and [CHANGELOG.md](../CHANGELOG.md).
