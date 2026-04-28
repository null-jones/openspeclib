"""Loader for the EcoSIS (Ecological Spectral Information System) spectral library.

EcoSIS is a NASA-supported collection of ecological spectral datasets hosted
at https://ecosis.org. Unlike monolithic spectral libraries, EcoSIS aggregates
individual researcher-contributed datasets, each with its own citation and
metadata. This loader ingests a curated subset of datasets defined in
``ecosis_datasets.json``.

Data is fetched via the EcoSIS REST API:
    - Dataset metadata: ``GET /api/package/{id}``
    - Spectra: ``GET /api/spectra/search/{id}?start=N&stop=M``

Reference: https://ecosis.org
"""

import json
import logging
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from tqdm import tqdm

from openspeclib.loaders.base import BaseLoader
from openspeclib.loaders.ecosis_scales import infer_dataset_divisor
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

ECOSIS_API_BASE = "https://ecosis.org/api"
DATASETS_FILE = Path(__file__).parent / "ecosis_datasets.json"
SPECTRA_PAGE_SIZE = 500

ECOSIS_CATEGORY_MAP: dict[str, MaterialCategory] = {
    "leaf": MaterialCategory.VEGETATION,
    "canopy": MaterialCategory.VEGETATION,
    "vegetation": MaterialCategory.VEGETATION,
    "flower": MaterialCategory.VEGETATION,
    "branch": MaterialCategory.NPV,
    "bark": MaterialCategory.NPV,
    "litter": MaterialCategory.NPV,
    "npv": MaterialCategory.NPV,
    "npv / litter": MaterialCategory.NPV,
    "npv / soil": MaterialCategory.SOIL,
    "soil": MaterialCategory.SOIL,
    "ground / soil": MaterialCategory.SOIL,
    "ground": MaterialCategory.SOIL,
    "water": MaterialCategory.WATER,
    "snow": MaterialCategory.SNOW_ICE,
    "mineral": MaterialCategory.MINERAL,
    "rock": MaterialCategory.ROCK,
    "gravel / rock": MaterialCategory.ROCK,
    "asphalt": MaterialCategory.MAN_MADE,
    "concrete": MaterialCategory.MAN_MADE,
    "brick/paver": MaterialCategory.MAN_MADE,
    "paint": MaterialCategory.MAN_MADE,
    "road/gravel": MaterialCategory.MAN_MADE,
    "other": MaterialCategory.OTHER,
    "reference": MaterialCategory.OTHER,
}

ECOSIS_TECHNIQUE_MAP: dict[str, MeasurementTechnique] = {
    "reflectance": MeasurementTechnique.REFLECTANCE,
    "transmittance": MeasurementTechnique.TRANSMITTANCE,
    "absorbance": MeasurementTechnique.ABSORBANCE,
    "absorptance": MeasurementTechnique.ABSORBANCE,
}


def _api_get(path: str, retries: int = 3) -> Any:
    """Fetch JSON from the EcoSIS API with retry logic.

    Args:
        path: API path (appended to ``ECOSIS_API_BASE``).
        retries: Number of retry attempts on transient failures.

    Returns:
        Parsed JSON response.

    Raises:
        urllib.error.URLError: If all retries are exhausted.
    """
    url = f"{ECOSIS_API_BASE}{path}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries - 1:
                wait = 2**attempt
                logger.warning("EcoSIS API request failed (%s), retrying in %ds...", exc, wait)
                time.sleep(wait)
            else:
                raise


def _load_curated_datasets() -> list[dict[str, str]]:
    """Load the curated dataset list from the JSON config file.

    Returns:
        List of ``{"id": ..., "name": ...}`` entries.
    """
    result: list[dict[str, str]] = json.loads(DATASETS_FILE.read_text(encoding="utf-8"))
    return result


def _classify_target_type(target_types: list[str]) -> MaterialCategory:
    """Map EcoSIS Target Type values to a canonical material category.

    Args:
        target_types: List of target type strings from the dataset/spectrum.

    Returns:
        The best-matching material category.
    """
    for raw in target_types:
        key = raw.strip().lower()
        if key in ECOSIS_CATEGORY_MAP:
            return ECOSIS_CATEGORY_MAP[key]
    return MaterialCategory.OTHER


def _detect_technique(
    measurement_quantities: list[str],
) -> MeasurementTechnique:
    """Map EcoSIS Measurement Quantity to a measurement technique.

    Args:
        measurement_quantities: Values from the dataset metadata.

    Returns:
        The matching measurement technique, defaulting to reflectance.
    """
    for raw in measurement_quantities:
        key = raw.strip().lower()
        if key in ECOSIS_TECHNIQUE_MAP:
            return ECOSIS_TECHNIQUE_MAP[key]
    return MeasurementTechnique.REFLECTANCE


def _parse_datapoints(
    datapoints: dict[str, str],
) -> tuple[list[float], list[float]]:
    """Extract wavelength and value arrays from an EcoSIS datapoints dict.

    EcoSIS returns datapoints as ``{"wavelength_nm": "value_str", ...}``
    with metadata keys mixed in. This function filters to numeric
    wavelength keys and returns sorted parallel arrays.

    Args:
        datapoints: Raw datapoints dict from the API.

    Returns:
        Tuple of ``(wavelengths, values)`` sorted by wavelength.

    Raises:
        ValueError: If no valid spectral data points are found.
    """
    pairs: list[tuple[float, float]] = []
    for key, val in datapoints.items():
        try:
            wl = float(key)
        except (ValueError, TypeError):
            continue
        try:
            v = float(val)
        except (ValueError, TypeError):
            continue
        # Skip NaN, Inf, and other non-finite values
        if not (math.isfinite(wl) and math.isfinite(v)):
            continue
        pairs.append((wl, v))

    if not pairs:
        raise ValueError("No valid spectral data points found in datapoints")

    pairs.sort(key=lambda p: p[0])
    wavelengths = [p[0] for p in pairs]
    values = [p[1] for p in pairs]
    return wavelengths, values


class EcosisLoader(BaseLoader):
    """Loader for the EcoSIS Ecological Spectral Information System."""

    def source_name(self) -> str:
        """Return the source library identifier."""
        return "ecosis"

    def download(self, target_dir: Path) -> Path:
        """Download curated EcoSIS datasets via the REST API.

        Fetches dataset metadata and all spectra for each dataset listed
        in ``ecosis_datasets.json``, saving them as JSON files in the
        target directory.

        Args:
            target_dir: Directory to save downloaded data into.

        Returns:
            Path to the EcoSIS data directory.
        """
        output_dir = target_dir / "ecosis"
        output_dir.mkdir(parents=True, exist_ok=True)

        datasets = _load_curated_datasets()
        logger.info("Downloading %d curated EcoSIS datasets", len(datasets))

        for entry in tqdm(datasets, desc="Downloading EcoSIS datasets"):
            dataset_id = entry["id"]
            dataset_name = entry["name"]
            output_file = output_dir / f"{dataset_id}.json"

            if output_file.exists():
                logger.info("Already downloaded: %s", dataset_name)
                continue

            logger.info("Fetching dataset: %s (%s)", dataset_name, dataset_id)

            # Fetch dataset metadata
            try:
                metadata = _api_get(f"/package/{dataset_id}")
            except Exception:
                logger.warning("Failed to fetch metadata for %s", dataset_name, exc_info=True)
                continue

            # Fetch all spectra with pagination
            spectra: list[dict[str, Any]] = []
            total = metadata.get("ecosis", {}).get("spectra_count", 0)
            offset = 0

            while offset < total:
                try:
                    stop = offset + SPECTRA_PAGE_SIZE
                    page = _api_get(f"/spectra/search/{dataset_id}?start={offset}&stop={stop}")
                    items = page.get("items", [])
                    if not items:
                        break
                    spectra.extend(items)
                    offset += len(items)
                except Exception:
                    logger.warning(
                        "Failed to fetch spectra page at offset %d for %s",
                        offset,
                        dataset_name,
                        exc_info=True,
                    )
                    break

            # Save combined metadata + spectra
            bundle = {
                "metadata": metadata,
                "spectra": spectra,
            }
            output_file.write_text(json.dumps(bundle), encoding="utf-8")
            logger.info("Saved %d spectra for %s", len(spectra), dataset_name)

        return output_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse downloaded EcoSIS dataset JSON files.

        Args:
            source_dir: Directory containing ``{dataset_id}.json`` files.

        Yields:
            SpectrumRecord for each valid spectrum.
        """
        json_files = sorted(source_dir.rglob("*.json"))
        if not json_files:
            logger.warning("No EcoSIS JSON files found in %s", source_dir)
            return

        logger.info("Loading %d EcoSIS dataset files", len(json_files))

        total_parsed = 0
        total_errors = 0

        for json_file in json_files:
            try:
                bundle = json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read %s: %s", json_file.name, exc)
                continue

            metadata = bundle.get("metadata", {})
            spectra_list = bundle.get("spectra", [])
            dataset_id = metadata.get("_id", json_file.stem)
            ecosis_meta = metadata.get("ecosis", {})
            dataset_title = ecosis_meta.get("package_title", json_file.stem)

            # Dataset-level metadata
            citation_list = metadata.get("Citation", [])
            citation = citation_list[0] if citation_list else ""
            organization = ecosis_meta.get("organization", "")
            author_list = metadata.get("Author", metadata.get("Author Email", []))
            authors = "; ".join(author_list) if author_list else ""
            dataset_target_types = metadata.get("Target Type", [])
            dataset_mq = metadata.get("Measurement Quantity", ["reflectance"])
            instrument_mfr = metadata.get("Instrument Manufacturer", [""])[0]
            instrument_model = metadata.get("Instrument Model", [""])[0]
            instrument = " ".join(filter(None, [instrument_mfr, instrument_model])) or None
            ecosystem = metadata.get("Ecosystem Type", [""])[0]

            technique = _detect_technique(dataset_mq)
            dataset_url = f"https://ecosis.org/package/{dataset_id}"

            source_template = Source(
                library=SourceLibrary.ECOSIS,
                library_version="1.0",
                original_id="",
                url=dataset_url,
                license="EcoSIS Data Use Policy",
                citation=citation,
            )

            # Pre-pass: parse every spectrum's datapoints so we can infer
            # the dataset's source reflectance scale from the actual data
            # before emitting any records. ECOSIS bundles are split per
            # dataset, so this fits comfortably in memory and avoids
            # mis-classifying individual spectra in isolation.
            parsed_spectra: list[tuple[dict[str, Any], list[float], list[float]]] = []
            for spectrum in spectra_list:
                datapoints = spectrum.get("datapoints", {})
                if not datapoints:
                    total_errors += 1
                    continue
                try:
                    wavelengths, values = _parse_datapoints(datapoints)
                except ValueError:
                    total_errors += 1
                    continue
                if len(wavelengths) < 2:
                    total_errors += 1
                    continue
                parsed_spectra.append((spectrum, wavelengths, values))

            source_divisor = infer_dataset_divisor([vals for (_, _, vals) in parsed_spectra])
            if source_divisor != 1:
                logger.info(
                    "Detected source scale 0-%d for ECOSIS dataset %s (%s); "
                    "dividing values by %d to normalise to the unit interval",
                    source_divisor,
                    dataset_title,
                    dataset_id,
                    source_divisor,
                )

            for spectrum, wavelengths, values in tqdm(
                parsed_spectra,
                desc=f"  {dataset_title[:50]}",
                leave=False,
            ):
                spectrum_id = spectrum.get("_id", "")

                # Normalise to the 0–1 convention shared with USGS /
                # ECOSTRESS so downstream consumers (notably the
                # viewer's shared y-axis) can plot them together.
                if source_divisor != 1:
                    values = [v / source_divisor for v in values]

                # Per-spectrum metadata (may override dataset-level)
                spec_target = spectrum.get("Target Type", "")
                target_types = [spec_target] if spec_target else dataset_target_types
                category = _classify_target_type(target_types)

                spec_mq = spectrum.get("Measurement Quantity", "")
                if spec_mq:
                    technique = _detect_technique([spec_mq])

                # Build material name from available fields
                common_name = spectrum.get("Common Name", "")
                latin_genus = spectrum.get("Latin Genus", "")
                latin_species = spectrum.get("Latin Species", "")
                spectra_label = spectrum.get("Spectra", "")

                if common_name:
                    material_name = common_name
                elif latin_genus:
                    material_name = f"{latin_genus} {latin_species}".strip()
                elif spectra_label:
                    material_name = spectra_label
                else:
                    material_name = spec_target or dataset_title

                # Sample info
                sample_id = spectrum.get("USDA Symbol", spectrum.get("Sample ID", None))
                sample_desc = spectrum.get("Sample Description", None)

                # Build keywords
                keywords: list[str] = []
                if common_name:
                    keywords.append(common_name.lower())
                if latin_genus:
                    keywords.append(latin_genus.lower())
                if latin_species:
                    keywords.append(latin_species.lower())
                if ecosystem:
                    keywords.append(ecosystem.lower())

                # Additional properties for dataset-level traceability
                additional: dict[str, Any] = {
                    "dataset_id": dataset_id,
                    "dataset_title": dataset_title,
                }
                if authors:
                    additional["dataset_authors"] = authors
                if organization:
                    additional["dataset_organization"] = organization
                # Preserve the pre-normalisation source scale when it
                # differs from the canonical 'unit' scale, for provenance.
                if source_divisor != 1:
                    additional["source_reflectance_divisor"] = source_divisor

                global_id = f"ecosis:{dataset_id}:{spectrum_id}"

                try:
                    record = SpectrumRecord(
                        id=global_id,
                        name=f"{material_name} ({dataset_title[:40]})",
                        source=source_template.model_copy(
                            update={
                                "original_id": spectrum_id,
                                "filename": None,
                            }
                        ),
                        material=Material(
                            name=material_name,
                            category=category,
                            subcategory=spec_target or None,
                            keywords=keywords,
                        ),
                        sample=Sample(
                            id=sample_id,
                            description=sample_desc,
                        ),
                        measurement=Measurement(
                            instrument=instrument,
                            technique=technique,
                            laboratory=organization or None,
                        ),
                        spectral_data=SpectralData(
                            type=technique,
                            wavelength_unit=WavelengthUnit.NANOMETERS,
                            wavelength_min=wavelengths[0],
                            wavelength_max=wavelengths[-1],
                            num_points=len(wavelengths),
                            wavelengths=wavelengths,
                            values=values,
                            reflectance_scale="unit",
                        ),
                        additional_properties=additional,
                        quality=Quality(
                            coverage_fraction=1.0,
                        ),
                    )
                    yield record
                    total_parsed += 1
                except Exception:
                    total_errors += 1
                    logger.warning(
                        "Failed to build record for %s in %s",
                        spectrum_id,
                        dataset_title,
                        exc_info=True,
                    )

        logger.info(
            "EcoSIS ingest summary: parsed=%d, errors=%d",
            total_parsed,
            total_errors,
        )
