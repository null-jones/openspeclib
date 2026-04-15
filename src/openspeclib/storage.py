"""Parquet-backed storage layer for OpenSpecLib library chunk files.

Chunks are written as Parquet files with one row per spectrum. Nested
Pydantic models are flattened to dot-separated columns (e.g.
``material.name``, ``spectral_data.wavelength_min``) for clean queryability
in DuckDB / Polars / pandas. The ``additional_properties`` ``dict[str, Any]``
field is serialised to a single JSON utf8 column.

Chunk-level metadata (``openspeclib_version``, ``source``, ``category``,
``spectrum_count``) is stored in the Parquet file's footer key-value
metadata so it round-trips back into ``LibraryChunkFile`` without
duplication on every row.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from openspeclib.models import (
    LibraryChunkFile,
    Material,
    Measurement,
    Quality,
    Sample,
    Source,
    SpectralData,
    SpectrumRecord,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical Arrow schema
# ---------------------------------------------------------------------------


def _build_arrow_schema() -> pa.Schema:
    """Construct the canonical Arrow schema for chunk files.

    Returns:
        The ``pa.Schema`` describing one spectrum per row, with the
        nested Pydantic models flattened to dot-separated columns.
    """
    fields = [
        pa.field("id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        # source
        pa.field("source.library", pa.string(), nullable=False),
        pa.field("source.library_version", pa.string(), nullable=False),
        pa.field("source.original_id", pa.string(), nullable=False),
        pa.field("source.filename", pa.string(), nullable=True),
        pa.field("source.url", pa.string(), nullable=False),
        pa.field("source.license", pa.string(), nullable=False),
        pa.field("source.citation", pa.string(), nullable=False),
        # material
        pa.field("material.name", pa.string(), nullable=False),
        pa.field("material.category", pa.string(), nullable=False),
        pa.field("material.subcategory", pa.string(), nullable=True),
        pa.field("material.formula", pa.string(), nullable=True),
        pa.field("material.keywords", pa.list_(pa.string()), nullable=False),
        # sample
        pa.field("sample.id", pa.string(), nullable=True),
        pa.field("sample.description", pa.string(), nullable=True),
        pa.field("sample.particle_size", pa.string(), nullable=True),
        pa.field("sample.origin", pa.string(), nullable=True),
        pa.field("sample.owner", pa.string(), nullable=True),
        pa.field("sample.collection_date", pa.date32(), nullable=True),
        pa.field("sample.preparation", pa.string(), nullable=True),
        # measurement
        pa.field("measurement.instrument", pa.string(), nullable=True),
        pa.field("measurement.instrument_type", pa.string(), nullable=True),
        pa.field("measurement.laboratory", pa.string(), nullable=True),
        pa.field("measurement.technique", pa.string(), nullable=False),
        pa.field("measurement.geometry", pa.string(), nullable=True),
        pa.field("measurement.date", pa.date32(), nullable=True),
        # spectral_data
        pa.field("spectral_data.type", pa.string(), nullable=False),
        pa.field("spectral_data.wavelength_unit", pa.string(), nullable=False),
        pa.field("spectral_data.wavelength_min", pa.float64(), nullable=False),
        pa.field("spectral_data.wavelength_max", pa.float64(), nullable=False),
        pa.field("spectral_data.num_points", pa.int32(), nullable=False),
        pa.field("spectral_data.wavelengths", pa.list_(pa.float64()), nullable=False),
        pa.field("spectral_data.values", pa.list_(pa.float64()), nullable=False),
        pa.field("spectral_data.bandpass", pa.list_(pa.float64()), nullable=True),
        # quality
        pa.field("quality.has_bad_bands", pa.bool_(), nullable=False),
        pa.field("quality.bad_band_count", pa.int32(), nullable=False),
        pa.field("quality.coverage_fraction", pa.float64(), nullable=False),
        pa.field("quality.notes", pa.string(), nullable=True),
        # additional_properties as JSON-serialized utf8
        pa.field("additional_properties", pa.string(), nullable=False),
    ]
    return pa.schema(fields)


ARROW_SCHEMA: pa.Schema = _build_arrow_schema()

# Footer metadata keys (chunk-level fields from LibraryChunkFile)
_META_VERSION = b"openspeclib_version"
_META_SOURCE = b"source"
_META_CATEGORY = b"category"
_META_SPECTRUM_COUNT = b"spectrum_count"


# ---------------------------------------------------------------------------
# Record <-> table conversion
# ---------------------------------------------------------------------------


def _enum_value(value: Any) -> str:
    """Return the string value of an enum or the value itself if already a string.

    ``SpectrumRecord`` may be constructed via ``model_copy(update=...)`` with raw
    string values for enum fields (Pydantic v2's update path does not coerce),
    so this helper accepts both the enum instance and the underlying string.

    Args:
        value: An enum member or string.

    Returns:
        The string representation of the enum value.
    """
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _records_to_table(records: list[SpectrumRecord]) -> pa.Table:
    """Convert a list of SpectrumRecord into a pyarrow Table.

    Args:
        records: Spectrum records to serialise.

    Returns:
        A ``pa.Table`` matching ``ARROW_SCHEMA``.
    """
    columns: dict[str, list[Any]] = {field.name: [] for field in ARROW_SCHEMA}

    for r in records:
        columns["id"].append(r.id)
        columns["name"].append(r.name)

        s = r.source
        columns["source.library"].append(_enum_value(s.library))
        columns["source.library_version"].append(s.library_version)
        columns["source.original_id"].append(s.original_id)
        columns["source.filename"].append(s.filename)
        columns["source.url"].append(s.url)
        columns["source.license"].append(s.license)
        columns["source.citation"].append(s.citation)

        m = r.material
        columns["material.name"].append(m.name)
        columns["material.category"].append(_enum_value(m.category))
        columns["material.subcategory"].append(m.subcategory)
        columns["material.formula"].append(m.formula)
        columns["material.keywords"].append(list(m.keywords))

        sa = r.sample
        columns["sample.id"].append(sa.id)
        columns["sample.description"].append(sa.description)
        columns["sample.particle_size"].append(sa.particle_size)
        columns["sample.origin"].append(sa.origin)
        columns["sample.owner"].append(sa.owner)
        columns["sample.collection_date"].append(sa.collection_date)
        columns["sample.preparation"].append(sa.preparation)

        me = r.measurement
        columns["measurement.instrument"].append(me.instrument)
        columns["measurement.instrument_type"].append(me.instrument_type)
        columns["measurement.laboratory"].append(me.laboratory)
        columns["measurement.technique"].append(_enum_value(me.technique))
        columns["measurement.geometry"].append(me.geometry)
        columns["measurement.date"].append(me.date)

        sd = r.spectral_data
        columns["spectral_data.type"].append(_enum_value(sd.type))
        columns["spectral_data.wavelength_unit"].append(_enum_value(sd.wavelength_unit))
        columns["spectral_data.wavelength_min"].append(sd.wavelength_min)
        columns["spectral_data.wavelength_max"].append(sd.wavelength_max)
        columns["spectral_data.num_points"].append(sd.num_points)
        columns["spectral_data.wavelengths"].append(list(sd.wavelengths))
        columns["spectral_data.values"].append(list(sd.values))
        columns["spectral_data.bandpass"].append(
            list(sd.bandpass) if sd.bandpass is not None else None
        )

        q = r.quality
        columns["quality.has_bad_bands"].append(q.has_bad_bands)
        columns["quality.bad_band_count"].append(q.bad_band_count)
        columns["quality.coverage_fraction"].append(q.coverage_fraction)
        columns["quality.notes"].append(q.notes)

        columns["additional_properties"].append(
            json.dumps(r.additional_properties, default=_json_default, sort_keys=True)
        )

    return pa.table(columns, schema=ARROW_SCHEMA)


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for non-native types in additional_properties.

    Args:
        value: Value that ``json.dumps`` could not serialise natively.

    Returns:
        A JSON-serialisable representation of ``value``.

    Raises:
        TypeError: If ``value`` has no known serialisation.
    """
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def _table_to_records(table: pa.Table, footer_metadata: dict[bytes, bytes]) -> LibraryChunkFile:
    """Reconstruct a LibraryChunkFile from a pyarrow Table and footer metadata.

    Args:
        table: The Parquet table containing one row per spectrum.
        footer_metadata: Raw key-value metadata from the Parquet footer.

    Returns:
        The fully-populated ``LibraryChunkFile``.
    """
    py = table.to_pylist()
    records: list[SpectrumRecord] = []

    for row in py:
        records.append(
            SpectrumRecord(
                id=row["id"],
                name=row["name"],
                source=Source(
                    library=row["source.library"],
                    library_version=row["source.library_version"],
                    original_id=row["source.original_id"],
                    filename=row["source.filename"],
                    url=row["source.url"],
                    license=row["source.license"],
                    citation=row["source.citation"],
                ),
                material=Material(
                    name=row["material.name"],
                    category=row["material.category"],
                    subcategory=row["material.subcategory"],
                    formula=row["material.formula"],
                    keywords=list(row["material.keywords"]),
                ),
                sample=Sample(
                    id=row["sample.id"],
                    description=row["sample.description"],
                    particle_size=row["sample.particle_size"],
                    origin=row["sample.origin"],
                    owner=row["sample.owner"],
                    collection_date=row["sample.collection_date"],
                    preparation=row["sample.preparation"],
                ),
                measurement=Measurement(
                    instrument=row["measurement.instrument"],
                    instrument_type=row["measurement.instrument_type"],
                    laboratory=row["measurement.laboratory"],
                    technique=row["measurement.technique"],
                    geometry=row["measurement.geometry"],
                    date=row["measurement.date"],
                ),
                spectral_data=SpectralData(
                    type=row["spectral_data.type"],
                    wavelength_unit=row["spectral_data.wavelength_unit"],
                    wavelength_min=row["spectral_data.wavelength_min"],
                    wavelength_max=row["spectral_data.wavelength_max"],
                    num_points=row["spectral_data.num_points"],
                    wavelengths=list(row["spectral_data.wavelengths"]),
                    values=list(row["spectral_data.values"]),
                    bandpass=(
                        list(row["spectral_data.bandpass"])
                        if row["spectral_data.bandpass"] is not None
                        else None
                    ),
                ),
                additional_properties=json.loads(row["additional_properties"]),
                quality=Quality(
                    has_bad_bands=row["quality.has_bad_bands"],
                    bad_band_count=row["quality.bad_band_count"],
                    coverage_fraction=row["quality.coverage_fraction"],
                    notes=row["quality.notes"],
                ),
            )
        )

    return LibraryChunkFile(
        openspeclib_version=footer_metadata[_META_VERSION].decode("utf-8"),
        source=footer_metadata[_META_SOURCE].decode("utf-8"),
        category=footer_metadata[_META_CATEGORY].decode("utf-8"),
        spectrum_count=int(footer_metadata[_META_SPECTRUM_COUNT].decode("utf-8")),
        spectra=records,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_chunk(
    records: list[SpectrumRecord],
    path: Path,
    source: str,
    category: str,
) -> None:
    """Write a list of spectrum records to a Parquet chunk file.

    Args:
        records: Spectrum records to include in the chunk.
        path: Destination file path (typically with a ``.parquet`` extension).
        source: Source library identifier (stored in footer metadata).
        category: Material category or chunk label (stored in footer metadata).
    """
    from openspeclib import __version__

    path.parent.mkdir(parents=True, exist_ok=True)
    table = _records_to_table(records)
    footer_metadata = {
        _META_VERSION: __version__.encode("utf-8"),
        _META_SOURCE: source.encode("utf-8"),
        _META_CATEGORY: category.encode("utf-8"),
        _META_SPECTRUM_COUNT: str(len(records)).encode("utf-8"),
    }
    table = table.replace_schema_metadata(footer_metadata)
    pq.write_table(table, path, compression="zstd")
    logger.info("Wrote %d spectra to %s", len(records), path)


def read_chunk(path: Path) -> LibraryChunkFile:
    """Read a Parquet chunk file back into a LibraryChunkFile.

    Args:
        path: Path to the Parquet chunk file.

    Returns:
        The reconstructed ``LibraryChunkFile``.

    Raises:
        ValueError: If the file is missing required footer metadata keys.
    """
    table = pq.read_table(path)
    raw_metadata = table.schema.metadata or {}
    required_keys = (_META_VERSION, _META_SOURCE, _META_CATEGORY, _META_SPECTRUM_COUNT)
    missing = [k.decode("utf-8") for k in required_keys if k not in raw_metadata]
    if missing:
        raise ValueError(f"Chunk file {path} is missing required footer metadata keys: {missing}")
    return _table_to_records(table, dict(raw_metadata))


def validate_parquet_schema(path: Path) -> list[str]:
    """Verify that a Parquet file's schema matches the canonical ARROW_SCHEMA.

    Args:
        path: Path to the Parquet chunk file to validate.

    Returns:
        A list of error strings describing any schema drift; empty if valid.
    """
    errors: list[str] = []
    try:
        actual = pq.read_schema(path)
    except Exception as e:  # noqa: BLE001 — surface any read failure as a single error
        return [f"Failed to read Parquet schema: {e}"]

    expected_fields = {f.name: f for f in ARROW_SCHEMA}
    actual_fields = {f.name: f for f in actual}

    missing = sorted(set(expected_fields) - set(actual_fields))
    extra = sorted(set(actual_fields) - set(expected_fields))
    for name in missing:
        errors.append(f"missing column '{name}'")
    for name in extra:
        errors.append(f"unexpected column '{name}'")

    for name, expected_field in expected_fields.items():
        if name not in actual_fields:
            continue
        actual_field = actual_fields[name]
        if not actual_field.type.equals(expected_field.type):
            errors.append(
                f"column '{name}': expected type {expected_field.type}, " f"got {actual_field.type}"
            )
        if actual_field.nullable != expected_field.nullable:
            errors.append(
                f"column '{name}': expected nullable={expected_field.nullable}, "
                f"got nullable={actual_field.nullable}"
            )

    return errors
