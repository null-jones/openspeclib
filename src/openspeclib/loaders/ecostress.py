"""Loader for the ECOSTRESS Spectral Library.

The ECOSTRESS library stores each spectrum as an individual text file
with a header section of key-value metadata followed by two-column
(wavelength, value) spectral data.

The raw archive is mirrored on Hugging Face Datasets; see
``ECOSTRESS_HF_REPO`` below.

Every ECOSTRESS 1.0 spectrum declares its value scale in a ``Y Units``
header, e.g. ``Y Units: Reflectance (percent)``. The spelling is
inconsistent across contributors (``percent`` / ``percentage``,
``Reflectence``, ``Transmittence``, ``Transmission``, varying whitespace
and stray tabs), so :func:`parse_y_units` normalises it. Percent values are
divided by 100 at ingest so the library is on the unit interval, and the
divisor is recorded in ``extra["source_reflectance_divisor"]``.

One header is known to be wrong: 36 ``usgs.perknic`` rock spectra say
``Reflectance (percent)`` but hold fractions (e.g. ``ro359`` limestone, 0.541
at 0.4 um). :func:`declared_percent_holds_fractions` catches this from the
data: a "percent" spectrum that reaches into the visible/near-infrared (starts
below 2 um) but whose 90th-percentile level is at most 1.5 is fractions, since
no ECOSTRESS VSWIR spectrum is that dark in percent (the next darkest, tap
water, has a level of 2.95). Thermal-only spectra are exempt: dark
carbonaceous-chondrite meteorites genuinely sit near 1.3 % in the TIR. Such a
spectrum is not divided; ``extra`` records divisor 1, the declared scale and
the level, and the quality notes say why.
A file whose ``Y Units`` is missing or unrecognised raises
:class:`EcostressUnitError`; :meth:`EcostressLoader.load` skips such files
with an ERROR log and reports the count, so no spectrum of unknown scale
reaches the library.

The ``Y Units`` quantity (reflectance vs. transmittance) is also the
authority for the measured quantity: it describes the value column
itself, whereas the free-text ``Measurement`` header uses spellings the
quantity map did not recognise (``Transmission``), which previously
labelled transmittance spectra as reflectance.

Reference: https://speclib.jpl.nasa.gov
"""

import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from huggingface_hub import hf_hub_download
from tqdm import tqdm

from openspeclib.loaders.base import (
    BaseLoader,
    drop_invalid_wavelengths,
    mask_invalid_values,
    monotonic_axis,
    spectrum_id,
)
from openspeclib.loaders.ecosis_scales import UNIT_CEILING, spectrum_level
from openspeclib.models import (
    CollectionInfo,
    Geometry,
    Inclusion,
    LicenseRef,
    Material,
    MaterialCategory,
    Measurement,
    Quality,
    Quantity,
    Sample,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
)

logger = logging.getLogger(__name__)

# Raw archive mirrored on Hugging Face Datasets.
ECOSTRESS_HF_REPO = "null-jones/ecostresslib"
ECOSTRESS_HF_FILENAME = "ecospeclib_1776217602535.zip"
ECOSTRESS_HF_REVISION = "main"

ECOSTRESS_CATEGORY_MAP: dict[str, MaterialCategory] = {
    "mineral": MaterialCategory.MINERAL,
    "rock": MaterialCategory.ROCK,
    "soil": MaterialCategory.SOIL,
    "vegetation": MaterialCategory.VEGETATION,
    "nonphotosyntheticvegetation": MaterialCategory.NPV,
    "npv": MaterialCategory.NPV,
    "manmade": MaterialCategory.MAN_MADE,
    "man-made": MaterialCategory.MAN_MADE,
    "meteorite": MaterialCategory.METEORITE,
    "meteorites": MaterialCategory.METEORITE,
    "water": MaterialCategory.WATER,
    "snow": MaterialCategory.SNOW_ICE,
    "ice": MaterialCategory.SNOW_ICE,
    "frost": MaterialCategory.SNOW_ICE,
    "lunar": MaterialCategory.LUNAR,
}

# Quantity-word prefixes -> quantity. Prefix matching tolerates the
# misspellings present in ECOSTRESS 1.0 headers ("Reflectence",
# "Transmittence", "Transittance", "Transmission", "Tansmission"). Order
# matters: first match wins.
_QUANTITY_PREFIXES: tuple[tuple[tuple[str, ...], Quantity], ...] = (
    (("reflect",), Quantity.REFLECTANCE),
    (("transm", "tansm", "transit"), Quantity.TRANSMITTANCE),
    (("emissiv",), Quantity.EMISSIVITY),
    (("absorb",), Quantity.ABSORBANCE),
)

# Y-unit quantities whose percent values are divided onto the unit interval.
# Absorbance is logarithmic and never reported in percent, so it is not
# accepted as a Y unit.
_Y_UNIT_QUANTITIES = frozenset(
    {
        Quantity.REFLECTANCE,
        Quantity.TRANSMITTANCE,
        Quantity.EMISSIVITY,
    }
)

# The whole (whitespace-normalised, lowercased) Y Units value: one quantity
# word, then a parenthesised percent scale and nothing else.
_Y_UNITS_PATTERN = re.compile(r"^[a-z]+ ?\( ?(?:percent|percentage|%) ?\)$")

# Divisor that maps an ECOSTRESS percent value onto the unit interval.
PERCENT_DIVISOR = 100

# A spectrum declared in percent whose 90th-percentile level is at most this,
# over an axis starting below ``_VSWIR_MIN_UM``, holds fractions (see the
# module docstring). Same ceiling as the EcoSIS unit-scale test.
FRACTION_LEVEL_CEILING = UNIT_CEILING


class EcostressUnitError(ValueError):
    """Raised when a spectrum's ``Y Units`` header is missing or unrecognised."""


COLLECTION_ID = "ecostress"

ECOSTRESS_LICENSE = "LicenseRef-ECOSTRESS-Terms"

ECOSTRESS_COLLECTION = CollectionInfo(
    id=COLLECTION_ID,
    title="ECOSTRESS Spectral Library",
    description=(
        "Spectra of natural and man-made materials from the visible to the thermal "
        "infrared (about 0.3 to 25 micrometres), consolidating and extending the ASTER "
        "Spectral Library with measurements from JHU, JPL and USGS."
    ),
    version="1.0",
    url="https://speclib.jpl.nasa.gov",
    publisher="NASA Jet Propulsion Laboratory",
    license=ECOSTRESS_LICENSE,
    license_note=(
        "The library is distributed without a standard licence. JPL is operated by "
        "Caltech for NASA, so its works are not automatically U.S. Government public "
        "domain; follow the provider's terms at https://speclib.jpl.nasa.gov and cite "
        "Meerdink et al. (2019)."
    ),
    license_refs={
        ECOSTRESS_LICENSE: LicenseRef(
            name="ECOSTRESS Spectral Library terms of use (NASA JPL)",
            text=(
                "Distributed free of charge by NASA's Jet Propulsion Laboratory. No "
                "standard licence is attached; the provider's terms at "
                "https://speclib.jpl.nasa.gov apply. Cite Meerdink et al. (2019), "
                "doi:10.1016/j.rse.2019.05.015."
            ),
            url="https://speclib.jpl.nasa.gov",
        )
    },
    citation=(
        "Meerdink, S.K., Hook, S.J., Roberts, D.A., Abbott, E.A., 2019. "
        "The ECOSTRESS spectral library version 1.0. Remote Sensing of Environment, 230, "
        "111196. https://doi.org/10.1016/j.rse.2019.05.015."
    ),
    inclusion=Inclusion.COMPLETE,
    keywords=["minerals", "rocks", "vegetation", "thermal infrared", "laboratory spectroscopy"],
)

# Normalised ``Measurement`` header value -> geometry. The header is the
# source's own statement of the setup; values not listed here (plain
# "Reflectance", transmission spectra, spliced spectra) stay ``unknown``
# (spec section 4.2). Keys are lowercased, whitespace-collapsed, with a
# trailing full stop removed (see ``_normalize_measurement``). Counts are
# from the ECOSTRESS 1.0 archive (ecospeclib-all).
#
# * "hemispherical reflectance" (jpl.beckman, jpl.perkin, jpl.nicolet,
#   ucsb.nicolet, 1 usgs.perknic; ~1980 spectra): laboratory spectrometers
#   with an integrating sphere, i.e. directional illumination and hemispherical
#   collection (Baldridge et al. 2009, Remote Sens. Environ. 113, 711-715;
#   Meerdink et al. 2019, Remote Sens. Environ. 230, 111196). The archive
#   itself confirms the reading: usgs.perknic ``me5`` has ``Measurement:
#   Hemispherical reflectance`` and its Description says "Measurement:
#   Directional Hemispherical Reflectance".
# * "directional hemispherical reflectance" and "directional (10 degree)
#   hemispherical reflectance" (jhu.becknic, jpl.nicolet, usgs.perknic):
#   stated directly.
# * "bidirectional reflectance" (jhu.nicolet, jhu.becknic meteorites, jpl.asd,
#   ucsb.asd) and "bidirectional reflectance packed" (one packed-powder
#   jhu.nicolet spectrum) -> biconical. These are laboratory measurements with
#   an ASD and a lamp/fore-optic or an FTIR reflectance attachment, which
#   Schaepman-Strub et al. (2006) call biconical (BCRF); older libraries call
#   them "bidirectional" (Kokaly et al. 2017, USGS DS 1035, p. 4: "Measurements
#   of this type are described as BCRF ... Traditionally, these measurements
#   have been referred to as bidirectional reflectance factors (BRF)"). The
#   spec reserves ``bidirectional`` for goniometric or near-collimated
#   measurements with stated angles; no ECOSTRESS header or ancillary file
#   states illumination and viewing angles for these spectra. The verbatim
#   header is kept in ``extra["source_geometry_term"]``.
# * Left unknown: "reflectance" (36 usgs.perknic rocks; setup not stated),
#   "transmission"/"transmittance" (jhu.nicolet; no geometry stated), and
#   "bidirectional and directional hemispherical reflectance." (4 jhu.becknic
#   vegetation spectra spliced from a bidirectional VNIR/SWIR segment and a
#   hemispherical MWIR/TIR segment, per their Description; a row has one
#   geometry).
ECOSTRESS_MEASUREMENT_GEOMETRY: dict[str, Geometry] = {
    "hemispherical reflectance": Geometry.DIRECTIONAL_HEMISPHERICAL,
    "directional hemispherical reflectance": Geometry.DIRECTIONAL_HEMISPHERICAL,
    "directional (10 degree) hemispherical reflectance": Geometry.DIRECTIONAL_HEMISPHERICAL,
    "bidirectional reflectance": Geometry.BICONICAL,
    "bidirectional reflectance packed": Geometry.BICONICAL,
}

# Filename instrument token (``<...>.<lab>.<instrument>.spectrum.txt``; the
# token is also in each Description's "Original ASTER Spectral Library name")
# -> (instrument, instrument type). Model numbers are not in the archive and
# are not recorded.
ECOSTRESS_INSTRUMENTS: dict[str, tuple[str, str]] = {
    "asd": ("ASD spectroradiometer", "field_spectrometer"),
    "beckman": ("Beckman spectrophotometer", "laboratory_spectrometer"),
    "becknic": (
        "Beckman spectrophotometer and Nicolet FTIR spectrometer",
        "laboratory_spectrometer",
    ),
    "nicolet": ("Nicolet FTIR spectrometer", "ftir"),
    "perkin": ("Perkin-Elmer spectrophotometer", "laboratory_spectrometer"),
    "perknic": (
        "Perkin-Elmer spectrophotometer and Nicolet FTIR spectrometer",
        "laboratory_spectrometer",
    ),
}

# Instrument pairs: their spectra join a dispersive VSWIR measurement and a
# Nicolet TIR measurement (``Wavelength Range: All``, e.g. 0.4-14 um). Spectra
# covering only the TIR part (jhu.becknic meteorites, from 2.08 um) come from
# the Nicolet alone.
_SPLICED_INSTRUMENTS = frozenset({"becknic", "perknic"})
# VSWIR spectrometers end at 2.5 um; Nicolet TIR spectra start near 2.0 um.
_VSWIR_MIN_UM = 2.0
_VSWIR_MAX_UM = 2.6

# Descriptions of spectra assembled from segments measured by other
# instruments than the filename token names ("Spectra were assembled from two
# segments ... measured ... with a GER IRIS Mark IV"); no instrument is
# recorded for them.
_ASSEMBLED_SEGMENTS = re.compile(r"assembled from two segments", re.I)

# Filename laboratory token -> measuring laboratory.
ECOSTRESS_LABORATORIES: dict[str, str] = {
    "jhu": "Johns Hopkins University",
    "jpl": "NASA Jet Propulsion Laboratory",
    "ucsb": "University of California, Santa Barbara",
    "usgs": "U.S. Geological Survey",
}


def _normalize_measurement(raw: str) -> str:
    """Normalise a ``Measurement`` header value for geometry lookup.

    Args:
        raw: Header value, e.g. ``"Directional (10 Degree) Hemispherical Reflectance"``.

    Returns:
        The value lowercased, whitespace collapsed, trailing full stop removed.
    """
    return " ".join(raw.split()).lower().rstrip(".").strip()


def measurement_geometry(raw: str) -> Geometry:
    """Map an ECOSTRESS ``Measurement`` header value to a geometry.

    Args:
        raw: Header value (may be empty).

    Returns:
        The geometry the header states, or ``Geometry.UNKNOWN``.
    """
    return ECOSTRESS_MEASUREMENT_GEOMETRY.get(_normalize_measurement(raw), Geometry.UNKNOWN)


@dataclass(frozen=True)
class _InstrumentInfo:
    """Laboratory and instrument of one ECOSTRESS spectrum.

    Attributes:
        laboratory: Measuring laboratory, or ``None``.
        instrument: Instrument description, or ``None``.
        instrument_type: Instrument category, or ``None`` (unknown or mixed).
        spliced: Whether the spectrum joins measurements of two instruments.
    """

    laboratory: str | None
    instrument: str | None
    instrument_type: str | None
    spliced: bool


def _lab_and_instrument(
    filename: str, wavelengths: list[float], description: str | None = None
) -> _InstrumentInfo:
    """Read the laboratory and instrument of a spectrum from its filename tokens.

    Args:
        filename: Spectrum filename, e.g.
            ``"mineral.arsenate.none.coarse.tir.a-1a.jpl.nicolet.spectrum.txt"``.
        wavelengths: The spectrum's axis in micrometres, used to tell a spliced
            Beckman/Perkin-Elmer + Nicolet spectrum from a Nicolet-only one.
        description: The ``Description`` header, or ``None``.

    Returns:
        The laboratory and instrument; fields are ``None`` when the token is not
        recognised. A spliced spectrum has no single instrument type.
    """
    parts = filename.lower().split(".")
    if len(parts) < 4 or parts[-2:] != ["spectrum", "txt"]:
        return _InstrumentInfo(None, None, None, False)
    lab, inst = parts[-4], parts[-3]
    laboratory = ECOSTRESS_LABORATORIES.get(lab)
    instrument, instrument_type = ECOSTRESS_INSTRUMENTS.get(inst, (None, None))
    if inst not in _SPLICED_INSTRUMENTS or not wavelengths:
        return _InstrumentInfo(laboratory, instrument, instrument_type, False)
    if min(wavelengths) >= _VSWIR_MIN_UM:
        nicolet, nicolet_type = ECOSTRESS_INSTRUMENTS["nicolet"]
        return _InstrumentInfo(laboratory, nicolet, nicolet_type, False)
    spliced = max(wavelengths) > _VSWIR_MAX_UM
    if spliced and description and _ASSEMBLED_SEGMENTS.search(description):
        instrument = None
    return _InstrumentInfo(laboratory, instrument, None if spliced else instrument_type, spliced)


def declared_percent_holds_fractions(
    divisor: int, wavelengths: list[float], values: list[float]
) -> float | None:
    """Tell whether a spectrum declared in percent actually holds fractions.

    Args:
        divisor: Divisor implied by the ``Y Units`` header.
        wavelengths: The axis in micrometres.
        values: The values, invalid ones already NaN.

    Returns:
        The spectrum's 90th-percentile level when the header says percent but
        the values are fractions (level at most ``FRACTION_LEVEL_CEILING`` on an
        axis that starts below ``_VSWIR_MIN_UM``), otherwise ``None``.
    """
    if divisor != PERCENT_DIVISOR or not wavelengths or min(wavelengths) >= _VSWIR_MIN_UM:
        return None
    level = spectrum_level(values)
    if level is None or level > FRACTION_LEVEL_CEILING:
        return None
    return level


def _normalize_category(raw: str) -> MaterialCategory:
    """Map an ECOSTRESS class string to a canonical MaterialCategory.

    Args:
        raw: Raw class string from the ECOSTRESS header.

    Returns:
        The matching material category, or ``OTHER`` if unrecognised.
    """
    key = re.sub(r"[\s\-_]+", "", raw.strip().lower())
    return ECOSTRESS_CATEGORY_MAP.get(key, MaterialCategory.OTHER)


def _match_quantities(text: str) -> set[Quantity]:
    """Find every physical quantity named in a free-text header value.

    Args:
        text: Header value, e.g. ``"Reflectence (percent)"`` or
            ``"Directional (10 Degree) Hemispherical Reflectance"``.

    Returns:
        The quantities whose quantity word appears in ``text`` (matched by
        word prefix, case-insensitively). Empty if none does.
    """
    words = re.findall(r"[a-z]+", text.lower())
    return {
        quantity
        for prefixes, quantity in _QUANTITY_PREFIXES
        if any(word.startswith(prefixes) for word in words)
    }


def _match_quantity(text: str) -> Quantity | None:
    """Return the single physical quantity named in a header value.

    Args:
        text: Free-text header value.

    Returns:
        The quantity if exactly one is named, otherwise ``None``
        (no quantity, or an ambiguous value naming several).
    """
    found = _match_quantities(text)
    return next(iter(found)) if len(found) == 1 else None


def parse_y_units(raw: str | None) -> tuple[Quantity, int]:
    """Parse an ECOSTRESS ``Y Units`` header value.

    The value must be exactly one quantity word followed by a parenthesised
    percent scale, e.g. ``Reflectance (percent)``. Case, surrounding/inner
    whitespace, stray tabs, and the misspellings found in ECOSTRESS 1.0
    (``Reflectence``, ``Transmittence``, ``Transittance``, ``Transmission``)
    are tolerated; anything more elaborate (several quantities, a scale
    outside the parentheses, extra qualifiers) is rejected rather than
    guessed at.

    Args:
        raw: The header value after ``Y Units:``, or ``None`` if absent.

    Returns:
        ``(quantity, divisor)`` where ``divisor`` maps the stored values
        onto the unit interval (always ``PERCENT_DIVISOR`` for ECOSTRESS).

    Raises:
        EcostressUnitError: If the header is missing, names no recognised
            quantity or more than one, or does not state exactly
            ``(percent)``, ``(percentage)`` or ``(%)`` as its scale.
    """
    if raw is None or not raw.strip():
        raise EcostressUnitError("missing 'Y Units' header")
    shown = raw.strip()
    value = " ".join(raw.split()).lower()
    quantities = _match_quantities(value)
    if len(quantities) > 1:
        raise EcostressUnitError(f"ambiguous quantity in 'Y Units: {shown}'")
    if not quantities:
        raise EcostressUnitError(f"unrecognised quantity in 'Y Units: {shown}'")
    quantity = next(iter(quantities))
    if quantity not in _Y_UNIT_QUANTITIES:
        raise EcostressUnitError(f"quantity cannot be percent-scaled in 'Y Units: {shown}'")
    # Anchored: the value is exactly "<quantity> (percent|percentage|%)", so
    # the single matched word is the quantity and the scale is unqualified.
    if not _Y_UNITS_PATTERN.match(value):
        raise EcostressUnitError(f"unrecognised scale in 'Y Units: {shown}'")
    return quantity, PERCENT_DIVISOR


def _without_macos_metadata(paths: Iterable[Path]) -> list[Path]:
    """Drop macOS archive artefacts (``__MACOSX/`` and ``._*`` AppleDouble files).

    The ECOSTRESS ZIP ships an ``__MACOSX`` tree mirroring every spectrum
    with a binary ``._<name>.spectrum.txt`` resource-fork stub.

    Args:
        paths: Candidate spectrum file paths.

    Returns:
        Sorted paths excluding macOS metadata files.
    """
    return sorted(p for p in paths if "__MACOSX" not in p.parts and not p.name.startswith("._"))


def _normalize_key(key: str) -> str:
    """Normalise a header key for lookup (``"Y Units"`` -> ``"yunits"``).

    Args:
        key: Raw header key.

    Returns:
        The key lowercased with whitespace, dots, dashes and underscores removed.
    """
    return re.sub(r"[\s.\-_]+", "", key.lower())


# Header values that stand for "no value" (ECOSTRESS 1.0 counts: "Subclass:
# None" 736, "Origin: Unknown." 34, "Origin: N/A" 26, "Particle Size: none"
# 41, "Sample No.: None" 2, ...). They are stored as null, not as text.
_PLACEHOLDERS = frozenset({"", "none", "n/a", "na", "unknown", "null", "not available"})

# The ``Description`` header is the original free text followed by parts the
# ECOSTRESS 1.0 conversion appended from other ASTER fields, often with no
# separator: "white limestoneSample was Whole rock chips. Original ASTER
# Spectral Library name was ...", "Rose quartzParticle size was 0-45um.",
# "Geologic age -  CambrianCollected by:  Ward's Original ASTER ...". Each
# appended part starts with one of these phrases.
_APPENDED_PART = re.compile(
    r"(?<=\S)(\s*)(?=Sample was |Sample measured |Particle size was |Collected by\b"
    r"|Original ASTER Spectral Library name )"
)


def _join_description_parts(text: str) -> str:
    """Separate the appended parts of an ECOSTRESS description.

    A part that follows text ending without sentence punctuation gets ``". "``
    in front of it; one that follows a full stop (or ``!``, ``?``, ``:``,
    ``;``) gets a single space if it had none. Text inside the original
    description is left as it is.

    Args:
        text: The ``Description`` header value.

    Returns:
        The description with its parts separated.
    """

    def separator(match: "re.Match[str]") -> str:
        before = text[match.start() - 1]
        if before in ".!?:;":
            return match.group(1) or " "
        return ". "

    return _APPENDED_PART.sub(separator, text)


def _present(value: str | None) -> str | None:
    """Return a header value, or ``None`` if it is missing or a placeholder.

    Args:
        value: Raw header value, or ``None`` if the key is absent.

    Returns:
        The stripped value, or ``None`` for ``None``/``N/A``/``Unknown``/empty
        (case-insensitive, trailing full stop ignored).
    """
    if value is None:
        return None
    stripped = value.strip()
    return None if stripped.rstrip(".").strip().lower() in _PLACEHOLDERS else stripped


def _first_present(header: dict[str, str], *keys: str) -> str | None:
    """Return the first non-placeholder value among several header keys.

    Args:
        header: Parsed header key-value pairs.
        *keys: Candidate keys, in order of preference.

    Returns:
        The first present value, or ``None``.
    """
    for key in keys:
        value = _present(header.get(key))
        if value is not None:
            return value
    return None


def parse_ecostress_file(filepath: Path) -> SpectrumRecord:
    """Parse a single ECOSTRESS spectrum text file.

    Values are converted from the percent scale declared in the ``Y Units``
    header to the unit interval, and non-finite values become NaN.

    Args:
        filepath: Path to a .spectrum.txt file.

    Returns:
        A SpectrumRecord with all available metadata and spectral data.

    Raises:
        EcostressUnitError: If the ``Y Units`` header is missing or
            unrecognised (see :func:`parse_y_units`).
        ValueError: If no spectral data is found.
    """
    raw_bytes = filepath.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # 13 ECOSTRESS 1.0 files are legacy 8-bit text (e.g. "Na2CO3·10H2O").
        text = raw_bytes.decode("cp1252", errors="replace")
    lines = text.strip().splitlines()

    # Parse header key-value pairs until we hit numeric data
    header: dict[str, str] = {}
    data_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Detect start of numeric data: line with two tab/space-separated numbers
        parts = stripped.split()
        if len(parts) >= 2:
            try:
                float(parts[0])
                float(parts[1])
                data_start = i
                break
            except ValueError:
                pass
        # Parse header line
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            header[key.strip()] = value.strip()

    # Parse spectral data columns
    wavelengths: list[float] = []
    values: list[float] = []
    for line in lines[data_start:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            try:
                w = float(parts[0])
                v = float(parts[1])
                wavelengths.append(w)
                values.append(v)
            except ValueError:
                continue

    wavelengths, values = drop_invalid_wavelengths(wavelengths, values)
    if not wavelengths:
        raise ValueError(f"No spectral data found in {filepath}")

    # Scale + quantity come from "Y Units"; fail loudly rather than guess.
    normalized_header = {_normalize_key(k): v for k, v in header.items()}
    quantity, divisor = parse_y_units(normalized_header.get("yunits"))
    values = mask_invalid_values(values)
    notes: list[str] = []
    scale_extra: dict[str, Any] = {}
    fraction_level = declared_percent_holds_fractions(divisor, wavelengths, values)
    if fraction_level is not None:
        # The header's "percent" is wrong for this file; dividing would store
        # it 100x too small.
        divisor = 1
        scale_extra = {
            "declared_value_scale": "percent",
            "value_scale_rule": "declared_percent_level_below_1.5_vswir",
            "source_value_level_p90": fraction_level,
        }
        notes.append(
            f"Y Units '{normalized_header['yunits']}' says percent, but the values are "
            f"fractions (90th-percentile level {fraction_level:g} on an axis reaching "
            f"the VNIR); they were not divided."
        )
        logger.info("%s: %s", filepath.name, notes[-1])
    values = [v / divisor for v in values] if divisor != 1 else values
    wavelengths, values, axis_extra = monotonic_axis(wavelengths, values)

    # The free-text "Measurement" header should agree with "Y Units"; when it
    # does not, "Y Units" wins (it describes the value column) and the
    # disagreement is surfaced in the quality notes.
    measurement_type = header.get("Measurement", "")
    measurement_quantity = _match_quantity(measurement_type)
    if measurement_quantity is not None and measurement_quantity != quantity:
        notes.append(
            f"Source header conflict: Measurement '{measurement_type}' vs "
            f"Y Units '{normalized_header['yunits']}'; quantity taken from Y Units."
        )
        logger.warning("%s: %s", filepath.name, notes[-1])
    quality_notes = " ".join(notes) or None

    # Extract metadata from header
    name = _present(header.get("Name")) or filepath.stem
    sample_id = _first_present(header, "SampleNo", "Sample No", "Sample No.")
    # ECOSTRESS puts the top-level category in "Type" (e.g. Mineral, Soil,
    # Water) and the finer grouping in "Class" (e.g. Silicate, Lunar, Snow).
    # A Class that itself names a category is the more specific statement and
    # wins ("Soil / Lunar" is lunar, "Water / Snow|Ice|Frost" is snow_ice);
    # otherwise Type decides. Both are kept verbatim in ``extra``.
    raw_type = _present(header.get("Type")) or ""
    raw_class_field = _present(header.get("Class")) or ""
    type_category = _normalize_category(raw_type)
    class_category = _normalize_category(raw_class_field)
    category = class_category if class_category is not MaterialCategory.OTHER else type_category
    # An explicit placeholder ("Subclass: None") means no subclass; only an
    # absent key falls back to Class.
    if any(k in header for k in ("SubClass", "Subclass")):
        subcategory = _first_present(header, "SubClass", "Subclass")
    else:
        subcategory = raw_class_field or None
    particle_size = _first_present(header, "ParticleSize", "Particle Size")
    origin = _present(header.get("Origin"))
    description = _present(header.get("Description"))
    if description is not None:
        description = _join_description_parts(description)
    owner = _present(header.get("Owner"))

    original_id = filepath.stem.replace(".spectrum", "")
    info = _lab_and_instrument(filepath.name, wavelengths, description)
    extra: dict[str, Any] = {"source_reflectance_divisor": divisor, **scale_extra, **axis_extra}
    # The source's own classification, verbatim: Type and Class are otherwise
    # only reflected in the category and (without a Subclass) the subcategory.
    if raw_type:
        extra["source_type"] = raw_type
    if raw_class_field:
        extra["source_class"] = raw_class_field
    if category is not type_category and type_category is not MaterialCategory.OTHER:
        extra["original_category"] = type_category.value
    keywords: list[str] = []
    for word in (name.lower(), raw_type.lower(), raw_class_field.lower(), subcategory):
        if word and word not in keywords:
            keywords.append(word)
    if measurement_type.strip():
        # Verbatim set-up term; geometry may relabel it (spec section 5).
        extra["source_geometry_term"] = measurement_type.strip()

    return SpectrumRecord(
        id=spectrum_id(COLLECTION_ID, original_id),
        collection=COLLECTION_ID,
        original_id=original_id,
        name=name,
        license=ECOSTRESS_LICENSE,
        material=Material(
            name=name,
            category=category,
            subcategory=subcategory,
            keywords=keywords,
        ),
        sample=Sample(
            id=sample_id,
            description=description,
            particle_size=particle_size,
            origin=origin,
            owner=owner,
        ),
        measurement=Measurement(
            quantity=quantity,
            # The mapped headers all describe reflectance; if Y Units says
            # otherwise the header is not about this value column.
            geometry=(
                measurement_geometry(measurement_type)
                if quantity == Quantity.REFLECTANCE
                else Geometry.UNKNOWN
            ),
            instrument=info.instrument,
            instrument_type=info.instrument_type,
            laboratory=info.laboratory,
            processing=["spliced"] if info.spliced else [],
        ),
        spectral_data=SpectralData(
            wavelength_unit=WavelengthUnit.MICROMETERS,
            wavelengths=wavelengths,
            values=values,
        ),
        quality=Quality(notes=quality_notes),
        extra=extra,
    )


class EcostressLoader(BaseLoader):
    """Loader for the ECOSTRESS Spectral Library.

    Attributes:
        unit_errors: Files skipped by the last :meth:`load` because their
            ``Y Units`` header was missing or unrecognised.
        parse_errors: Files skipped by the last :meth:`load` for any other
            parse failure.
    """

    def __init__(self) -> None:
        """Initialise per-run skip counters."""
        self.unit_errors = 0
        self.parse_errors = 0

    @property
    def fatal_error_count(self) -> int:
        """Files skipped for an unknown value scale; these fail ``ingest``."""
        return self.unit_errors

    def source_name(self) -> str:
        """Return the collection id."""
        return COLLECTION_ID

    def collection_info(self) -> CollectionInfo:
        """Return the ECOSTRESS Spectral Library collection metadata.

        Returns:
            The footer ``collection`` object.
        """
        return ECOSTRESS_COLLECTION

    def download(self, target_dir: Path) -> Path:
        """Download and extract the ECOSTRESS spectral library archive.

        Fetches the bundled ZIP from the Hugging Face dataset mirror at
        ``ECOSTRESS_HF_REPO`` and extracts it into ``target_dir``. If the
        extracted directory already exists with data, the download and
        extraction steps are skipped — safe to call repeatedly from cached
        CI runs.

        Args:
            target_dir: Directory to download and extract into.

        Returns:
            Path to the extracted ECOSTRESS data directory.

        Raises:
            RuntimeError: If the downloaded file is not a valid ZIP archive.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        extract_dir = target_dir / "ecospeclib-all"

        # Fast path: extraction already done (e.g. restored from CI cache).
        if extract_dir.exists() and any(extract_dir.iterdir()):
            logger.info("ECOSTRESS data already extracted at %s", extract_dir)
            return extract_dir

        logger.info(
            "Downloading ECOSTRESS from Hugging Face: %s (%s @ %s)",
            ECOSTRESS_HF_REPO,
            ECOSTRESS_HF_FILENAME,
            ECOSTRESS_HF_REVISION,
        )
        archive_path = Path(
            hf_hub_download(
                repo_id=ECOSTRESS_HF_REPO,
                filename=ECOSTRESS_HF_FILENAME,
                repo_type="dataset",
                revision=ECOSTRESS_HF_REVISION,
                local_dir=str(target_dir),
            )
        )

        if not zipfile.is_zipfile(archive_path):
            raise RuntimeError(
                f"File downloaded from {ECOSTRESS_HF_REPO}/{ECOSTRESS_HF_FILENAME} "
                f"is not a valid ZIP archive."
            )

        logger.info("Extracting ECOSTRESS archive to %s", extract_dir)
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)

        return extract_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse all ECOSTRESS spectrum files in the source directory.

        Files whose ``Y Units`` header is missing or unrecognised are skipped
        with an ERROR log (their value scale is unknown, so they must not
        enter the unit-scale library), and the total is reported at the end
        and exposed as :attr:`unit_errors` / :attr:`fatal_error_count` so the
        ``ingest`` command can fail the run.

        Args:
            source_dir: Root of the extracted ECOSTRESS data.

        Yields:
            SpectrumRecord for each .spectrum.txt (or, failing that, .txt)
            file that parses with a recognised ``Y Units`` header.
        """
        # Find all spectrum files — ECOSTRESS uses various naming conventions
        spectrum_files = _without_macos_metadata(source_dir.rglob("*.spectrum.txt"))
        if not spectrum_files:
            # Fall back to any .txt files that look like spectra
            spectrum_files = _without_macos_metadata(source_dir.rglob("*.txt"))

        logger.info("Found %d ECOSTRESS spectrum files", len(spectrum_files))

        parsed = 0
        self.unit_errors = 0
        self.parse_errors = 0
        for filepath in tqdm(spectrum_files, desc="Processing ECOSTRESS"):
            try:
                record = parse_ecostress_file(filepath)
            except EcostressUnitError as exc:
                self.unit_errors += 1
                logger.error("Skipping %s: %s", filepath, exc)
                continue
            except Exception:
                self.parse_errors += 1
                logger.warning("Failed to parse %s", filepath, exc_info=True)
                continue
            parsed += 1
            yield record

        logger.info(
            "ECOSTRESS ingest summary: parsed=%d, unit_errors=%d, parse_errors=%d",
            parsed,
            self.unit_errors,
            self.parse_errors,
        )
        if self.unit_errors:
            logger.error(
                "%d ECOSTRESS file(s) skipped because their 'Y Units' header was "
                "missing or unrecognised; extend parse_y_units() after checking them.",
                self.unit_errors,
            )
