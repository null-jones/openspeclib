"""Tests for the ASU Thermal Emission Spectral Library loader."""

import math
from pathlib import Path

from openspeclib.loaders.asu_tes import AsuTesLoader, parse_asu_tes_file
from openspeclib.models import Geometry, MaterialCategory, Quantity, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "asu_tes"


class TestParseAsuTesFile:
    def test_quartz_spectrum(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "quartz_bur4120.txt")
        assert record.id == "asu_tes:quartz_bur4120"
        assert record.name == "Quartz BUR-4120"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.material.formula == "SiO2"
        assert record.spectral_data.wavelength_unit == WavelengthUnit.WAVENUMBERS
        assert record.measurement.quantity == Quantity.EMISSIVITY
        assert len(record.spectral_data.wavelengths) == 12
        assert min(record.spectral_data.wavelengths) == 220.0
        assert max(record.spectral_data.wavelengths) == 2000.0

    def test_olivine_spectrum(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "olivine_ki3115.txt")
        assert record.name == "Olivine KI-3115"
        assert record.material.formula == "(Mg,Fe)2SiO4"
        assert record.measurement.quantity == Quantity.EMISSIVITY
        assert record.measurement.instrument_type == "ftir"
        assert record.measurement.geometry == Geometry.DIRECTIONAL
        assert record.measurement.reference == "blackbody"

    def test_mineral_group_detection(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "quartz_bur4120.txt")
        assert record.material.subcategory == "tectosilicate"

    def test_source_provenance(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "quartz_bur4120.txt")
        assert record.collection == "asu_tes"
        assert "Christensen" in AsuTesLoader().collection_info().citation


class TestAsuTesLoader:
    def test_load_fixtures(self) -> None:
        loader = AsuTesLoader()
        assert loader.source_name() == "asu_tes"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        for record in records:
            assert record.material.category == MaterialCategory.MINERAL
            assert record.measurement.quantity == Quantity.EMISSIVITY


class TestMissingValues:
    """Sentinels / non-finite values become NaN and are counted in quality."""

    def test_sentinel_and_nan_wavelength(self, tmp_path: Path) -> None:
        text = (FIXTURES / "quartz_bur4120.txt").read_text()
        assert "1600\t0.940" in text and "1100\t0.400" in text
        text = text.replace("1600\t0.940", "1600\t-999").replace("1100\t0.400", "NaN\t0.400")
        path = tmp_path / "quartz_bur4120.txt"
        path.write_text(text)

        record = parse_asu_tes_file(path)
        sd = record.spectral_data
        # The NaN-wavelength row is dropped; wavelengths are never NaN.
        assert len(sd.wavelengths) == 11
        assert not any(math.isnan(w) for w in sd.wavelengths)
        # The sentinel value is stored as NaN, never as a magic number.
        assert sum(math.isnan(v) for v in sd.values) == 1
        assert all(abs(v) < 1e10 for v in sd.values if not math.isnan(v))
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 1

    def test_clean_fixture_has_full_coverage(self) -> None:
        record = parse_asu_tes_file(FIXTURES / "quartz_bur4120.txt")
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 0
