"""Tests for wavelength unit conversion utilities."""

import pytest

from openspeclib.models import WavelengthUnit
from openspeclib.units import (
    convert_wavelength,
    convert_wavelength_array,
    nm_to_um,
    nm_to_wn,
    um_to_nm,
    um_to_wn,
    wn_to_nm,
    wn_to_um,
)


class TestDirectConversions:
    def test_um_to_nm(self) -> None:
        assert um_to_nm(1.0) == 1000.0
        assert um_to_nm(0.55) == 550.0

    def test_nm_to_um(self) -> None:
        assert nm_to_um(1000.0) == 1.0
        assert nm_to_um(550.0) == 0.55

    def test_um_to_wn(self) -> None:
        assert um_to_wn(10.0) == 1000.0
        assert um_to_wn(1.0) == 10_000.0

    def test_wn_to_um(self) -> None:
        assert wn_to_um(1000.0) == 10.0
        assert wn_to_um(10_000.0) == 1.0

    def test_nm_to_wn(self) -> None:
        # 1000 nm = 1 um → 10000 cm-1
        assert nm_to_wn(1000.0) == 10_000.0

    def test_wn_to_nm(self) -> None:
        assert wn_to_nm(10_000.0) == 1000.0

    def test_zero_um_raises(self) -> None:
        with pytest.raises(ValueError):
            um_to_wn(0.0)

    def test_zero_wn_raises(self) -> None:
        with pytest.raises(ValueError):
            wn_to_um(0.0)


class TestRoundTrips:
    def test_um_nm_roundtrip(self) -> None:
        original = 2.5
        assert nm_to_um(um_to_nm(original)) == pytest.approx(original)

    def test_um_wn_roundtrip(self) -> None:
        original = 2.5
        assert wn_to_um(um_to_wn(original)) == pytest.approx(original)

    def test_nm_wn_roundtrip(self) -> None:
        original = 550.0
        assert wn_to_nm(nm_to_wn(original)) == pytest.approx(original)


class TestConvertWavelength:
    def test_same_unit_noop(self) -> None:
        result = convert_wavelength(2.5, WavelengthUnit.MICROMETERS, WavelengthUnit.MICROMETERS)
        assert result == 2.5

    def test_um_to_nm(self) -> None:
        result = convert_wavelength(2.5, WavelengthUnit.MICROMETERS, WavelengthUnit.NANOMETERS)
        assert result == 2500.0

    def test_nm_to_wn(self) -> None:
        result = convert_wavelength(1000.0, WavelengthUnit.NANOMETERS, WavelengthUnit.WAVENUMBERS)
        assert result == pytest.approx(10_000.0)


class TestConvertWavelengthArray:
    def test_noop(self) -> None:
        values = [0.35, 0.55, 2.5]
        um = WavelengthUnit.MICROMETERS
        result = convert_wavelength_array(values, um, um)
        assert result is values  # same object, not a copy

    def test_um_to_nm_array(self) -> None:
        values = [0.35, 0.55, 2.5]
        result = convert_wavelength_array(
            values, WavelengthUnit.MICROMETERS, WavelengthUnit.NANOMETERS
        )
        assert result == [350.0, 550.0, 2500.0]
