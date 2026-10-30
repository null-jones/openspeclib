"""Shared machinery of the format converters.

Every importer (ENVI, CSV) parses its input into :class:`SourceSpectrum`
objects: the arrays as numpy plus a dict of metadata already mapped to
Format v1 column names by :class:`FieldMapper`. :func:`build_records` then
turns them into validated :class:`~openspeclib.models.SpectrumRecord` objects:
it cleans the wavelength axis (rule F8), masks sentinels (F9), converts
scaled values to fractions (section 4.1), allocates ids (F5) and checks
licences (F11) and dataset references (F12).

Collection-level metadata (the footer ``collection`` object) comes from
command-line flags and/or a JSON file; :func:`build_collection_info` merges
them and reports every missing required field at once.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from openspeclib.conformance import check_spdx_expression
from openspeclib.loaders.base import FILL_VALUE_MAGNITUDE, GENERIC_SENTINELS, make_local_id
from openspeclib.models import (
    LOCAL_ID_MAX_LENGTH,
    CollectionInfo,
    DatasetInfo,
    Geometry,
    Material,
    MaterialCategory,
    Measurement,
    Quality,
    Quantity,
    Sample,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
    is_valid_local_id,
)

__all__ = [
    "ConversionError",
    "FieldMapper",
    "ImportDefaults",
    "ImportReport",
    "SourceSpectrum",
    "build_collection_info",
    "build_datasets",
    "build_records",
    "load_mapping_file",
    "load_metadata_json",
    "normalize_wavelength_unit",
]


class ConversionError(ValueError):
    """Raised when an input cannot be converted; the message says what to fix."""


# Numeric cell values that mean "no value" in spreadsheets and EcoSIS uploads.
# Numeric -9999/-999 are masked as sentinels after parsing (see _mask_values).
MISSING_TOKENS = frozenset({"", "na", "n/a", "nan", "null", "none", "#n/a"})

# Quantities whose values are fractions (spec section 4.1) and get rescaled.
FRACTION_QUANTITIES = frozenset({"reflectance", "transmittance", "emissivity"})

# ``extra`` key recording the divisor applied to a spectrum's values: the
# same key the loaders write (docs/processing-pipeline.md, CLAUDE.md).
DIVISOR_KEY = "source_reflectance_divisor"
# Key the converters wrote before 0.2.0; renamed to DIVISOR_KEY on re-import.
LEGACY_DIVISOR_KEY = "source_value_divisor"

# Footer ``collection`` fields that must be present, with the flag that sets each.
REQUIRED_COLLECTION_FIELDS: dict[str, str] = {
    "id": "--collection-id",
    "title": "--title",
    "license": "--license",
    "citation": "--citation",
}


# ---------------------------------------------------------------------------
# Vocabulary normalisation
# ---------------------------------------------------------------------------

_UNIT_ALIASES: dict[str, str] = {
    "nm": "nm",
    "nanometer": "nm",
    "nanometers": "nm",
    "nanometre": "nm",
    "nanometres": "nm",
    "um": "um",
    "µm": "um",
    "μm": "um",
    "micron": "um",
    "microns": "um",
    "micrometer": "um",
    "micrometers": "um",
    "micrometre": "um",
    "micrometres": "um",
    "cm-1": "cm-1",
    "cm^-1": "cm-1",
    "cm⁻¹": "cm-1",
    "1/cm": "cm-1",
    "wavenumber": "cm-1",
    "wavenumbers": "cm-1",
}


def normalize_wavelength_unit(raw: str | None) -> str | None:
    """Map a wavelength-unit label to the ``wavelength_unit`` vocabulary.

    Args:
        raw: A label such as ``"Nanometers"``, ``"microns"`` or ``"Wavenumber"``.

    Returns:
        ``"nm"``, ``"um"`` or ``"cm-1"``, or ``None`` when the label is not
        one of those units.
    """
    if raw is None:
        return None
    key = raw.strip().lower().replace(" ", "")
    return _UNIT_ALIASES.get(key)


def _vocab_key(raw: str) -> str:
    """Normalise free text for a vocabulary lookup.

    Args:
        raw: The text.

    Returns:
        Lower-case text with runs of spaces, hyphens and slashes as ``_``.
    """
    return re.sub(r"[\s\-/]+", "_", raw.strip().lower())


# Recognised quantity labels, keyed by :func:`_quantity_key`. Only whole
# labels are matched: a label that merely contains one of these words
# ("first derivative reflectance", "reflectance (log 1/R)") is a different
# quantity and must not be stored as this one.
_QUANTITY_ALIASES: dict[str, str] = {
    **{q.value: q.value for q in Quantity},
    # reflectance
    "reflectance_factor": "reflectance",
    "reflectivity": "reflectance",
    "spectral_reflectance": "reflectance",
    "relative_reflectance": "reflectance",
    "percent_reflectance": "reflectance",
    "reflectance_percent": "reflectance",
    "bidirectional_reflectance": "reflectance",
    "bidirectional_reflectance_factor": "reflectance",
    "hemispherical_reflectance": "reflectance",
    "directional_hemispherical_reflectance": "reflectance",
    "hemispherical_directional_reflectance": "reflectance",
    "biconical_reflectance": "reflectance",
    # transmittance
    "transmission": "transmittance",
    "transmissivity": "transmittance",
    "spectral_transmittance": "transmittance",
    "percent_transmittance": "transmittance",
    "transmittance_percent": "transmittance",
    "directional_hemispherical_transmittance": "transmittance",
    # emissivity
    "spectral_emissivity": "emissivity",
    # absorbance (decadic, spec section 4.1)
    "decadic_absorbance": "absorbance",
    "absorbance_decadic": "absorbance",
    "optical_density": "absorbance",
}


def _quantity_key(raw: str) -> str:
    """Normalise a quantity label for an exact lookup.

    Args:
        raw: The label.

    Returns:
        Lower-case text with every run of spaces and punctuation as one
        ``_``, trimmed (``"Reflectance (%)"`` → ``"reflectance"``).
    """
    return re.sub(r"[^0-9a-z]+", "_", raw.strip().lower()).strip("_")


def normalize_quantity(raw: str) -> str:
    """Map a quantity label to the ``quantity`` vocabulary.

    The label must be a vocabulary value or one of the recognised aliases
    (:data:`_QUANTITY_ALIASES`) after case, spaces and punctuation are
    normalised. Labels that only contain a quantity's name, such as
    derivative or log forms, are rejected rather than guessed.

    Args:
        raw: E.g. ``"Reflectance"``, ``"reflectance factor"``, ``"Emissivity"``.

    Returns:
        A ``quantity`` value.

    Raises:
        ConversionError: If the label names no supported quantity.
    """
    key = _quantity_key(raw)
    if key in _QUANTITY_ALIASES:
        return _QUANTITY_ALIASES[key]
    if "absorptance" in key:
        raise ConversionError(
            f"quantity {raw!r}: absorptance (1 - R - T) is not a Format v1 quantity"
        )
    raise ConversionError(
        f"unknown quantity {raw!r}; use one of {', '.join(q.value for q in Quantity)} "
        f"(derived forms such as derivatives or log(1/R) are not Format v1 quantities)"
    )


def normalize_geometry(raw: str) -> str:
    """Map a geometry label to the ``geometry`` vocabulary.

    Args:
        raw: E.g. ``"directional-hemispherical"`` or ``"Unknown"``.

    Returns:
        A ``geometry`` value.

    Raises:
        ConversionError: If the label is not in the vocabulary.
    """
    key = _vocab_key(raw)
    if key in {g.value for g in Geometry}:
        return key
    raise ConversionError(
        f"unknown geometry {raw!r}; use one of {', '.join(g.value for g in Geometry)}"
    )


_CATEGORY_ALIASES: dict[str, str] = {
    "minerals": "mineral",
    "rocks": "rock",
    "soils": "soil",
    "plant": "vegetation",
    "plants": "vegetation",
    "manmade": "man_made",
    "man_made_materials": "man_made",
    "artificial": "man_made",
    "snow": "snow_ice",
    "ice": "snow_ice",
    "organic": "organic_compound",
    "organics": "organic_compound",
    "organic_compounds": "organic_compound",
    "meteorites": "meteorite",
    "mixtures": "mixture",
}


def normalize_category(raw: str) -> str | None:
    """Map a category label to the ``material_category`` vocabulary.

    Recognises the vocabulary itself, common plurals and variants, and the
    EcoSIS ``Target Type`` values (leaf, canopy, bark, soil, …).

    Args:
        raw: The label.

    Returns:
        A ``material_category`` value, or ``None`` when the label is not
        recognised.
    """
    from openspeclib.loaders.ecosis import ECOSIS_CATEGORY_MAP

    key = _vocab_key(raw)
    if key in {c.value for c in MaterialCategory}:
        return key
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    ecosis = ECOSIS_CATEGORY_MAP.get(raw.strip().lower())
    return ecosis.value if ecosis is not None else None


def _is_category_label(raw: str) -> bool:
    """Tell whether a label is a ``material_category`` value or a variant spelling.

    Args:
        raw: The label.

    Returns:
        ``True`` for vocabulary values and :data:`_CATEGORY_ALIASES` keys.
    """
    key = _vocab_key(raw)
    return key in {c.value for c in MaterialCategory} or key in _CATEGORY_ALIASES


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------

# Format v1 columns a metadata field can be mapped to (arrays and derived
# columns excluded: ``collection`` and the nm extent are computed).
RECORD_COLUMNS: frozenset[str] = frozenset(
    {
        "original_id",
        "name",
        "material_name",
        "material_category",
        "quantity",
        "geometry",
        "wavelength_unit",
        "license",
        "dataset_id",
        "material_subcategory",
        "material_formula",
        "material_keywords",
        "sample_id",
        "sample_description",
        "sample_particle_size",
        "sample_origin",
        "sample_owner",
        "sample_collection_date",
        "sample_preparation",
        "measurement_instrument",
        "measurement_instrument_type",
        "measurement_laboratory",
        "measurement_date",
        "measurement_processing",
        "measurement_light_source",
        "measurement_venue",
        "measurement_acquisition_method",
        "measurement_foreoptic",
        "measurement_reference",
        "quality_notes",
    }
)

# Targets with special handling. ``id`` is a spectrum id or local id;
# ``extra`` is a JSON object merged into ``extra``; ``ignore`` drops the field;
# the ``_`` targets are EcoSIS fields combined into other columns.
SPECIAL_TARGETS: frozenset[str] = frozenset(
    {
        "id",
        "extra",
        "ignore",
        "_latin_genus",
        "_latin_species",
        "_instrument_manufacturer",
        "_instrument_model",
        "_processing_averaged",
        "_processing_interpolated",
        "_processing_resampled",
        "_value_units",
    }
)

# Targets used only by the long CSV layout (the per-point columns).
POINT_TARGETS: frozenset[str] = frozenset({"wavelength", "value", "bandpass_fwhm", "values_stddev"})

# Default mapping, keyed by normalised column name (see :func:`normalize_column`).
# Every Format v1 column name maps to itself; the rest are common spellings
# and the EcoSIS upload metadata names.
DEFAULT_ALIASES: dict[str, str] = {
    **{name: name for name in RECORD_COLUMNS},
    "id": "id",
    "extra": "extra",
    # identity
    "spectrum_id": "original_id",
    "spectra_id": "original_id",
    "sample_name": "name",
    "spectrum": "original_id",
    "spectra": "original_id",
    "spectrum_name": "name",
    "spectra_name": "name",
    "label": "name",
    # material
    "material": "material_name",
    "common_name": "material_name",
    "latin_genus": "_latin_genus",
    "latin_species": "_latin_species",
    "category": "material_category",
    "class": "material_category",
    "target_type": "material_category",
    "subcategory": "material_subcategory",
    "subclass": "material_subcategory",
    "formula": "material_formula",
    "chemical_formula": "material_formula",
    "keywords": "material_keywords",
    "keyword": "material_keywords",
    # measurement
    "measurement_quantity": "quantity",
    "wavelength_units": "wavelength_unit",
    "licence": "license",
    "dataset": "dataset_id",
    "description": "sample_description",
    "particle_size": "sample_particle_size",
    "grain_size": "sample_particle_size",
    "origin": "sample_origin",
    "locality": "sample_origin",
    "owner": "sample_owner",
    "collection_date": "sample_collection_date",
    "preparation": "sample_preparation",
    "instrument": "measurement_instrument",
    "instrument_manufacturer": "_instrument_manufacturer",
    "instrument_model": "_instrument_model",
    "instrument_type": "measurement_instrument_type",
    "laboratory": "measurement_laboratory",
    "lab": "measurement_laboratory",
    "date": "measurement_date",
    "processing": "measurement_processing",
    "processing_averaged": "_processing_averaged",
    "processing_interpolated": "_processing_interpolated",
    "processing_resampled": "_processing_resampled",
    "light_source": "measurement_light_source",
    "venue": "measurement_venue",
    "acquisition_method": "measurement_acquisition_method",
    "foreoptic": "measurement_foreoptic",
    "foreoptic_type": "measurement_foreoptic",
    "reference": "measurement_reference",
    "reference_standard": "measurement_reference",
    "notes": "quality_notes",
    "measurement_units": "_value_units",
    # derived or export-only columns, recomputed on import
    "collection": "ignore",
    "wavelength_min_nm": "ignore",
    "wavelength_max_nm": "ignore",
    "num_points": "ignore",
    "file": "ignore",
    "attribution": "ignore",
    "citation": "ignore",
}

# Columns holding several values in one cell, split on ``;`` or ``|``.
LIST_COLUMNS = frozenset({"material_keywords", "measurement_processing"})
DATE_COLUMNS = frozenset({"sample_collection_date", "measurement_date"})


def normalize_column(name: str) -> str:
    """Normalise a column or header-field name for mapping lookups.

    Args:
        name: E.g. ``"Common Name"``, ``"material-name"``, ``"Measurement Date "``.

    Returns:
        Lower-case snake case, e.g. ``"common_name"``.
    """
    return re.sub(r"[^0-9a-z]+", "_", name.strip().lower()).strip("_")


def _check_target(target: str, where: str) -> None:
    """Validate a mapping target.

    Args:
        target: The target name.
        where: Context for the error message.

    Raises:
        ConversionError: If the target is not a column, special target,
            point target or ``extra.<key>``.
    """
    if target in RECORD_COLUMNS or target in SPECIAL_TARGETS or target in POINT_TARGETS:
        return
    if target.startswith("extra.") and len(target) > len("extra."):
        return
    raise ConversionError(
        f"{where}: unknown mapping target {target!r}; use a Format v1 column name "
        f"(e.g. material_name, sample_id), 'id', 'extra', 'extra.<key>' or 'ignore'"
    )


def load_mapping_file(path: Path) -> dict[str, str]:
    """Read a JSON column-mapping file.

    The file is a JSON object from source column names to targets, e.g.
    ``{"Mineral": "material_name", "Grain": "sample_particle_size",
    "Operator": "extra.operator", "Junk": "ignore"}``.

    Args:
        path: The JSON file.

    Returns:
        The mapping.

    Raises:
        ConversionError: If the file is not a JSON object of strings or names
            an unknown target.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionError(f"cannot read mapping file {path}: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ConversionError(f"{path}: a mapping file must be a JSON object of strings")
    for target in data.values():
        _check_target(target, str(path))
    return dict(data)


def is_missing(value: Any) -> bool:
    """Tell whether a numeric cell means "no value".

    Only for numeric cells: metadata text such as ``"NA"`` (sodium) or
    ``"none"`` (no fore-optic) is a value (see :func:`is_blank`).

    Args:
        value: The raw value.

    Returns:
        ``True`` for ``None``, NaN, empty strings and the tokens in
        :data:`MISSING_TOKENS`.
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, str):
        return value.strip().casefold() in MISSING_TOKENS
    return False


def is_blank(value: Any) -> bool:
    """Tell whether a metadata value is absent.

    Args:
        value: The raw value.

    Returns:
        ``True`` for ``None``, NaN, empty or whitespace-only strings and
        empty lists.
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def add_source_metadata(extra: dict[str, Any], values: Mapping[str, Any]) -> None:
    """Merge fields into ``extra["source_metadata"]``.

    An existing ``source_metadata`` that is not an object (it can come from
    an ``extra`` cell) is kept under the key ``"extra.source_metadata"``
    inside the new object rather than overwritten.

    Args:
        extra: The record's ``extra`` object, updated in place.
        values: Source field name to value.
    """
    if not values:
        return
    current = extra.get("source_metadata")
    if not isinstance(current, dict):
        current = {} if current is None else {"extra.source_metadata": current}
        extra["source_metadata"] = current
    current.update(values)


class FieldMapper:
    """Maps source metadata fields to Format v1 columns.

    Lookup order: the user mapping (exact column name, then normalised
    name), then :data:`DEFAULT_ALIASES` by normalised name. Fields that map
    nowhere are kept under ``extra["source_metadata"]`` with their original
    names, so no source metadata is silently lost.
    """

    def __init__(self, overrides: Mapping[str, str] | None = None) -> None:
        """Create a mapper.

        Args:
            overrides: User mapping from source column names to targets.

        Raises:
            ConversionError: If an override names an unknown target.
        """
        self._exact: dict[str, str] = {}
        self._normalized: dict[str, str] = {}
        for column, target in (overrides or {}).items():
            _check_target(target, f"mapping for {column!r}")
            self._exact[column] = target
            self._normalized[normalize_column(column)] = target
        self.unmapped: set[str] = set()
        # (column, target) pairs whose value lost to an earlier column with
        # the same target and was kept in source_metadata instead.
        self.collisions: set[tuple[str, str]] = set()

    def has_override(self, column: str) -> bool:
        """Tell whether the user mapping names a column.

        Args:
            column: The source column name.

        Returns:
            ``True`` when the column has a user-mapping entry.
        """
        return column in self._exact or normalize_column(column) in self._normalized

    def target(self, column: str) -> str | None:
        """Return the target of a source column.

        Args:
            column: The source column name.

        Returns:
            The target, or ``None`` when the column is not mapped.
        """
        if column in self._exact:
            return self._exact[column]
        key = normalize_column(column)
        return self._normalized.get(key) or DEFAULT_ALIASES.get(key)

    def apply(self, raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Map one spectrum's metadata.

        Args:
            raw: Source field name to value (strings, or lists for list columns).

        Returns:
            ``(fields, extra)``: values keyed by target column, and the
            ``extra`` object (merged ``extra`` JSON, ``extra.<key>`` targets
            and unmapped fields under ``source_metadata``).
        """
        fields: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        unmapped: dict[str, Any] = {}
        winners: dict[str, str] = {}
        for column, value in raw.items():
            if is_blank(value):
                continue
            value = value.strip() if isinstance(value, str) else value
            target = self.target(column)
            if target is None:
                self.unmapped.add(column)
                unmapped[column] = value
            elif target == "ignore" or target in POINT_TARGETS:
                continue
            elif target == "extra":
                parsed = _parse_extra(value)
                if parsed is None:
                    unmapped[column] = value
                else:
                    for key, item in parsed.items():
                        if key == "source_metadata" and isinstance(item, dict):
                            unmapped = {**item, **unmapped}
                        else:
                            extra[key] = item
            elif target.startswith("extra."):
                extra[target[len("extra.") :]] = value
            elif target in winners:
                if fields[target] != value:
                    self.collisions.add((column, target))
                    unmapped[column] = value
            else:
                fields[target] = value
                winners[target] = column
        _combine_ecosis_fields(fields)
        add_source_metadata(extra, unmapped)
        return fields, extra


def _parse_extra(value: Any) -> dict[str, Any] | None:
    """Parse an ``extra`` cell.

    Args:
        value: A dict or a JSON object string.

    Returns:
        The object, or ``None`` when it is not a JSON object.
    """
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _combine_ecosis_fields(fields: dict[str, Any]) -> None:
    """Fold the EcoSIS helper targets into Format v1 columns, in place.

    ``Latin Genus``/``Latin Species`` name the material when no material
    name is given and become keywords; ``Instrument Manufacturer``/``Model``
    form the instrument; ``Processing Averaged`` etc. (``yes``/``no``)
    become processing flags.

    Args:
        fields: Mapped fields; the ``_`` helper keys are removed.
    """
    genus = str(fields.pop("_latin_genus", "") or "").strip()
    species = str(fields.pop("_latin_species", "") or "").strip()
    latin = " ".join(p for p in (genus, species) if p)
    if latin:
        fields.setdefault("material_name", latin)
        keywords = _as_list(fields.get("material_keywords"))
        if latin.lower() not in (k.lower() for k in keywords):
            keywords.append(latin)
        fields["material_keywords"] = keywords
    maker = str(fields.pop("_instrument_manufacturer", "") or "").strip()
    model = str(fields.pop("_instrument_model", "") or "").strip()
    instrument = " ".join(p for p in (maker, model) if p)
    if instrument:
        fields.setdefault("measurement_instrument", instrument)
    flags = _as_list(fields.get("measurement_processing"))
    for key, flag in (
        ("_processing_averaged", "averaged"),
        ("_processing_interpolated", "interpolated"),
        ("_processing_resampled", "resampled"),
    ):
        answer = str(fields.pop(key, "") or "").strip().lower()
        if answer in ("yes", "y", "true", "1") and flag not in flags:
            flags.append(flag)
    if flags:
        fields["measurement_processing"] = flags


def _as_list(value: Any) -> list[str]:
    """Split a list cell into its items.

    Args:
        value: ``None``, a list, or a string separated by ``;`` or ``|``.

    Returns:
        The non-empty, stripped items.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[;|]", str(value))
    return [item.strip() for item in items if item.strip()]


def _as_date(value: Any) -> _dt.date | None:
    """Parse a date cell.

    Args:
        value: A date, or a string ``YYYY-MM-DD``, ``YYYY/MM/DD`` or an ISO
            datetime.

    Returns:
        The date, or ``None`` when it cannot be parsed unambiguously.
    """
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value).strip()
    match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ].*)?$", text)
    if not match:
        return None
    try:
        return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Collection metadata
# ---------------------------------------------------------------------------


# Key of the ``.collection.json`` sidecar that marks an ``openspeclib export``.
EXPORT_MARKER_KEY = "openspeclib_export"


def load_metadata_json(path: Path) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Read collection (and dataset) metadata from a JSON file.

    Accepted shapes: the footer ``collection`` object itself; an object
    with ``collection`` and optional ``datasets`` keys (such as a whole
    ``openspeclib`` footer, or the ``.collection.json`` sidecar ``export``
    writes).

    Args:
        path: The JSON file.

    Returns:
        ``(collection, datasets, from_export)``: plain dicts, and whether the
        file is an ``openspeclib export`` sidecar (its values are fractions).

    Raises:
        ConversionError: If the file cannot be read or has the wrong shape.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionError(f"cannot read metadata file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConversionError(f"{path}: metadata must be a JSON object")
    if "collection" in data:
        collection, datasets = data["collection"], data.get("datasets") or {}
    else:
        collection, datasets = data, {}
    if not isinstance(collection, dict) or not isinstance(datasets, dict):
        raise ConversionError(f"{path}: 'collection' and 'datasets' must be JSON objects")
    return dict(collection), dict(datasets), isinstance(data.get(EXPORT_MARKER_KEY), dict)


def _validation_message(exc: ValidationError) -> str:
    """Render a Pydantic validation error as one line per problem.

    Args:
        exc: The error.

    Returns:
        ``"field: message"`` lines joined with ``"; "``.
    """
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ())) or "value"
        parts.append(f"{where}: {err.get('msg')}")
    return "; ".join(parts)


def check_licence(
    expression: str,
    refs: Iterable[str],
    where: str,
    hint: str = "add it to collection.license_refs in --metadata-json (--license-name "
    "defines only a LicenseRef- used by the collection licence)",
) -> None:
    """Check a licence expression against rule F11.

    Args:
        expression: The licence string.
        refs: ``LicenseRef-`` identifiers defined in the footer.
        where: Context for the error message.
        hint: How to define a missing ``LicenseRef-``.

    Raises:
        ConversionError: If the syntax is invalid or a ``LicenseRef-`` is
            not defined.
    """
    error, used = check_spdx_expression(expression)
    if error is not None:
        raise ConversionError(
            f"{where}: licence {expression!r} is not an SPDX expression ({error}); use an "
            f"SPDX id such as CC-BY-4.0 (operators AND/OR/WITH in upper case), NOASSERTION, "
            f"or a defined LicenseRef-"
        )
    undefined = sorted(set(used) - set(refs))
    if undefined:
        raise ConversionError(f"{where}: {', '.join(undefined)} is not defined; {hint}")


def build_collection_info(
    base: Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
    *,
    license_name: str | None = None,
    license_url: str | None = None,
) -> CollectionInfo:
    """Build the footer ``collection`` object from a JSON base and flags.

    Flags (``overrides``) win over the JSON file. ``inclusion`` defaults to
    ``complete``.

    Args:
        base: The ``collection`` object from ``--metadata-json``, if any.
        overrides: Flag values keyed by footer field; ``None`` values are
            ignored.
        license_name: Name for the ``LicenseRef-`` given as the licence, if any.
        license_url: URL for that ``LicenseRef-``.

    Returns:
        The validated collection metadata.

    Raises:
        ConversionError: If required fields are missing (all are listed),
            a field is invalid, or the licence fails rule F11.
    """
    merged: dict[str, Any] = dict(base or {})
    merged.update({k: v for k, v in overrides.items() if v is not None})
    missing = [f for f in REQUIRED_COLLECTION_FIELDS if not str(merged.get(f) or "").strip()]
    if missing:
        hints = ", ".join(f"{f} ({REQUIRED_COLLECTION_FIELDS[f]})" for f in missing)
        raise ConversionError(
            f"missing required collection metadata: {hints}. Pass the flags or a "
            f"--metadata-json file with these fields."
        )
    merged.setdefault("inclusion", "complete")
    licence = str(merged["license"])
    refs = dict(merged.get("license_refs") or {})
    if license_name:
        for ref in re.findall(r"LicenseRef-[A-Za-z0-9.-]+", licence):
            entry = {"name": license_name}
            if license_url:
                entry["url"] = license_url
            refs[ref] = entry
        merged["license_refs"] = refs
    # Checked before the model so the message says which flag fixes it.
    check_licence(
        licence,
        refs,
        "collection licence",
        "pass --license-name (and --license-url) or add it to license_refs in --metadata-json",
    )
    try:
        return CollectionInfo.model_validate(merged)
    except ValidationError as exc:
        raise ConversionError(f"invalid collection metadata: {_validation_message(exc)}") from exc


def build_datasets(raw: Mapping[str, Any], collection: CollectionInfo) -> dict[str, DatasetInfo]:
    """Validate footer ``datasets`` entries.

    Args:
        raw: Dataset id to footer entry (the id is the key).
        collection: The collection, for licence checks.

    Returns:
        The datasets keyed by id.

    Raises:
        ConversionError: If an entry is invalid.
    """
    datasets: dict[str, DatasetInfo] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ConversionError(f"dataset {key!r}: entry must be a JSON object")
        try:
            datasets[key] = DatasetInfo.model_validate({**entry, "id": key})
        except ValidationError as exc:
            raise ConversionError(f"dataset {key!r}: {_validation_message(exc)}") from exc
        if datasets[key].license:
            check_licence(str(datasets[key].license), collection.license_refs, f"dataset {key!r}")
    return datasets


# ---------------------------------------------------------------------------
# Spectra
# ---------------------------------------------------------------------------


@dataclass
class SourceSpectrum:
    """One spectrum parsed from an input file, before validation.

    Attributes:
        label: Where it came from, for messages (e.g. ``"row 3"``).
        fallback_id: Original id used when the input names none (row or
            spectrum number).
        wavelengths: The axis as parsed (float64).
        values: The values as parsed, unscaled (float64; NaN = missing).
        fields: Metadata keyed by Format v1 column (see :class:`FieldMapper`).
        extra: Producer metadata for the ``extra`` column.
        bandpass_fwhm: Optional per-band FWHM.
        values_stddev: Optional per-band standard deviation.
        wavelength_unit: Unit of the axis from the file format, the
            ``--wavelength-unit`` flag or a reader default, if any. A
            per-spectrum ``wavelength_unit`` field wins over it unless
            ``wavelength_unit_stated_by`` is set.
        wavelength_unit_stated_by: What in the file stated
            ``wavelength_unit`` (e.g. ``"the band headers"``), or ``None``
            when it is a default or flag. A per-spectrum ``wavelength_unit``
            field that disagrees with a stated unit is an error.
        integer_data: Whether the values are integers by type (ENVI integer
            data); ``None`` decides from the values.
    """

    label: str
    fallback_id: str
    wavelengths: np.ndarray
    values: np.ndarray
    fields: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    bandpass_fwhm: np.ndarray | None = None
    values_stddev: np.ndarray | None = None
    wavelength_unit: str | None = None
    wavelength_unit_stated_by: str | None = None
    integer_data: bool | None = None


@dataclass
class ImportDefaults:
    """Per-spectrum values used when the input does not state them.

    Attributes:
        quantity: ``quantity`` for spectra without one (required if any lacks it).
        geometry: ``geometry`` for spectra without one; ``unknown`` by default
            (spec section 4.2: never guess).
        material_category: ``material_category`` for spectra without one;
            ``other`` (with a warning) when unset.
        wavelength_unit: Unit of axes the input does not label.
        value_scale: Divisor applied to the values of every reflectance,
            transmittance and emissivity spectrum (e.g. 100 for percent);
            other quantities are never rescaled. ``None`` uses the file's
            scale factor or detects the scale.
    """

    quantity: str | None = None
    geometry: str = "unknown"
    material_category: str | None = None
    wavelength_unit: str | None = None
    value_scale: float | None = None


@dataclass
class ImportReport:
    """What an import did beyond the plain mapping.

    Attributes:
        warnings: Human-readable notes (each distinct message once).
        unmapped_columns: Source fields kept in ``extra.source_metadata``.
        value_divisor: Divisor applied to the values (the largest one when
            spectra differ).
        divisor_source: How it was chosen: ``flag``, ``openspeclib export``,
            ``header``, ``declared units``, ``detected`` or ``none``.
    """

    warnings: list[str] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    value_divisor: float = 1.0
    divisor_source: str = "none"

    def warn(self, message: str) -> None:
        """Record a warning once.

        Args:
            message: The warning.
        """
        if message not in self.warnings:
            self.warnings.append(message)


def _clean_axis(
    spectrum: SourceSpectrum, extra: dict[str, Any], report: ImportReport | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None]:
    """Make the axis conform to rule F8, moving the per-band arrays with it.

    Drops bands whose wavelength is not finite and positive, sorts a
    non-monotonic axis ascending and drops repeated wavelengths (keeping
    the first finite band, with a warning), recording each step in ``extra`` (spec
    section 4.3): ``source_band_index`` gives each stored band's position
    in the source, and ``dropped_duplicate_bands`` lists the dropped
    ``[wavelength, value]`` pairs. A repeated wavelength whose values
    differ is an error: dropping either band would lose data.

    Args:
        spectrum: The parsed spectrum.
        extra: The record's ``extra`` object, updated in place.
        report: Receives the warning about dropped repeated bands.

    Returns:
        ``(wavelengths, values, bandpass_fwhm, values_stddev)``.

    Raises:
        ConversionError: If fewer than two bands remain, arrays differ in
            length, or a repeated wavelength has different values.
    """
    wl = np.asarray(spectrum.wavelengths, dtype=np.float64)
    arrays = [np.asarray(spectrum.values, dtype=np.float64)]
    optional = [spectrum.bandpass_fwhm, spectrum.values_stddev]
    for name, array in zip(("values", "bandpass_fwhm", "values_stddev"), [arrays[0], *optional]):
        if array is not None and len(array) != len(wl):
            raise ConversionError(
                f"{spectrum.label}: {name} has {len(array)} points, wavelengths {len(wl)}"
            )
    stack = arrays + [None if a is None else np.asarray(a, np.float64) for a in optional]
    index = np.arange(wl.size)

    keep = np.isfinite(wl) & (wl > 0)
    if not keep.all():
        extra["dropped_invalid_wavelength_count"] = int((~keep).sum())
        wl, index = wl[keep], index[keep]
        stack = [None if a is None else a[keep] for a in stack]
    if wl.size >= 2:
        diffs = np.diff(wl)
        if not (np.all(diffs > 0) or np.all(diffs < 0)):
            order = np.argsort(wl, kind="stable")
            wl, index = wl[order], index[order]
            stack = [None if a is None else a[order] for a in stack]
            unique = np.concatenate(([True], np.diff(wl) > 0))
            if not unique.all():
                values = stack[0]
                assert values is not None
                keep = np.zeros(wl.size, dtype=bool)
                dropped_bands: list[list[float | None]] = []
                starts = np.flatnonzero(unique)
                ends = np.append(starts[1:], wl.size)
                for begin, stop in zip(starts, ends):
                    group = np.arange(begin, stop)
                    finite = group[~np.isnan(values[group])]
                    # As in the loaders: the first finite band wins over a
                    # missing (NaN) one.
                    kept = int(finite[0]) if finite.size else int(begin)
                    keep[kept] = True
                    if finite.size and np.any(values[finite] != values[kept]):
                        other = float(values[finite][values[finite] != values[kept]][0])
                        raise ConversionError(
                            f"{spectrum.label}: wavelength {wl[kept]:g} appears "
                            f"{group.size} times with different values ({values[kept]:g} and "
                            f"{other:g}); remove the repeated band from the input"
                        )
                    dropped_bands.extend(
                        [float(wl[i]), None if np.isnan(values[i]) else float(values[i])]
                        for i in group
                        if i != kept
                    )
                if report is not None:
                    report.warn(
                        "repeated wavelengths with equal values dropped (the first finite band "
                        "kept; listed in extra.dropped_duplicate_bands)"
                    )
                extra["dropped_duplicate_bands"] = dropped_bands
                wl, index = wl[keep], index[keep]
                stack = [None if a is None else a[keep] for a in stack]
            if not np.all(np.diff(index) > 0):
                extra["bands_reordered"] = True
                extra["source_band_index"] = index.tolist()
    if wl.size < 2:
        raise ConversionError(
            f"{spectrum.label}: fewer than 2 bands with a finite, positive wavelength"
        )
    values, fwhm, stddev = stack
    assert values is not None
    return wl, values, fwhm, stddev


def _mask_values(values: np.ndarray, quantity: str, extra: dict[str, Any]) -> np.ndarray:
    """Replace infinities and sentinels (rule F9) with NaN.

    Masks values above 1e10 in magnitude and the conventional no-data
    numbers -9999 and -999 (``GENERIC_SENTINELS``): no reflectance,
    transmittance, emissivity or absorbance takes them, whatever the scale.
    Counts go to ``extra``.

    Args:
        values: The values.
        quantity: Their quantity (kept for future quantity-specific sentinels).
        extra: The record's ``extra`` object; the counts masked are recorded.

    Returns:
        The masked values.
    """
    present = ~np.isnan(values)
    fill = present & (~np.isfinite(values) | (np.abs(values) > FILL_VALUE_MAGNITUDE))
    sentinel = np.zeros_like(present)
    for marker in GENERIC_SENTINELS:
        sentinel |= present & np.isclose(values, marker, rtol=1e-6, atol=0.0)
    if fill.any() or sentinel.any():
        values = values.copy()
        values[fill | sentinel] = np.nan
        if fill.any():
            extra["masked_fill_value_count"] = int(fill.sum())
        if sentinel.any():
            extra["masked_sentinel_count"] = int(sentinel.sum())
    return values


class _IdAllocator:
    """Allocates unique local ids within one collection (rule F5)."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def allocate(self, preferred: str) -> str:
        """Return a unique local id derived from ``preferred``.

        Args:
            preferred: A local id candidate or upstream identifier.

        Returns:
            ``preferred`` made legal, with ``~2``, ``~3``, … appended on a clash.
        """
        local = preferred if is_valid_local_id(preferred) else make_local_id(preferred)
        candidate, n = local, 1
        while candidate in self.used:
            n += 1
            suffix = f"~{n}"
            candidate = local[: LOCAL_ID_MAX_LENGTH - len(suffix)] + suffix
        self.used.add(candidate)
        return candidate


def _local_from_id(value: str) -> str:
    """Return the local part of an ``id`` field.

    Args:
        value: ``{collection}:{local}`` or a bare local id.

    Returns:
        The part after the first ``:`` when the value looks like a spectrum
        id, else the value itself.
    """
    prefix, sep, rest = value.partition(":")
    if sep and rest and re.fullmatch(r"[a-z0-9][a-z0-9_]{1,62}", prefix):
        return rest
    return value


def _original_from_id(value: str, collection_id: str) -> str:
    """Return the original id an ``id`` field stands for.

    The ``{collection}:`` prefix is removed only when it is the target
    collection's own id, so ``other:abc`` keeps its prefix.

    Args:
        value: The ``id`` field.
        collection_id: The target collection id.

    Returns:
        The original id.
    """
    prefix = f"{collection_id}:"
    return value[len(prefix) :] if value.startswith(prefix) and len(value) > len(prefix) else value


@dataclass
class _Prepared:
    """A spectrum after axis and value cleaning, before scaling."""

    source: SourceSpectrum
    quantity: str
    wavelengths: np.ndarray
    values: np.ndarray
    bandpass_fwhm: np.ndarray | None
    values_stddev: np.ndarray | None
    extra: dict[str, Any]


def build_records(
    spectra: Sequence[SourceSpectrum],
    collection: CollectionInfo,
    datasets: Mapping[str, DatasetInfo],
    defaults: ImportDefaults,
    report: ImportReport,
    *,
    header_divisor: float | None = None,
    from_export: bool = False,
) -> list[SpectrumRecord]:
    """Validate parsed spectra and build Format v1 records (see :func:`iter_records`).

    Args:
        spectra: The parsed spectra.
        collection: The footer ``collection`` object.
        datasets: Footer datasets; ``dataset_id`` values must be keys.
        defaults: Values for fields the input does not state.
        report: Collects warnings and the chosen scale.
        header_divisor: Scale factor stated by the input format.
        from_export: The input is an ``openspeclib export``.

    Returns:
        One record per spectrum, in input order.
    """
    return list(
        iter_records(
            spectra,
            collection,
            datasets,
            defaults,
            report,
            header_divisor=header_divisor,
            from_export=from_export,
        )
    )


def iter_records(
    spectra: Sequence[SourceSpectrum],
    collection: CollectionInfo,
    datasets: Mapping[str, DatasetInfo],
    defaults: ImportDefaults,
    report: ImportReport,
    *,
    header_divisor: float | None = None,
    from_export: bool = False,
) -> Iterator[SpectrumRecord]:
    """Validate parsed spectra and yield Format v1 records one at a time.

    Quantities, axes and the value scale of every spectrum are resolved
    before the first record is yielded, so scale errors surface early;
    records are then built lazily so a large file is never held as Python
    floats all at once.

    Args:
        spectra: The parsed spectra.
        collection: The footer ``collection`` object.
        datasets: Footer datasets; ``dataset_id`` values must be keys.
        defaults: Values for fields the input does not state.
        report: Collects warnings and the chosen scale.
        header_divisor: Scale factor stated by the input format.
        from_export: The input is an ``openspeclib export`` (values are
            already fractions).

    Returns:
        An iterator over one record per spectrum, in input order.

    Raises:
        ConversionError: On a missing quantity or unit, an invalid
            vocabulary value, licence or dataset reference, an unusable
            axis or an unresolvable value scale. The message names the
            spectrum.
    """
    from openspeclib.convert.scale import ScaleItem, declared_divisor, integral, resolve_scales

    if not spectra:
        raise ConversionError("the input holds no spectra")
    prepared: list[_Prepared] = []
    items: list[ScaleItem] = []
    for s in spectra:
        raw = s.fields.get("quantity") or defaults.quantity
        if not raw:
            raise ConversionError(
                f"{s.label}: no quantity; pass --quantity "
                f"({'/'.join(q.value for q in Quantity)}) or add a quantity column"
            )
        try:
            quantity = normalize_quantity(str(raw))
            extra = dict(s.extra)
            wl, values, fwhm, stddev = _clean_axis(s, extra, report)
        except ConversionError as exc:
            message = str(exc)
            raise ConversionError(
                message if message.startswith(s.label) else f"{s.label}: {message}"
            ) from exc
        values = _mask_values(values, quantity, extra)
        prepared.append(_Prepared(s, quantity, wl, values, fwhm, stddev, extra))
        units = s.fields.get("_value_units")
        declared = declared_divisor(str(units)) if units else None
        if units and declared is None:
            report.warn(f"value units {units!r} not recognised; the scale is detected instead")
        integer = s.integer_data if s.integer_data is not None else integral(values)
        items.append(ScaleItem(s.label, quantity, values, integer, declared))
    divisors = resolve_scales(
        items,
        report,
        explicit=defaults.value_scale,
        from_export=from_export,
        header_divisor=header_divisor,
    )

    def records() -> Iterator[SpectrumRecord]:
        """Build the records lazily.

        Yields:
            One record per spectrum.

        Raises:
            ConversionError: If a spectrum cannot be represented.
        """
        ids = _IdAllocator()
        for item, divisor in zip(prepared, divisors):
            s = item.source
            try:
                record = _build_record(item, divisor, ids, collection, datasets, defaults, report)
            except ConversionError as exc:
                message = str(exc)
                raise ConversionError(
                    message if message.startswith(s.label) else f"{s.label}: {message}"
                ) from exc
            yield record

    return records()


def _build_record(
    item: _Prepared,
    divisor: float,
    ids: _IdAllocator,
    collection: CollectionInfo,
    datasets: Mapping[str, DatasetInfo],
    defaults: ImportDefaults,
    report: ImportReport,
) -> SpectrumRecord:
    """Build one record (see :func:`build_records`).

    Args:
        item: The cleaned spectrum.
        divisor: Its value divisor.
        ids: Local id allocator.
        collection: The footer ``collection`` object.
        datasets: Footer datasets.
        defaults: Import defaults.
        report: Collects warnings.

    Returns:
        The record.

    Raises:
        ConversionError: If the spectrum cannot be represented.
    """
    s, quantity = item.source, item.quantity
    f = dict(s.fields)
    extra = item.extra
    row_unit = f.pop("wavelength_unit", None)
    unit_raw = row_unit or s.wavelength_unit or defaults.wavelength_unit
    unit = normalize_wavelength_unit(str(unit_raw)) if unit_raw else None
    if unit is None:
        raise ConversionError(
            f"wavelength unit {unit_raw!r} is not nm, um or cm-1; pass --wavelength-unit"
            if unit_raw
            else "the wavelength unit is not stated; pass --wavelength-unit (nm, um or cm-1)"
        )
    if row_unit and s.wavelength_unit_stated_by and unit != s.wavelength_unit:
        # A wrong pick here is a factor of 1000 (nm/um) or a reciprocal axis.
        raise ConversionError(
            f"its wavelength_unit field says {row_unit!r} but {s.wavelength_unit_stated_by} "
            f"gives {s.wavelength_unit}; make them agree (or drop the wavelength_unit field)"
        )
    wl, values, fwhm, stddev = item.wavelengths, item.values, item.bandpass_fwhm, item.values_stddev
    if unit == "nm" and float(np.max(wl)) < 100.0:
        report.warn(
            "some axes labelled nm end below 100 nm; check the unit (micrometres?) "
            "and pass --wavelength-unit if it is wrong"
        )
    # Earlier converter versions wrote the divisor under another key; a
    # re-import carries it over under the documented one.
    legacy = extra.pop(LEGACY_DIVISOR_KEY, None)
    if legacy is not None:
        extra.setdefault(DIVISOR_KEY, legacy)
    if divisor != 1.0:
        values = values / divisor
        stddev = None if stddev is None else stddev / divisor
        extra[DIVISOR_KEY] = divisor
    if not np.isfinite(values).any():
        report.warn(f"{s.label}: every value is missing (NaN)")

    geometry = normalize_geometry(str(f.pop("geometry", None) or defaults.geometry))
    category_raw = f.pop("material_category", None) or defaults.material_category
    category = normalize_category(str(category_raw)) if category_raw else None
    if category is None:
        if category_raw:
            f.setdefault("material_subcategory", str(category_raw))
            report.warn(
                f"category {category_raw!r} is not in the material_category vocabulary; "
                f"stored as 'other' with the label as material_subcategory"
            )
        else:
            report.warn("no material category given; using 'other' (pass --category)")
        category = "other"
    elif category_raw and not _is_category_label(str(category_raw)):
        # An EcoSIS Target Type (leaf, bark, ...) is more specific than the category.
        f.setdefault("material_subcategory", str(category_raw))

    id_field = str(f.pop("id", "") or "")
    original_id = str(f.pop("original_id", "") or "") or (
        _original_from_id(id_field, collection.id) if id_field else ""
    )
    name = str(f.pop("name", "") or "")
    material_name = str(f.pop("material_name", "") or "") or name
    original_id = original_id or name or s.fallback_id
    local = ids.allocate(_local_from_id(id_field) if id_field else original_id)
    name = name or material_name or original_id
    material_name = material_name or name

    dataset_id = f.pop("dataset_id", None)
    if dataset_id is not None and str(dataset_id) not in datasets:
        raise ConversionError(
            f"dataset_id {dataset_id!r} is not defined; add it under 'datasets' in "
            f"--metadata-json or map the column to 'ignore'"
        )
    # Licence: the row's own, else its dataset's, else the collection's.
    dataset = datasets.get(str(dataset_id)) if dataset_id is not None else None
    licence = str(
        f.pop("license", "") or (dataset.license if dataset else "") or collection.license or ""
    ).strip()
    if not licence:
        raise ConversionError(
            "no licence: the row has none, nor do its dataset or the collection; add a "
            "license column, a dataset licence in --metadata-json, or --license"
        )
    check_licence(licence, collection.license_refs, "licence")

    dates: dict[str, _dt.date | None] = {}
    for column in DATE_COLUMNS:
        raw = f.pop(column, None)
        dates[column] = None if raw is None else _as_date(raw)
        if raw is not None and dates[column] is None:
            report.warn(f"unparseable {column} values (e.g. {raw!r}) kept in extra")
            add_source_metadata(extra, {column: str(raw)})
    lists = {column: _as_list(f.pop(column, None)) for column in LIST_COLUMNS}
    f.pop("_value_units", None)

    def text(column: str) -> str | None:
        value = f.pop(column, None)
        return None if value is None else str(value)

    try:
        record = SpectrumRecord(
            id=f"{collection.id}:{local}",
            collection=collection.id,
            original_id=original_id,
            name=name,
            license=licence,
            dataset_id=None if dataset_id is None else str(dataset_id),
            material=Material(
                name=material_name,
                category=MaterialCategory(category),
                subcategory=text("material_subcategory"),
                formula=text("material_formula"),
                keywords=lists["material_keywords"],
            ),
            sample=Sample(
                id=text("sample_id"),
                description=text("sample_description"),
                particle_size=text("sample_particle_size"),
                origin=text("sample_origin"),
                owner=text("sample_owner"),
                collection_date=dates["sample_collection_date"],
                preparation=text("sample_preparation"),
            ),
            measurement=Measurement(
                quantity=Quantity(quantity),
                geometry=Geometry(geometry),
                instrument=text("measurement_instrument"),
                instrument_type=text("measurement_instrument_type"),
                laboratory=text("measurement_laboratory"),
                date=dates["measurement_date"],
                processing=lists["measurement_processing"],
                light_source=text("measurement_light_source"),
                venue=text("measurement_venue"),
                acquisition_method=text("measurement_acquisition_method"),
                foreoptic=text("measurement_foreoptic"),
                reference=text("measurement_reference"),
            ),
            spectral_data=SpectralData(
                wavelength_unit=WavelengthUnit(unit),
                wavelengths=wl.tolist(),
                values=values.tolist(),
                bandpass_fwhm=None if fwhm is None else fwhm.tolist(),
                values_stddev=None if stddev is None else stddev.tolist(),
            ),
            quality=Quality(notes=text("quality_notes")),
            extra=extra,
        )
    except ValidationError as exc:
        raise ConversionError(_validation_message(exc)) from exc
    return record
