"""Tests for the USGS Spectral Library loader."""

from pathlib import Path

from openspeclib.loaders.usgs import (
    UsgsLoader,
    _classify_from_path,
    _detect_spectrometer,
    _parse_name_from_filename,
    _read_single_column,
    parse_usgs_file,
)
from openspeclib.models import MaterialCategory, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "usgs"


class TestHelperFunctions:
    def test_detect_spectrometer(self) -> None:
        assert _detect_spectrometer("s07AV95a_Olivine_GDS70_ASD.txt") == "ASD"
        assert _detect_spectrometer("s07AV95a_Granite_HS66_ASD.txt") == "ASD"

    def test_parse_name_from_filename(self) -> None:
        name, sample_id = _parse_name_from_filename("s07AV95a_Olivine_GDS70_ASD.txt")
        assert name == "Olivine"
        assert "GDS70" in sample_id

    def test_classify_from_path(self) -> None:
        mineral_path = FIXTURES / "ChapterM_Minerals" / "s07AV95a_Olivine_GDS70_ASD.txt"
        assert _classify_from_path(mineral_path) == MaterialCategory.MINERAL

        rock_path = FIXTURES / "ChapterR_Rocks" / "s07AV95a_Granite_HS66_ASD.txt"
        assert _classify_from_path(rock_path) == MaterialCategory.ROCK

    def test_read_single_column(self) -> None:
        values = _read_single_column(
            FIXTURES / "ChapterM_Minerals" / "s07AV95a_Olivine_GDS70_ASD.txt"
        )
        assert len(values) == 10
        assert values[0] == 0.042


class TestParseUsgsFile:
    def test_mineral_spectrum(self) -> None:
        wavelengths = _read_single_column(FIXTURES / "s07_AV95_Wavelengths_ASD.txt")
        record = parse_usgs_file(
            FIXTURES / "ChapterM_Minerals" / "s07AV95a_Olivine_GDS70_ASD.txt",
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.id == "usgs_splib07:s07AV95a_Olivine_GDS70_ASD"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert record.spectral_data.num_points == 10
        assert record.spectral_data.wavelength_min == 0.35
        assert record.spectral_data.wavelength_max == 0.8

    def test_rock_spectrum(self) -> None:
        wavelengths = _read_single_column(FIXTURES / "s07_AV95_Wavelengths_ASD.txt")
        record = parse_usgs_file(
            FIXTURES / "ChapterR_Rocks" / "s07AV95a_Granite_HS66_ASD.txt",
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.material.category == MaterialCategory.ROCK
        assert "Granite" in record.name

    def test_source_provenance(self) -> None:
        wavelengths = _read_single_column(FIXTURES / "s07_AV95_Wavelengths_ASD.txt")
        record = parse_usgs_file(
            FIXTURES / "ChapterM_Minerals" / "s07AV95a_Olivine_GDS70_ASD.txt",
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.source.library.value == "usgs_splib07"
        assert "Kokaly" in record.source.citation


class TestUsgsLoader:
    def test_load_fixtures(self) -> None:
        loader = UsgsLoader()
        assert loader.source_name() == "usgs_splib07"

        records = list(loader.load(FIXTURES))
        assert len(records) == 2

        categories = {r.material.category for r in records}
        assert MaterialCategory.MINERAL in categories
        assert MaterialCategory.ROCK in categories
