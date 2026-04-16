"""Tests for the EcoSIS loader."""

from pathlib import Path

import pytest

from openspeclib.loaders.ecosis import (
    EcosisLoader,
    _classify_target_type,
    _detect_technique,
    _parse_datapoints,
)
from openspeclib.models import MaterialCategory, MeasurementTechnique

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ecosis"


class TestParseDatapoints:
    def test_valid_datapoints(self):
        dp = {"350": "0.05", "400": "0.10", "450": "0.15"}
        wl, vals = _parse_datapoints(dp)
        assert wl == [350.0, 400.0, 450.0]
        assert vals == [0.05, 0.10, 0.15]

    def test_filters_non_numeric_keys(self):
        dp = {"350": "0.05", "Common Name": "oak", "400": "0.10"}
        wl, vals = _parse_datapoints(dp)
        assert len(wl) == 2
        assert wl == [350.0, 400.0]

    def test_sorts_by_wavelength(self):
        dp = {"500": "0.3", "350": "0.1", "400": "0.2"}
        wl, vals = _parse_datapoints(dp)
        assert wl == [350.0, 400.0, 500.0]
        assert vals == [0.1, 0.2, 0.3]

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="No valid spectral data"):
            _parse_datapoints({})

    def test_all_metadata_raises(self):
        dp = {"Common Name": "oak", "Latin Genus": "Quercus"}
        with pytest.raises(ValueError, match="No valid spectral data"):
            _parse_datapoints(dp)

    def test_handles_float_wavelengths(self):
        dp = {"350.5": "0.05", "400.2": "0.10"}
        wl, vals = _parse_datapoints(dp)
        assert wl == [350.5, 400.2]


class TestClassifyTargetType:
    def test_leaf(self):
        assert _classify_target_type(["leaf"]) == MaterialCategory.VEGETATION

    def test_canopy(self):
        assert _classify_target_type(["canopy"]) == MaterialCategory.VEGETATION

    def test_soil(self):
        assert _classify_target_type(["soil"]) == MaterialCategory.SOIL

    def test_water(self):
        assert _classify_target_type(["water"]) == MaterialCategory.WATER

    def test_npv(self):
        assert _classify_target_type(["bark"]) == MaterialCategory.NPV

    def test_unknown_returns_other(self):
        assert _classify_target_type(["something_weird"]) == MaterialCategory.OTHER

    def test_empty_returns_other(self):
        assert _classify_target_type([]) == MaterialCategory.OTHER

    def test_case_insensitive(self):
        assert _classify_target_type(["Leaf"]) == MaterialCategory.VEGETATION

    def test_first_match_wins(self):
        assert _classify_target_type(["leaf", "soil"]) == MaterialCategory.VEGETATION


class TestDetectTechnique:
    def test_reflectance(self):
        assert _detect_technique(["reflectance"]) == MeasurementTechnique.REFLECTANCE

    def test_transmittance(self):
        assert _detect_technique(["transmittance"]) == MeasurementTechnique.TRANSMITTANCE

    def test_absorptance_maps_to_absorbance(self):
        assert _detect_technique(["absorptance"]) == MeasurementTechnique.ABSORBANCE

    def test_default_reflectance(self):
        assert _detect_technique([]) == MeasurementTechnique.REFLECTANCE


class TestEcosisLoader:
    def test_source_name(self):
        loader = EcosisLoader()
        assert loader.source_name() == "ecosis"

    def test_load_fixture(self):
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        assert len(records) == 2

        r = records[0]
        assert r.id.startswith("ecosis:")
        assert r.source.library.value == "ecosis"
        assert r.source.license == "EcoSIS Data Use Policy"
        assert r.source.citation != ""
        assert r.source.url.startswith("https://ecosis.org/package/")
        assert r.spectral_data.wavelength_unit.value == "nm"
        assert r.spectral_data.num_points > 0
        assert len(r.spectral_data.wavelengths) == r.spectral_data.num_points
        assert len(r.spectral_data.values) == r.spectral_data.num_points
        assert r.material.category == MaterialCategory.VEGETATION
        assert "dataset_id" in r.additional_properties
        assert "dataset_title" in r.additional_properties

    def test_fixture_provenance(self):
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        for r in records:
            assert r.source.library.value == "ecosis"
            assert r.additional_properties["dataset_id"] == "03e46f54-7d68-4a8c-896a-fcea87e9cf10"
