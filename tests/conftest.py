"""Shared test fixtures for OpenSpecLib."""

from datetime import date

import pytest

from openspeclib.models import (
    Material,
    MaterialCategory,
    Measurement,
    MeasurementTechnique,
    Quality,
    Sample,
    Source,
    SourceLibrary,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
)


@pytest.fixture
def sample_spectrum() -> SpectrumRecord:
    """A minimal but complete spectrum record for testing."""
    wavelengths = [0.35, 0.40, 0.45, 0.50, 0.55]
    values = [0.04, 0.06, 0.08, 0.12, 0.15]

    return SpectrumRecord(
        id="usgs_splib07:olivine_gds70",
        name="Olivine GDS70",
        source=Source(
            library=SourceLibrary.USGS_SPLIB07,
            library_version="7a",
            original_id="olivine_gds70",
            filename="s07_Olivine_GDS70.txt",
            url="https://doi.org/10.5066/F7RR1WDJ",
            license="Public Domain",
            citation="Kokaly, R.F., et al., 2017",
        ),
        material=Material(
            name="Olivine",
            category=MaterialCategory.MINERAL,
            subcategory="nesosilicate",
            formula="(Mg,Fe)2SiO4",
            keywords=["olivine", "forsterite", "silicate"],
        ),
        sample=Sample(
            id="GDS70",
            description="Olivine, forsterite-rich",
            origin="Twin Sisters, Washington, USA",
            owner="USGS",
            collection_date=date(2010, 6, 15),
        ),
        measurement=Measurement(
            instrument="ASD Fieldspec FR",
            instrument_type="field_spectrometer",
            laboratory="USGS Denver Spectroscopy Lab",
            technique=MeasurementTechnique.REFLECTANCE,
        ),
        spectral_data=SpectralData(
            type=MeasurementTechnique.REFLECTANCE,
            wavelength_unit=WavelengthUnit.MICROMETERS,
            wavelength_min=0.35,
            wavelength_max=0.55,
            num_points=5,
            wavelengths=wavelengths,
            values=values,
        ),
        quality=Quality(
            has_bad_bands=False,
            bad_band_count=0,
            coverage_fraction=1.0,
        ),
    )
