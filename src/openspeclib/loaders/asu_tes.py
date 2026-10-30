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

COLLECTION_ID = "asu_tes"

ASU_TES_LICENSE = "LicenseRef-ASU-TES-ResearchUse"

ASU_TES_COLLECTION = CollectionInfo(
    id=COLLECTION_ID,
    title="ASU Thermal Emission Spectral Library",
    description=(
        "Thermal infrared emission spectra of rock-forming minerals (2000 to 220 cm-1) "
        "measured at the Arizona State University Thermal Emission Spectroscopy Laboratory."
    ),
    version="2.0",
    url="https://speclib.asu.edu",
    publisher="Arizona State University",
    license=ASU_TES_LICENSE,
    license_refs={
        ASU_TES_LICENSE: LicenseRef(
            name="ASU spectral library data: freely available for research use with citation",
            url="https://speclib.asu.edu",
        )
    },
    inclusion=Inclusion.SUBSET,
    inclusion_notes=(
        "Whatever spectra are placed in the ingest directory by hand (the source has no "
        "automatic download), so completeness depends on that download."
    ),
    citation=(
        "Christensen, P.R., Bandfield, J.L., Hamilton, V.E., Howard, D.A., "
        "Lane, M.D., Piatek, J.L., Ruff, S.W., and Stefanov, W.L., 2000, "
        "A thermal emission spectral library of rock-forming minerals: "
        "Journal of Geophysical Research, v. 105, no. E4, p. 9735-9739. "
        "https://doi.org/10.1029/1999JE001138."
    ),
)

# Every ASU TES spectrum is an emission measurement: the heated sample is
# viewed from above by the spectrometer's fore-optics and its radiance is
# ratioed to that of a blackbody at the sample temperature (Christensen et al.
# 2000, cited above; calibration per Ruff et al. 1997, J. Geophys. Res. 102,
# 14899-14913, which uses blackbody targets at two temperatures). That is a
# directional emissivity with no illumination geometry: ``directional``.
ASU_TES_GEOMETRY = Geometry.DIRECTIONAL
ASU_TES_REFERENCE = "blackbody"

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

    # Missing-value contract (see loaders.base): no NaN wavelengths, and
    # sentinel / non-finite values become NaN.
    wavenumbers, values = drop_invalid_wavelengths(wavenumbers, values)
    values = mask_invalid_values(values, sentinels=GENERIC_SENTINELS)
    wavenumbers, values, axis_extra = monotonic_axis(wavenumbers, values)

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
        id=spectrum_id(COLLECTION_ID, original_id),
        collection=COLLECTION_ID,
        original_id=original_id,
        name=name,
        license=ASU_TES_LICENSE,
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
            quantity=Quantity.EMISSIVITY,
            geometry=ASU_TES_GEOMETRY,
            instrument="Mattson Cygnus 100 FTIR spectrometer",
            instrument_type="ftir",
            laboratory="ASU Thermal Emission Spectroscopy Laboratory",
            reference=ASU_TES_REFERENCE,
        ),
        spectral_data=SpectralData(
            wavelength_unit=WavelengthUnit.WAVENUMBERS,
            wavelengths=wavenumbers,
            values=values,
        ),
        extra=axis_extra,
    )


class AsuTesLoader(BaseLoader):
    """Loader for the ASU Thermal Emission Spectral Library."""

    @property
    def supports_auto_download(self) -> bool:
        """ASU TES requires manual download from the ASU web interface."""
        return False

    def source_name(self) -> str:
        """Return the collection id."""
        return COLLECTION_ID

    def collection_info(self) -> CollectionInfo:
        """Return the ASU TES collection metadata.

        Returns:
            The footer ``collection`` object.
        """
        return ASU_TES_COLLECTION

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
