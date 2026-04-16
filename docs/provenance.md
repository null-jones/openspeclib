# Data Provenance and Attribution

## Overview

OpenSpecLib is an amalgamation of spectral measurements from multiple authoritative source libraries. This document records the provenance, licensing terms, and recommended citations for each constituent data source.

Users of OpenSpecLib should cite both the OpenSpecLib project and the original source libraries when using spectral data in research or publications.

---

## USGS Spectral Library Version 7 (Speclib 07)

**Full Title:** USGS Spectral Library Version 7

**Publisher:** U.S. Geological Survey (USGS), Spectroscopy Laboratory

**Version:** 7a (splib07a)

**DOI:** [10.3133/ds1035](https://doi.org/10.3133/ds1035)

**Data Access:** [https://doi.org/10.5066/F7RR1WDJ](https://doi.org/10.5066/F7RR1WDJ)

**License:** Public Domain — as a work of the United States Government, this data is not subject to copyright protection within the United States.

**Recommended Citation:**

> Kokaly, R.F., Clark, R.N., Swayze, G.A., Livo, K.E., Hoefen, T.M., Pearson, N.C., Wise, R.A., Benzel, W.M., Lowers, H.A., Driscoll, R.L., and Klein, A.J., 2017, USGS Spectral Library Version 7: U.S. Geological Survey Data Series 1035, 61 p., https://doi.org/10.3133/ds1035.

**Description:**
The USGS Spectral Library Version 7 contains spectral measurements for a comprehensive collection of minerals, rocks, soils, vegetation, man-made materials, and other natural substances. Spectra span the wavelength range of 0.2 to 200 micrometers, measured using laboratory and field spectrometers. The library includes both measured spectra (splib07a) and oversampled versions derived through cubic-spline interpolation (splib07b). Measurements were performed at the USGS Spectroscopy Laboratory in Denver, Colorado, with supporting analytical characterization including X-ray diffraction (XRD) and electron probe micro-analysis (EPMA).

---

## ECOSTRESS Spectral Library

**Full Title:** The ECOSTRESS Spectral Library Version 1.0

**Publisher:** Jet Propulsion Laboratory (JPL), NASA

**Version:** 1.0

**URL:** [https://speclib.jpl.nasa.gov](https://speclib.jpl.nasa.gov)

**License:** Public Domain — as a work of the United States Government (NASA/JPL).

**Recommended Citation:**

> Meerdink, S.K., Hook, S.J., Roberts, D.A., Abbott, E.A., 2019. The ECOSTRESS spectral library version 1.0. Remote Sensing of Environment, 230, 111196. https://doi.org/10.1016/j.rse.2019.05.015.

**Description:**
The ECOSTRESS Spectral Library is a compilation of over 3,400 spectral measurements of natural and man-made materials, spanning the visible through thermal infrared wavelength range (0.35 to 15.4 micrometers). The library consolidates and extends measurements from the earlier ASTER Spectral Library, incorporating contributions from multiple institutions including Johns Hopkins University (JHU), the Jet Propulsion Laboratory (JPL), and the U.S. Geological Survey (USGS). Material categories include minerals, rocks, soils, vegetation, non-photosynthetic vegetation, water, snow/ice, man-made materials, and meteorites. Visible and shortwave infrared measurements report directional hemispherical reflectance, while thermal infrared measurements report emissivity.

---

## RELAB Spectral Database

**Full Title:** Reflectance Experiment Laboratory (RELAB) Spectral Database

**Publisher:** Brown University, Department of Earth, Environmental, and Planetary Sciences

**URL:** [https://sites.brown.edu/relab/](https://sites.brown.edu/relab/)

**License:** Public Domain — academic data freely available for research use with citation.

**Recommended Citation:**

> Pieters, C.M., and Hiroi, T., RELAB (Reflectance Experiment Laboratory): A NASA Multiuser Spectroscopy Facility. 35th Lunar and Planetary Science Conference, abstract #1720, 2004.

**Description:**
The RELAB spectral database contains bidirectional reflectance spectra of minerals, meteorites, lunar samples, and terrestrial rocks, measured at the NASA-supported Reflectance Experiment Laboratory at Brown University. Spectra cover the ultraviolet through mid-infrared range (approximately 0.3 to 26 micrometers). The facility provides controlled-geometry reflectance measurements under standardised incidence (30 degrees) and emission (0 degrees) angles. The collection is particularly valued for its extensive holdings of meteorite and lunar sample spectra, which support planetary surface composition studies and remote sensing calibration.

---

## ASU Thermal Emission Spectral Library

**Full Title:** ASU Thermal Emission Spectral Library

**Publisher:** Arizona State University, School of Earth and Space Exploration

**Version:** 2.0

**URL:** [https://speclib.asu.edu](https://speclib.asu.edu)

**License:** Public Domain — academic data freely available for research use with citation.

**Recommended Citation:**

> Christensen, P.R., Bandfield, J.L., Hamilton, V.E., Howard, D.A., Lane, M.D., Piatek, J.L., Ruff, S.W., and Stefanov, W.L., 2000, A thermal emission spectral library of rock-forming minerals: Journal of Geophysical Research, v. 105, no. E4, p. 9735-9739.

**Description:**
The ASU Thermal Emission Spectral Library provides high-spectral-resolution thermal infrared emission spectra of rock-forming minerals, measured at the Arizona State University Thermal Emission Spectroscopy Laboratory. Spectra cover the 5 to 45 micrometer range (2000 to 220 cm-1) at 2 cm-1 spectral resolution, acquired using a Mattson Cygnus 100 FTIR spectrometer operating in emission mode. The library emphasises the silicate, carbonate, sulfate, oxide, and phosphate mineral groups that constitute the primary rock-forming minerals of planetary surfaces. Data are reported as emissivity derived from measured radiance calibrated against known blackbody standards.

---

## Bishop Spectral Library

**Full Title:** Bishop Spectral Library

**Publisher:** SETI Institute / Janice L. Bishop

**Version:** 1.0

**URL:** [https://dmp.seti.org/jbishop/spectral-library.html](https://dmp.seti.org/jbishop/spectral-library.html)

**License:** Public Domain (non-commercial use with citation)

**Recommended Citation:**

> Bishop, J.L., Lane, M.D., Dyar, M.D., and Brown, A.J., Reflectance and emission spectroscopy study of four groups of phyllosilicates: smectites, kaolinite-serpentines, chlorites and micas. Clay Minerals, 43, 35-54, 2008.

**Description:**
The Bishop Spectral Library provides high-quality reflectance spectra of minerals with emphasis on carbonates, hydrated minerals, phyllosilicates, sulfates, and ices. The collection is curated by Janice Bishop at the SETI Institute and supports studies of aqueous alteration mineralogy on Mars and other planetary surfaces. Spectra are measured in the visible through mid-infrared range (approximately 0.3 to 25 micrometers) using laboratory spectrometers. The library is particularly valued for its detailed characterisation of hydrated and hydroxylated mineral phases relevant to understanding water-rock interactions.

---

## Attribution Requirements

When publishing results derived from OpenSpecLib data, please cite:

1. The OpenSpecLib project
2. The specific source library or libraries from which the data originated (identifiable via the `source.library` field in each spectrum record)

The `source.citation` field in each spectrum record provides the appropriate citation for that specific data point.

---

## Machine-Readable Licensing

Every OpenSpecLib release includes a `licenses.json` file that provides the same licensing and citation information in a machine-readable format, keyed by source library identifier. See [Licensing and Citations](licensing.md) for details on its structure and usage.
