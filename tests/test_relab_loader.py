"""Tests for the RELAB spectral database loader."""

import math
from pathlib import Path

from openspeclib.loaders.relab import RelabLoader, parse_relab_file
from openspeclib.models import (
    Geometry,
    MaterialCategory,
    Quantity,
    SpectrumRecord,
    WavelengthUnit,
)

FIXTURES = Path(__file__).parent / "fixtures" / "relab"


class TestParseRelabFile:
    def test_mineral_spectrum(self) -> None:
        record = parse_relab_file(FIXTURES / "olivine_fo91.txt")
        assert record.id == "relab:olivine_fo91"
        assert record.name == "Olivine Fo91"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert len(record.spectral_data.wavelengths) == 11
        assert min(record.spectral_data.wavelengths) == 0.3
        assert max(record.spectral_data.wavelengths) == 2.5
        assert record.measurement.quantity == Quantity.REFLECTANCE

    def test_meteorite_spectrum(self) -> None:
        record = parse_relab_file(FIXTURES / "allende_meteorite.txt")
        assert record.name == "Allende CV3 Meteorite"
        assert record.material.category == MaterialCategory.METEORITE
        assert record.sample.origin == "Allende, Chihuahua, Mexico"

    def test_sample_metadata(self) -> None:
        record = parse_relab_file(FIXTURES / "olivine_fo91.txt")
        assert record.sample.id == "DD-MDD-065"
        assert record.sample.particle_size == "45-90 um"
        assert record.sample.origin == "San Carlos, Arizona"

    def test_source_provenance(self) -> None:
        record = parse_relab_file(FIXTURES / "olivine_fo91.txt")
        assert record.collection == "relab"
        assert "RELAB" in RelabLoader().collection_info().citation
        # The fixture names no spectrometer, so the geometry is not asserted.
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.instrument_type is None
        assert record.extra == {}


class TestRelabMeasurementSemantics:
    """Geometry only from catalogue metadata naming the instrument."""

    @staticmethod
    def _parse(tmp_path: Path, extra_header: str, data: str = "") -> SpectrumRecord:
        text = extra_header + (FIXTURES / "olivine_fo91.txt").read_text() + data
        path = tmp_path / "olivine.txt"
        path.write_text(text)
        return parse_relab_file(path)

    def test_bd_vnir_nominal_angles(self, tmp_path: Path) -> None:
        record = self._parse(tmp_path, "# Spectrometer: BD-VNIR\n")
        assert record.measurement.geometry == Geometry.BIDIRECTIONAL
        assert record.measurement.instrument_type == "laboratory_spectrometer"
        assert record.extra == {"nominal_incidence_angle_deg": 30, "nominal_emission_angle_deg": 0}

    def test_bd_vnir_catalogue_angles(self, tmp_path: Path) -> None:
        record = self._parse(tmp_path, "# Spectrometer: BD-VNIR\n# Incidence: 30\n# Emission: 15\n")
        assert record.measurement.geometry == Geometry.BIDIRECTIONAL
        assert record.extra == {"incidence_angle_deg": 30.0, "emission_angle_deg": 15.0}

    def test_ftir_ending_near_2_5_um_is_not_bidirectional(self, tmp_path: Path) -> None:
        # The axis alone (0.3-2.5 um) would look like BD-VNIR data.
        record = self._parse(tmp_path, "# Spectrometer: FTIR\n")
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.instrument_type == "ftir"
        assert record.extra == {}

    def test_spliced(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path, "# Spectrometer: BD-VNIR + FTIR\n", "5.000\t0.050\n25.000\t0.040\n"
        )
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.instrument_type is None
        assert record.measurement.processing == ["spliced"]


class TestRelabLoader:
    def test_load_fixtures(self) -> None:
        loader = RelabLoader()
        assert loader.source_name() == "relab"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        categories = {r.material.category for r in records}
        assert MaterialCategory.MINERAL in categories
        assert MaterialCategory.METEORITE in categories


class TestMissingValues:
    """Sentinels / non-finite values become NaN and are counted in quality."""

    def test_sentinel_and_nan_wavelength(self, tmp_path: Path) -> None:
        text = (FIXTURES / "olivine_fo91.txt").read_text()
        assert "0.500\t0.080" in text and "0.700\t0.100" in text
        text = text.replace("0.500\t0.080", "0.500\t-9999").replace("0.700\t0.100", "nan\t0.100")
        path = tmp_path / "olivine_fo91.txt"
        path.write_text(text)

        record = parse_relab_file(path)
        sd = record.spectral_data
        # The NaN-wavelength row is dropped; wavelengths are never NaN.
        assert len(sd.wavelengths) == 10
        assert not any(math.isnan(w) for w in sd.wavelengths)
        # The sentinel value is stored as NaN, never as a magic number.
        assert sum(math.isnan(v) for v in sd.values) == 1
        assert all(abs(v) < 1e10 for v in sd.values if not math.isnan(v))
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 1

    def test_clean_fixture_has_full_coverage(self) -> None:
        record = parse_relab_file(FIXTURES / "olivine_fo91.txt")
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 0
