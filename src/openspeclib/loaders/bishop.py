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

from openspeclib.loaders.base import BaseLoader
from openspeclib.models import (
    Material,
    MaterialCategory,
    Measurement,
    MeasurementTechnique,
    Quality,
    Sample,
    Source,
    SourceLibrary,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
)

logger = logging.getLogger(__name__)

BISHOP_SOURCE = Source(
    library=SourceLibrary.BISHOP,
    library_version="1.0",
    original_id="",
    url="https://dmp.seti.org/jbishop/spectral-library.html",
    license="Public Domain (non-commercial use with citation)",
    citation=(
        "Bishop, J.L., Lane, M.D., Dyar, M.D., and Brown, A.J., "
        "Reflectance and emission spectroscopy study of four groups "
        "of phyllosilicates: smectites, kaolinite-serpentines, chlorites "
        "and micas. Clay Minerals, 43, 35-54, 2008."
    ),
)

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
        id=f"bishop:{original_id}",
        name=name,
        source=BISHOP_SOURCE.model_copy(
            update={"original_id": original_id, "filename": filepath.name}
        ),
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
            instrument_type="laboratory_spectrometer",
            laboratory="SETI Institute",
            technique=MeasurementTechnique.REFLECTANCE,
        ),
        spectral_data=SpectralData(
            type=MeasurementTechnique.REFLECTANCE,
            wavelength_unit=wl_unit,
            wavelength_min=min(wavelengths),
            wavelength_max=max(wavelengths),
            num_points=len(wavelengths),
            wavelengths=wavelengths,
            values=values,
        ),
        quality=Quality(
            has_bad_bands=False,
            bad_band_count=0,
            coverage_fraction=1.0,
        ),
    )


class BishopLoader(BaseLoader):
    """Loader for the Bishop Spectral Library."""

    @property
    def supports_auto_download(self) -> bool:
        """Bishop library requires manual download from the SETI Institute."""
        return False

    def source_name(self) -> str:
        """Return the source library identifier."""
        return "bishop"

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
