"""Tests for the EcoSIS loader."""

from pathlib import Path

import pytest

from openspeclib.loaders.ecosis import (
    EcosisLoader,
    _classify_target_type,
    _detect_technique,
    _parse_datapoints,
)
from openspeclib.loaders.ecosis_scales import infer_dataset_divisor
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


class TestInferDatasetDivisor:
    def test_unit_scale_dataset(self):
        # Typical reflectance values, all under 1.0 → divisor 1.
        spectra = [[0.05, 0.1, 0.2, 0.5], [0.04, 0.08, 0.18, 0.45]]
        assert infer_dataset_divisor(spectra) == 1

    def test_percent_scale_dataset(self):
        # Typical 0-100 reflectance values → divisor 100.
        spectra = [[5.0, 10.0, 25.0, 60.0], [3.0, 8.0, 22.0, 55.0]]
        assert infer_dataset_divisor(spectra) == 100

    def test_scaled_int_dataset(self):
        # Typical 0-10000 scaled-integer reflectance → divisor 10000.
        spectra = [[500, 1500, 3000, 5500], [400, 1400, 2800, 5300]]
        assert infer_dataset_divisor(spectra) == 10000

    def test_unit_scale_with_one_outlier_spectrum(self):
        # A single noisy spectrum with one out-of-range value shouldn't
        # flip the classification — the median spectrum max still sits
        # below the unit ceiling.
        spectra = [
            [0.05, 0.1, 0.2, 0.5],
            [0.04, 0.08, 0.18, 0.45],
            [0.04, 0.08, 0.18, 0.45],
            [0.04, 0.08, 0.18, 99.0],  # one bad spectrum
            [0.04, 0.08, 0.18, 0.45],
        ]
        assert infer_dataset_divisor(spectra) == 1

    def test_emissivity_above_one_still_unit(self):
        # Emissivity / absorbance can legitimately exceed 1.0 in some
        # bands but stays well below the 1.5 unit ceiling.
        spectra = [[0.5, 0.9, 1.1, 1.2], [0.4, 0.85, 1.05, 1.15]]
        assert infer_dataset_divisor(spectra) == 1

    def test_empty_dataset_defaults_to_one(self):
        assert infer_dataset_divisor([]) == 1
        assert infer_dataset_divisor([[], []]) == 1

    def test_picks_smallest_divisor_that_normalises(self):
        # A dataset whose median sits at ~50 fits divisor 100 (50/100 =
        # 0.5) but not divisor 1 (50 > 1.5). The function must pick the
        # smallest divisor that brings the median into the unit range.
        spectra = [[10.0, 30.0, 50.0, 70.0]] * 5
        assert infer_dataset_divisor(spectra) == 100


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

    def test_fixture_is_auto_detected_as_unit_scale(self):
        # The bundled fixture has spectrum max values < 1.5, so the
        # median-of-maxes heuristic returns divisor 1 (unit-scale) and
        # the loader applies no normalisation.
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        for r in records:
            assert r.spectral_data.reflectance_scale == "unit"
            assert "source_reflectance_divisor" not in r.additional_properties
