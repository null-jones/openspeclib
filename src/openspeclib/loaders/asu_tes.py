"""Loader for the ASU Thermal Emission Spectral Library.

The ASU (Arizona State University) TES library provides thermal infrared
emission spectra of rock-forming minerals, measured at the Thermal Emission
Spectroscopy Laboratory. Spectra cover the 5--45 um (2000--220 cm-1) range.

Reference: http://tes.asu.edu/spectral/library/
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

ASU_TES_SOURCE = Source(
    library=SourceLibrary.ASU_TES,
    library_version="2.0",
    original_id="",
    url="https://speclib.asu.edu",
    license="Public Domain",
    citation=(
        "Christensen, P.R., Bandfield, J.L., Hamilton, V.E., Howard, D.A., "
        "Lane, M.D., Piatek, J.L., Ruff, S.W., and Stefanov, W.L., 2000, "
        "A thermal emission spectral library of rock-forming minerals: "
        "Journal of Geophysical Research, v. 105, no. E4, p. 9735-9739."
    ),
)

ASU_TES_SOURCE_INFO_EXTRA = {
    "license_url": None,
    "citation_doi": "10.1029/1999JE001138",
}

ASU_MINERAL_GROUP_MAP: dict[str, str] = {
    "silicate": "silicate",
    "carbonate": "carbonate",
    "sulfate": "sulfate",
    "oxide": "oxide",
    "phosphate": "phosphate",
    "halide": "halide",
    "sulfide": "sulfide",
    "hydroxide": "hydroxide",
    "feldspar": "tectosilicate",
    "pyroxene": "inosilicate",
    "olivine": "nesosilicate",
    "amphibole": "inosilicate",
    "mica": "phyllosilicate",
    "quartz": "tectosilicate",
    "clay": "phyllosilicate",
}


def _detect_mineral_group(name: str) -> str | None:
    """Try to identify the mineral group from the sample name.

    Args:
        name: Sample name to classify.

    Returns:
        Mineral group string (e.g. ``"tectosilicate"``), or ``None``.
    """
    name_lower = name.lower()
    for keyword, group in ASU_MINERAL_GROUP_MAP.items():
        if keyword in name_lower:
            return group
    return None


def parse_asu_tes_file(filepath: Path) -> SpectrumRecord:
    """Parse a single ASU TES spectrum file.

    ASU TES files are typically two-column ASCII (wavenumber, emissivity)
    with optional header lines.

    Args:
        filepath: Path to the ASU TES spectrum text file.

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
        parts = stripped.split()
        if len(parts) >= 2:
            try:
                float(parts[0])
                float(parts[1])
                data_start = i
                break
            except ValueError:
                pass

        # Parse header — strip comment prefix before checking for ':'
        if stripped.startswith("#") or stripped.startswith("!"):
            content = stripped.lstrip("#!").strip()
            if ":" in content:
                key, _, value = content.partition(":")
                header[key.strip()] = value.strip()
        elif ":" in stripped:
            key, _, value = stripped.partition(":")
            header[key.strip()] = value.strip()

    # Parse spectral data — ASU TES uses wavenumber (cm-1) vs emissivity
    wavenumbers: list[float] = []
    values: list[float] = []

    for line in lines[data_start:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            try:
                wn = float(parts[0])
                v = float(parts[1])
                wavenumbers.append(wn)
                values.append(v)
            except ValueError:
                continue

    if not wavenumbers:
        raise ValueError(f"No spectral data found in {filepath}")

    name = header.get("Name", header.get("Sample", filepath.stem))
    sample_id = header.get("Sample ID", header.get("ID", filepath.stem))
    origin = header.get("Origin", header.get("Locality", None))
    description = header.get("Description", None)
    formula = header.get("Formula", header.get("Composition", None))

    mineral_group = _detect_mineral_group(name)
    original_id = filepath.stem

    return SpectrumRecord(
        id=f"asu_tes:{original_id}",
        name=name,
        source=ASU_TES_SOURCE.model_copy(
            update={"original_id": original_id, "filename": filepath.name}
        ),
        material=Material(
            name=name,
            category=MaterialCategory.MINERAL,
            subcategory=mineral_group,
            formula=formula,
            keywords=[name.lower(), "thermal", "emissivity"],
        ),
        sample=Sample(
            id=sample_id,
            description=description,
            origin=origin,
            owner="ASU Thermal Emission Spectroscopy Laboratory",
        ),
        measurement=Measurement(
            instrument="Mattson Cygnus 100 FTIR spectrometer",
            instrument_type="ftir",
            laboratory="ASU Thermal Emission Spectroscopy Laboratory",
            technique=MeasurementTechnique.EMISSIVITY,
        ),
        spectral_data=SpectralData(
            type=MeasurementTechnique.EMISSIVITY,
            wavelength_unit=WavelengthUnit.WAVENUMBERS,
            wavelength_min=min(wavenumbers),
            wavelength_max=max(wavenumbers),
            num_points=len(wavenumbers),
            wavelengths=wavenumbers,
            values=values,
        ),
        quality=Quality(
            has_bad_bands=False,
            bad_band_count=0,
            coverage_fraction=1.0,
        ),
    )


class AsuTesLoader(BaseLoader):
    """Loader for the ASU Thermal Emission Spectral Library."""

    @property
    def supports_auto_download(self) -> bool:
        """ASU TES requires manual download from the ASU web interface."""
        return False

    def source_name(self) -> str:
        """Return the source library identifier."""
        return "asu_tes"

    def download(self, target_dir: Path) -> Path:
        """Download ASU TES spectral data.

        Data is available through the ASU spectral library web interface:
        https://speclib.asu.edu
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "ASU TES data should be placed in %s. " "Download from: https://speclib.asu.edu",
            target_dir,
        )
        return target_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse ASU TES ASCII files from the source directory."""
        spectrum_files = sorted(
            f
            for f in source_dir.rglob("*.txt")
            if not any(kw in f.name.lower() for kw in ["readme", "catalog", "index", "description"])
        )
        spectrum_files.extend(sorted(source_dir.rglob("*.asc")))
        spectrum_files.extend(sorted(source_dir.rglob("*.csv")))

        logger.info("Found %d ASU TES spectrum files", len(spectrum_files))

        for filepath in tqdm(spectrum_files, desc="Processing ASU TES"):
            try:
                yield parse_asu_tes_file(filepath)
            except Exception:
                logger.warning("Failed to parse %s", filepath, exc_info=True)
