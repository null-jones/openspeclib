"""Validation for OpenSpecLib output files.

Provides both JSON Schema validation and semantic validation
(cross-referencing catalog entries with chunk files, verifying
data consistency, checking for duplicates, etc.).
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from openspeclib import storage
from openspeclib.models import (
    CatalogFile,
    LicensesFile,
    MaterialCategory,
    MeasurementTechnique,
    WavelengthUnit,
)

logger = logging.getLogger(__name__)

SCHEMAS_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"


@dataclass
class ValidationResult:
    """Result of a validation run."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Return ``True`` if no errors were recorded."""
        return len(self.errors) == 0

    def summary(self) -> str:
        """Return a human-readable summary of validation results."""
        lines = []
        if self.errors:
            lines.append(f"{len(self.errors)} error(s):")
            for e in self.errors:
                lines.append(f"  ERROR: {e}")
        if self.warnings:
            lines.append(f"{len(self.warnings)} warning(s):")
            for w in self.warnings:
                lines.append(f"  WARN: {w}")
        if self.is_valid and not self.warnings:
            lines.append("Validation passed with no issues.")
        return "\n".join(lines)


def validate_schema(data: dict[str, object], schema_name: str) -> list[str]:
    """Validate a dictionary against a JSON Schema file.

    Args:
        data: The data to validate.
        schema_name: Filename of the schema in the schemas/ directory.

    Returns:
        List of validation error messages (empty if valid).
    """
    schema_path = SCHEMAS_DIR / schema_name
    if not schema_path.exists():
        return [f"Schema file not found: {schema_path}"]

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [
        (
            f"{'.'.join(str(p) for p in e.absolute_path)}: {e.message}"
            if e.absolute_path
            else e.message
        )
        for e in validator.iter_errors(data)
    ]


def validate_library(library_dir: Path) -> ValidationResult:
    """Run full validation on a built library directory.

    Performs both schema validation and semantic checks.

    Args:
        library_dir: Root directory of the built library.

    Returns:
        ValidationResult with all errors and warnings found.
    """
    result = ValidationResult()

    # Check required files exist
    catalog_path = library_dir / "catalog.json"
    version_path = library_dir / "VERSION"

    if not catalog_path.exists():
        result.errors.append("catalog.json not found")
        return result

    if not version_path.exists():
        result.warnings.append("VERSION file not found")

    # Load and validate catalog
    try:
        catalog_text = catalog_path.read_text(encoding="utf-8")
        catalog_data = json.loads(catalog_text)
    except (json.JSONDecodeError, OSError) as e:
        result.errors.append(f"Failed to read catalog.json: {e}")
        return result

    # Schema validation for catalog
    schema_errors = validate_schema(catalog_data, "catalog.schema.json")
    for err in schema_errors:
        result.errors.append(f"Catalog schema: {err}")

    # Parse catalog with Pydantic for semantic checks
    try:
        catalog = CatalogFile.model_validate(catalog_data)
    except Exception as e:
        result.errors.append(f"Catalog parse error: {e}")
        return result

    # Semantic checks
    _check_duplicate_ids(catalog, result)
    _check_chunk_files_exist(catalog, library_dir, result)
    _check_statistics_consistency(catalog, result)
    _check_enum_values(catalog, result)
    _validate_chunk_files(catalog, library_dir, result)
    _validate_licenses_file(catalog, library_dir, result)

    return result


def _check_duplicate_ids(catalog: CatalogFile, result: ValidationResult) -> None:
    """Check for duplicate spectrum IDs.

    Args:
        catalog: The parsed catalog to check.
        result: Validation result to append errors to.
    """
    id_counts = Counter(entry.id for entry in catalog.spectra)
    for spec_id, count in id_counts.items():
        if count > 1:
            result.errors.append(f"Duplicate spectrum ID: '{spec_id}' appears {count} times")


def _check_chunk_files_exist(
    catalog: CatalogFile, library_dir: Path, result: ValidationResult
) -> None:
    """Verify all referenced chunk files exist on disk.

    Args:
        catalog: The parsed catalog to check.
        library_dir: Root directory of the built library.
        result: Validation result to append errors to.
    """
    chunk_files = {entry.chunk_file for entry in catalog.spectra}
    for chunk_file in sorted(chunk_files):
        chunk_path = library_dir / chunk_file
        if not chunk_path.exists():
            result.errors.append(f"Referenced chunk file missing: {chunk_file}")


def _check_statistics_consistency(catalog: CatalogFile, result: ValidationResult) -> None:
    """Verify aggregate statistics match the actual entries.

    Args:
        catalog: The parsed catalog to check.
        result: Validation result to append errors/warnings to.
    """
    actual_total = len(catalog.spectra)
    if catalog.statistics.total_spectra != actual_total:
        result.errors.append(
            f"Statistics total_spectra ({catalog.statistics.total_spectra}) "
            f"does not match actual entry count ({actual_total})"
        )

    # Check per-source counts
    actual_source_counts: dict[str, int] = Counter()
    for entry in catalog.spectra:
        actual_source_counts[entry.source.library.value] += 1

    for source_name, info in catalog.sources.items():
        actual = actual_source_counts.get(source_name, 0)
        if info.spectrum_count != actual:
            result.errors.append(
                f"Source '{source_name}' count ({info.spectrum_count}) "
                f"does not match actual ({actual})"
            )

    # Check per-category counts
    actual_cat_counts: dict[str, int] = Counter()
    for entry in catalog.spectra:
        actual_cat_counts[entry.material.category.value] += 1

    for cat, count in catalog.statistics.categories.items():
        actual = actual_cat_counts.get(cat, 0)
        if count != actual:
            result.warnings.append(
                f"Category '{cat}' count ({count}) does not match actual ({actual})"
            )


def _check_enum_values(catalog: CatalogFile, result: ValidationResult) -> None:
    """Verify all enum values are from controlled vocabularies.

    Args:
        catalog: The parsed catalog to check.
        result: Validation result to append errors to.
    """
    valid_categories = {c.value for c in MaterialCategory}
    valid_techniques = {t.value for t in MeasurementTechnique}
    valid_units = {u.value for u in WavelengthUnit}

    for entry in catalog.spectra:
        if entry.material.category.value not in valid_categories:
            result.errors.append(
                f"Spectrum '{entry.id}': invalid category '{entry.material.category}'"
            )
        if entry.measurement.technique.value not in valid_techniques:
            result.errors.append(
                f"Spectrum '{entry.id}': invalid technique '{entry.measurement.technique}'"
            )
        if entry.spectral_data.wavelength_unit.value not in valid_units:
            result.errors.append(
                f"Spectrum '{entry.id}': invalid wavelength unit "
                f"'{entry.spectral_data.wavelength_unit}'"
            )


def _validate_chunk_files(
    catalog: CatalogFile, library_dir: Path, result: ValidationResult
) -> None:
    """Validate the content of each per-source Parquet file.

    Args:
        catalog: The parsed catalog whose chunk references to validate.
        library_dir: Root directory of the built library.
        result: Validation result to append errors to.
    """
    chunk_files = {entry.chunk_file for entry in catalog.spectra}
    catalog_ids = {entry.id for entry in catalog.spectra}

    for chunk_file in sorted(chunk_files):
        chunk_path = library_dir / chunk_file
        if not chunk_path.exists():
            continue

        # Schema validation against the canonical Arrow schema
        schema_errors = storage.validate_parquet_schema(chunk_path)
        for err in schema_errors:
            result.errors.append(f"Chunk '{chunk_file}' schema: {err}")
        if schema_errors:
            continue

        try:
            chunk = storage.read_chunk(chunk_path)
        except Exception as e:
            result.errors.append(f"Chunk '{chunk_file}' parse error: {e}")
            continue

        # Verify each spectrum in the chunk has a catalog entry
        for spectrum in chunk.spectra:
            if spectrum.id not in catalog_ids:
                result.errors.append(
                    f"Chunk '{chunk_file}': spectrum '{spectrum.id}' "
                    f"has no corresponding catalog entry"
                )

            # Verify array consistency
            sd = spectrum.spectral_data
            if len(sd.wavelengths) != sd.num_points:
                result.errors.append(
                    f"Spectrum '{spectrum.id}': wavelengths length ({len(sd.wavelengths)}) "
                    f"!= num_points ({sd.num_points})"
                )
            if len(sd.values) != sd.num_points:
                result.errors.append(
                    f"Spectrum '{spectrum.id}': values length ({len(sd.values)}) "
                    f"!= num_points ({sd.num_points})"
                )
            if sd.bandpass is not None and len(sd.bandpass) != sd.num_points:
                result.errors.append(
                    f"Spectrum '{spectrum.id}': bandpass length ({len(sd.bandpass)}) "
                    f"!= num_points ({sd.num_points})"
                )


def _validate_licenses_file(
    catalog: CatalogFile, library_dir: Path, result: ValidationResult
) -> None:
    """Validate the licenses.json file exists and is consistent with the catalog.

    Args:
        catalog: The parsed catalog for cross-referencing.
        library_dir: Root directory of the built library.
        result: Validation result to append errors/warnings to.
    """
    licenses_path = library_dir / "licenses.json"
    if not licenses_path.exists():
        result.warnings.append("licenses.json not found")
        return

    try:
        licenses_text = licenses_path.read_text(encoding="utf-8")
        licenses_data = json.loads(licenses_text)
    except (json.JSONDecodeError, OSError) as e:
        result.errors.append(f"Failed to read licenses.json: {e}")
        return

    # Schema validation
    schema_errors = validate_schema(licenses_data, "licenses.schema.json")
    for err in schema_errors:
        result.errors.append(f"Licenses schema: {err}")

    # Parse with Pydantic
    try:
        licenses = LicensesFile.model_validate(licenses_data)
    except Exception as e:
        result.errors.append(f"Licenses parse error: {e}")
        return

    # Cross-reference: every source in catalog should have a license entry
    for source_name in catalog.sources:
        if source_name not in licenses.sources:
            result.errors.append(
                f"Source '{source_name}' in catalog but missing from licenses.json"
            )

    # Every license entry should have non-empty license and citation
    for source_name, entry in licenses.sources.items():
        if not entry.license:
            result.warnings.append(f"licenses.json: '{source_name}' has empty license field")
        if not entry.citation:
            result.warnings.append(f"licenses.json: '{source_name}' has empty citation field")
