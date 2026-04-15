"""Loader for the ECOSTRESS Spectral Library.

The ECOSTRESS library stores each spectrum as an individual text file
with a header section of key-value metadata followed by two-column
(wavelength, value) spectral data.

Reference: https://speclib.jpl.nasa.gov
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
    "lunar": MaterialCategory.LUNAR,
}

ECOSTRESS_TECHNIQUE_MAP: dict[str, MeasurementTechnique] = {
    "reflectance": MeasurementTechnique.REFLECTANCE,
    "directional hemispherical reflectance": MeasurementTechnique.REFLECTANCE,
    "hemispherical reflectance": MeasurementTechnique.REFLECTANCE,
    "emissivity": MeasurementTechnique.EMISSIVITY,
    "transmittance": MeasurementTechnique.TRANSMITTANCE,
    "absorbance": MeasurementTechnique.ABSORBANCE,
}

ECOSTRESS_SOURCE = Source(
    library=SourceLibrary.ECOSTRESS,
    library_version="1.0",
    original_id="",  # filled per spectrum
    url="https://speclib.jpl.nasa.gov",
    license="Public Domain",
    citation=(
        "Meerdink, S.K., Hook, S.J., Roberts, D.A., Abbott, E.A., 2019. "
        "The ECOSTRESS spectral library version 1.0. Remote Sensing of Environment, 230, 111196."
    ),
)


def _normalize_category(raw: str) -> MaterialCategory:
    """Map an ECOSTRESS class string to a canonical MaterialCategory.

    Args:
        raw: Raw class string from the ECOSTRESS header.

    Returns:
        The matching material category, or ``OTHER`` if unrecognised.
    """
    key = re.sub(r"[\s\-_]+", "", raw.strip().lower())
    return ECOSTRESS_CATEGORY_MAP.get(key, MaterialCategory.OTHER)


def _parse_technique(raw: str) -> MeasurementTechnique:
    """Map an ECOSTRESS measurement string to a MeasurementTechnique.

    Args:
        raw: Raw measurement type string from the ECOSTRESS header.

    Returns:
        The matching measurement technique, defaulting to ``REFLECTANCE``.
    """
    key = raw.strip().lower()
    return ECOSTRESS_TECHNIQUE_MAP.get(key, MeasurementTechnique.REFLECTANCE)


def parse_ecostress_file(filepath: Path) -> SpectrumRecord:
    """Parse a single ECOSTRESS spectrum text file.

    Args:
        filepath: Path to a .spectrum.txt file.

    Returns:
        A SpectrumRecord with all available metadata and spectral data.
    """
    text = filepath.read_text(encoding="utf-8", errors="replace")
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

    if not wavelengths:
        raise ValueError(f"No spectral data found in {filepath}")

    # Extract metadata from header
    name = header.get("Name", filepath.stem)
    sample_id = header.get("SampleNo", header.get("Sample No", None))
    raw_class = header.get("Class", header.get("Type", "other"))
    subcategory = header.get("SubClass", header.get("Subclass", None))
    particle_size = header.get("ParticleSize", header.get("Particle Size", None))
    origin = header.get("Origin", None)
    description = header.get("Description", None)
    measurement_type = header.get("Measurement", header.get("YUnits", "reflectance"))
    owner = header.get("Owner", None)

    technique = _parse_technique(measurement_type)
    category = _normalize_category(raw_class)
    original_id = filepath.stem.replace(".spectrum", "")

    return SpectrumRecord(
        id=f"ecostress:{original_id}",
        name=name,
        source=ECOSTRESS_SOURCE.model_copy(
            update={"original_id": original_id, "filename": filepath.name}
        ),
        material=Material(
            name=name,
            category=category,
            subcategory=subcategory,
            keywords=[kw for kw in [name.lower(), raw_class.lower(), subcategory] if kw],
        ),
        sample=Sample(
            id=sample_id,
            description=description,
            particle_size=particle_size,
            origin=origin,
            owner=owner,
        ),
        measurement=Measurement(
            technique=technique,
        ),
        spectral_data=SpectralData(
            type=technique,
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


class EcostressLoader(BaseLoader):
    """Loader for the ECOSTRESS Spectral Library."""

    @property
    def supports_auto_download(self) -> bool:
        """ECOSTRESS requires manual download from the JPL web portal."""
        return False

    def source_name(self) -> str:
        """Return the source library identifier."""
        return "ecostress"

    def download(self, target_dir: Path) -> Path:
        """Prepare the ECOSTRESS data directory.

        The ECOSTRESS download portal uses a JavaScript-based cart system
        and does not provide a direct bulk download URL. Users must download
        the data manually from https://speclib.jpl.nasa.gov/download and
        place the spectrum files in the target directory.

        Args:
            target_dir: Directory where ECOSTRESS data should be placed.

        Returns:
            Path to the data directory.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "ECOSTRESS data should be placed in %s. "
            "Download from: https://speclib.jpl.nasa.gov/download",
            target_dir,
        )
        return target_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse all ECOSTRESS spectrum files in the source directory.

        Yields SpectrumRecord for each .spectrum.txt or .txt file found.
        """
        # Find all spectrum files — ECOSTRESS uses various naming conventions
        spectrum_files = sorted(source_dir.rglob("*.spectrum.txt"))
        if not spectrum_files:
            # Fall back to any .txt files that look like spectra
            spectrum_files = sorted(source_dir.rglob("*.txt"))

        logger.info("Found %d ECOSTRESS spectrum files", len(spectrum_files))

        for filepath in tqdm(spectrum_files, desc="Processing ECOSTRESS"):
            try:
                yield parse_ecostress_file(filepath)
            except Exception:
                logger.warning("Failed to parse %s", filepath, exc_info=True)
