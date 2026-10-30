"""Loader for the Bishop Spectral Library (SETI Institute).

The Bishop Spectral Library provides high-quality reflectance spectra
of minerals with emphasis on carbonates, hydrated minerals, and ices.
Data is curated by Janice Bishop at the SETI Institute.

Reference: https://dmp.seti.org/jbishop/spectral-library.html
"""

import logging
import re
from pathlib import Path
from typing import Iterator

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

COLLECTION_ID = "bishop"

BISHOP_LICENSE = "LicenseRef-Bishop-NonCommercial"

BISHOP_COLLECTION = CollectionInfo(
    id=COLLECTION_ID,
    title="Bishop Spectral Library",
    description=(
        "Reflectance spectra of minerals with emphasis on carbonates, hydrated minerals, "
        "phyllosilicates, sulfates and ices, curated by Janice Bishop at the SETI Institute."
    ),
    version="1.0",
    url="https://dmp.seti.org/jbishop/spectral-library.html",
    publisher="SETI Institute",
    license=BISHOP_LICENSE,
    license_refs={
        BISHOP_LICENSE: LicenseRef(
            name="Bishop Spectral Library terms: non-commercial use with citation",
            text=(
                "Free for non-commercial use; publications using the spectra must cite "
                "the library."
            ),
            url="https://dmp.seti.org/jbishop/spectral-library.html",
        )
    },
    inclusion=Inclusion.SUBSET,
    inclusion_notes=(
        "Whatever spectra are placed in the ingest directory by hand (the source has no "
        "automatic download), so completeness depends on that download."
    ),
    citation=(
        "Bishop, J.L., Lane, M.D., Dyar, M.D., and Brown, A.J., "
        "Reflectance and emission spectroscopy study of four groups "
        "of phyllosilicates: smectites, kaolinite-serpentines, chlorites "
        "and micas. Clay Minerals, 43, 35-54, 2008. "
        "https://doi.org/10.1180/claymin.2008.043.1.03."
    ),
)

# Measurement geometry recorded on every Bishop record: ``unknown`` (spec
# section 4.2). The library's spectra come from more than one set-up (much of
# it measured at RELAB, whose bidirectional VNIR and FTIR data are often
# spliced into one spectrum), and the files this loader reads neither name the
# instrument nor state the geometry, so no single value is right. For the same
# reason no instrument type is recorded.
BISHOP_GEOMETRY = Geometry.UNKNOWN

BISHOP_CATEGORY_MAP: dict[str, MaterialCategory] = {
    "carbonate": MaterialCategory.MINERAL,
    "sulfate": MaterialCategory.MINERAL,
    "phyllosilicate": MaterialCategory.MINERAL,
    "oxide": MaterialCategory.MINERAL,
    "ice": MaterialCategory.SNOW_ICE,
    "frost": MaterialCategory.SNOW_ICE,
    "mineral": MaterialCategory.MINERAL,
    "rock": MaterialCategory.ROCK,
    "soil": MaterialCategory.SOIL,
    "salt": MaterialCategory.MINERAL,
    "clay": MaterialCategory.MINERAL,
}


def _classify_bishop(name: str, header: dict[str, str]) -> MaterialCategory:
    """Classify a Bishop library sample.

    Prioritises the Type/Class header field. Falls back to whole-word
    keyword matching against the sample name to avoid substring false
    positives (e.g. "ice" inside "Iceland").

    Args:
        name: Sample name from the file header.
        header: Parsed key-value header dictionary.

    Returns:
        The inferred material category, defaulting to ``MINERAL``.
    """
    sample_type = header.get("Type", header.get("Class", "")).lower()

    # First pass: match against the explicit Type/Class header
    for keyword, category in BISHOP_CATEGORY_MAP.items():
        if keyword in sample_type:
            return category

    # Second pass: match whole words in the sample name
    name_lower = name.lower()
    for keyword, category in BISHOP_CATEGORY_MAP.items():
        if re.search(rf"\b{re.escape(keyword)}\b", name_lower):
            return category

    return MaterialCategory.MINERAL


def _detect_wavelength_unit(
    wavelengths: list[float],
) -> WavelengthUnit:
    """Infer the wavelength unit from the data range.

    Bishop spectra may use nm or um depending on the instrument/file.
    Values exceeding 100 are assumed to be nanometers.

    Args:
        wavelengths: List of wavelength values from the parsed data.

    Returns:
        The inferred wavelength unit.
    """
    if not wavelengths:
        return WavelengthUnit.MICROMETERS
    max_wl = max(wavelengths)
    if max_wl > 100:
        # Values > 100 are likely nanometers
        return WavelengthUnit.NANOMETERS
    return WavelengthUnit.MICROMETERS


def parse_bishop_file(filepath: Path) -> SpectrumRecord:
    """Parse a single Bishop spectral library file.

    Bishop files are tab-separated text with wavelength in the first
    column and reflectance in the second column. Header lines may
    contain metadata.

    Args:
        filepath: Path to the Bishop spectrum text file.

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

        # Try parsing as numeric data
        parts = re.split(r"[\t,\s]+", stripped)
        if len(parts) >= 2:
            try:
                float(parts[0])
                float(parts[1])
                data_start = i
                break
            except ValueError:
                pass

        # Parse header — strip comment prefix before checking for ':'
        if stripped.startswith("#"):
            content = stripped.lstrip("#").strip()
            if ":" in content:
                key, _, value = content.partition(":")
                header[key.strip()] = value.strip()
        elif ":" in stripped:
            key, _, value = stripped.partition(":")
            header[key.strip()] = value.strip()

    # Parse spectral data
    wavelengths: list[float] = []
    values: list[float] = []

    for line in lines[data_start:]:
        parts = re.split(r"[\t,\s]+", line.strip())
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

    wl_unit = _detect_wavelength_unit(wavelengths)
    name = header.get("Name", header.get("Sample", filepath.stem))
    sample_id = header.get("Sample ID", header.get("ID", None))
    origin = header.get("Origin", header.get("Locality", None))
    formula = header.get("Formula", header.get("Composition", None))
    description = header.get("Description", None)

    category = _classify_bishop(name, header)
    original_id = filepath.stem

    return SpectrumRecord(
        id=spectrum_id(COLLECTION_ID, original_id),
        collection=COLLECTION_ID,
        original_id=original_id,
        name=name,
        license=BISHOP_LICENSE,
        material=Material(
            name=name,
            category=category,
            formula=formula,
            keywords=[name.lower(), "bishop"],
        ),
        sample=Sample(
            id=sample_id,
            description=description,
            origin=origin,
            owner="SETI Institute / J. Bishop",
        ),
        measurement=Measurement(
            quantity=Quantity.REFLECTANCE,
            geometry=BISHOP_GEOMETRY,
            laboratory="SETI Institute",
        ),
        spectral_data=SpectralData(
            wavelength_unit=wl_unit,
            wavelengths=wavelengths,
            values=values,
        ),
        extra=axis_extra,
    )


class BishopLoader(BaseLoader):
    """Loader for the Bishop Spectral Library."""

    @property
    def supports_auto_download(self) -> bool:
        """Bishop library requires manual download from the SETI Institute."""
        return False

    def source_name(self) -> str:
        """Return the collection id."""
        return COLLECTION_ID

    def collection_info(self) -> CollectionInfo:
        """Return the Bishop Spectral Library collection metadata.

        Returns:
            The footer ``collection`` object.
        """
        return BISHOP_COLLECTION

    def download(self, target_dir: Path) -> Path:
        """Download Bishop Spectral Library data.

        Data is available from the SETI Institute:
        https://dmp.seti.org/jbishop/spectral-library.html
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Bishop library data should be placed in %s. "
            "Download from: "
            "https://dmp.seti.org/jbishop/spectral-library.html",
            target_dir,
        )
        return target_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse Bishop spectral files from the source directory."""
        spectrum_files = sorted(
            f
            for f in source_dir.rglob("*.txt")
            if not any(kw in f.name.lower() for kw in ["readme", "catalog", "index", "description"])
        )
        spectrum_files.extend(sorted(source_dir.rglob("*.asc")))
        spectrum_files.extend(sorted(source_dir.rglob("*.csv")))

        logger.info("Found %d Bishop spectrum files", len(spectrum_files))

        for filepath in tqdm(spectrum_files, desc="Processing Bishop"):
            try:
                yield parse_bishop_file(filepath)
            except Exception:
                logger.warning("Failed to parse %s", filepath, exc_info=True)
