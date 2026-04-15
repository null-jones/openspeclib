"""Tests for the ASU Thermal Emission Spectral Library loader."""

from pathlib import Path

from openspeclib.loaders.asu_tes import AsuTesLoader, parse_asu_tes_file
from openspeclib.models import MaterialCategory, MeasurementTechnique, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "asu_tes"


class TestParseAsuTesFile:
    def test_quartz_spectrum(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "quartz_bur4120.txt")
        assert record.id == "asu_tes:quartz_bur4120"
        assert record.name == "Quartz BUR-4120"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.material.formula == "SiO2"
        assert record.spectral_data.wavelength_unit == WavelengthUnit.WAVENUMBERS
        assert record.spectral_data.type == MeasurementTechnique.EMISSIVITY
        assert record.spectral_data.num_points == 12
        assert record.spectral_data.wavelength_min == 220.0
        assert record.spectral_data.wavelength_max == 2000.0

    def test_olivine_spectrum(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "olivine_ki3115.txt")
        assert record.name == "Olivine KI-3115"
        assert record.material.formula == "(Mg,Fe)2SiO4"
        assert record.measurement.technique == MeasurementTechnique.EMISSIVITY
        assert record.measurement.instrument_type == "ftir"

    def test_mineral_group_detection(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "quartz_bur4120.txt")
        assert record.material.subcategory == "tectosilicate"

    def test_source_provenance(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "quartz_bur4120.txt")
        assert record.source.library.value == "asu_tes"
        assert "Christensen" in record.source.citation


class TestAsuTesLoader:
    def test_load_fixtures(self) -> None:
        loader = AsuTesLoader()
        assert loader.source_name() == "asu_tes"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        for record in records:
            assert record.material.category == MaterialCategory.MINERAL
            assert record.spectral_data.type == MeasurementTechnique.EMISSIVITY
