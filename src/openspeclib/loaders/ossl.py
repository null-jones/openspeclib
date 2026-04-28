"""Loader for the Open Soil Spectral Library (OSSL).

OSSL is a global, open-access soil spectral library curated by the
Soil Spectroscopy for Global Good (SoilSpec4GG) consortium. It
aggregates ~80,000 soil samples drawn from KSSL (USDA), AfSIS,
ICRAF-ISRIC, NEON, LUCAS, and other sources, with paired VisNIR
reflectance (350–2500 nm at 2 nm) and MIR absorbance (600–4000 cm⁻¹
at 2 cm⁻¹) spectra plus rich site metadata (location, depth, soil
classification).

Data is distributed as compressed CSVs hosted on Google Cloud Storage:
    - ossl_visnir_L0_v1.2.csv.gz  — VNIR reflectance (one row per sample)
    - ossl_mir_L0_v1.2.csv.gz     — MIR absorbance (one row per sample)
    - ossl_soilsite_L0_v1.2.csv.gz — site metadata (lat/lon/depth/...)

Spectral columns are named ``scan_visnir.{nm}_ref`` and
``scan_mir.{cm-1}_abs`` respectively. The unique join key across all
tables is ``id.layer_uuid_txt``.

A single sample may have either VNIR, MIR, or both. The loader emits
**one SpectrumRecord per (sample, technique) pair**, with id
``ossl:{layer_uuid}:{vnir|mir}``.

License: CC-BY 4.0. Reference:
https://doi.org/10.1371/journal.pone.0296545
"""

from __future__ import annotations

import logging
import re
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pyarrow.csv as pa_csv
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

OSSL_VERSION = "L0_v1.2"
OSSL_BASE_URL = "https://storage.googleapis.com/soilspec4gg-public"

# We don't ingest the lab-chemistry table — only the spectral and site
# metadata tables are needed to materialise SpectrumRecord objects.
OSSL_FILES = {
    "visnir": f"ossl_visnir_{OSSL_VERSION}.csv.gz",
    "mir": f"ossl_mir_{OSSL_VERSION}.csv.gz",
    "soilsite": f"ossl_soilsite_{OSSL_VERSION}.csv.gz",
}

OSSL_LICENSE = "CC-BY 4.0"
OSSL_CITATION = (
    "Sanderman, J., Savage, K., Dangal, S.R.S., Duran, G., Rivard, C., "
    "Cavigelli, M.A., Gollany, H.T., Jastrow, J.D., Millar, N., "
    "Paustian, K., Rotz, A.C., Toosi, E.R., Wander, M.M., Wills, S.A., "
    "Hartemink, A.E., Vagen, T.-G., Walsh, M., et al. (2024). "
    "Open Soil Spectral Library (OSSL): Building reproducible soil "
    "calibration models through open development and community "
    "engagement. PLOS ONE, 19(1): e0296545."
)
OSSL_DOI = "10.1371/journal.pone.0296545"
OSSL_URL = "https://soilspectroscopy.github.io/ossl-manual/"

# Column-name patterns for the spectral channels.
_VNIR_PATTERN = re.compile(r"^scan_visnir\.([0-9]+(?:\.[0-9]+)?)_ref$")
_MIR_PATTERN = re.compile(r"^scan_mir\.([0-9]+(?:\.[0-9]+)?)_abs$")


def _download_one(url: str, dest: Path) -> None:
    """Stream a single OSSL CSV.gz to disk if it isn't already there.

    Args:
        url: HTTPS URL of the source file.
        dest: Destination path on disk.
    """
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("Already downloaded: %s", dest.name)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s -> %s", url, dest)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as f:
        while True:
            buf = resp.read(1024 * 1024)
            if not buf:
                break
            f.write(buf)
    tmp.rename(dest)


def _index_spectral_columns(
    schema: list[str],
    pattern: re.Pattern[str],
) -> list[tuple[str, float]]:
    """Find all spectral columns in a header and parse their wavelengths.

    Args:
        schema: Column names from the CSV header.
        pattern: Regex matching the spectral columns.

    Returns:
        Sorted list of ``(column_name, wavelength_or_wavenumber)``.
    """
    indexed: list[tuple[str, float]] = []
    for name in schema:
        m = pattern.match(name)
        if m:
            indexed.append((name, float(m.group(1))))
    indexed.sort(key=lambda t: t[1])
    return indexed


def _load_soilsite(path: Path) -> dict[str, dict[str, Any]]:
    """Load soilsite metadata as a dict keyed by layer_uuid.

    Args:
        path: Path to ``ossl_soilsite_*.csv.gz``.

    Returns:
        Mapping of ``id.layer_uuid_txt`` -> row dict (column -> value).
    """
    table = pa_csv.read_csv(path)
    rows = table.to_pylist()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        uuid = row.get("id.layer_uuid_txt")
        if uuid:
            out[uuid] = row
    logger.info("Loaded %d OSSL site metadata rows from %s", len(out), path.name)
    return out


def _build_record(
    *,
    layer_uuid: str,
    technique_short: str,
    technique: MeasurementTechnique,
    wavelength_unit: WavelengthUnit,
    wavelengths: list[float],
    values: list[float],
    site: dict[str, Any] | None,
) -> SpectrumRecord:
    """Construct a SpectrumRecord from one OSSL row.

    Args:
        layer_uuid: ``id.layer_uuid_txt`` for this sample.
        technique_short: Short tag used in the global id (``"vnir"`` / ``"mir"``).
        technique: Measurement technique enum value.
        wavelength_unit: Unit of the wavelength axis.
        wavelengths: Axis values, ascending.
        values: Spectral values aligned to ``wavelengths``.
        site: Row from soilsite for this layer, or ``None`` if absent.

    Returns:
        A populated ``SpectrumRecord``.
    """
    site = site or {}

    # USDA texture or pedon taxa as a soil subcategory when available
    subcategory = (
        site.get("layer.texture_usda_txt")
        or site.get("pedon.taxa_usda_txt")
        or site.get("layer.horizon_designation_txt")
        or None
    )
    sample_origin_parts = [
        str(site.get(k))
        for k in ("location.country_iso.3166_txt", "location.address_utf8_txt")
        if site.get(k)
    ]
    origin = " / ".join(sample_origin_parts) or None

    # Sample collection date can land in either of two columns depending
    # on the contributing source.
    obs_date = site.get("observation.date.begin_iso.8601_yyyy.mm.dd") or site.get(
        "observation.date.end_iso.8601_yyyy.mm.dd"
    )

    additional: dict[str, Any] = {"layer_uuid": layer_uuid}
    for k in (
        "dataset.code_ascii_txt",
        "dataset.title_utf8_txt",
        "location.longitude_wgs84_dd",
        "location.latitude_wgs84_dd",
        "layer.upper.depth_usda_cm",
        "layer.lower.depth_usda_cm",
    ):
        v = site.get(k)
        if v not in (None, ""):
            additional[k] = v

    name_parts = [str(additional.get("dataset.code_ascii_txt", "OSSL")), layer_uuid[:8]]
    if technique_short == "mir":
        name_parts.append("MIR")
    else:
        name_parts.append("VNIR")
    name = " ".join(name_parts)

    return SpectrumRecord(
        id=f"ossl:{layer_uuid}:{technique_short}",
        name=name,
        source=Source(
            library=SourceLibrary.OSSL,
            library_version=OSSL_VERSION,
            original_id=layer_uuid,
            url=OSSL_URL,
            license=OSSL_LICENSE,
            citation=OSSL_CITATION,
        ),
        material=Material(
            name="Soil",
            category=MaterialCategory.SOIL,
            subcategory=subcategory,
            keywords=["soil", "ossl"],
        ),
        sample=Sample(
            id=layer_uuid,
            origin=origin,
            collection_date=_parse_date(obs_date),
        ),
        measurement=Measurement(
            technique=technique,
            laboratory=str(site.get("dataset.code_ascii_txt") or "OSSL contributing lab"),
            date=_parse_date(obs_date),
        ),
        spectral_data=SpectralData(
            type=technique,
            wavelength_unit=wavelength_unit,
            wavelength_min=wavelengths[0],
            wavelength_max=wavelengths[-1],
            num_points=len(wavelengths),
            wavelengths=wavelengths,
            values=values,
            reflectance_scale="unit",
        ),
        additional_properties=additional,
        quality=Quality(coverage_fraction=1.0),
    )


def _parse_date(raw: Any) -> Any:
    """Parse an OSSL ISO-8601 date string, returning ``None`` on failure.

    Args:
        raw: Value from a date column (string, None, or other).

    Returns:
        A ``datetime.date`` if the value parses, else ``None``.
    """
    import datetime as _dt

    if not raw or not isinstance(raw, str):
        return None
    try:
        return _dt.date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def _iter_table_records(
    path: Path,
    *,
    pattern: re.Pattern[str],
    technique: MeasurementTechnique,
    technique_short: str,
    wavelength_unit: WavelengthUnit,
    sites: dict[str, dict[str, Any]],
    desc: str,
) -> Iterator[SpectrumRecord]:
    """Stream SpectrumRecord objects from one OSSL spectral CSV.

    Args:
        path: Path to ``ossl_{visnir|mir}_*.csv.gz``.
        pattern: Regex matching the spectral column names.
        technique: Measurement technique to stamp on each record.
        technique_short: Short tag for the global id (``"vnir"`` / ``"mir"``).
        wavelength_unit: Unit of the spectral axis.
        sites: ``layer_uuid`` -> site row mapping for metadata lookup.
        desc: Progress-bar label.

    Yields:
        A ``SpectrumRecord`` per row that has a non-empty wavelength axis.
    """
    # pyarrow.csv handles gzip transparently and is much faster than the
    # stdlib csv module on the wide OSSL tables (~1100+ columns each).
    table = pa_csv.read_csv(path)
    schema_names = table.schema.names
    spectral = _index_spectral_columns(schema_names, pattern)
    if not spectral:
        logger.warning("No spectral columns matched pattern in %s", path.name)
        return
    wavelengths = [w for (_, w) in spectral]
    spectral_cols = [c for (c, _) in spectral]

    n_rows = table.num_rows
    # Materialise just the columns we need as Python lists, in one pass.
    uuid_col = table.column("id.layer_uuid_txt").to_pylist()
    spectra_cols_data = [table.column(c).to_pylist() for c in spectral_cols]

    for i in tqdm(range(n_rows), desc=desc, leave=False):
        layer_uuid = uuid_col[i]
        if not layer_uuid:
            continue
        values: list[float] = []
        for col_data in spectra_cols_data:
            v = col_data[i]
            if v is None:
                values.append(float("nan"))
            else:
                try:
                    values.append(float(v))
                except (ValueError, TypeError):
                    values.append(float("nan"))
        # Skip rows that are entirely missing — common for samples that
        # exist in soilsite but were never measured on this instrument.
        if not any(v == v for v in values):  # all NaN
            continue
        try:
            yield _build_record(
                layer_uuid=layer_uuid,
                technique_short=technique_short,
                technique=technique,
                wavelength_unit=wavelength_unit,
                wavelengths=wavelengths,
                values=values,
                site=sites.get(layer_uuid),
            )
        except Exception:
            logger.warning(
                "Failed to build OSSL record for %s (%s)",
                layer_uuid,
                technique_short,
                exc_info=True,
            )


class OsslLoader(BaseLoader):
    """Loader for the Open Soil Spectral Library."""

    def source_name(self) -> str:
        """Return the canonical source identifier (matches :class:`SourceLibrary`)."""
        return "ossl"

    def download(self, target_dir: Path) -> Path:
        """Download the three OSSL CSV.gz files into ``target_dir/ossl/``.

        Args:
            target_dir: Directory to download into.

        Returns:
            Path to the OSSL data directory.
        """
        out = target_dir / "ossl"
        out.mkdir(parents=True, exist_ok=True)
        for filename in OSSL_FILES.values():
            _download_one(f"{OSSL_BASE_URL}/{filename}", out / filename)
        return out

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse the downloaded OSSL CSVs and yield SpectrumRecord objects.

        Yields one record per (sample, technique) pair: a sample with both
        VNIR and MIR measurements emits two records with ids
        ``ossl:{uuid}:vnir`` and ``ossl:{uuid}:mir``.

        Args:
            source_dir: Directory containing the OSSL CSV.gz files.

        Yields:
            A ``SpectrumRecord`` per VNIR or MIR measurement.
        """
        # Allow callers to point at either the parent target_dir or the
        # ossl/ subdirectory directly.
        base = source_dir / "ossl" if (source_dir / "ossl").is_dir() else source_dir
        soilsite_path = base / OSSL_FILES["soilsite"]
        if not soilsite_path.exists():
            logger.warning(
                "OSSL soilsite file not found at %s; site metadata will be empty",
                soilsite_path,
            )
            sites: dict[str, dict[str, Any]] = {}
        else:
            sites = _load_soilsite(soilsite_path)

        visnir_path = base / OSSL_FILES["visnir"]
        if visnir_path.exists():
            yield from _iter_table_records(
                visnir_path,
                pattern=_VNIR_PATTERN,
                technique=MeasurementTechnique.REFLECTANCE,
                technique_short="vnir",
                wavelength_unit=WavelengthUnit.NANOMETERS,
                sites=sites,
                desc="OSSL VNIR",
            )

        mir_path = base / OSSL_FILES["mir"]
        if mir_path.exists():
            yield from _iter_table_records(
                mir_path,
                pattern=_MIR_PATTERN,
                technique=MeasurementTechnique.ABSORBANCE,
                technique_short="mir",
                wavelength_unit=WavelengthUnit.WAVENUMBERS,
                sites=sites,
                desc="OSSL MIR",
            )


__all__ = ["OsslLoader", "OSSL_FILES", "OSSL_BASE_URL", "OSSL_VERSION"]
