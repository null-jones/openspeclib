"""Tests for the Bishop Spectral Library loader."""

from pathlib import Path

from openspeclib.loaders.bishop import BishopLoader, parse_bishop_file
from openspeclib.models import MaterialCategory, MeasurementTechnique, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "bishop"


class TestParseBishopFile:
    def test_calcite_spectrum(self) -> None:
        record = parse_bishop_file(FIXTURES / "calcite_iceland_spar.txt")
        assert record.id == "bishop:calcite_iceland_spar"
        assert record.name == "Calcite Iceland Spar"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.material.formula == "CaCO3"
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert record.spectral_data.type == MeasurementTechnique.REFLECTANCE
        assert record.spectral_data.num_points == 11
        assert record.spectral_data.wavelength_min == 0.35
        assert record.spectral_data.wavelength_max == 4.0

    def test_gypsum_spectrum(self) -> None:
        record = parse_bishop_file(FIXTURES / "gypsum_selenite.txt")
        assert record.name == "Gypsum Selenite"
        assert record.material.formula == "CaSO4\u00b72H2O"
        assert record.sample.origin == "White Sands, New Mexico"
        assert record.spectral_data.num_points == 12

    def test_carbonate_classification(self) -> None:
        record = parse_bishop_file(FIXTURES / "calcite_iceland_spar.txt")
        assert record.material.category == MaterialCategory.MINERAL

    def test_source_provenance(self) -> None:
        record = parse_bishop_file(FIXTURES / "calcite_iceland_spar.txt")
        assert record.source.library.value == "bishop"
        assert "Bishop" in record.source.citation


class TestBishopLoader:
    def test_load_fixtures(self) -> None:
        loader = BishopLoader()
        assert loader.source_name() == "bishop"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        names = {r.name for r in records}
        assert "Calcite Iceland Spar" in names
        assert "Gypsum Selenite" in names
