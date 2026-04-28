"""Parquet-backed storage layer for OpenSpecLib source library files.

Each source produces a single Parquet file with one row per spectrum. Nested
Pydantic models are flattened to dot-separated columns (e.g.
``material.name``, ``spectral_data.wavelength_min``) for clean queryability
in DuckDB / Polars / pandas. The ``additional_properties`` ``dict[str, Any]``
field is serialised to a single JSON utf8 column.

Wavelength axes are stored once per unique grid in a side file
``spectra/wavelengths.parquet`` and referenced from each spectrum row by an
``int32`` ``spectral_data.wavelength_grid_id``. This deduplicates the
heaviest column in the per-source files (USGS has 3 grids across ~2.5k
spectra; ECOSIS has ~10 grids across ~17k spectra) and lets the viewer
load the grids once at init.

Per-source files are sorted by ``id`` before write and Parquet column
statistics are enabled so HTTP Range-request consumers (DuckDB) can prune
row groups that don't overlap a target ``id`` set. Row groups are sized
small enough that a successful prune fetches only a few hundred KB.

File-level metadata (``openspeclib_version`` and ``source``) is stored in
the Parquet footer key-value metadata. The spectrum count is derived from
Parquet's native ``num_rows`` on read, so there's no second key to keep in
sync with the actual row count.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Optional

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

# Per-source row group size. Smaller than the catalog count so a successful
# row-group prune (enabled by sort-by-id + column statistics) fetches only a
# few hundred KB per ``id IN (...)`` lookup over HTTP Range requests.
DEFAULT_ROW_GROUP_SIZE = 250


# ---------------------------------------------------------------------------
# Canonical Arrow schemas
# ---------------------------------------------------------------------------


def _build_arrow_schema() -> pa.Schema:
    """Construct the canonical Arrow schema for source library files.

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
        # spectral_data — wavelengths are referenced by grid_id, see
        # WAVELENGTHS_ARROW_SCHEMA below.
        pa.field("spectral_data.type", pa.string(), nullable=False),
        pa.field("spectral_data.wavelength_unit", pa.string(), nullable=False),
        pa.field("spectral_data.wavelength_min", pa.float64(), nullable=False),
        pa.field("spectral_data.wavelength_max", pa.float64(), nullable=False),
        pa.field("spectral_data.num_points", pa.int32(), nullable=False),
        pa.field("spectral_data.wavelength_grid_id", pa.int32(), nullable=False),
        pa.field("spectral_data.values", pa.list_(pa.float64()), nullable=False),
        pa.field("spectral_data.bandpass", pa.list_(pa.float64()), nullable=True),
        pa.field("spectral_data.reflectance_scale", pa.string(), nullable=False),
        # quality
        pa.field("quality.has_bad_bands", pa.bool_(), nullable=False),
        pa.field("quality.bad_band_count", pa.int32(), nullable=False),
        pa.field("quality.coverage_fraction", pa.float64(), nullable=False),
        pa.field("quality.notes", pa.string(), nullable=True),
        # additional_properties as JSON-serialized utf8
        pa.field("additional_properties", pa.string(), nullable=False),
    ]
    return pa.schema(fields)


def _build_wavelengths_arrow_schema() -> pa.Schema:
    """Construct the canonical Arrow schema for the wavelengths side file.

    Returns:
        The ``pa.Schema`` describing the ``spectra/wavelengths.parquet``
        master grid registry: one row per (unit, n_points, wavelengths)
        combination shared across spectra.
    """
    fields = [
        pa.field("grid_id", pa.int32(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("wavelength_unit", pa.string(), nullable=False),
        pa.field("n_points", pa.int32(), nullable=False),
        pa.field("wavelengths", pa.list_(pa.float64()), nullable=False),
    ]
    return pa.schema(fields)


ARROW_SCHEMA: pa.Schema = _build_arrow_schema()
WAVELENGTHS_ARROW_SCHEMA: pa.Schema = _build_wavelengths_arrow_schema()

# Footer metadata keys. spectrum_count is intentionally omitted — Parquet's
# native num_rows is authoritative.
_META_VERSION = b"openspeclib_version"
_META_SOURCE = b"source"

# Filename of the deduplicated wavelength grid registry, written alongside
# the per-source parquets in the ``spectra/`` directory.
WAVELENGTHS_FILENAME = "wavelengths.parquet"


# ---------------------------------------------------------------------------
# Wavelength grid registry
# ---------------------------------------------------------------------------


class WavelengthRegistry:
    """Deduplicates wavelength axes across spectra into a compact id table.

    A grid is keyed by ``(wavelength_unit, tuple(wavelengths))``, so two
    spectra reporting the same axis in the same unit share a single row in
    ``wavelengths.parquet``. The ``source`` recorded for a grid is the
    first source seen contributing it (provenance only — multiple sources
    may legitimately share a grid).
    """

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._grid_id_by_key: dict[tuple[str, tuple[float, ...]], int] = {}
        self._grids: list[dict[str, Any]] = []

    def register(
        self,
        wavelengths: list[float],
        wavelength_unit: str,
        source: str,
    ) -> int:
        """Look up or assign a grid_id for a given wavelength axis.

        Args:
            wavelengths: Wavelength values to register.
            wavelength_unit: Unit of the wavelength axis.
            source: Source identifier credited with this grid if it's new.

        Returns:
            The integer ``grid_id`` for this (unit, wavelengths) pair.
        """
        key = (wavelength_unit, tuple(wavelengths))
        existing = self._grid_id_by_key.get(key)
        if existing is not None:
            return existing
        grid_id = len(self._grids)
        self._grid_id_by_key[key] = grid_id
        self._grids.append(
            {
                "grid_id": grid_id,
                "source": source,
                "wavelength_unit": wavelength_unit,
                "n_points": len(wavelengths),
                "wavelengths": list(wavelengths),
            }
        )
        return grid_id

    def get(self, grid_id: int) -> tuple[list[float], str]:
        """Resolve a grid_id back to its (wavelengths, unit) pair.

        Args:
            grid_id: The id returned from a previous ``register`` call.

        Returns:
            A tuple of (wavelengths, wavelength_unit).

        Raises:
            KeyError: If ``grid_id`` is not present in the registry.
        """
        if grid_id < 0 or grid_id >= len(self._grids):
            raise KeyError(f"Unknown wavelength grid_id: {grid_id}")
        entry = self._grids[grid_id]
        return list(entry["wavelengths"]), entry["wavelength_unit"]

    def __len__(self) -> int:
        """Return the number of unique grids registered."""
        return len(self._grids)

    def to_table(self) -> pa.Table:
        """Materialise the registry as a pyarrow Table for writing.

        Returns:
            A ``pa.Table`` matching ``WAVELENGTHS_ARROW_SCHEMA``.
        """
        columns: dict[str, list[Any]] = {field.name: [] for field in WAVELENGTHS_ARROW_SCHEMA}
        for entry in self._grids:
            for field in WAVELENGTHS_ARROW_SCHEMA:
                columns[field.name].append(entry[field.name])
        return pa.table(columns, schema=WAVELENGTHS_ARROW_SCHEMA)


def write_wavelengths(registry: WavelengthRegistry, path: Path) -> int:
    """Write the master wavelength grid registry to ``wavelengths.parquet``.

    Args:
        registry: The registry to materialise.
        path: Destination Parquet file path.

    Returns:
        The number of unique grids written.
    """
    from openspeclib import __version__

    path.parent.mkdir(parents=True, exist_ok=True)
    schema_with_metadata = WAVELENGTHS_ARROW_SCHEMA.with_metadata(
        {_META_VERSION: __version__.encode("utf-8")}
    )
    table = registry.to_table().replace_schema_metadata(schema_with_metadata.metadata)
    pq.write_table(table, path, compression="zstd", write_statistics=True)
    logger.info("Wrote %d wavelength grids to %s", len(registry), path)
    return len(registry)


def read_wavelengths(path: Path) -> WavelengthRegistry:
    """Load the master wavelength grid registry from ``wavelengths.parquet``.

    Args:
        path: Path to the wavelengths Parquet file.

    Returns:
        A populated ``WavelengthRegistry``.
    """
    table = pq.read_table(path)
    registry = WavelengthRegistry()
    for row in table.to_pylist():
        # Re-register in original grid_id order. We trust the file's grid_id
        # column matches its row order (the writer guarantees this).
        registry.register(
            wavelengths=list(row["wavelengths"]),
            wavelength_unit=row["wavelength_unit"],
            source=row["source"],
        )
    return registry


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


def _records_to_table(
    records: list[SpectrumRecord],
    registry: WavelengthRegistry,
    source: str,
) -> pa.Table:
    """Convert a list of SpectrumRecord into a pyarrow Table.

    Wavelengths are looked up in the registry; each record stores only the
    resulting ``wavelength_grid_id`` rather than the full axis array.

    Args:
        records: Spectrum records to serialise.
        registry: Shared wavelength grid registry to register axes into.
        source: Source identifier credited with new grids registered here.

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
        wavelength_unit = _enum_value(sd.wavelength_unit)
        grid_id = registry.register(
            wavelengths=list(sd.wavelengths),
            wavelength_unit=wavelength_unit,
            source=source,
        )
        columns["spectral_data.type"].append(_enum_value(sd.type))
        columns["spectral_data.wavelength_unit"].append(wavelength_unit)
        columns["spectral_data.wavelength_min"].append(sd.wavelength_min)
        columns["spectral_data.wavelength_max"].append(sd.wavelength_max)
        columns["spectral_data.num_points"].append(sd.num_points)
        columns["spectral_data.wavelength_grid_id"].append(grid_id)
        columns["spectral_data.values"].append(list(sd.values))
        columns["spectral_data.bandpass"].append(
            list(sd.bandpass) if sd.bandpass is not None else None
        )
        columns["spectral_data.reflectance_scale"].append(sd.reflectance_scale)

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


def _rows_to_records(
    rows: list[dict[str, Any]],
    registry: Optional[WavelengthRegistry] = None,
) -> list[SpectrumRecord]:
    """Convert dict-of-columns rows back into a list of SpectrumRecord.

    Args:
        rows: Row dicts from ``pa.Table.to_pylist()``.
        registry: Wavelength grid registry used to rehydrate the
            ``wavelengths`` array from each row's ``wavelength_grid_id``. If
            ``None`` (e.g. during round-trip tests of an isolated chunk),
            wavelengths are reconstructed as an empty list and the caller is
            responsible for resolving grids.

    Returns:
        The reconstructed list of ``SpectrumRecord``.
    """
    records: list[SpectrumRecord] = []
    for row in rows:
        if registry is not None:
            wavelengths, _ = registry.get(int(row["spectral_data.wavelength_grid_id"]))
        else:
            wavelengths = []
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
                    wavelengths=wavelengths,
                    values=list(row["spectral_data.values"]),
                    bandpass=(
                        list(row["spectral_data.bandpass"])
                        if row["spectral_data.bandpass"] is not None
                        else None
                    ),
                    reflectance_scale=row["spectral_data.reflectance_scale"],
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
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_source(
    records: Iterable[SpectrumRecord],
    path: Path,
    source: str,
    registry: Optional[WavelengthRegistry] = None,
    row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
) -> int:
    """Write spectrum records for one source to a Parquet file.

    Records are buffered, sorted by ``id``, and written in row groups of
    ``row_group_size`` with column statistics enabled. The sort plus
    statistics let HTTP Range-request consumers (DuckDB) prune row groups
    that don't cover a target id, fetching only a few row groups per
    ``id IN (...)`` lookup instead of the full file.

    Wavelength axes are registered with ``registry`` and replaced by
    ``int32`` ``wavelength_grid_id`` references. When called as part of
    :func:`openspeclib.combine.build_library`, callers pass a shared
    registry across sources and flush it once at the end via
    :func:`write_wavelengths`. For standalone use (e.g. tests), omit
    ``registry`` and a fresh one is materialised to a sibling
    ``wavelengths.parquet`` alongside ``path``.

    Args:
        records: Iterable of spectrum records for a single source.
        path: Destination Parquet file path.
        source: Source library identifier (stored in footer metadata).
        registry: Shared wavelength grid registry to dedupe axes into.
            If ``None``, a fresh registry is created and flushed alongside
            the chunk on completion.
        row_group_size: Maximum spectra per Parquet row group.

    Returns:
        The total number of spectra written.
    """
    from openspeclib import __version__

    path.parent.mkdir(parents=True, exist_ok=True)

    standalone = registry is None
    if registry is None:
        registry = WavelengthRegistry()

    # Buffer all records so we can sort by id before writing. Since
    # wavelengths are deduplicated into the side registry, each record's
    # in-memory footprint is just its values list (~16 KB per spectrum) —
    # acceptable even for the largest source (~17k records ≈ a few hundred
    # MB peak).
    buffered: list[SpectrumRecord] = sorted(records, key=lambda r: r.id)
    total = len(buffered)

    if total == 0:
        path.unlink(missing_ok=True)
        logger.warning("No records written for source %s; skipping %s", source, path)
        return 0

    schema_with_metadata = ARROW_SCHEMA.with_metadata(
        {
            _META_VERSION: __version__.encode("utf-8"),
            _META_SOURCE: source.encode("utf-8"),
        }
    )

    writer: pq.ParquetWriter | None = None
    try:
        for start in range(0, total, row_group_size):
            batch = buffered[start : start + row_group_size]
            table = _records_to_table(batch, registry, source).replace_schema_metadata(
                schema_with_metadata.metadata
            )
            if writer is None:
                writer = pq.ParquetWriter(
                    path,
                    schema_with_metadata,
                    compression="zstd",
                    write_statistics=True,
                )
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()

    if standalone:
        write_wavelengths(registry, path.parent / WAVELENGTHS_FILENAME)

    logger.info("Wrote %d spectra for %s to %s", total, source, path)
    return total


def read_chunk(path: Path, registry: Optional[WavelengthRegistry] = None) -> LibraryChunkFile:
    """Read a Parquet source library file back into a LibraryChunkFile.

    The spectrum count is read from Parquet's native row count rather than
    a footer key, so the two can never drift.

    Args:
        path: Path to the per-source Parquet file.
        registry: Wavelength grid registry, normally loaded from the
            sibling ``wavelengths.parquet``. If omitted, the function
            attempts to load it from ``path.parent / WAVELENGTHS_FILENAME``;
            if that file is missing, returned spectra will have empty
            ``wavelengths`` arrays.

    Returns:
        The reconstructed ``LibraryChunkFile``.

    Raises:
        ValueError: If the file is missing required footer metadata keys.
    """
    pf = pq.ParquetFile(path)
    raw_metadata = pf.schema_arrow.metadata or {}
    required_keys = (_META_VERSION, _META_SOURCE)
    missing = [k.decode("utf-8") for k in required_keys if k not in raw_metadata]
    if missing:
        raise ValueError(f"Chunk file {path} is missing required footer metadata keys: {missing}")

    if registry is None:
        sibling = path.parent / WAVELENGTHS_FILENAME
        if sibling.exists():
            registry = read_wavelengths(sibling)

    table = pf.read()
    records = _rows_to_records(table.to_pylist(), registry)
    return LibraryChunkFile(
        openspeclib_version=raw_metadata[_META_VERSION].decode("utf-8"),
        source=raw_metadata[_META_SOURCE].decode("utf-8"),
        spectrum_count=pf.metadata.num_rows,
        spectra=records,
    )


def iter_records(
    path: Path,
    batch_size: int = 1000,
    registry: Optional[WavelengthRegistry] = None,
) -> Iterator[SpectrumRecord]:
    """Stream records from a source Parquet file without loading it all into memory.

    Args:
        path: Path to the per-source Parquet file.
        batch_size: Number of rows to buffer per read.
        registry: Wavelength grid registry, normally loaded from the
            sibling ``wavelengths.parquet``. If omitted, the function
            attempts to load it from ``path.parent / WAVELENGTHS_FILENAME``.

    Yields:
        ``SpectrumRecord`` instances in file order.
    """
    if registry is None:
        sibling = path.parent / WAVELENGTHS_FILENAME
        if sibling.exists():
            registry = read_wavelengths(sibling)

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_size):
        table = pa.Table.from_batches([batch], schema=pf.schema_arrow)
        yield from _rows_to_records(table.to_pylist(), registry)


def validate_parquet_schema(path: Path, schema: pa.Schema = ARROW_SCHEMA) -> list[str]:
    """Verify that a Parquet file's schema matches a canonical Arrow schema.

    Args:
        path: Path to the Parquet file to validate.
        schema: Expected Arrow schema; defaults to ``ARROW_SCHEMA`` (the
            per-source spectrum schema). Pass ``WAVELENGTHS_ARROW_SCHEMA``
            to validate the side wavelengths file.

    Returns:
        A list of error strings describing any schema drift; empty if valid.
    """
    errors: list[str] = []
    try:
        actual = pq.read_schema(path)
    except Exception as e:  # noqa: BLE001 — surface any read failure as a single error
        return [f"Failed to read Parquet schema: {e}"]

    expected_fields = {f.name: f for f in schema}
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
