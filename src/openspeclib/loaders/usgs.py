"""Loader for the USGS Spectral Library Version 7 (Speclib 07).

The USGS library organizes spectra as ASCII text files within chapter
directories (by material type). Each spectrum file contains a single
column of reflectance values. Wavelength and bandpass axes are stored
in separate shared files per spectrometer.

The raw archive is mirrored on Hugging Face Datasets as a bit-identical
copy of the upstream USGS ScienceBase release; see ``USGS_HF_REPO`` below.

Reference: https://doi.org/10.5066/F7RR1WDJ
"""

import logging
import re
import zipfile
from pathlib import Path
from typing import Iterator

from huggingface_hub import hf_hub_download
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

# Raw archive is mirrored on Hugging Face Datasets. The file is the
# upstream ScienceBase ZIP, uploaded unmodified so its SHA-256 matches
# the original publication at https://doi.org/10.5066/F7RR1WDJ.
USGS_HF_REPO = "null-jones/usgs_splib07"
USGS_HF_FILENAME = "usgs_splib07.zip"
USGS_HF_REVISION = "main"

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

USGS_SOURCE_INFO_EXTRA = {
    "license_url": None,
    "citation_doi": "10.3133/ds1035",
}

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

    USGS splib07a/b filenames follow the pattern
    ``splib07a_<Material>[_<SampleID...>]_<Spectrometer>_<DataType>.txt``
    where ``<DataType>`` is the final segment (e.g. ``AREF``, ``RREF``,
    ``RTGC``, ``TRAN``) and ``<Spectrometer>`` is the segment immediately
    before it (e.g. ``BECKa``, ``ASDFRb``, ``NIC4a``, ``AVIRISb``).

    Args:
        filename: Spectrum filename (basename, not full path).

    Returns:
        Spectrometer code string, or ``"unknown"`` if not detected.
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 3:
        return parts[-2]
    return "unknown"


# Prefix -> wavelength-file family. All ASD sub-families (ASDFR/ASDHR/ASDNG)
# share one wavelength axis file named ``...Wavelengths_ASD_...``.
_WAVELENGTH_FAMILIES: tuple[tuple[str, str], ...] = (
    ("ASD", "asd"),
    ("AVIRIS", "aviris"),
    ("BECK", "beck"),
    ("NIC4", "nic4"),
)

# Prefix -> bandpass-file family. The ASD sub-families each have their own
# FWHM file (standard/high-res/next-gen) so they must be distinguished.
_BANDPASS_FAMILIES: tuple[tuple[str, str], ...] = (
    ("ASDFR", "asdfr"),
    ("ASDHR", "asdhr"),
    ("ASDNG", "asdng"),
    ("AVIRIS", "aviris"),
    ("BECK", "beck"),
    ("NIC4", "nic4"),
)


def _match_family(code: str, families: tuple[tuple[str, str], ...]) -> str | None:
    """Return the family token for ``code`` from a prefix mapping, or ``None``.

    Args:
        code: Spectrometer code (case-insensitive, e.g. ``"ASDFRa"``).
        families: Ordered ``(prefix, family_token)`` pairs. First prefix
            match wins, so more specific prefixes should come first.

    Returns:
        The matching family token, or ``None`` if no prefix matches.
    """
    upper = code.upper()
    for prefix, family in families:
        if upper.startswith(prefix):
            return family
    return None


def _parse_name_from_filename(filename: str) -> tuple[str, str]:
    """Extract a human-readable name and sample ID from a USGS filename.

    Parses the splib07a/b convention
    ``splib07a_<Material>[_<SampleID...>]_<Spectrometer>_<DataType>.txt``
    by stripping the library prefix and the trailing two segments.

    Args:
        filename: Spectrum filename (basename, not full path).

    Returns:
        Tuple of ``(material_name, sample_id)``. ``sample_id`` is the
        joined remaining segments (may be empty).
    """
    stem = Path(filename).stem
    parts = stem.split("_")

    # Strip library prefix (first) and <Spectrometer>_<DataType> (last two).
    if len(parts) >= 4:
        middle = parts[1:-2]
    elif len(parts) >= 3:
        middle = parts[1:-1]
    else:
        middle = parts

    if not middle:
        return stem, ""

    material_name = middle[0].replace("+", " ")
    sample_id = "_".join(middle[1:])
    return material_name, sample_id


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


def _library_prefix(filename: str) -> str:
    """Return the library prefix (first underscore-delimited token) of a filename.

    Args:
        filename: Spectrum or axis filename (basename, not full path).

    Returns:
        The leading token (e.g. ``"splib07a"``), or the full stem if no
        underscore is present.
    """
    return Path(filename).stem.split("_", 1)[0]


def _find_axis_file(
    spectrum_path: Path,
    source_dir: Path,
    glob_patterns: tuple[str, ...],
    family: str,
) -> Path | None:
    """Locate an axis file (wavelength or bandpass) for a spectrum.

    Matches are ranked by (1) same library prefix as the spectrum
    (e.g. both ``splib07a_``), then (2) any file whose stem contains the
    family token as a standalone underscore-delimited segment. The
    prefix preference is what keeps an ``splib07a`` spectrum from
    binding to an ``splib07b`` axis — an rglob-order hazard that
    silently truncated ingest on Linux filesystems.

    Args:
        spectrum_path: Path to the spectrum file whose axis is needed.
        source_dir: Root of the USGS data directory to search.
        glob_patterns: Filename glob patterns to enumerate candidates
            (e.g. ``("*[Ww]avelength*",)`` or
            ``("*[Bb]andpass*", "*[Ff]whm*")``).
        family: Lowercased family token to match inside the axis
            filename (e.g. ``"asd"``, ``"asdfr"``).

    Returns:
        The best-ranked matching axis file, or ``None`` if none found.
    """
    prefix = _library_prefix(spectrum_path.name)
    candidates: list[Path] = []
    for pattern in glob_patterns:
        candidates.extend(source_dir.rglob(pattern))

    preferred: Path | None = None
    fallback: Path | None = None
    for axis_file in candidates:
        tokens = axis_file.stem.lower().split("_")
        if family not in tokens:
            continue
        if axis_file.name.startswith(f"{prefix}_") and preferred is None:
            preferred = axis_file
        elif fallback is None:
            fallback = axis_file
    return preferred or fallback


def _find_wavelength_file(spectrum_path: Path, source_dir: Path) -> Path | None:
    """Find the wavelength file corresponding to a spectrum file.

    USGS stores wavelength files with names like
    ``splib07a_Wavelengths_ASD_0.35-2.5_microns_2151_ch.txt``. The
    instrument family (``ASD``, ``AVIRIS``, ``BECK``, ``NIC4``) appears
    as an underscore-delimited token, and all sub-family spectrometers
    (e.g. ``ASDFRa``, ``ASDHRb``, ``ASDNGc``) share the same axis.

    The glob is restricted to ``.txt`` to skip ``GIFplots/`` companion
    images (``splib07a_Wavelengths_*.gif``) which otherwise share the
    same prefix + family tokens and — with ``rglob`` ordering that is
    filesystem-dependent — can be picked ahead of the real axis file on
    Linux, silently collapsing spectra to a single garbage point.

    Args:
        spectrum_path: Path to the spectrum file.
        source_dir: Root of the USGS data directory to search.

    Returns:
        Path to the matching wavelength file, or ``None`` if not found.
    """
    spectrometer = _detect_spectrometer(spectrum_path.name)
    family = _match_family(spectrometer, _WAVELENGTH_FAMILIES)
    if family is None:
        return None
    return _find_axis_file(spectrum_path, source_dir, ("*[Ww]avelength*.txt",), family)


def _find_bandpass_file(spectrum_path: Path, source_dir: Path) -> Path | None:
    """Find the bandpass/FWHM file for the spectrometer.

    Unlike wavelengths, each ASD sub-family (``ASDFR``, ``ASDHR``,
    ``ASDNG``) has its own FWHM file at a different native resolution,
    so this function preserves that distinction. The glob is restricted
    to ``.txt`` for the same reason as :func:`_find_wavelength_file`.

    Args:
        spectrum_path: Path to the spectrum file.
        source_dir: Root of the USGS data directory to search.

    Returns:
        Path to the matching bandpass file, or ``None`` if not found.
    """
    spectrometer = _detect_spectrometer(spectrum_path.name)
    family = _match_family(spectrometer, _BANDPASS_FAMILIES)
    if family is None:
        return None
    return _find_axis_file(
        spectrum_path, source_dir, ("*[Bb]andpass*.txt", "*[Ff]whm*.txt"), family
    )


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
        """Download and extract the USGS Speclib 07 archive.

        Fetches the upstream ZIP from the Hugging Face dataset mirror at
        ``USGS_HF_REPO`` and extracts it into ``target_dir``. If the
        extracted directory already exists, the download and extraction
        steps are skipped — safe to call repeatedly from cached CI runs.

        Args:
            target_dir: Directory to download and extract into.

        Returns:
            Path to the extracted USGS data directory.

        Raises:
            RuntimeError: If the downloaded file is not a valid ZIP archive.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        extract_dir = target_dir / "usgs_splib07"

        # Fast path: extraction already done (e.g. restored from CI cache).
        if extract_dir.exists() and any(extract_dir.iterdir()):
            logger.info("USGS data already extracted at %s", extract_dir)
            return extract_dir

        logger.info(
            "Downloading USGS Speclib 07 from Hugging Face: %s (%s @ %s)",
            USGS_HF_REPO,
            USGS_HF_FILENAME,
            USGS_HF_REVISION,
        )
        archive_path = Path(
            hf_hub_download(
                repo_id=USGS_HF_REPO,
                filename=USGS_HF_FILENAME,
                repo_type="dataset",
                revision=USGS_HF_REVISION,
                local_dir=str(target_dir),
            )
        )

        if not zipfile.is_zipfile(archive_path):
            raise RuntimeError(
                f"File downloaded from {USGS_HF_REPO}/{USGS_HF_FILENAME} "
                f"is not a valid ZIP archive."
            )

        logger.info("Extracting USGS archive to %s", extract_dir)
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)

        return extract_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse USGS ASCII spectrum files.

        Searches for spectrum data files, matches them with wavelength
        files by spectrometer, and yields SpectrumRecord objects.
        """
        # Scope ingest to the splib07a main library (standard-resolution
        # canonical sampling). The splib07b archive and the various
        # ``..._cvAVIRIS*`` / ``..._cvASD*`` subdirectories are
        # re-convolved/resampled copies of the same underlying spectra;
        # including them would double-count. Errorbars files are the
        # per-band 1-sigma uncertainties, not spectra in their own right.
        all_txt = sorted(source_dir.rglob("*.txt"))
        spectrum_files = [
            f
            for f in all_txt
            if "errorbars" not in (p.lower() for p in f.parts)
            and not any(
                kw in f.name.lower()
                for kw in [
                    "wavelength",
                    "wavenumber",
                    "bandpass",
                    "fwhm",
                    "readme",
                    "description",
                ]
            )
            and re.match(r"splib07a_", f.name)
        ]

        logger.info("Found %d USGS spectrum files", len(spectrum_files))

        # Cache wavelength/bandpass arrays per spectrometer
        wl_cache: dict[str, list[float]] = {}
        bp_cache: dict[str, list[float] | None] = {}

        # Outcome tallies per spectrometer so the final summary makes silent
        # drops visible in CI logs even when tqdm reshuffles stderr output.
        skipped_no_wl: dict[str, int] = {}
        parsed: dict[str, int] = {}
        parse_errors: dict[str, int] = {}

        for filepath in tqdm(spectrum_files, desc="Processing USGS"):
            spectrometer = _detect_spectrometer(filepath.name)

            # Load wavelength file if not cached
            if spectrometer not in wl_cache:
                wl_file = _find_wavelength_file(filepath, source_dir)
                if wl_file:
                    wl_cache[spectrometer] = _read_single_column(wl_file)
                    logger.info(
                        "Using wavelength file %s for spectrometer %s (%d points)",
                        wl_file.name,
                        spectrometer,
                        len(wl_cache[spectrometer]),
                    )
                else:
                    logger.warning(
                        "No wavelength file found for spectrometer %s (e.g. %s)",
                        spectrometer,
                        filepath.name,
                    )
                    wl_cache[spectrometer] = []

                bp_file = _find_bandpass_file(filepath, source_dir)
                bp_cache[spectrometer] = _read_single_column(bp_file) if bp_file else None

            wavelengths = wl_cache[spectrometer]
            if not wavelengths:
                skipped_no_wl[spectrometer] = skipped_no_wl.get(spectrometer, 0) + 1
                continue

            try:
                yield parse_usgs_file(filepath, wavelengths, bp_cache[spectrometer], source_dir)
                parsed[spectrometer] = parsed.get(spectrometer, 0) + 1
            except Exception:
                parse_errors[spectrometer] = parse_errors.get(spectrometer, 0) + 1
                logger.warning("Failed to parse %s", filepath, exc_info=True)

        total_parsed = sum(parsed.values())
        total_skipped = sum(skipped_no_wl.values())
        total_errors = sum(parse_errors.values())
        logger.info(
            "USGS ingest summary: parsed=%d, skipped_no_wavelength=%d, parse_errors=%d",
            total_parsed,
            total_skipped,
            total_errors,
        )
        if skipped_no_wl:
            logger.warning(
                "Spectrometers with missing wavelength axis (dropped): %s",
                ", ".join(f"{spec}×{n}" for spec, n in sorted(skipped_no_wl.items())),
            )
