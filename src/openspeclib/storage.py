"""Parquet storage for OpenSpecLib Format v1 collection and index files.

A collection file (``docs/spec/format-v1.md``) holds one collection, one
row per spectrum, in flat snake_case columns: the core columns every file
has, the columns of whichever optional extensions the data fills, an
optional ``extra`` JSON column, and the spectral axis inline as
``wavelengths``. Its Parquet footer carries the ``openspeclib`` key with the
collection's provenance, licensing and dataset metadata.

Writer settings follow the spec recommendations R1–R3:

* rows are sorted by ``id`` and written in row groups of
  :data:`DEFAULT_ROW_GROUP_SIZE` (100), so a single ``id = ...`` lookup over
  HTTP Range requests fetches one small row group (DuckDB prunes row groups
  for single equality predicates, not for ``IN`` lists);
* the float list columns (``values``, ``wavelengths``, ``bandpass_fwhm``,
  ``values_stddev``) use BYTE_STREAM_SPLIT without dictionary encoding, all
  other columns use dictionary encoding, everything is zstd level 9;
* Parquet min/max statistics are kept only on the columns readers prune on
  (:data:`STATISTICS_COLUMNS`), so remote footer fetches stay small.

Values are float32 (sources publish 4–8 significant digits); wavelengths are
float64 in the source's native unit. Missing bands are NaN.

An index file (spec section 7) lists the non-array columns of several
collection files plus ``num_points`` and ``file``, under the footer key
``openspeclib_index``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from openspeclib.models import (
    FORMAT_VERSION,
    CollectionInfo,
    DatasetInfo,
    Material,
    Measurement,
    Quality,
    Sample,
    SpectralData,
    SpectrumRecord,
    license_refs_used,
)

logger = logging.getLogger(__name__)

# Footer key-value metadata keys.
FOOTER_KEY = "openspeclib"
INDEX_FOOTER_KEY = "openspeclib_index"

# File name of the index in the OpenSpecLib distribution.
INDEX_FILENAME = "openspeclib-index.parquet"

# Spectra per row group in collection files (spec R2 recommends at most ~250).
# With float32 + BYTE_STREAM_SPLIT values a 100-row group is well under 1 MB,
# so a pruned single-id lookup fetches only that much.
DEFAULT_ROW_GROUP_SIZE = 100

# Rows per row group in index files. The index holds no arrays and is read
# whole or filtered by column predicates, so large groups compress best.
INDEX_ROW_GROUP_SIZE = 10_000

# zstd level for every file. Values dominate collection files and are
# BYTE_STREAM_SPLIT-encoded, where level 9 buys ~5% over the default at
# negligible write-time cost for these sizes.
_ZSTD_LEVEL = 9

# Columns that keep Parquet min/max statistics: everything readers prune or
# filter on. Statistics on long text columns are pure footer bloat.
STATISTICS_COLUMNS: tuple[str, ...] = (
    "id",
    "collection",
    "material_category",
    "quantity",
    "geometry",
    "wavelength_min_nm",
    "wavelength_max_nm",
    "dataset_id",
)


# ---------------------------------------------------------------------------
# Column definitions (schemas/format-v1/columns.json)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnDef:
    """One Format v1 column.

    Attributes:
        name: Column name.
        type: Arrow type.
        nullable: Whether the column may hold nulls.
        extension: ``None`` for a core column, the extension name for an
            extension column, ``"*"`` for ``extra`` and ``"index"`` for the
            index-only columns.
    """

    name: str
    type: pa.DataType
    nullable: bool
    extension: str | None

    def field(self) -> pa.Field:
        """Return the Arrow field for this column.

        Returns:
            A ``pa.Field`` with the column's name, type and nullability.
        """
        return pa.field(self.name, self.type, nullable=self.nullable)


_STR = pa.string()
_F64 = pa.float64()
_LIST_F64 = pa.list_(pa.float64())
_LIST_F32 = pa.list_(pa.float32())
_LIST_STR = pa.list_(pa.string())

CORE_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("id", _STR, False, None),
    ColumnDef("collection", _STR, False, None),
    ColumnDef("original_id", _STR, False, None),
    ColumnDef("name", _STR, False, None),
    ColumnDef("material_name", _STR, False, None),
    ColumnDef("material_category", _STR, False, None),
    ColumnDef("quantity", _STR, False, None),
    ColumnDef("geometry", _STR, False, None),
    ColumnDef("wavelength_unit", _STR, False, None),
    ColumnDef("wavelengths", _LIST_F64, False, None),
    ColumnDef("values", _LIST_F32, False, None),
    ColumnDef("wavelength_min_nm", _F64, False, None),
    ColumnDef("wavelength_max_nm", _F64, False, None),
    ColumnDef("license", _STR, False, None),
    ColumnDef("dataset_id", _STR, True, None),
)

EXTENSION_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("material_subcategory", _STR, True, "material"),
    ColumnDef("material_formula", _STR, True, "material"),
    ColumnDef("material_keywords", _LIST_STR, True, "material"),
    ColumnDef("sample_id", _STR, True, "sample"),
    ColumnDef("sample_description", _STR, True, "sample"),
    ColumnDef("sample_particle_size", _STR, True, "sample"),
    ColumnDef("sample_origin", _STR, True, "sample"),
    ColumnDef("sample_owner", _STR, True, "sample"),
    ColumnDef("sample_collection_date", pa.date32(), True, "sample"),
    ColumnDef("sample_preparation", _STR, True, "sample"),
    ColumnDef("measurement_instrument", _STR, True, "measurement"),
    ColumnDef("measurement_instrument_type", _STR, True, "measurement"),
    ColumnDef("measurement_laboratory", _STR, True, "measurement"),
    ColumnDef("measurement_date", pa.date32(), True, "measurement"),
    ColumnDef("measurement_processing", _LIST_STR, True, "measurement"),
    ColumnDef("measurement_light_source", _STR, True, "measurement"),
    ColumnDef("measurement_venue", _STR, True, "measurement"),
    ColumnDef("measurement_acquisition_method", _STR, True, "measurement"),
    ColumnDef("measurement_foreoptic", _STR, True, "measurement"),
    ColumnDef("measurement_reference", _STR, True, "measurement"),
    ColumnDef("bandpass_fwhm", _LIST_F32, True, "bandpass"),
    ColumnDef("values_stddev", _LIST_F32, True, "uncertainty"),
    ColumnDef("quality_notes", _STR, True, "quality"),
)

EXTRA_COLUMN = ColumnDef("extra", _STR, True, "*")

INDEX_ONLY_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("num_points", pa.int32(), False, "index"),
    ColumnDef("file", _STR, False, "index"),
)

# Extension names in canonical order.
EXTENSIONS: tuple[str, ...] = (
    "material",
    "sample",
    "measurement",
    "bandpass",
    "uncertainty",
    "quality",
)

# Every collection-file column in canonical order.
COLLECTION_COLUMNS: tuple[ColumnDef, ...] = CORE_COLUMNS + EXTENSION_COLUMNS + (EXTRA_COLUMN,)
COLUMNS: dict[str, ColumnDef] = {c.name: c for c in COLLECTION_COLUMNS + INDEX_ONLY_COLUMNS}

# Per-band float arrays: stored BYTE_STREAM_SPLIT, and left out of index files.
ARRAY_COLUMNS: tuple[str, ...] = ("wavelengths", "values", "bandpass_fwhm", "values_stddev")

# Optional columns whose presence decides which extensions a file declares.
_OPTIONAL_COLUMNS: tuple[ColumnDef, ...] = EXTENSION_COLUMNS + (EXTRA_COLUMN,)


def collection_schema(column_names: Iterable[str]) -> pa.Schema:
    """Build the Arrow schema of a collection file holding the given columns.

    Args:
        column_names: Optional (extension and ``extra``) columns the file
            holds besides the core columns.

    Returns:
        The schema: core columns, then the requested optional columns, in
        canonical order.

    Raises:
        KeyError: If a name is not a Format v1 collection column.
    """
    wanted = set(column_names)
    unknown = wanted - {c.name for c in _OPTIONAL_COLUMNS} - {c.name for c in CORE_COLUMNS}
    if unknown:
        raise KeyError(f"not Format v1 collection columns: {sorted(unknown)}")
    return pa.schema(
        [c.field() for c in COLLECTION_COLUMNS if c.extension is None or c.name in wanted]
    )


# ---------------------------------------------------------------------------
# Wavelength helpers
# ---------------------------------------------------------------------------


def wavelength_extent_nm(wavelengths: np.ndarray, unit: str) -> tuple[float, float]:
    """Return the shortest and longest wavelength of an axis, in nanometres.

    Args:
        wavelengths: The axis in its native unit (positive values).
        unit: ``"nm"``, ``"um"`` or ``"cm-1"``.

    Returns:
        ``(min_nm, max_nm)``. Wavenumbers convert as λ[nm] = 1e7 / ν[cm-1].

    Raises:
        ValueError: If the unit is unknown or the axis is empty.
    """
    if wavelengths.size == 0:
        raise ValueError("empty wavelength axis")
    lo = float(np.min(wavelengths))
    hi = float(np.max(wavelengths))
    if unit == "nm":
        return lo, hi
    if unit == "um":
        return lo * 1000.0, hi * 1000.0
    if unit == "cm-1":
        return 1e7 / hi, 1e7 / lo
    raise ValueError(f"unknown wavelength unit {unit!r}")


class AxisInterner:
    """Shares identical wavelength axes between rows to bound memory.

    Most spectra of a collection share one of a few axes; keeping one numpy
    array per distinct axis keeps a 25k-spectrum collection well under a
    gigabyte while its rows are buffered for sorting.
    """

    def __init__(self) -> None:
        self._axes: dict[bytes, np.ndarray] = {}

    def intern(self, axis: np.ndarray) -> np.ndarray:
        """Return the shared copy of ``axis``.

        Args:
            axis: A float64 axis.

        Returns:
            An equal array, shared with every earlier equal axis.
        """
        key = axis.tobytes()
        shared = self._axes.get(key)
        if shared is None:
            axis.setflags(write=False)
            self._axes[key] = axis
            return axis
        return shared


# ---------------------------------------------------------------------------
# Record -> row
# ---------------------------------------------------------------------------


@dataclass
class SpectrumRow:
    """One collection-file row, with arrays held as numpy.

    Attributes:
        meta: Every non-array column value keyed by column name (``None`` for
            an empty optional column).
        wavelengths: The float64 axis.
        values: The float32 values.
        bandpass_fwhm: Optional float32 bandpass.
        values_stddev: Optional float32 standard deviations.
    """

    meta: dict[str, Any]
    wavelengths: np.ndarray
    values: np.ndarray
    bandpass_fwhm: np.ndarray | None = None
    values_stddev: np.ndarray | None = None

    @property
    def id(self) -> str:
        """Return the spectrum id."""
        return str(self.meta["id"])

    def array(self, name: str) -> np.ndarray | None:
        """Return one of the array columns by name.

        Args:
            name: One of :data:`ARRAY_COLUMNS`.

        Returns:
            The array, or ``None`` for an absent optional array.
        """
        value: np.ndarray | None = getattr(self, name)
        return value


def _jsonable(value: Any) -> Any:
    """Convert a value to strict JSON: non-finite floats become null, dates strings.

    Args:
        value: A value from a record's ``extra`` mapping.

    Returns:
        A structure ``json.dumps(..., allow_nan=False)`` accepts.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return value


def encode_extra(extra: Mapping[str, Any]) -> str | None:
    """Serialise a record's ``extra`` mapping for the ``extra`` column.

    Args:
        extra: Producer-specific metadata.

    Returns:
        A compact, key-sorted JSON object string, or ``None`` when empty.
    """
    if not extra:
        return None
    return json.dumps(
        _jsonable(dict(extra)), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _enum_value(value: Any) -> str:
    """Return the string value of an enum member or the value itself.

    Args:
        value: An enum member or a string.

    Returns:
        The string value.
    """
    return str(getattr(value, "value", value))


def _or_none(value: str | None) -> str | None:
    """Map an empty or whitespace-only string to ``None``.

    Args:
        value: A string or ``None``.

    Returns:
        The string, or ``None`` when it is empty.
    """
    return value if value is not None and value.strip() else None


def _float_array(values: Sequence[float] | None, dtype: Any) -> np.ndarray | None:
    """Convert an optional float sequence to a numpy array.

    Args:
        values: The values, or ``None``.
        dtype: Target numpy dtype.

    Returns:
        The array, or ``None``.
    """
    return None if values is None else np.asarray(values, dtype=dtype)


def _float32(values: Sequence[float]) -> np.ndarray:
    """Convert values to float32 without an overflow warning.

    Args:
        values: The values.

    Returns:
        A float32 array (out-of-range magnitudes become ±inf).
    """
    with np.errstate(over="ignore"):
        return np.asarray(values, dtype=np.float32)


def record_to_row(record: SpectrumRecord, interner: AxisInterner | None = None) -> SpectrumRow:
    """Flatten a :class:`SpectrumRecord` into a collection-file row.

    Empty strings in the columns the spec requires to be non-empty are
    replaced by a fallback (``name`` ← ``material_name`` ← ``original_id``);
    empty optional strings and lists become null.

    Args:
        record: The record.
        interner: Optional shared-axis cache (see :class:`AxisInterner`).

    Returns:
        The row.
    """
    sd = record.spectral_data
    unit = _enum_value(sd.wavelength_unit)
    wavelengths = np.asarray(sd.wavelengths, dtype=np.float64)
    if interner is not None:
        wavelengths = interner.intern(wavelengths)
    lo, hi = wavelength_extent_nm(wavelengths, unit)
    m, sa, me = record.material, record.sample, record.measurement
    material_name = _or_none(m.name) or _or_none(record.name) or record.original_id
    meta: dict[str, Any] = {
        "id": record.id,
        "collection": record.collection,
        "original_id": record.original_id,
        "name": _or_none(record.name) or material_name,
        "material_name": material_name,
        "material_category": _enum_value(m.category),
        "quantity": _enum_value(me.quantity),
        "geometry": _enum_value(me.geometry),
        "wavelength_unit": unit,
        "wavelength_min_nm": lo,
        "wavelength_max_nm": hi,
        "license": record.license,
        "dataset_id": record.dataset_id,
        "material_subcategory": _or_none(m.subcategory),
        "material_formula": _or_none(m.formula),
        "material_keywords": list(m.keywords) or None,
        "sample_id": _or_none(sa.id),
        "sample_description": _or_none(sa.description),
        "sample_particle_size": _or_none(sa.particle_size),
        "sample_origin": _or_none(sa.origin),
        "sample_owner": _or_none(sa.owner),
        "sample_collection_date": sa.collection_date,
        "sample_preparation": _or_none(sa.preparation),
        "measurement_instrument": _or_none(me.instrument),
        "measurement_instrument_type": _or_none(me.instrument_type),
        "measurement_laboratory": _or_none(me.laboratory),
        "measurement_date": me.date,
        "measurement_processing": list(me.processing) or None,
        "measurement_light_source": _or_none(me.light_source),
        "measurement_venue": _or_none(me.venue),
        "measurement_acquisition_method": _or_none(me.acquisition_method),
        "measurement_foreoptic": _or_none(me.foreoptic),
        "measurement_reference": _or_none(me.reference),
        "quality_notes": _or_none(record.quality.notes),
        "extra": encode_extra(record.extra),
    }
    return SpectrumRow(
        meta=meta,
        wavelengths=wavelengths,
        # Values beyond float32 range become inf here and are then rejected
        # by the conformance gate in write_rows (F9).
        values=_float32(sd.values),
        bandpass_fwhm=_float_array(sd.bandpass_fwhm, np.float32),
        values_stddev=_float_array(sd.values_stddev, np.float32),
    )


# ---------------------------------------------------------------------------
# Footers
# ---------------------------------------------------------------------------


def utc_timestamp(when: _dt.datetime | None = None) -> str:
    """Format a timestamp as RFC 3339 UTC with a ``Z`` designator.

    Args:
        when: The moment to format; defaults to now. ``SOURCE_DATE_EPOCH``
            (reproducible builds) overrides the default.

    Returns:
        A string such as ``2026-10-29T12:00:00Z``.
    """
    if when is None:
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if epoch:
            when = _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc)
        else:
            when = _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generator() -> str:
    """Return the footer ``generator`` string.

    Returns:
        ``"openspeclib <version>"``.
    """
    from openspeclib import __version__

    return f"openspeclib {__version__}"


def build_footer(
    collection: CollectionInfo,
    datasets: Mapping[str, DatasetInfo] | None,
    extensions: Sequence[str],
    created: str | None = None,
) -> dict[str, Any]:
    """Build the ``openspeclib`` footer object of a collection file.

    Args:
        collection: The footer ``collection`` object.
        datasets: Contributing datasets keyed by dataset id.
        extensions: Extensions the file declares.
        created: RFC 3339 creation timestamp; defaults to :func:`utc_timestamp`.

    Returns:
        A JSON-serialisable dict valid against ``#/$defs/collectionFooter``.
    """
    return {
        "format_version": FORMAT_VERSION,
        "collection": collection.to_footer(),
        "datasets": {key: ds.to_footer() for key, ds in sorted((datasets or {}).items())},
        "extensions": [e for e in EXTENSIONS if e in set(extensions)],
        "created": created or utc_timestamp(),
        "generator": _generator(),
    }


def _footer_json(document: Mapping[str, Any]) -> str:
    """Serialise a footer object compactly.

    The footer is stored once, as a Parquet key-value entry; the writers
    disable the embedded ``ARROW:schema`` copy so it is not duplicated there
    and every remote footer fetch stays small.

    Args:
        document: The footer object.

    Returns:
        Compact JSON (UTF-8, no ASCII escaping).
    """
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _read_kv(path: str | os.PathLike[str], key: str) -> dict[str, Any]:
    """Read and parse one JSON footer key of a Parquet file.

    Args:
        path: Local Parquet file.
        key: Footer key.

    Returns:
        The parsed JSON object.

    Raises:
        ValueError: If the key is missing or does not hold a JSON object.
    """
    metadata = pq.read_metadata(path).metadata or {}
    raw = metadata.get(key.encode("utf-8"))
    if raw is None:
        raise ValueError(f"{os.fspath(path)} has no '{key}' footer key")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{os.fspath(path)}: footer key '{key}' is not a JSON object")
    return value


def read_footer(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the ``openspeclib`` footer of a collection file.

    Only the Parquet footer is read.

    Args:
        path: Local collection file.

    Returns:
        The footer object.

    Raises:
        ValueError: If the file has no ``openspeclib`` footer key.
    """
    return _read_kv(path, FOOTER_KEY)


def read_index_footer(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the ``openspeclib_index`` footer of an index file.

    Args:
        path: Local index file.

    Returns:
        The footer object.

    Raises:
        ValueError: If the file has no ``openspeclib_index`` footer key.
    """
    return _read_kv(path, INDEX_FOOTER_KEY)


# ---------------------------------------------------------------------------
# Writing collection files
# ---------------------------------------------------------------------------


def _leaf_path(field: pa.Field) -> str:
    """Return the Parquet leaf column path of a top-level Arrow field.

    Writer options (encodings, dictionary, statistics) address columns by
    leaf path; a list column's values live at ``<name>.list.element``. A
    top-level name for a list column is silently ignored by pyarrow.

    Args:
        field: A top-level field.

    Returns:
        The leaf path.
    """
    if pa.types.is_list(field.type):
        return f"{field.name}.list.element"
    return str(field.name)


def _writer_options(schema: pa.Schema, statistics: Sequence[str]) -> dict[str, Any]:
    """Build ``pq.ParquetWriter`` options for a Format v1 file.

    Args:
        schema: The file's Arrow schema.
        statistics: Top-level columns that keep min/max statistics.

    Returns:
        Keyword arguments: zstd level 9, BYTE_STREAM_SPLIT (no dictionary)
        for the float array leaves, dictionary encoding for all others, and
        statistics only on ``statistics``.
    """
    bss = [_leaf_path(f) for f in schema if f.name in ARRAY_COLUMNS]
    dictionary = [_leaf_path(f) for f in schema if f.name not in ARRAY_COLUMNS]
    stats = [_leaf_path(f) for f in schema if f.name in set(statistics)]
    return {
        "compression": "zstd",
        "compression_level": _ZSTD_LEVEL,
        "use_dictionary": dictionary,
        "column_encoding": {leaf: "BYTE_STREAM_SPLIT" for leaf in bss},
        "write_statistics": stats,
    }


def _list_array(arrays: Sequence[np.ndarray | None], dtype: Any, arrow_type: pa.DataType) -> Any:
    """Build an Arrow list array from per-row numpy arrays without Python floats.

    Args:
        arrays: One array per row, or ``None`` for a null list.
        dtype: numpy dtype of the values.
        arrow_type: The Arrow list type.

    Returns:
        A ``pa.ListArray``.
    """
    lengths = np.fromiter((0 if a is None else a.size for a in arrays), np.int64, len(arrays))
    offsets = np.zeros(len(arrays) + 1, dtype=np.int32)
    np.cumsum(lengths, out=offsets[1:])
    present = [a for a in arrays if a is not None]
    flat = np.concatenate(present).astype(dtype, copy=False) if present else np.empty(0, dtype)
    mask = None
    if len(present) != len(arrays):
        mask = pa.array([a is None for a in arrays], pa.bool_())
    return pa.ListArray.from_arrays(
        pa.array(offsets, pa.int32()),
        pa.array(flat, arrow_type.value_type),
        type=arrow_type,
        mask=mask,
    )


def _rows_to_table(rows: Sequence[SpectrumRow], schema: pa.Schema) -> pa.Table:
    """Convert buffered rows into a table with ``schema``.

    Args:
        rows: The rows.
        schema: Target schema (a subset of the Format v1 columns).

    Returns:
        The table.
    """
    columns = []
    for field in schema:
        if field.name in ARRAY_COLUMNS:
            dtype = np.float64 if field.name == "wavelengths" else np.float32
            columns.append(_list_array([r.array(field.name) for r in rows], dtype, field.type))
        else:
            columns.append(pa.array([r.meta.get(field.name) for r in rows], type=field.type))
    return pa.Table.from_arrays(columns, schema=schema)


def _present_optional_columns(rows: Sequence[SpectrumRow]) -> list[str]:
    """Find the optional columns that hold at least one non-null value.

    Args:
        rows: The rows.

    Returns:
        Names of the non-empty optional columns, in canonical order.
    """
    present: list[str] = []
    for column in _OPTIONAL_COLUMNS:
        if column.name in ARRAY_COLUMNS:
            if any(r.array(column.name) is not None for r in rows):
                present.append(column.name)
        elif any(r.meta.get(column.name) is not None for r in rows):
            present.append(column.name)
    return present


def _check_rows(
    rows: Sequence[SpectrumRow],
    collection: CollectionInfo,
    datasets: Mapping[str, DatasetInfo],
) -> None:
    """Check the cross-row rules the writer is responsible for.

    Args:
        rows: The rows.
        collection: The collection metadata.
        datasets: The footer datasets.

    Raises:
        ValueError: On a duplicate id (F5), a row of another collection
            (F6), an unknown ``dataset_id``, a dataset whose ``id`` differs
            from its key or whose ``spectra_included`` differs from its row
            count (F12), or an invalid licence expression or undefined
            ``LicenseRef-`` (F11).
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    licences: set[str] = {collection.license}
    licences.update(ds.license for ds in datasets.values() if ds.license)
    # Re-validate: model_copy(update=...) and model_construct bypass validation.
    CollectionInfo.model_validate(collection.model_dump())
    for key, ds in datasets.items():
        DatasetInfo.model_validate(ds.model_dump())
        if ds.id != key:
            raise ValueError(f"datasets key {key!r} holds DatasetInfo with id {ds.id!r}")
    for row in rows:
        rid = row.id
        if rid in seen:
            duplicates.add(rid)
        seen.add(rid)
        if row.meta["collection"] != collection.id:
            raise ValueError(f"{rid}: collection {row.meta['collection']!r} != {collection.id!r}")
        dataset_id = row.meta.get("dataset_id")
        if dataset_id is not None and dataset_id not in datasets:
            raise ValueError(f"{rid}: dataset_id {dataset_id!r} is not in the footer datasets")
        licences.add(row.meta["license"])
    if duplicates:
        raise ValueError(f"duplicate spectrum ids: {sorted(duplicates)[:10]}")
    counts: dict[str, int] = {}
    for row in rows:
        row_dataset = row.meta.get("dataset_id")
        if row_dataset is not None:
            counts[str(row_dataset)] = counts.get(str(row_dataset), 0) + 1
    for key, ds in datasets.items():
        if ds.spectra_included is not None and ds.spectra_included != counts.get(key, 0):
            raise ValueError(
                f"F12: dataset {key!r} declares spectra_included={ds.spectra_included} "
                f"but {counts.get(key, 0)} rows reference it"
            )
    undefined = {
        ref
        for expr in licences
        for ref in license_refs_used(expr)
        if ref not in collection.license_refs
    }
    if undefined:
        raise ValueError(f"LicenseRef identifiers not defined in license_refs: {sorted(undefined)}")


class NonConformantFileError(ValueError):
    """Raised when a file the writer produced fails a Format v1 must-rule.

    Attributes:
        failures: ``(rule id, message)`` for every failed rule.
    """

    def __init__(self, path: Path, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        detail = "; ".join(f"{rule}: {message}" for rule, message in failures)
        super().__init__(f"refusing to write non-conformant file {path.name}: {detail}")


def _conformance_failures(path: Path) -> list[tuple[str, str]]:
    """Run the reference conformance checker on a written file.

    Args:
        path: The file to check.

    Returns:
        ``(rule id, message with up to three examples)`` for each failed
        must-rule; empty when the file conforms.
    """
    from openspeclib.conformance import check_collection

    report = check_collection(path)
    if report.conformant:
        return []
    failures = []
    for rule in report.to_dict()["rules"]:
        if rule["rule"] not in report.failed:
            continue
        examples = [str(e) for e in rule.get("examples") or []][:3]
        detail = rule.get("message") or f"{rule.get('count', 0)} problem(s)"
        if examples:
            detail += " — e.g. " + " | ".join(examples)
        failures.append((rule["rule"], f"{rule.get('title', '')}: {detail}"))
    return failures or [("?", "the file does not conform")]


def _index_conformance_failures(path: Path) -> list[tuple[str, str]]:
    """Run the reference conformance checker on a written index file.

    Args:
        path: The index file to check (its collection files are checked too).

    Returns:
        ``(rule id, message with up to three examples)`` for each failed
        must-rule of the index or of a listed collection file; empty when
        everything conforms.
    """
    from openspeclib.conformance import check_index

    report = check_index(path)
    if report.conformant:
        return []
    failures = []
    for each in [report, *report.collections]:
        name = Path(each.location).name
        for rule in each.results:
            if rule.status != "failed":
                continue
            examples = rule.examples[:3]
            detail = rule.message or f"{rule.count} problem(s)"
            if examples:
                detail += " — e.g. " + " | ".join(examples)
            failures.append((rule.rule_id, f"{name}: {rule.title}: {detail}"))
    return failures or [("?", "the index does not conform")]


@dataclass(frozen=True)
class WriteResult:
    """Outcome of writing a collection file.

    Attributes:
        path: The written file.
        num_spectra: Rows written.
        footer: The ``openspeclib`` footer object written.
    """

    path: Path
    num_spectra: int
    footer: dict[str, Any]


def write_rows(
    rows: list[SpectrumRow],
    path: Path,
    collection: CollectionInfo,
    datasets: Mapping[str, DatasetInfo] | None = None,
    *,
    row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
    created: str | None = None,
) -> WriteResult:
    """Write prepared rows as a Format v1 collection file.

    The rows are sorted by id in place. Only optional columns holding at
    least one value are written, and only their extensions are declared.

    Args:
        rows: The rows (see :func:`record_to_row`).
        path: Destination file.
        collection: The footer ``collection`` object.
        datasets: Contributing datasets keyed by dataset id.
        row_group_size: Rows per Parquet row group.
        created: RFC 3339 creation timestamp for the footer.

    Returns:
        The write result.

    The file is written to ``{path}.part``, checked with the reference
    conformance checker (:func:`openspeclib.conformance.check_collection`)
    and only then renamed, so the writer never leaves a non-conformant file
    at ``path``. Datasets that no row references are left out of the footer
    (F12).

    Raises:
        ValueError: If there are no rows or a cross-row rule is broken
            (see :func:`_check_rows`).
        NonConformantFileError: If the written file fails a must-rule; the
            message names each rule with examples.
    """
    if not rows:
        raise ValueError(f"no spectra to write for collection {collection.id!r}")
    datasets = dict(datasets or {})
    _check_rows(rows, collection, datasets)
    used = {row.meta.get("dataset_id") for row in rows}
    unused = sorted(key for key in datasets if key not in used)
    if unused:
        logger.info("%s: leaving out %d datasets without rows", collection.id, len(unused))
        datasets = {key: ds for key, ds in datasets.items() if key in used}
    rows.sort(key=lambda r: r.id)

    optional = _present_optional_columns(rows)
    extensions = {COLUMNS[name].extension for name in optional} - {"*"}
    footer = build_footer(collection, datasets, [e for e in extensions if e], created)
    schema = collection_schema(optional)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        with pq.ParquetWriter(
            tmp, schema, store_schema=False, **_writer_options(schema, STATISTICS_COLUMNS)
        ) as writer:
            for start in range(0, len(rows), row_group_size):
                table = _rows_to_table(rows[start : start + row_group_size], schema)
                writer.write_table(table, row_group_size=row_group_size)
            writer.add_key_value_metadata({FOOTER_KEY: _footer_json(footer)})
        failures = _conformance_failures(tmp)
        if failures:
            raise NonConformantFileError(path, failures)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    logger.info("Wrote %d spectra for %s to %s", len(rows), collection.id, path)
    return WriteResult(path=path, num_spectra=len(rows), footer=footer)


def write_collection(
    records: Iterable[SpectrumRecord],
    path: Path,
    collection: CollectionInfo,
    datasets: Mapping[str, DatasetInfo] | None = None,
    *,
    row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
    created: str | None = None,
) -> WriteResult:
    """Write spectrum records as a conformant Format v1 collection file.

    Args:
        records: The collection's records (any order; ids must be unique).
        path: Destination file, conventionally ``{collection_id}.parquet``.
        collection: The footer ``collection`` object.
        datasets: Contributing datasets keyed by dataset id (aggregated
            collections only).
        row_group_size: Rows per Parquet row group.
        created: RFC 3339 creation timestamp for the footer.

    Returns:
        The write result.
    """
    interner = AxisInterner()
    rows = [record_to_row(r, interner) for r in records]
    return write_rows(
        rows, path, collection, datasets, row_group_size=row_group_size, created=created
    )


# ---------------------------------------------------------------------------
# Reading collection files back into records
# ---------------------------------------------------------------------------


def row_to_record(row: Mapping[str, Any]) -> SpectrumRecord:
    """Rebuild a :class:`SpectrumRecord` from a collection-file row.

    Args:
        row: A row dict (``pa.Table.to_pylist()``); absent optional columns
            are treated as null.

    Returns:
        The record.
    """

    def get(name: str) -> Any:
        return row.get(name)

    extra = get("extra")
    return SpectrumRecord(
        id=row["id"],
        collection=row["collection"],
        original_id=row["original_id"],
        name=row["name"],
        license=row["license"],
        dataset_id=get("dataset_id"),
        material=Material(
            name=row["material_name"],
            category=row["material_category"],
            subcategory=get("material_subcategory"),
            formula=get("material_formula"),
            keywords=list(get("material_keywords") or []),
        ),
        sample=Sample(
            id=get("sample_id"),
            description=get("sample_description"),
            particle_size=get("sample_particle_size"),
            origin=get("sample_origin"),
            owner=get("sample_owner"),
            collection_date=get("sample_collection_date"),
            preparation=get("sample_preparation"),
        ),
        measurement=Measurement(
            quantity=row["quantity"],
            geometry=row["geometry"],
            instrument=get("measurement_instrument"),
            instrument_type=get("measurement_instrument_type"),
            laboratory=get("measurement_laboratory"),
            date=get("measurement_date"),
            processing=list(get("measurement_processing") or []),
            light_source=get("measurement_light_source"),
            venue=get("measurement_venue"),
            acquisition_method=get("measurement_acquisition_method"),
            foreoptic=get("measurement_foreoptic"),
            reference=get("measurement_reference"),
        ),
        spectral_data=SpectralData(
            wavelength_unit=row["wavelength_unit"],
            wavelengths=list(row["wavelengths"]),
            values=list(row["values"]),
            bandpass_fwhm=get("bandpass_fwhm"),
            values_stddev=get("values_stddev"),
        ),
        quality=Quality(notes=get("quality_notes")),
        extra=json.loads(extra) if extra else {},
    )


def iter_records(path: Path, batch_size: int = 1000) -> Iterator[SpectrumRecord]:
    """Stream the records of a collection file.

    Builds a Pydantic model per row, so use :mod:`openspeclib.reader` for
    fast access; this is for round trips and tooling.

    Args:
        path: Collection file.
        batch_size: Rows per read batch.

    Yields:
        Each record, in file order.
    """
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_size):
        for row in batch.to_pylist():
            yield row_to_record(row)


# ---------------------------------------------------------------------------
# Index files
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 of a file.

    Args:
        path: The file.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _num_points(pf: pq.ParquetFile) -> pa.Array:
    """Compute the length of ``values`` for every row, one row group at a time.

    Args:
        pf: An open collection file.

    Returns:
        An int32 array of point counts, in file order.
    """
    lengths = [
        pc.list_value_length(pf.read_row_group(i, columns=["values"])["values"])
        for i in range(pf.num_row_groups)
    ]
    chunked = (
        pa.chunked_array(lengths, type=pa.int32()) if lengths else pa.chunked_array([], pa.int32())
    )
    return chunked.combine_chunks().cast(pa.int32())


def write_index(
    collection_files: Sequence[Path],
    index_path: Path,
    *,
    release: str | None = None,
    created: str | None = None,
    with_sha256: bool = True,
) -> dict[str, Any]:
    """Write an index file over collection files (spec section 7).

    The index holds every non-array column found in any of the collection
    files (null for rows of files that lack the column; a list column that
    only a newer minor version defines is left out, since it may be a
    per-band array), plus
    ``num_points`` and ``file``, sorted by ``id``. Each ``file`` is the
    collection file's path relative to the index file's directory.

    Args:
        collection_files: Collection files to index.
        index_path: Destination index file.
        release: Release version recorded in the footer, if any.
        created: RFC 3339 creation timestamp; defaults to now.
        with_sha256: Record each collection file's SHA-256.

    Returns:
        The ``openspeclib_index`` footer object written.

    Raises:
        ValueError: If a collection file lacks its footer or two files hold
            the same spectrum id.
        NonConformantFileError: If the written index (checked with
            :func:`openspeclib.conformance.check_index`, including its
            collection files) fails a must-rule. The index is written to a
            temporary ``.part`` file next to ``index_path`` and checked there;
            on failure the ``.part`` file is removed and ``index_path`` is left
            as it was (a previous index there is kept, and none is created).
            The error names ``index_path``.
    """
    base = index_path.parent.resolve()
    tables: list[pa.Table] = []
    entries: list[dict[str, Any]] = []
    present: set[str] = set()
    for path in collection_files:
        pf = pq.ParquetFile(path)
        footer = read_footer(path)
        names = [n for n in pf.schema_arrow.names if n not in ARRAY_COLUMNS]
        table = pf.read(columns=names)
        rel = os.path.relpath(Path(path).resolve(), base).replace(os.sep, "/")
        table = table.append_column(INDEX_ONLY_COLUMNS[0].field(), _num_points(pf))
        table = table.append_column(
            INDEX_ONLY_COLUMNS[1].field(), pa.array([rel] * table.num_rows, pa.string())
        )
        # A column a newer minor version defines may be a per-band array,
        # which an index leaves out; such list columns are not carried.
        present.update(
            n
            for n in names
            if n in COLUMNS
            or n.startswith("x_")
            or not pa.types.is_list(pf.schema_arrow.field(n).type)
            and not pa.types.is_large_list(pf.schema_arrow.field(n).type)
        )
        tables.append(table)
        entry: dict[str, Any] = {"file": rel, "num_spectra": int(pf.metadata.num_rows)}
        if with_sha256:
            entry["sha256"] = sha256_file(Path(path))
        entry["metadata"] = footer
        entries.append(entry)

    columns = [c for c in COLLECTION_COLUMNS if c.name in present and c.name not in ARRAY_COLUMNS]
    fields = [c.field() for c in columns] + [c.field() for c in INDEX_ONLY_COLUMNS]
    # x_ columns, and columns a newer 1.x minor version defines (section 11),
    # are carried with the type the collection files give them.
    known = {c.name for c in COLLECTION_COLUMNS} | {c.name for c in INDEX_ONLY_COLUMNS}
    other_names = sorted(present - known)
    for name in other_names:
        types = {t.schema.field(name).type for t in tables if name in t.column_names}
        if len(types) != 1:
            raise ValueError(
                f"column {name!r} has conflicting types across collection files: "
                f"{sorted(str(t) for t in types)}"
            )
        fields.append(pa.field(name, types.pop()))
    footer_doc = {
        "format_version": FORMAT_VERSION,
        **({"release": release} if release else {}),
        "created": created or utc_timestamp(),
        "generator": _generator(),
        "collections": entries,
    }
    schema = pa.schema(fields)

    aligned = []
    for table in tables:
        arrays = []
        for field in schema:
            if field.name in table.column_names:
                arrays.append(table[field.name].cast(field.type))
            else:
                arrays.append(pa.nulls(table.num_rows, field.type))
        aligned.append(pa.Table.from_arrays(arrays, schema=schema))
    combined = pa.concat_tables(aligned) if aligned else schema.empty_table()
    ids = combined["id"]
    if pc.count_distinct(ids).as_py() != combined.num_rows:
        raise ValueError("collection files share spectrum ids; cannot build an index")
    combined = combined.sort_by("id")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_name(index_path.name + ".part")
    stats = STATISTICS_COLUMNS + ("file",)
    try:
        with pq.ParquetWriter(
            tmp, schema, store_schema=False, **_writer_options(schema, stats)
        ) as writer:
            writer.write_table(combined, row_group_size=INDEX_ROW_GROUP_SIZE)
            writer.add_key_value_metadata({INDEX_FOOTER_KEY: _footer_json(footer_doc)})
        # The .part file sits next to the final one, so its relative `file`
        # values resolve the same way.
        failures = _index_conformance_failures(tmp)
        if failures:
            # Name the destination in messages, not the temporary .part file.
            part = f"{tmp.name}: "
            failures = [
                (rule, f"{index_path.name}: {text[len(part):]}" if text.startswith(part) else text)
                for rule, text in failures
            ]
            raise NonConformantFileError(index_path, failures)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(index_path)
    logger.info("Wrote index of %d spectra to %s", combined.num_rows, index_path)
    return footer_doc


# ---------------------------------------------------------------------------
# Vectorised value statistics (quality profile)
# ---------------------------------------------------------------------------

# Any finite spectral value whose magnitude exceeds this is a fill/sentinel
# value rather than a measurement (rule F9).
SENTINEL_ABS_THRESHOLD = 1e10


@dataclass(frozen=True)
class SpectralValueStats:
    """Per-spectrum summary statistics of a ``values`` column.

    Every attribute is a list with one element per spectrum, in row order.
    ``min``/``max``/``median`` are computed over *valid* values only — finite
    and not a sentinel — and are ``None`` when a spectrum has no valid values.

    Attributes:
        n_values: Length of each spectrum's values list (0 for a null list).
        n_nan: Number of NaN values.
        n_non_finite: Number of null or ±infinite values (NaN excluded).
        n_sentinel: Number of finite values with ``|v| > sentinel_threshold``.
        n_below: Number of valid values strictly below the spectrum's
            ``lower`` bound (0 when no bounds were given).
        n_above: Number of valid values strictly above the spectrum's
            ``upper`` bound (0 when no bounds were given).
        min: Minimum valid value, or ``None``.
        max: Maximum valid value, or ``None``.
        median: Approximate (t-digest) median of the valid values, or ``None``.
    """

    n_values: list[int]
    n_nan: list[int]
    n_non_finite: list[int]
    n_sentinel: list[int]
    n_below: list[int]
    n_above: list[int]
    min: list[float | None]
    max: list[float | None]
    median: list[float | None]


def compute_value_stats(
    values: pa.Array | pa.ChunkedArray,
    sentinel_threshold: float = SENTINEL_ABS_THRESHOLD,
    lower: list[float] | None = None,
    upper: list[float] | None = None,
) -> SpectralValueStats:
    """Compute per-spectrum NaN/sentinel counts and valid-value summaries with pyarrow.

    Runs entirely in Arrow compute kernels over the flattened list column.
    Null lists, null elements and empty lists are all handled.

    Args:
        values: A ``list<float>`` array (or chunked array) with one list per
            spectrum, e.g. the ``values`` column.
        sentinel_threshold: Finite values with a magnitude above this are
            counted as sentinels and excluded from the valid-value summaries.
        lower: Optional per-spectrum lower bound; valid values strictly below
            it are counted in ``n_below``. Compared in the values' own float
            type, so pass bounds already rounded to that precision.
        upper: Optional per-spectrum upper bound for ``n_above``.

    Returns:
        A ``SpectralValueStats`` with one entry per input list.

    Raises:
        ValueError: If ``lower``/``upper`` lengths differ from ``values``.
    """
    if isinstance(values, pa.ChunkedArray):
        values = values.combine_chunks()
    n = len(values)
    for name, bound in (("lower", lower), ("upper", upper)):
        if bound is not None and len(bound) != n:
            raise ValueError(f"{name} has {len(bound)} entries for {n} spectra")
    n_values = [int(v) for v in pc.fill_null(pc.list_value_length(values), 0).to_pylist()]
    n_nan = [0] * n
    n_non_finite = [0] * n
    n_sentinel = [0] * n
    n_below = [0] * n
    n_above = [0] * n
    v_min: list[float | None] = [None] * n
    v_max: list[float | None] = [None] * n
    v_median: list[float | None] = [None] * n

    flat = pc.list_flatten(values)
    if len(flat) > 0:
        parents = pc.list_parent_indices(values)
        # Every mask is made null-free up front: Arrow's plain and/or are not
        # Kleene, so a single null element would otherwise null out a sum.
        finite = pc.fill_null(pc.is_finite(flat), False)
        is_nan = pc.fill_null(pc.is_nan(flat), False)
        is_other = pc.or_(pc.fill_null(pc.is_inf(flat), False), pc.is_null(flat))
        is_sentinel = pc.fill_null(
            pc.and_(finite, pc.greater(pc.abs(flat), sentinel_threshold)), False
        )
        valid = pc.and_(finite, pc.invert(is_sentinel))
        masked = pc.if_else(valid, flat, pa.scalar(None, type=flat.type))
        columns: dict[str, Any] = {
            "p": parents,
            "nan": is_nan.cast(pa.int64()),
            "other": is_other.cast(pa.int64()),
            "sentinel": is_sentinel.cast(pa.int64()),
            "v": masked,
        }
        aggregations = [
            ("nan", "sum"),
            ("other", "sum"),
            ("sentinel", "sum"),
            ("v", "min"),
            ("v", "max"),
            ("v", "approximate_median"),
        ]
        for name, bound, op in (("below", lower, pc.less), ("above", upper, pc.greater)):
            if bound is None:
                continue
            per_value = pc.take(pa.array(bound, type=flat.type), parents)
            columns[name] = pc.fill_null(op(masked, per_value), False).cast(pa.int64())
            aggregations.append((name, "sum"))
        cols = pa.table(columns).group_by("p").aggregate(aggregations).to_pydict()

        def _count(key: str, i: int) -> int:
            value = cols[key][i] if key in cols else 0
            return int(value or 0)

        def _value(key: str, i: int) -> float | None:
            value = cols[key][i]
            return None if value is None else float(value)

        for i, p in enumerate(cols["p"]):
            n_nan[p] = _count("nan_sum", i)
            n_non_finite[p] = _count("other_sum", i)
            n_sentinel[p] = _count("sentinel_sum", i)
            n_below[p] = _count("below_sum", i)
            n_above[p] = _count("above_sum", i)
            v_min[p] = _value("v_min", i)
            v_max[p] = _value("v_max", i)
            v_median[p] = _value("v_approximate_median", i)

    return SpectralValueStats(
        n_values=n_values,
        n_nan=n_nan,
        n_non_finite=n_non_finite,
        n_sentinel=n_sentinel,
        n_below=n_below,
        n_above=n_above,
        min=v_min,
        max=v_max,
        median=v_median,
    )
