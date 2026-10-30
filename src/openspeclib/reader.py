"""Fast reader for OpenSpecLib Format v1 collection and index files.

>>> import openspeclib
>>> lib = openspeclib.open("usgs_splib07.parquet")
>>> lib.metadata["collection"]["title"]
'USGS Spectral Library Version 7'
>>> hits = lib.search(material="olivine", wavelength_range_nm=(400, 2500))
>>> spectrum = lib.get(hits["id"][0].as_py())
>>> spectrum.wavelengths, spectrum.values  # numpy arrays

Local files are read with pyarrow only. A single :meth:`Collection.get`
reads just the row group whose ``id`` statistics cover the id (files are
sorted by id in row groups of ~100), so it costs a few milliseconds and no
Pydantic validation. Searches run as Arrow compute filters over the
non-array columns, which are read once and cached.

``http(s)://`` locations are read with DuckDB (``pip install
openspeclib[remote]``), which fetches only the footer and the row groups a
query needs via HTTP Range requests. Each id is fetched with its own
``WHERE id = '...'`` query because DuckDB prunes row groups for single
equality predicates but not for ``IN`` lists.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from openspeclib.storage import ARRAY_COLUMNS, FOOTER_KEY, INDEX_FOOTER_KEY

__all__ = ["Collection", "Index", "Spectrum", "open", "open_index"]

# Major version of the format this reader understands (spec section 11).
SUPPORTED_MAJOR_VERSION = 1


@dataclass
class Spectrum:
    """One spectrum with its arrays as numpy and its other columns as a dict.

    Attributes:
        id: The spectrum id.
        wavelengths: The axis in ``wavelength_unit`` (float64).
        values: The values (float32); NaN marks a missing band.
        wavelength_unit: ``nm``, ``um`` or ``cm-1``.
        metadata: Every other column (``extra`` parsed into a dict).
        bandpass_fwhm: Per-band FWHM, if the file has it.
        values_stddev: Per-band standard deviation, if the file has it.
    """

    id: str
    wavelengths: np.ndarray
    values: np.ndarray
    wavelength_unit: str
    metadata: dict[str, Any] = field(default_factory=dict)
    bandpass_fwhm: np.ndarray | None = None
    values_stddev: np.ndarray | None = None

    @property
    def quantity(self) -> str:
        """Return the measured quantity (vocabulary ``quantity``)."""
        return str(self.metadata.get("quantity"))

    def wavelengths_nm(self) -> np.ndarray:
        """Return the axis converted to nanometres.

        Returns:
            A float64 array (wavenumbers convert as 1e7 / ν, so a cm-1 axis
            comes back in reverse wavelength order).
        """
        if self.wavelength_unit == "um":
            return self.wavelengths * 1000.0
        if self.wavelength_unit == "cm-1":
            return 1e7 / self.wavelengths
        return self.wavelengths.copy()


def _is_url(location: str) -> bool:
    """Tell whether a location is an http(s) URL.

    Args:
        location: Path or URL.

    Returns:
        ``True`` for ``http://`` and ``https://`` locations.
    """
    return location.startswith(("http://", "https://"))


def _duckdb() -> Any:
    """Import DuckDB for remote reads.

    Returns:
        The ``duckdb`` module.

    Raises:
        ImportError: If DuckDB is not installed.
    """
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Reading OpenSpecLib files over http(s) needs DuckDB: "
            "pip install 'openspeclib[remote]'"
        ) from exc
    return duckdb


def _fetch_arrow(con: Any, sql: str) -> pa.Table:
    """Run a query and fetch the result as a pyarrow table across DuckDB versions.

    The query runs as a relation (``con.sql``): DuckDB 2.0 warns that Arrow
    output from an ``execute`` result copies the rows.

    Args:
        con: A DuckDB connection.
        sql: The query.

    Returns:
        The result table.
    """
    relation = con.sql(sql)
    fetch = getattr(relation, "to_arrow_table", None) or relation.fetch_arrow_table
    return fetch()


def _fetch_first(con: Any, sql: str, params: list[Any] | None = None) -> tuple[Any, ...] | None:
    """Run a query and return its first row.

    Fetches every row: DuckDB 2.0 refuses further work on a connection while an
    ``execute`` result is unfinished, which ``fetchone`` leaves it.

    Args:
        con: A DuckDB connection.
        sql: The query.
        params: Query parameters.

    Returns:
        The first row, or ``None`` when there is none.
    """
    rows = con.execute(sql, params or []).fetchall()
    return rows[0] if rows else None


def _sql_string(value: str) -> str:
    """Quote a value as an SQL string literal.

    Args:
        value: The value.

    Returns:
        The quoted literal.
    """
    return "'" + value.replace("'", "''") + "'"


def _check_version(footer: dict[str, Any], location: str) -> None:
    """Reject a file whose format major version this reader does not support.

    Args:
        footer: The parsed footer object.
        location: The file, for the error message.

    Raises:
        ValueError: If ``format_version`` is missing or has another major version.
    """
    version = str(footer.get("format_version", ""))
    major = version.split(".", 1)[0]
    if not major.isdigit() or int(major) != SUPPORTED_MAJOR_VERSION:
        raise ValueError(
            f"{location}: unsupported OpenSpecLib format_version {version!r} "
            f"(this reader supports {SUPPORTED_MAJOR_VERSION}.x)"
        )


def _to_numpy(value: Any, dtype: Any) -> np.ndarray | None:
    """Convert an Arrow list scalar to a numpy array.

    Args:
        value: A ``pa.ListScalar`` (possibly null).
        dtype: Target dtype.

    Returns:
        The array, or ``None`` for a null list.
    """
    if value is None or not value.is_valid:
        return None
    return np.asarray(value.values.to_numpy(zero_copy_only=False), dtype=dtype)


def _row_to_spectrum(table: pa.Table, row: int) -> Spectrum:
    """Build a :class:`Spectrum` from one row of a collection table.

    Args:
        table: A table with (at least) the core columns.
        row: Row position.

    Returns:
        The spectrum.
    """
    metadata: dict[str, Any] = {}
    arrays: dict[str, np.ndarray | None] = {}
    for name in table.column_names:
        scalar = table.column(name)[row]
        if name in ARRAY_COLUMNS:
            arrays[name] = _to_numpy(scalar, np.float64 if name == "wavelengths" else np.float32)
        else:
            metadata[name] = scalar.as_py()
    extra = metadata.get("extra")
    if isinstance(extra, str):
        metadata["extra"] = json.loads(extra)
    wavelengths = arrays.get("wavelengths")
    values = arrays.get("values")
    return Spectrum(
        id=str(metadata["id"]),
        wavelengths=wavelengths if wavelengths is not None else np.empty(0),
        values=values if values is not None else np.empty(0, np.float32),
        wavelength_unit=str(metadata.get("wavelength_unit")),
        metadata=metadata,
        bandpass_fwhm=arrays.get("bandpass_fwhm"),
        values_stddev=arrays.get("values_stddev"),
    )


def _search_mask(
    table: pa.Table,
    *,
    material: str | None,
    category: str | Sequence[str] | None,
    quantity: str | Sequence[str] | None,
    geometry: str | Sequence[str] | None,
    wavelength_range_nm: tuple[float | None, float | None] | None,
    dataset_id: str | Sequence[str] | None,
    collection: str | Sequence[str] | None,
) -> Any:
    """Build the boolean filter for :meth:`Collection.search`.

    Args:
        table: The metadata table.
        material: Case-insensitive substring of ``material_name`` or ``name``.
        category: ``material_category`` value(s).
        quantity: ``quantity`` value(s).
        geometry: ``geometry`` value(s).
        wavelength_range_nm: ``(lo, hi)``; keeps spectra whose axis covers
            the whole range. Either end may be ``None``.
        dataset_id: ``dataset_id`` value(s).
        collection: ``collection`` value(s).

    Returns:
        A boolean array, or ``None`` when no criterion is given.
    """
    masks: list[Any] = []

    def one_of(column: str, wanted: str | Sequence[str] | None) -> None:
        if wanted is None:
            return
        values = [wanted] if isinstance(wanted, str) else list(wanted)
        if column not in table.column_names:
            masks.append(pa.array(np.zeros(table.num_rows, dtype=bool)))
            return
        masks.append(pc.is_in(table[column], value_set=pa.array(values, pa.string())))

    one_of("material_category", category)
    one_of("quantity", quantity)
    one_of("geometry", geometry)
    one_of("dataset_id", dataset_id)
    one_of("collection", collection)
    if material:
        masks.append(
            pc.or_(
                pc.match_substring(table["material_name"], material, ignore_case=True),
                pc.match_substring(table["name"], material, ignore_case=True),
            )
        )
    if wavelength_range_nm is not None:
        lo, hi = wavelength_range_nm
        if lo is not None:
            masks.append(pc.less_equal(table["wavelength_min_nm"], lo))
        if hi is not None:
            masks.append(pc.greater_equal(table["wavelength_max_nm"], hi))
    if not masks:
        return None
    mask = masks[0]
    for other in masks[1:]:
        mask = pc.and_(mask, other)
    return pc.fill_null(mask, False)


class _Searchable:
    """Shared search over a cached table of non-array columns."""

    _meta: pa.Table | None = None

    def _metadata_table(self) -> pa.Table:  # pragma: no cover - abstract
        raise NotImplementedError

    def search(
        self,
        material: str | None = None,
        category: str | Sequence[str] | None = None,
        quantity: str | Sequence[str] | None = None,
        wavelength_range_nm: tuple[float | None, float | None] | None = None,
        dataset_id: str | Sequence[str] | None = None,
        *,
        geometry: str | Sequence[str] | None = None,
        collection: str | Sequence[str] | None = None,
    ) -> pa.Table:
        """Find spectra by metadata. Criteria are combined with AND.

        Args:
            material: Case-insensitive substring of ``material_name`` or ``name``.
            category: ``material_category`` value or values.
            quantity: ``quantity`` value or values.
            wavelength_range_nm: ``(lo, hi)`` in nm; keeps spectra whose axis
                covers the whole range (either end may be ``None``).
            dataset_id: ``dataset_id`` value or values.
            geometry: ``geometry`` value or values.
            collection: ``collection`` value or values.

        Returns:
            The matching rows (non-array columns only), as a pyarrow table.
        """
        if self._meta is None:
            self._meta = self._metadata_table()
        mask = _search_mask(
            self._meta,
            material=material,
            category=category,
            quantity=quantity,
            geometry=geometry,
            wavelength_range_nm=wavelength_range_nm,
            dataset_id=dataset_id,
            collection=collection,
        )
        return self._meta if mask is None else self._meta.filter(mask)


class Collection(_Searchable):
    """A Format v1 collection file opened for reading.

    Attributes:
        location: The path or URL.
        metadata: The ``openspeclib`` footer object.
    """

    def __init__(self, location: str | os.PathLike[str]) -> None:
        """Open a collection file and read its footer.

        Args:
            location: Local path or http(s) URL.

        Raises:
            ValueError: If the file is not a Format v1 collection file.
        """
        self.location = os.fspath(location)
        self.remote = _is_url(self.location)
        self._pf: pq.ParquetFile | None = None
        self._ranges: list[tuple[str, str]] | None = None
        self._meta = None
        if self.remote:
            self._con = _duckdb().connect()
            raw = self._kv(FOOTER_KEY)
        else:
            self._pf = pq.ParquetFile(self.location)
            raw = (self._pf.metadata.metadata or {}).get(FOOTER_KEY.encode("utf-8"))
        if raw is None:
            raise ValueError(f"{self.location} is not an OpenSpecLib collection file")
        self.metadata: dict[str, Any] = json.loads(raw)
        _check_version(self.metadata, self.location)

    # -- remote helpers -------------------------------------------------

    def _kv(self, key: str) -> bytes | None:
        """Fetch one footer key of a remote file.

        Args:
            key: The footer key.

        Returns:
            The raw value, or ``None``.
        """
        row = _fetch_first(
            self._con,
            "SELECT value FROM parquet_kv_metadata(?) WHERE decode(key) = ?",
            [self.location, key],
        )
        return None if row is None else bytes(row[0])

    def _sql(self, columns: Iterable[str] | None, where: str | None = None) -> pa.Table:
        """Run a DuckDB query against the remote file.

        Args:
            columns: Columns to select (all when ``None``).
            where: Optional SQL condition.

        Returns:
            The result as a pyarrow table.
        """
        select = "*" if columns is None else ", ".join(f'"{c}"' for c in columns)
        sql = f"SELECT {select} FROM read_parquet({_sql_string(self.location)})"
        if where:
            sql += f" WHERE {where}"
        return _fetch_arrow(self._con, sql)

    # -- public API -----------------------------------------------------

    @property
    def collection_id(self) -> str:
        """Return the collection id from the footer."""
        return str(self.metadata["collection"]["id"])

    @property
    def column_names(self) -> list[str]:
        """Return the file's column names."""
        if self._pf is not None:
            return list(self._pf.schema_arrow.names)
        return list(self._sql(None, "false").column_names)

    def __len__(self) -> int:
        """Return the number of spectra."""
        if self._pf is not None:
            return int(self._pf.metadata.num_rows)
        row = _fetch_first(
            self._con, f"SELECT count(*) FROM read_parquet({_sql_string(self.location)})"
        )
        assert row is not None
        return int(row[0])

    def __repr__(self) -> str:
        """Return a short description."""
        return f"<openspeclib.Collection {self.collection_id!r} at {self.location!r}>"

    def _row_group_ranges(self) -> list[tuple[str, str]]:
        """Return the (min, max) id of each row group from the statistics.

        Returns:
            One pair per row group; an empty string bound when statistics are
            missing (the group is then always scanned).
        """
        if self._ranges is None:
            assert self._pf is not None
            md = self._pf.metadata
            # Statistics are addressed by Parquet leaf path, never by Arrow
            # field position: list columns have their own leaves.
            leaves = [md.schema.column(j).path for j in range(md.num_columns)]
            col = leaves.index("id")
            ranges: list[tuple[str, str]] = []
            for i in range(md.num_row_groups):
                stats = md.row_group(i).column(col).statistics
                if stats is not None and stats.has_min_max:
                    lo, hi = stats.min, stats.max
                    if isinstance(lo, bytes):
                        lo, hi = lo.decode("utf-8"), hi.decode("utf-8")
                    ranges.append((str(lo), str(hi)))
                else:
                    ranges.append(("", ""))
            self._ranges = ranges
        return self._ranges

    def get(self, spectrum_id: str) -> Spectrum:
        """Fetch one spectrum by id.

        Args:
            spectrum_id: The spectrum id.

        Returns:
            The spectrum.

        Raises:
            KeyError: If the id is not in the file.
        """
        if self.remote:
            table = self._sql(None, f"id = {_sql_string(spectrum_id)}")
            if table.num_rows == 0:
                raise KeyError(spectrum_id)
            return _row_to_spectrum(table, 0)
        assert self._pf is not None
        for i, (lo, hi) in enumerate(self._row_group_ranges()):
            if lo and not (lo <= spectrum_id <= hi):
                continue
            table = self._pf.read_row_group(i)
            position = pc.index(table["id"], spectrum_id).as_py()
            if position >= 0:
                return _row_to_spectrum(table, position)
        raise KeyError(spectrum_id)

    def get_many(self, spectrum_ids: Iterable[str]) -> list[Spectrum]:
        """Fetch several spectra, one lookup per id.

        Args:
            spectrum_ids: The ids.

        Returns:
            The spectra, in the order given.
        """
        return [self.get(i) for i in spectrum_ids]

    def to_table(
        self,
        columns: Sequence[str] | None = None,
        filter: Any = None,
    ) -> pa.Table:
        """Read the file (or some columns of it) as a pyarrow table.

        Args:
            columns: Columns to read; all when ``None`` and none when empty.
            filter: A ``pyarrow.compute.Expression`` (e.g.
                ``pc.field("quantity") == "emissivity"``) applied to the rows.
                Locally it is pushed down into the Parquet read. For a remote
                file the requested columns are downloaded in full and the
                filter is applied afterwards; use :meth:`search` or
                :meth:`get` (or select only small columns) to avoid
                transferring the arrays.

        Returns:
            The table.
        """
        selected = None if columns is None else list(columns)
        if self.remote:
            if selected == []:
                return self._sql(["id"]).select([])
            table = self._sql(selected)
            return table if filter is None else table.filter(filter)
        return pq.read_table(self.location, columns=selected, filters=filter)

    def to_pandas(self, include_arrays: bool = False) -> Any:
        """Return the collection as a pandas DataFrame.

        Args:
            include_arrays: Also include the per-band array columns.

        Returns:
            A ``pandas.DataFrame`` (requires pandas).
        """
        if include_arrays:
            return self.to_table().to_pandas()
        return self._metadata_table().to_pandas()

    def _metadata_table(self) -> pa.Table:
        """Read every non-array column.

        Returns:
            The table.
        """
        if self._meta is None:
            names = [n for n in self.column_names if n not in ARRAY_COLUMNS]
            self._meta = self.to_table(columns=names)
        return self._meta


class Index(_Searchable):
    """An index file over several collection files.

    Attributes:
        location: The path or URL.
        metadata: The ``openspeclib_index`` footer object.
    """

    def __init__(self, location: str | os.PathLike[str]) -> None:
        """Open an index file and read its footer.

        Args:
            location: Local path or http(s) URL.

        Raises:
            ValueError: If the file is not an OpenSpecLib index file.
        """
        self.location = os.fspath(location)
        self.remote = _is_url(self.location)
        self._meta = None
        self._collections: dict[str, Collection] = {}
        if self.remote:
            self._con = _duckdb().connect()
            row = _fetch_first(
                self._con,
                "SELECT value FROM parquet_kv_metadata(?) WHERE decode(key) = ?",
                [self.location, INDEX_FOOTER_KEY],
            )
            raw = None if row is None else bytes(row[0])
        else:
            md = pq.read_metadata(self.location)
            raw = (md.metadata or {}).get(INDEX_FOOTER_KEY.encode("utf-8"))
        if raw is None:
            raise ValueError(f"{self.location} is not an OpenSpecLib index file")
        self.metadata: dict[str, Any] = json.loads(raw)
        _check_version(self.metadata, self.location)

    def __len__(self) -> int:
        """Return the number of indexed spectra."""
        return sum(int(c["num_spectra"]) for c in self.metadata["collections"])

    def __repr__(self) -> str:
        """Return a short description."""
        return (
            f"<openspeclib.Index of {len(self.metadata['collections'])} files at {self.location!r}>"
        )

    @property
    def collections(self) -> dict[str, dict[str, Any]]:
        """Return each listed collection's footer, keyed by collection id."""
        return {
            c["metadata"]["collection"]["id"]: c["metadata"] for c in self.metadata["collections"]
        }

    def _metadata_table(self) -> pa.Table:
        """Read the whole index.

        Returns:
            The table.
        """
        if self._meta is None:
            if self.remote:
                self._meta = _fetch_arrow(
                    self._con, f"SELECT * FROM read_parquet({_sql_string(self.location)})"
                )
            else:
                self._meta = pq.read_table(self.location)
        return self._meta

    def to_table(self, columns: Sequence[str] | None = None, filter: Any = None) -> pa.Table:
        """Return the index (or some columns of it) as a pyarrow table.

        Args:
            columns: Columns to return; all when ``None`` and none when empty.
            filter: Optional ``pyarrow.compute.Expression``.

        Returns:
            The table.
        """
        table = self._metadata_table()
        if filter is not None:
            table = table.filter(filter)
        return table if columns is None else table.select(list(columns))

    def to_pandas(self) -> Any:
        """Return the index as a pandas DataFrame.

        Returns:
            A ``pandas.DataFrame`` (requires pandas).
        """
        return self._metadata_table().to_pandas()

    def _resolve(self, file: str) -> str:
        """Resolve a ``file`` value against the index location.

        Args:
            file: Relative path or URL from the ``file`` column.

        Returns:
            A path or URL that can be opened.
        """
        if _is_url(file):
            return file
        if self.remote:
            return urljoin(self.location, file)
        return str(Path(self.location).parent / file)

    def open_collection(self, file: str) -> Collection:
        """Open (and cache) one of the indexed collection files.

        Args:
            file: The ``file`` value or a collection id.

        Returns:
            The collection.

        Raises:
            KeyError: If no listed collection matches.
        """
        for entry in self.metadata["collections"]:
            if file in (entry["file"], entry["metadata"]["collection"]["id"]):
                key = entry["file"]
                if key not in self._collections:
                    self._collections[key] = Collection(self._resolve(key))
                return self._collections[key]
        raise KeyError(file)

    def get(self, spectrum_id: str) -> Spectrum:
        """Fetch one spectrum, opening the collection file that holds it.

        Args:
            spectrum_id: The spectrum id.

        Returns:
            The spectrum.

        Raises:
            KeyError: If the id is not indexed.
        """
        table = self._metadata_table()
        position = pc.index(table["id"], spectrum_id).as_py()
        if position < 0:
            raise KeyError(spectrum_id)
        return self.open_collection(table["file"][position].as_py()).get(spectrum_id)


def open(path_or_url: str | os.PathLike[str]) -> Collection:  # noqa: A001
    """Open a Format v1 collection file.

    Args:
        path_or_url: Local path or http(s) URL (remote needs DuckDB).

    Returns:
        The collection.
    """
    return Collection(path_or_url)


def open_index(path_or_url: str | os.PathLike[str]) -> Index:
    """Open an OpenSpecLib index file.

    Args:
        path_or_url: Local path or http(s) URL (remote needs DuckDB).

    Returns:
        The index.
    """
    return Index(path_or_url)
