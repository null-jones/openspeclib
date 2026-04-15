"""Tests for the RELAB spectral database loader."""

from pathlib import Path

from openspeclib.loaders.relab import RelabLoader, parse_relab_file
from openspeclib.models import MaterialCategory, MeasurementTechnique, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "relab"


class TestParseRelabFile:
    def test_mineral_spectrum(self) -> None:
        record = parse_relab_file(FIXTURES / "olivine_fo91.txt")
        assert record.id == "relab:olivine_fo91"
        assert record.name == "Olivine Fo91"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert record.spectral_data.num_points == 11
        assert record.spectral_data.wavelength_min == 0.3
        assert record.spectral_data.wavelength_max == 2.5
        assert record.measurement.technique == MeasurementTechnique.REFLECTANCE

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
        assert record.source.library.value == "relab"
        assert "RELAB" in record.source.citation
        assert record.measurement.geometry == "bidirectional (i=30, e=0)"


class TestRelabLoader:
    def test_load_fixtures(self) -> None:
        loader = RelabLoader()
        assert loader.source_name() == "relab"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        categories = {r.material.category for r in records}
        assert MaterialCategory.MINERAL in categories
        assert MaterialCategory.METEORITE in categories
