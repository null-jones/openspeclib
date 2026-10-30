"""Shared test fixtures for OpenSpecLib."""

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from openspeclib.models import (
    CollectionInfo,
    CollectionMetadata,
    Geometry,
    Inclusion,
    LicenseRef,
    Material,
    MaterialCategory,
    Measurement,
    Quality,
    Quantity,
    Sample,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
)

USGS_LICENSE = "LicenseRef-USGS-PublicDomain"


def make_collection_info(collection_id: str = "usgs_splib07", **overrides: Any) -> CollectionInfo:
    """A small valid CollectionInfo for tests."""
    fields: dict[str, Any] = {
        "id": collection_id,
        "title": f"Test collection {collection_id}",
        "license": USGS_LICENSE,
        "license_refs": {USGS_LICENSE: LicenseRef(name="U.S. Government work, public domain")},
        "citation": "Test citation.",
        "inclusion": Inclusion.COMPLETE,
    }
    fields.update(overrides)
    return CollectionInfo(**fields)


def make_record(
    local_id: str = "olivine_gds70",
    *,
    collection: str = "usgs_splib07",
    original_id: str | None = None,
    wavelengths: list[float] | None = None,
    values: list[float] | None = None,
    unit: WavelengthUnit = WavelengthUnit.MICROMETERS,
    quantity: Quantity = Quantity.REFLECTANCE,
    category: MaterialCategory = MaterialCategory.MINERAL,
    license: str = USGS_LICENSE,
    dataset_id: str | None = None,
    extra: dict[str, Any] | None = None,
    **spectral: Any,
) -> SpectrumRecord:
    """Build a minimal valid record; the id is ``{collection}:{local_id}``."""
    wavelengths = wavelengths if wavelengths is not None else [0.35, 0.40, 0.45, 0.50, 0.55]
    values = values if values is not None else [0.04, 0.06, 0.08, 0.12, 0.15]
    return SpectrumRecord(
        id=f"{collection}:{local_id}",
        collection=collection,
        original_id=original_id or local_id,
        name=f"Spectrum {local_id}",
        license=license,
        dataset_id=dataset_id,
        material=Material(name="Olivine", category=category),
        measurement=Measurement(quantity=quantity, geometry=Geometry.UNKNOWN),
        spectral_data=SpectralData(
            wavelength_unit=unit, wavelengths=wavelengths, values=values, **spectral
        ),
        extra=extra or {},
    )


@pytest.fixture
def sample_spectrum() -> SpectrumRecord:
    """A minimal but complete spectrum record for testing."""
    return SpectrumRecord(
        id="usgs_splib07:olivine_gds70",
        collection="usgs_splib07",
        original_id="olivine_gds70",
        name="Olivine GDS70",
        license=USGS_LICENSE,
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
            quantity=Quantity.REFLECTANCE,
            geometry=Geometry.UNKNOWN,
            instrument="ASD Fieldspec FR",
            instrument_type="field_spectrometer",
            laboratory="USGS Denver Spectroscopy Lab",
        ),
        spectral_data=SpectralData(
            wavelength_unit=WavelengthUnit.MICROMETERS,
            wavelengths=[0.35, 0.40, 0.45, 0.50, 0.55],
            values=[0.04, 0.06, 0.08, 0.12, 0.15],
        ),
        quality=Quality(notes="test notes"),
    )


@pytest.fixture
def collection_info() -> CollectionInfo:
    """Footer collection metadata matching ``sample_spectrum``."""
    return make_collection_info()


@pytest.fixture
def record_factory() -> Callable[..., SpectrumRecord]:
    """Factory for minimal valid records (see ``make_record``)."""
    return make_record


@pytest.fixture
def collection_metadata() -> CollectionMetadata:
    """CollectionMetadata for the default test collection."""
    return CollectionMetadata(collection=make_collection_info())
