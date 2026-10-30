"""Loader for the RELAB Spectral Database (Brown University / NASA).

RELAB (Reflectance Experiment Laboratory) provides bidirectional
reflectance spectra of minerals, rocks, meteorites, and lunar samples.
Data is distributed as ASCII tab-delimited files through the PDS
Geosciences Node.

Reference: https://sites.brown.edu/relab/
"""

import logging
import re
from pathlib import Path
from typing import Any, Iterator

from tqdm import tqdm

from openspeclib.loaders.base import (
    GENERIC_SENTINELS,
    BaseLoader,
    drop_invalid_wavelengths,
    mask_invalid_values,
    monotonic_axis,
    spectrum_id,
)
from openspeclib.models import (
    CollectionInfo,
    Geometry,
    Inclusion,
    LicenseRef,
    Material,
    MaterialCategory,
    Measurement,
    Quantity,
    Sample,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
)

logger = logging.getLogger(__name__)

COLLECTION_ID = "relab"

RELAB_LICENSE = "LicenseRef-RELAB-ResearchUse"

RELAB_COLLECTION = CollectionInfo(
    id=COLLECTION_ID,
    title="RELAB Spectral Database",
    description=(
        "Bidirectional reflectance spectra of minerals, meteorites, lunar samples and "
        "terrestrial rocks measured at the NASA-supported Reflectance Experiment "
        "Laboratory, Brown University."
    ),
    version="2.0",
    url="https://pds-geosciences.wustl.edu/spectrallibrary/default.htm",
    publisher="Brown University",
    license=RELAB_LICENSE,
    license_refs={
        RELAB_LICENSE: LicenseRef(
            name="RELAB data: freely available for research use with citation",
            url="https://sites.brown.edu/relab/",
        )
    },
    citation=(
        "Pieters, C.M. and Hiroi, T., RELAB (Reflectance Experiment Laboratory): "
        "A NASA Multiuser Spectroscopy Facility. "
        "Lunar and Planetary Science Conference, Abstract #2196."
    ),
    inclusion=Inclusion.SUBSET,
    inclusion_notes=(
        "Whatever spectra are placed in the ingest directory by hand (the source has no "
        "automatic download), so completeness depends on that download."
    ),
)

# RELAB has two reflectance instruments (Pieters 1983, J. Geophys. Res. 88,
# 9534-9544; Pieters and Hiroi 2004, LPSC XXXV, "RELAB: A NASA multiuser
# spectroscopy facility"): the bidirectional spectrometer (BD-VNIR, 0.3-2.6 um,
# goniometric, standard geometry incidence 30 degrees, emission 0 degrees) and
# an FTIR (near- to far-infrared). RELAB also distributes spectra spliced from
# both. The wavelength axis does not tell them apart (an FTIR spectrum may end
# near 2.5 um), so the instrument is taken only from the file's catalogue
# metadata (a ``Spectrometer``/``Instrument`` header):
#
# * BD-VNIR named -> ``bidirectional``; angles from ``Incidence``/``Emission``
#   headers when present, otherwise the standard angles recorded as nominal.
# * FTIR named -> ``unknown`` geometry (the accessory is not stated), type ftir.
# * both named (spliced) -> ``unknown``, no instrument type, flag ``spliced``.
# * nothing named -> ``unknown`` (spec section 4.2).
_BD_VNIR_PATTERN = re.compile(r"\bBD[- ]?VNIR\b|\bbidirectional\b|\bBDR\b", re.I)
_FTIR_PATTERN = re.compile(r"\bFT-?IR\b|\bNicolet\b|\bNexus\b", re.I)
_INSTRUMENT_KEYS = ("Spectrometer", "Instrument", "Instrument Name")
_NOMINAL_ANGLES = {"nominal_incidence_angle_deg": 30, "nominal_emission_angle_deg": 0}
_ANGLE_KEYS = {
    "incidence_angle_deg": ("Incidence", "Incidence Angle"),
    "emission_angle_deg": ("Emission", "Emission Angle"),
}


def _relab_instrument(header: dict[str, str]) -> tuple[bool, bool]:
    """Tell which RELAB instruments a file's catalogue metadata names.

    Args:
        header: Parsed header key-value pairs.

    Returns:
        ``(bd_vnir, ftir)``: whether the bidirectional spectrometer and the
        FTIR are named.
    """
    text = " ".join(header.get(k, "") for k in _INSTRUMENT_KEYS)
    return bool(_BD_VNIR_PATTERN.search(text)), bool(_FTIR_PATTERN.search(text))


def _relab_angles(header: dict[str, str]) -> dict[str, float]:
    """Read the illumination/viewing angles of a BD-VNIR spectrum.

    Args:
        header: Parsed header key-value pairs.

    Returns:
        ``incidence_angle_deg``/``emission_angle_deg`` from the header when both
        parse, otherwise RELAB's standard angles as ``nominal_*`` keys.
    """
    angles: dict[str, float] = {}
    for out_key, keys in _ANGLE_KEYS.items():
        for key in keys:
            try:
                angles[out_key] = float(header[key].split()[0])
                break
            except (KeyError, IndexError, ValueError):
                continue
    if len(angles) == len(_ANGLE_KEYS):
        return angles
    return dict(_NOMINAL_ANGLES)


RELAB_CATEGORY_MAP: dict[str, MaterialCategory] = {
    "mineral": MaterialCategory.MINERAL,
    "rock": MaterialCategory.ROCK,
    "soil": MaterialCategory.SOIL,
    "meteorite": MaterialCategory.METEORITE,
    "lunar": MaterialCategory.LUNAR,
    "terrestrial": MaterialCategory.ROCK,
    "synthetic": MaterialCategory.MAN_MADE,
    "mixture": MaterialCategory.MIXTURE,
    "glass": MaterialCategory.MAN_MADE,
    "ice": MaterialCategory.SNOW_ICE,
}


def _classify_material(sample_type: str, name: str) -> MaterialCategory:
    """Classify a RELAB sample into a canonical MaterialCategory.

    Args:
        sample_type: Sample type string from the file header.
        name: Sample name, used as a fallback for classification.

    Returns:
        The inferred material category.
    """
    key = sample_type.strip().lower()
    for keyword, category in RELAB_CATEGORY_MAP.items():
        if keyword in key:
            return category
    # Fall back to name-based heuristics
    name_lower = name.lower()
    if "meteorite" in name_lower:
        return MaterialCategory.METEORITE
    if "lunar" in name_lower or "apollo" in name_lower:
        return MaterialCategory.LUNAR
    return MaterialCategory.MINERAL


def parse_relab_file(filepath: Path) -> SpectrumRecord:
    """Parse a single RELAB ASCII tab-delimited spectrum file.

    RELAB files typically have header lines (starting with ``#`` or containing
    metadata) followed by two-column tab-delimited data: wavelength (um)
    and reflectance.

    Args:
        filepath: Path to the RELAB spectrum text file.

    Returns:
        A ``SpectrumRecord`` populated with parsed metadata and spectral data.

    Raises:
        ValueError: If no spectral data points are found in the file.
    """
    text = filepath.read_text(encoding="utf-8", errors="replace")
    lines = text.strip().splitlines()

    header: dict[str, str] = {}
    data_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Try parsing as numeric data (tab or space separated)
        parts = stripped.split()
        if len(parts) >= 2:
            try:
                float(parts[0])
                float(parts[1])
                data_start = i
                break
            except ValueError:
                pass

        # Parse header lines (key: value or key = value, or # comments)
        if stripped.startswith("#"):
            content = stripped.lstrip("#").strip()
            if ":" in content:
                key, _, value = content.partition(":")
                header[key.strip()] = value.strip()
            elif "=" in content:
                key, _, value = content.partition("=")
                header[key.strip()] = value.strip()
        elif ":" in stripped:
            key, _, value = stripped.partition(":")
            header[key.strip()] = value.strip()
        elif "\t" in stripped and "=" in stripped:
            key, _, value = stripped.partition("=")
            header[key.strip()] = value.strip()

    # Parse spectral data
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

    # Missing-value contract (see loaders.base): no NaN wavelengths, and
    # sentinel / non-finite values become NaN.
    wavelengths, values = drop_invalid_wavelengths(wavelengths, values)
    values = mask_invalid_values(values, sentinels=GENERIC_SENTINELS)
    wavelengths, values, axis_extra = monotonic_axis(wavelengths, values)

    if not wavelengths:
        raise ValueError(f"No spectral data found in {filepath}")

    name = header.get("Name", header.get("Sample Name", filepath.stem))
    sample_id = header.get("Sample ID", header.get("PI", filepath.stem))
    sample_type = header.get("Type", header.get("Sample Type", "mineral"))
    origin = header.get("Origin", header.get("Locality", None))
    particle_size = header.get("Grain Size", header.get("Particle Size", None))
    description = header.get("Description", None)

    category = _classify_material(sample_type, name)
    original_id = filepath.stem
    bd_vnir, ftir = _relab_instrument(header)
    spliced = bd_vnir and ftir
    measurement_extra: dict[str, Any] = {}
    if spliced:
        geometry, instrument, instrument_type = Geometry.UNKNOWN, None, None
    elif bd_vnir:
        geometry = Geometry.BIDIRECTIONAL
        instrument, instrument_type = (
            "RELAB bidirectional spectrometer (BD-VNIR)",
            "laboratory_spectrometer",
        )
        measurement_extra = dict(_relab_angles(header))
    elif ftir:
        geometry, instrument, instrument_type = Geometry.UNKNOWN, "RELAB FTIR spectrometer", "ftir"
    else:
        geometry, instrument, instrument_type = Geometry.UNKNOWN, None, None

    return SpectrumRecord(
        id=spectrum_id(COLLECTION_ID, original_id),
        collection=COLLECTION_ID,
        original_id=original_id,
        name=name,
        license=RELAB_LICENSE,
        material=Material(
            name=name,
            category=category,
            keywords=[name.lower(), category.value],
        ),
        sample=Sample(
            id=sample_id,
            description=description,
            particle_size=particle_size,
            origin=origin,
            owner="RELAB / Brown University",
        ),
        measurement=Measurement(
            quantity=Quantity.REFLECTANCE,
            geometry=geometry,
            instrument=instrument,
            instrument_type=instrument_type,
            laboratory="RELAB, Brown University",
            processing=["spliced"] if spliced else [],
        ),
        spectral_data=SpectralData(
            wavelength_unit=WavelengthUnit.MICROMETERS,
            wavelengths=wavelengths,
            values=values,
        ),
        extra={**measurement_extra, **axis_extra},
    )


class RelabLoader(BaseLoader):
    """Loader for the RELAB Spectral Database."""

    @property
    def supports_auto_download(self) -> bool:
        """RELAB requires manual download from the PDS Geosciences Node."""
        return False

    def source_name(self) -> str:
        """Return the collection id."""
        return COLLECTION_ID

    def collection_info(self) -> CollectionInfo:
        """Return the RELAB collection metadata.

        Returns:
            The footer ``collection`` object.
        """
        return RELAB_COLLECTION

    def download(self, target_dir: Path) -> Path:
        """Download RELAB spectral data.

        RELAB data is distributed through the PDS Geosciences Node.
        Users may need to download manually from:
        https://pds-geosciences.wustl.edu/spectrallibrary/default.htm
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "RELAB data should be placed in %s. "
            "Download from: https://pds-geosciences.wustl.edu/spectrallibrary/",
            target_dir,
        )
        return target_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse RELAB ASCII files from the source directory."""
        spectrum_files = sorted(
            f
            for f in source_dir.rglob("*.txt")
            if not any(kw in f.name.lower() for kw in ["readme", "catalog", "index", "description"])
        )
        # Also check for .asc and .tab files
        spectrum_files.extend(sorted(source_dir.rglob("*.asc")))
        spectrum_files.extend(sorted(source_dir.rglob("*.tab")))

        logger.info("Found %d RELAB spectrum files", len(spectrum_files))

        for filepath in tqdm(spectrum_files, desc="Processing RELAB"):
            try:
                yield parse_relab_file(filepath)
            except Exception:
                logger.warning("Failed to parse %s", filepath, exc_info=True)
