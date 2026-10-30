"""Tests for the shared missing-value helpers in ``openspeclib.loaders.base``."""

import math

import pytest

from openspeclib.loaders.base import (
    FILL_VALUE_MAGNITUDE,
    GENERIC_SENTINELS,
    drop_invalid_wavelengths,
    make_local_id,
    mask_invalid_values,
    sort_by_wavelength,
    spectrum_id,
)
from openspeclib.models import is_valid_local_id

USGS_SENTINEL = -1.23e34


class TestMaskInvalidValues:
    @pytest.mark.parametrize(
        "value",
        [
            float("inf"),
            float("-inf"),
            float("nan"),
            # Fill-magnitude boundary: anything strictly beyond 1e10 is a fill.
            math.nextafter(FILL_VALUE_MAGNITUDE, math.inf),
            -math.nextafter(FILL_VALUE_MAGNITUDE, math.inf),
            1e11,
            -1e30,
            1e36,
            USGS_SENTINEL,  # masked by magnitude even without listing it
        ],
    )
    def test_always_masked(self, value: float) -> None:
        assert math.isnan(mask_invalid_values([value])[0])

    @pytest.mark.parametrize(
        "value",
        [
            FILL_VALUE_MAGNITUDE,  # boundary itself is not "> 1e10"
            -FILL_VALUE_MAGNITUDE,
            1e9,
            0.0,
            -0.0,
            0.5,
            1.2,
            -0.03,
        ],
    )
    def test_kept_without_sentinels(self, value: float) -> None:
        assert mask_invalid_values([value]) == [value]

    @pytest.mark.parametrize(
        "value",
        [
            -9999.0,
            -9999.001,  # same marker written with different precision
            -9998.9999,
            -999.0,
            -999.0000001,
        ],
    )
    def test_generic_sentinels_masked_with_rel_tol(self, value: float) -> None:
        assert math.isnan(mask_invalid_values([value], sentinels=GENERIC_SENTINELS)[0])

    @pytest.mark.parametrize(
        "value",
        [
            -999.5,  # outside rel_tol of -999
            -9990.0,
            9999.0,  # sign matters
            999.0,
            -99.0,
            -1000.0,
        ],
    )
    def test_values_near_sentinels_kept(self, value: float) -> None:
        assert mask_invalid_values([value], sentinels=GENERIC_SENTINELS) == [value]

    def test_usgs_sentinel_spellings(self) -> None:
        raw = [-1.2300000e034, float("-1.2300000e+034"), -1.23e34, 0.42]
        out = mask_invalid_values(raw, sentinels=(USGS_SENTINEL,))
        assert [math.isnan(v) for v in out] == [True, True, True, False]
        assert out[3] == 0.42

    def test_custom_rel_tol(self) -> None:
        assert mask_invalid_values([-9999.5], sentinels=(-9999.0,)) == [-9999.5]
        out = mask_invalid_values([-9999.5], sentinels=(-9999.0,), rel_tol=1e-3)
        assert math.isnan(out[0])

    def test_returns_new_list_preserving_order(self) -> None:
        raw = [0.1, -9999.0, 0.3]
        out = mask_invalid_values(raw, sentinels=GENERIC_SENTINELS)
        assert raw == [0.1, -9999.0, 0.3]
        assert out[0] == 0.1 and math.isnan(out[1]) and out[2] == 0.3


class TestDropInvalidWavelengths:
    def test_drops_non_finite_wavelength_rows(self) -> None:
        wl = [0.4, float("nan"), 0.6, float("inf"), 0.8, float("-inf")]
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        assert drop_invalid_wavelengths(wl, vals) == ([0.4, 0.6, 0.8], [1.0, 3.0, 5.0])

    def test_keeps_nan_values_on_valid_wavelengths(self) -> None:
        wl, vals = drop_invalid_wavelengths([0.4, 0.5], [float("nan"), 0.2])
        assert wl == [0.4, 0.5]
        assert math.isnan(vals[0]) and vals[1] == 0.2

    def test_empty(self) -> None:
        assert drop_invalid_wavelengths([], []) == ([], [])


class TestIdentifiers:
    @pytest.mark.parametrize(
        ("original", "local"),
        [
            ("splib07a_Olivine_GDS70.a_Fo89_165um_BECKa_AREF", None),
            (
                "splib07a_Iron_oxide_#4820_GDS782_ASDFRa_AREF",
                "splib07a_Iron_oxide__4820_GDS782_ASDFRa_AREF",
            ),
            ("Alizarin_crimson_(dk)", "Alizarin_crimson__dk_"),
            ("a+b%c d:e/f", "a_b_c_d_e_f"),
            ("5b7c365aa78fc80b50ffbbd1", None),
            ("tilde~ok.-", None),
        ],
    )
    def test_make_local_id(self, original: str, local: str | None) -> None:
        assert make_local_id(original) == (local or original)
        assert is_valid_local_id(make_local_id(original))

    def test_empty_and_long(self) -> None:
        assert make_local_id("") == "_"
        long_a, long_b = "x" * 300 + "a", "x" * 300 + "b"
        assert len(make_local_id(long_a)) == 200
        assert make_local_id(long_a) != make_local_id(long_b)
        assert is_valid_local_id(make_local_id(long_a))

    def test_spectrum_id(self) -> None:
        assert spectrum_id("usgs_splib07", "a+b") == "usgs_splib07:a_b"


class TestSortByWavelength:
    def test_monotonic_unchanged(self) -> None:
        up = sort_by_wavelength([1.0, 2.0], [5.0, 6.0])
        assert (up.wavelengths, up.arrays, up.reordered, up.dropped) == (
            [1.0, 2.0],
            [[5.0, 6.0]],
            False,
            [],
        )
        assert up.extra([1.0, 2.0]) == {}
        down = sort_by_wavelength([2.0, 1.0], [5.0, 6.0])
        assert down.wavelengths == [2.0, 1.0] and not down.reordered

    def test_overlap_sorted_with_arrays(self) -> None:
        source = [0.6, 0.7, 0.65, 0.8]
        axis = sort_by_wavelength(source, [1.0, 2.0, 3.0, 4.0], [0.1, 0.2, 0.3, 0.4], None)
        vals, bp, none = axis.arrays
        assert axis.reordered
        assert axis.wavelengths == [0.6, 0.65, 0.7, 0.8]
        assert vals == [1.0, 3.0, 2.0, 4.0]
        assert bp == [0.1, 0.3, 0.2, 0.4]
        assert none is None
        assert axis.extra(source) == {
            "bands_reordered_by_wavelength": True,
            "source_band_order": [0, 2, 1, 3],
        }

    def test_equal_wavelengths_keep_first_in_source_order(self) -> None:
        # Already ascending apart from a repeat: not reordered, the repeat dropped.
        source = [1.0, 2.0, 2.0, 3.0]
        axis = sort_by_wavelength(source, [10.0, 20.0, 21.0, 30.0])
        assert not axis.reordered
        assert axis.wavelengths == [1.0, 2.0, 3.0]
        assert axis.arrays == [[10.0, 20.0, 30.0]]
        assert axis.extra(source) == {
            "duplicate_wavelength_bands_dropped": [[2, 2.0]],
            "duplicate_wavelength_band_count": 1,
        }

    def test_equal_wavelengths_prefer_finite_band(self) -> None:
        nan = float("nan")
        source = [1.0, 2.0, 2.0, 3.0]
        axis = sort_by_wavelength(source, [10.0, nan, 21.0, 30.0], [0.1, 0.2, 0.3, 0.4])
        assert axis.wavelengths == [1.0, 2.0, 3.0]
        assert axis.arrays == [[10.0, 21.0, 30.0], [0.1, 0.3, 0.4]]
        assert axis.source_order == [0, 2, 3]
        assert axis.extra(source) == {
            "duplicate_wavelength_bands_dropped": [[1, 2.0]],
            "duplicate_wavelength_band_count": 1,
        }

    def test_equal_wavelengths_all_nan_keep_first(self) -> None:
        nan = float("nan")
        source = [3.0, 1.0, 1.0, 1.0, 2.0]
        axis = sort_by_wavelength(source, [30.0, nan, nan, 12.0, 20.0])
        assert axis.wavelengths == [1.0, 2.0, 3.0]
        assert axis.arrays[0] == [12.0, 20.0, 30.0]
        assert axis.dropped == [1, 2]
        all_nan = sort_by_wavelength([1.0, 1.0, 2.0], [nan, nan, 5.0])
        assert all_nan.source_order == [0, 2]
        assert all_nan.extra([1.0, 1.0, 2.0])["duplicate_wavelength_band_count"] == 1

    def test_equal_wavelengths_in_unsorted_axis(self) -> None:
        source = [3.0, 1.0, 3.0, 2.0, 1.0]
        axis = sort_by_wavelength(source, [30.0, 10.0, 31.0, 20.0, 11.0])
        assert axis.reordered
        assert axis.wavelengths == [1.0, 2.0, 3.0]
        assert axis.arrays == [[10.0, 20.0, 30.0]]
        assert axis.source_order == [1, 3, 0]
        assert axis.dropped == [2, 4]
        extra = axis.extra(source)
        assert extra["source_band_order"] == [1, 3, 0]
        assert extra["duplicate_wavelength_bands_dropped"] == [[2, 3.0], [4, 1.0]]

    def test_descending_with_repeat(self) -> None:
        axis = sort_by_wavelength([3.0, 2.0, 2.0, 1.0], [1.0, 2.0, 3.0, 4.0])
        assert not axis.reordered
        assert axis.wavelengths == [3.0, 2.0, 1.0]
        assert axis.arrays == [[1.0, 2.0, 4.0]]
