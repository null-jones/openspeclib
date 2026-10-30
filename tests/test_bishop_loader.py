"""Tests for the Bishop Spectral Library loader."""

import math
from pathlib import Path

from openspeclib.loaders.bishop import BishopLoader, parse_bishop_file
from openspeclib.models import Geometry, MaterialCategory, Quantity, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "bishop"


class TestParseBishopFile:
    def test_calcite_spectrum(self) -> None:
        record = parse_bishop_file(FIXTURES / "calcite_iceland_spar.txt")
        assert record.id == "bishop:calcite_iceland_spar"
        assert record.name == "Calcite Iceland Spar"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.material.formula == "CaCO3"
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert record.measurement.quantity == Quantity.REFLECTANCE
        assert len(record.spectral_data.wavelengths) == 11
        assert min(record.spectral_data.wavelengths) == 0.35
        assert max(record.spectral_data.wavelengths) == 4.0

    def test_gypsum_spectrum(self) -> None:
        record = parse_bishop_file(FIXTURES / "gypsum_selenite.txt")
        assert record.name == "Gypsum Selenite"
        assert record.material.formula == "CaSO4\u00b72H2O"
        assert record.sample.origin == "White Sands, New Mexico"
        assert len(record.spectral_data.wavelengths) == 12

    def test_carbonate_classification(self) -> None:
        record = parse_bishop_file(FIXTURES / "calcite_iceland_spar.txt")
        assert record.material.category == MaterialCategory.MINERAL

    def test_source_provenance(self) -> None:
        record = parse_bishop_file(FIXTURES / "calcite_iceland_spar.txt")
        assert record.collection == "bishop"
        assert "Bishop" in BishopLoader().collection_info().citation
        # Mixed set-ups (RELAB bidirectional VNIR + FTIR, often spliced); not stated.
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.instrument_type is None


class TestBishopLoader:
    def test_load_fixtures(self) -> None:
        loader = BishopLoader()
        assert loader.source_name() == "bishop"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        names = {r.name for r in records}
        assert "Calcite Iceland Spar" in names
        assert "Gypsum Selenite" in names


class TestMissingValues:
    """Sentinels / non-finite values become NaN and are counted in quality."""

    def test_sentinel_and_nan_wavelength(self, tmp_path: Path) -> None:
        text = (FIXTURES / "calcite_iceland_spar.txt").read_text()
        assert "0.80\t0.955" in text and "1.50\t0.950" in text
        text = text.replace("0.80\t0.955", "0.80\t-1.23e34").replace("1.50\t0.950", "nan\t0.950")
        path = tmp_path / "calcite_iceland_spar.txt"
        path.write_text(text)

        record = parse_bishop_file(path)
        sd = record.spectral_data
        # The NaN-wavelength row is dropped; wavelengths are never NaN.
        assert len(sd.wavelengths) == 10
        assert not any(math.isnan(w) for w in sd.wavelengths)
        # The sentinel value is stored as NaN, never as a magic number.
        assert sum(math.isnan(v) for v in sd.values) == 1
        assert all(abs(v) < 1e10 for v in sd.values if not math.isnan(v))
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 1

    def test_clean_fixture_has_full_coverage(self) -> None:
        record = parse_bishop_file(FIXTURES / "calcite_iceland_spar.txt")
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 0
