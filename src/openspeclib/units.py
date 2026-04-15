"""Wavelength unit conversion utilities.

Supports conversions between micrometers (um), nanometers (nm),
and wavenumbers (cm-1) for spectral data interoperability.
"""

from __future__ import annotations

from openspeclib.models import WavelengthUnit


def um_to_nm(um: float) -> float:
    """Convert micrometers to nanometers.

    Args:
        um: Wavelength in micrometers.

    Returns:
        Wavelength in nanometers.
    """
    return um * 1000.0


def nm_to_um(nm: float) -> float:
    """Convert nanometers to micrometers.

    Args:
        nm: Wavelength in nanometers.

    Returns:
        Wavelength in micrometers.
    """
    return nm / 1000.0


def um_to_wn(um: float) -> float:
    """Convert micrometers to wavenumbers (cm-1).

    Uses the relation: wavenumber = 10,000 / wavelength_in_um.

    Args:
        um: Wavelength in micrometers. Must be non-zero.

    Returns:
        Wavenumber in cm-1.

    Raises:
        ValueError: If ``um`` is zero.
    """
    if um == 0.0:
        raise ValueError("Cannot convert zero micrometers to wavenumbers.")
    return 10_000.0 / um


def wn_to_um(wn: float) -> float:
    """Convert wavenumbers (cm-1) to micrometers.

    Uses the relation: wavelength_in_um = 10,000 / wavenumber.

    Args:
        wn: Wavenumber in cm-1. Must be non-zero.

    Returns:
        Wavelength in micrometers.

    Raises:
        ValueError: If ``wn`` is zero.
    """
    if wn == 0.0:
        raise ValueError("Cannot convert zero wavenumbers to micrometers.")
    return 10_000.0 / wn


def nm_to_wn(nm: float) -> float:
    """Convert nanometers to wavenumbers (cm-1).

    Args:
        nm: Wavelength in nanometers.

    Returns:
        Wavenumber in cm-1.
    """
    return um_to_wn(nm_to_um(nm))


def wn_to_nm(wn: float) -> float:
    """Convert wavenumbers (cm-1) to nanometers.

    Args:
        wn: Wavenumber in cm-1.

    Returns:
        Wavelength in nanometers.
    """
    return um_to_nm(wn_to_um(wn))


def convert_wavelength(
    value: float,
    from_unit: WavelengthUnit,
    to_unit: WavelengthUnit,
) -> float:
    """Convert a wavelength value between any two supported units.

    Args:
        value: The wavelength or wavenumber value to convert.
        from_unit: The unit of the input value.
        to_unit: The desired output unit.

    Returns:
        The converted value in the target unit.
    """
    if from_unit == to_unit:
        return value

    _converters = {
        (WavelengthUnit.MICROMETERS, WavelengthUnit.NANOMETERS): um_to_nm,
        (WavelengthUnit.MICROMETERS, WavelengthUnit.WAVENUMBERS): um_to_wn,
        (WavelengthUnit.NANOMETERS, WavelengthUnit.MICROMETERS): nm_to_um,
        (WavelengthUnit.NANOMETERS, WavelengthUnit.WAVENUMBERS): nm_to_wn,
        (WavelengthUnit.WAVENUMBERS, WavelengthUnit.MICROMETERS): wn_to_um,
        (WavelengthUnit.WAVENUMBERS, WavelengthUnit.NANOMETERS): wn_to_nm,
    }

    converter = _converters[(from_unit, to_unit)]
    return converter(value)


def convert_wavelength_array(
    values: list[float],
    from_unit: WavelengthUnit,
    to_unit: WavelengthUnit,
) -> list[float]:
    """Convert an array of wavelength values between units.

    Args:
        values: List of wavelength or wavenumber values.
        from_unit: The unit of the input values.
        to_unit: The desired output unit.

    Returns:
        List of converted values.
    """
    if from_unit == to_unit:
        return values
    return [convert_wavelength(v, from_unit, to_unit) for v in values]
