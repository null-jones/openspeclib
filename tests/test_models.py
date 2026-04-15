"""Tests for the Pydantic data models."""

import pytest
from pydantic import ValidationError

from openspeclib.models import (
    CatalogRecord,
    MaterialCategory,
    MeasurementTechnique,
    Quality,
    SourceLibrary,
    SpectralData,
    SpectralDataSummary,
    SpectrumRecord,
    WavelengthUnit,
)


class TestSpectrumRecord:
    def test_valid_record(self, sample_spectrum: SpectrumRecord) -> None:
        assert sample_spectrum.id == "usgs_splib07:olivine_gds70"
        assert sample_spectrum.material.category == MaterialCategory.MINERAL
        assert len(sample_spectrum.spectral_data.wavelengths) == 5

    def test_serialization_roundtrip(self, sample_spectrum: SpectrumRecord) -> None:
        json_str = sample_spectrum.model_dump_json()
        restored = SpectrumRecord.model_validate_json(json_str)
        assert restored.id == sample_spectrum.id
        assert restored.spectral_data.wavelengths == sample_spectrum.spectral_data.wavelengths

    def test_additional_properties(self, sample_spectrum: SpectrumRecord) -> None:
        record = sample_spectrum.model_copy(
            update={"additional_properties": {"xrd_results": "quartz trace"}}
        )
        assert record.additional_properties["xrd_results"] == "quartz trace"

    def test_quality_defaults(self) -> None:
        q = Quality()
        assert q.has_bad_bands is False
        assert q.bad_band_count == 0
        assert q.coverage_fraction == 1.0

    def test_invalid_coverage_fraction(self) -> None:
        with pytest.raises(ValidationError):
            Quality(coverage_fraction=1.5)

    def test_invalid_num_points(self) -> None:
        with pytest.raises(ValidationError):
            SpectralData(
                type=MeasurementTechnique.REFLECTANCE,
                wavelength_unit=WavelengthUnit.MICROMETERS,
                wavelength_min=0.35,
                wavelength_max=0.55,
                num_points=0,
                wavelengths=[0.35],
                values=[0.04],
            )


class TestCatalogRecord:
    def test_from_spectrum(self, sample_spectrum: SpectrumRecord) -> None:
        catalog_entry = CatalogRecord.from_spectrum(
            sample_spectrum, chunk_file="spectra/usgs/minerals.json"
        )
        assert catalog_entry.id == sample_spectrum.id
        assert catalog_entry.chunk_file == "spectra/usgs/minerals.json"
        assert isinstance(catalog_entry.spectral_data, SpectralDataSummary)
        assert catalog_entry.spectral_data.num_points == 5

    def test_catalog_record_has_no_spectral_arrays(self, sample_spectrum: SpectrumRecord) -> None:
        catalog_entry = CatalogRecord.from_spectrum(
            sample_spectrum, chunk_file="spectra/usgs/minerals.json"
        )
        data = catalog_entry.model_dump()
        assert "wavelengths" not in data["spectral_data"]
        assert "values" not in data["spectral_data"]
        assert "bandpass" not in data["spectral_data"]


class TestEnums:
    def test_source_library_values(self) -> None:
        assert SourceLibrary.USGS_SPLIB07.value == "usgs_splib07"
        assert SourceLibrary.ECOSTRESS.value == "ecostress"
        assert SourceLibrary.RELAB.value == "relab"
        assert SourceLibrary.ASU_TES.value == "asu_tes"
        assert SourceLibrary.BISHOP.value == "bishop"

    def test_material_categories(self) -> None:
        assert len(MaterialCategory) == 13

    def test_wavelength_units(self) -> None:
        assert WavelengthUnit.MICROMETERS.value == "um"
        assert WavelengthUnit.NANOMETERS.value == "nm"
        assert WavelengthUnit.WAVENUMBERS.value == "cm-1"

    def test_measurement_techniques(self) -> None:
        assert len(MeasurementTechnique) == 4


class TestJsonSchema:
    def test_spectrum_schema_generation(self) -> None:
        schema = SpectrumRecord.model_json_schema()
        assert schema["type"] == "object"
        assert "id" in schema["properties"]

    def test_schema_has_enum_constraints(self) -> None:
        schema = SpectrumRecord.model_json_schema()
        # The material category enum should be in $defs
        defs = schema.get("$defs", {})
        assert "MaterialCategory" in defs
        assert "enum" in defs["MaterialCategory"]
