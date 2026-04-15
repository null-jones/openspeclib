"""Loader for the USGS Spectral Library Version 7 (Speclib 07).

The USGS library organizes spectra as ASCII text files within chapter
directories (by material type). Each spectrum file contains a single
column of reflectance values. Wavelength and bandpass axes are stored
in separate shared files per spectrometer.

Reference: https://doi.org/10.5066/F7RR1WDJ
"""

import logging
import re
import zipfile
from pathlib import Path
from typing import Iterator

import requests
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

USGS_DOWNLOAD_URL = "https://crustal.usgs.gov/speclab/QueryAll07a.php"

USGS_SOURCE = Source(
    library=SourceLibrary.USGS_SPLIB07,
    library_version="7a",
    original_id="",
    url="https://doi.org/10.5066/F7RR1WDJ",
    license="Public Domain",
    citation=(
        "Kokaly, R.F., Clark, R.N., Swayze, G.A., Livo, K.E., Hoefen, T.M., "
        "Pearson, N.C., Wise, R.A., Benzel, W.M., Lowers, H.A., Driscoll, R.L., "
        "and Klein, A.J., 2017, USGS Spectral Library Version 7: "
        "U.S. Geological Survey Data Series 1035, 61 p., https://doi.org/10.3133/ds1035."
    ),
)

BAD_VALUE = -1.23e34

USGS_CHAPTER_MAP: dict[str, MaterialCategory] = {
    "chapterlliquids": MaterialCategory.OTHER,
    "chaptermminerals": MaterialCategory.MINERAL,
    "chapterrrocks": MaterialCategory.ROCK,
    "chapterssoilsandmixtures": MaterialCategory.SOIL,
    "chapterssoils": MaterialCategory.SOIL,
    "chaptervvegetation": MaterialCategory.VEGETATION,
    "chapterwwater": MaterialCategory.WATER,
    "chapteriice": MaterialCategory.SNOW_ICE,
    "chapteraartificialmaterials": MaterialCategory.MAN_MADE,
    "chapteremeteorites": MaterialCategory.METEORITE,
    "chapterccoatings": MaterialCategory.MIXTURE,
    "chapteroorganiccompounds": MaterialCategory.ORGANIC_COMPOUND,
}


def _classify_from_path(filepath: Path) -> MaterialCategory:
    """Determine material category from the file's parent directory name.

    Args:
        filepath: Path to the spectrum file whose directory is inspected.

    Returns:
        The matching material category, or ``OTHER`` if not found.
    """
    for part in filepath.parts:
        key = re.sub(r"[\s\-_]+", "", part.strip().lower())
        for chapter_key, category in USGS_CHAPTER_MAP.items():
            if chapter_key in key:
                return category
    return MaterialCategory.OTHER


def _detect_spectrometer(filename: str) -> str:
    """Extract spectrometer abbreviation from the USGS filename convention.

    USGS filenames follow patterns like ``s07AV95a_Olivine_GDS70.a_ASD.txt``
    where the last underscore-delimited segment before ``.txt`` is the
    spectrometer code.

    Args:
        filename: Spectrum filename (basename, not full path).

    Returns:
        Spectrometer code string, or ``"unknown"`` if not detected.
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 2:
        return parts[-1]
    return "unknown"


def _parse_name_from_filename(filename: str) -> tuple[str, str]:
    """Extract a human-readable name and sample ID from a USGS filename.

    Args:
        filename: Spectrum filename (basename, not full path).

    Returns:
        Tuple of ``(material_name, sample_id)``.
    """
    stem = Path(filename).stem
    # Remove the leading prefix (e.g., "s07AV95a_") — alphanumeric only, stop at first _
    match = re.match(r"s\d+[A-Za-z0-9]+_(.+)", stem)
    if match:
        remainder = match.group(1)
    else:
        remainder = stem

    # The last segment is the spectrometer; remove it
    parts = remainder.rsplit("_", 1)
    name_part = parts[0] if len(parts) > 1 else remainder

    # Try to split into material name and sample ID
    # Pattern: MaterialName_SampleID (e.g., "Olivine_GDS70.a")
    name_parts = name_part.split("_", 1)
    material_name = name_parts[0].replace("+", " ")
    sample_id = name_parts[1] if len(name_parts) > 1 else None

    return material_name, sample_id or ""


def _read_single_column(filepath: Path) -> list[float]:
    """Read a single-column ASCII data file, skipping comment/header lines.

    Args:
        filepath: Path to the single-column text file.

    Returns:
        List of parsed float values.
    """
    values: list[float] = []
    for line in filepath.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        try:
            values.append(float(stripped))
        except ValueError:
            continue
    return values


def _find_wavelength_file(spectrum_path: Path, source_dir: Path) -> Path | None:
    """Find the wavelength file corresponding to a spectrum file.

    USGS stores wavelength files with names like
    ``s07_AV95_Wavelengths_ASD.txt``. This function searches the directory
    tree for a wavelength file matching the spectrometer code.

    Args:
        spectrum_path: Path to the spectrum file.
        source_dir: Root of the USGS data directory to search.

    Returns:
        Path to the matching wavelength file, or ``None`` if not found.
    """
    spectrometer = _detect_spectrometer(spectrum_path.name)
    # Search for wavelength files
    for wl_file in source_dir.rglob("*[Ww]avelength*"):
        if spectrometer.lower() in wl_file.stem.lower():
            return wl_file
    # Also check for files with "wlsall" or similar patterns
    for wl_file in source_dir.rglob("*wlsall*"):
        return wl_file
    return None


def _find_bandpass_file(spectrum_path: Path, source_dir: Path) -> Path | None:
    """Find the bandpass/FWHM file for the spectrometer.

    Args:
        spectrum_path: Path to the spectrum file.
        source_dir: Root of the USGS data directory to search.

    Returns:
        Path to the matching bandpass file, or ``None`` if not found.
    """
    spectrometer = _detect_spectrometer(spectrum_path.name)
    for bp_file in source_dir.rglob("*[Bb]andpass*"):
        if spectrometer.lower() in bp_file.stem.lower():
            return bp_file
    for bp_file in source_dir.rglob("*[Ff]whm*"):
        if spectrometer.lower() in bp_file.stem.lower():
            return bp_file
    return None


def parse_usgs_file(
    filepath: Path,
    wavelengths: list[float],
    bandpass: list[float] | None,
    source_dir: Path,
) -> SpectrumRecord:
    """Parse a single USGS spectrum ASCII file.

    Args:
        filepath: Path to the spectrum .txt file.
        wavelengths: Pre-loaded wavelength axis for this spectrometer.
        bandpass: Pre-loaded bandpass values, or None.
        source_dir: Root of the USGS data directory (for path-based classification).

    Returns:
        A SpectrumRecord.
    """
    values = _read_single_column(filepath)

    if not values:
        raise ValueError(f"No data values found in {filepath}")

    # Trim to match wavelength array length
    n = min(len(values), len(wavelengths))
    values = values[:n]
    wl = wavelengths[:n]
    bp = bandpass[:n] if bandpass and len(bandpass) >= n else None

    # Compute quality metrics
    bad_count = sum(1 for v in values if abs(v - BAD_VALUE) < 1e20)
    has_bad = bad_count > 0
    coverage = (n - bad_count) / n if n > 0 else 0.0

    material_name, sample_id = _parse_name_from_filename(filepath.name)
    category = _classify_from_path(filepath)
    spectrometer = _detect_spectrometer(filepath.name)
    original_id = filepath.stem

    return SpectrumRecord(
        id=f"usgs_splib07:{original_id}",
        name=f"{material_name} {sample_id}".strip(),
        source=USGS_SOURCE.model_copy(
            update={"original_id": original_id, "filename": filepath.name}
        ),
        material=Material(
            name=material_name,
            category=category,
            keywords=[material_name.lower()],
        ),
        sample=Sample(
            id=sample_id or None,
            description=f"{material_name}, measured with {spectrometer}",
        ),
        measurement=Measurement(
            instrument=spectrometer,
            technique=MeasurementTechnique.REFLECTANCE,
            laboratory="USGS Spectroscopy Lab",
        ),
        spectral_data=SpectralData(
            type=MeasurementTechnique.REFLECTANCE,
            wavelength_unit=WavelengthUnit.MICROMETERS,
            wavelength_min=min(wl),
            wavelength_max=max(wl),
            num_points=n,
            wavelengths=wl,
            values=values,
            bandpass=bp,
        ),
        quality=Quality(
            has_bad_bands=has_bad,
            bad_band_count=bad_count,
            coverage_fraction=round(coverage, 6),
        ),
    )


class UsgsLoader(BaseLoader):
    """Loader for the USGS Spectral Library Version 7."""

    def source_name(self) -> str:
        """Return the source library identifier."""
        return "usgs_splib07"

    def download(self, target_dir: Path) -> Path:
        """Download and extract the USGS Speclib 07 archive."""
        target_dir.mkdir(parents=True, exist_ok=True)
        archive_path = target_dir / "usgs_splib07.zip"

        if not archive_path.exists():
            logger.info("Downloading USGS Speclib 07...")
            resp = requests.get(USGS_DOWNLOAD_URL, stream=True, timeout=600)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with (
                open(archive_path, "wb") as f,
                tqdm(total=total, unit="B", unit_scale=True, desc="USGS Speclib 07") as bar,
            ):
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    bar.update(len(chunk))

        extract_dir = target_dir / "usgs_splib07"
        if not extract_dir.exists():
            logger.info("Extracting USGS archive...")
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(extract_dir)

        return extract_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse USGS ASCII spectrum files.

        Searches for spectrum data files, matches them with wavelength
        files by spectrometer, and yields SpectrumRecord objects.
        """
        # Find all potential spectrum files (exclude wavelength/bandpass files)
        all_txt = sorted(source_dir.rglob("*.txt"))
        spectrum_files = [
            f
            for f in all_txt
            if not any(
                kw in f.name.lower()
                for kw in ["wavelength", "bandpass", "fwhm", "readme", "description"]
            )
            and re.match(r"s\d+", f.name)
        ]

        logger.info("Found %d USGS spectrum files", len(spectrum_files))

        # Cache wavelength/bandpass arrays per spectrometer
        wl_cache: dict[str, list[float]] = {}
        bp_cache: dict[str, list[float] | None] = {}

        for filepath in tqdm(spectrum_files, desc="Processing USGS"):
            spectrometer = _detect_spectrometer(filepath.name)

            # Load wavelength file if not cached
            if spectrometer not in wl_cache:
                wl_file = _find_wavelength_file(filepath, source_dir)
                if wl_file:
                    wl_cache[spectrometer] = _read_single_column(wl_file)
                else:
                    logger.warning(
                        "No wavelength file found for spectrometer %s, skipping %s",
                        spectrometer,
                        filepath.name,
                    )
                    wl_cache[spectrometer] = []

                bp_file = _find_bandpass_file(filepath, source_dir)
                bp_cache[spectrometer] = _read_single_column(bp_file) if bp_file else None

            wavelengths = wl_cache[spectrometer]
            if not wavelengths:
                continue

            try:
                yield parse_usgs_file(filepath, wavelengths, bp_cache[spectrometer], source_dir)
            except Exception:
                logger.warning("Failed to parse %s", filepath, exc_info=True)
