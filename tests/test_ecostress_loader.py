"""Tests for the ECOSTRESS spectral library loader."""

from pathlib import Path

from openspeclib.loaders.ecostress import EcostressLoader, parse_ecostress_file
from openspeclib.models import MaterialCategory, MeasurementTechnique, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "ecostress"


class TestParseEcostressFile:
    def test_mineral_spectrum(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "mineral.calcite.powder.tir.jhu.becknic.spectrum.txt"
        )
        assert record.id.startswith("ecostress:")
        assert record.name == "Calcite WS272"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.material.subcategory == "Carbonate"
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert record.spectral_data.num_points == 10
        assert record.spectral_data.wavelength_min == 2.080
        assert record.spectral_data.wavelength_max == 14.0
        assert len(record.spectral_data.wavelengths) == 10
        assert len(record.spectral_data.values) == 10

    def test_vegetation_spectrum(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "vegetation.deciduous.oak.vis.jhu.asd.spectrum.txt"
        )
        assert record.name == "Red Oak Leaf"
        assert record.material.category == MaterialCategory.VEGETATION
        assert record.material.subcategory == "Deciduous"
        assert record.spectral_data.num_points == 8
        assert record.spectral_data.wavelength_min == 0.35
        assert record.spectral_data.wavelength_max == 2.5
        assert record.measurement.technique == MeasurementTechnique.REFLECTANCE

    def test_source_provenance(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "mineral.calcite.powder.tir.jhu.becknic.spectrum.txt"
        )
        assert record.source.library.value == "ecostress"
        assert record.source.license == "Public Domain"
        assert "Meerdink" in record.source.citation

    def test_sample_metadata(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "mineral.calcite.powder.tir.jhu.becknic.spectrum.txt"
        )
        assert record.sample.id == "WS272"
        assert record.sample.particle_size == "75-250 um"
        assert record.sample.origin == "Ward's Scientific"
        assert record.sample.owner == "JHU"


class TestEcostressLoader:
    def test_load_fixtures(self) -> None:
        loader = EcostressLoader()
        assert loader.source_name() == "ecostress"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        categories = {r.material.category for r in records}
        assert MaterialCategory.MINERAL in categories
        assert MaterialCategory.VEGETATION in categories
