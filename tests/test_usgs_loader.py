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
WAVELENGTHS_FILE = FIXTURES / "splib07a_Wavelengths_ASD_0.35-2.5_microns_10_ch.txt"
OLIVINE_FILE = FIXTURES / "ChapterM_Minerals" / "splib07a_Olivine_GDS70_ASDFRa_AREF.txt"
GRANITE_FILE = FIXTURES / "ChapterR_Rocks" / "splib07a_Granite_HS66_ASDFRa_AREF.txt"


class TestHelperFunctions:
    def test_detect_spectrometer(self) -> None:
        # Spectrometer is the segment immediately before the trailing data-type token.
        assert _detect_spectrometer("splib07a_Olivine_GDS70_ASDFRa_AREF.txt") == "ASDFRa"
        assert _detect_spectrometer("splib07a_Granite_HS66_BECKa_AREF.txt") == "BECKa"
        assert _detect_spectrometer("splib07a_Acmite_NMNH133746_Pyroxene_NIC4a_RREF.txt") == "NIC4a"

    def test_parse_name_from_filename(self) -> None:
        name, sample_id = _parse_name_from_filename("splib07a_Olivine_GDS70_ASDFRa_AREF.txt")
        assert name == "Olivine"
        assert "GDS70" in sample_id

        name, sample_id = _parse_name_from_filename(
            "splib07a_Acmite_NMNH133746_Pyroxene_BECKa_AREF.txt"
        )
        assert name == "Acmite"
        assert sample_id == "NMNH133746_Pyroxene"

    def test_classify_from_path(self) -> None:
        assert _classify_from_path(OLIVINE_FILE) == MaterialCategory.MINERAL
        assert _classify_from_path(GRANITE_FILE) == MaterialCategory.ROCK

    def test_read_single_column(self) -> None:
        values = _read_single_column(OLIVINE_FILE)
        assert len(values) == 10
        assert values[0] == 0.042


class TestParseUsgsFile:
    def test_mineral_spectrum(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        record = parse_usgs_file(
            OLIVINE_FILE,
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.id == "usgs_splib07:splib07a_Olivine_GDS70_ASDFRa_AREF"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert record.spectral_data.num_points == 10
        assert record.spectral_data.wavelength_min == 0.35
        assert record.spectral_data.wavelength_max == 0.8
        assert record.measurement.instrument == "ASDFRa"

    def test_rock_spectrum(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        record = parse_usgs_file(
            GRANITE_FILE,
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.material.category == MaterialCategory.ROCK
        assert "Granite" in record.name

    def test_source_provenance(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        record = parse_usgs_file(
            OLIVINE_FILE,
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
