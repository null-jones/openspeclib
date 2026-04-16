"""Loader for the RELAB Spectral Database (Brown University / NASA).

RELAB (Reflectance Experiment Laboratory) provides bidirectional
reflectance spectra of minerals, rocks, meteorites, and lunar samples.
Data is distributed as ASCII tab-delimited files through the PDS
Geosciences Node.

Reference: https://sites.brown.edu/relab/
"""

import logging
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

RELAB_SOURCE = Source(
    library=SourceLibrary.RELAB,
    library_version="2.0",
    original_id="",
    url="https://pds-geosciences.wustl.edu/spectrallibrary/default.htm",
    license="Public Domain",
    citation=(
        "Pieters, C.M. and Hiroi, T., RELAB (Reflectance Experiment Laboratory): "
        "A NASA Multiuser Spectroscopy Facility. "
        "Lunar and Planetary Science Conference, Abstract #2196."
    ),
)

RELAB_SOURCE_INFO_EXTRA = {
    "license_url": None,
    "citation_doi": None,
}

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

    return SpectrumRecord(
        id=f"relab:{original_id}",
        name=name,
        source=RELAB_SOURCE.model_copy(
            update={"original_id": original_id, "filename": filepath.name}
        ),
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
            instrument="RELAB bidirectional spectrometer",
            instrument_type="laboratory_spectrometer",
            laboratory="RELAB, Brown University",
            technique=MeasurementTechnique.REFLECTANCE,
            geometry="bidirectional (i=30, e=0)",
        ),
        spectral_data=SpectralData(
            type=MeasurementTechnique.REFLECTANCE,
            wavelength_unit=WavelengthUnit.MICROMETERS,
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


class RelabLoader(BaseLoader):
    """Loader for the RELAB Spectral Database."""

    @property
    def supports_auto_download(self) -> bool:
        """RELAB requires manual download from the PDS Geosciences Node."""
        return False

    def source_name(self) -> str:
        """Return the source library identifier."""
        return "relab"

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
