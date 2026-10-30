"""CSV spectral tables: wide (one row per spectrum) and long (one row per point).

**Wide** is the EcoSIS upload layout: one row per spectrum, metadata
columns, and one numeric column per wavelength whose header is the
wavelength. Following EcoSIS conventions:

* a column is a band when its header is a number, optionally prefixed with
  ``X`` (as R's ``read.csv`` writes) or suffixed with a unit (``350nm``,
  ``0.35 µm``; case-insensitive). Band columns must form one contiguous,
  monotonic run; numeric-looking headers outside it (a ``2019`` column)
  are metadata;
* headers usually carry no unit, so a wide table is read in nanometres
  unless a header suffix, a ``wavelength_unit`` column or
  ``--wavelength-unit`` says otherwise;
* EcoSIS metadata names (``Common Name``, ``Latin Genus``/``Latin Species``,
  ``Target Type``, ``Measurement Quantity``, ``Measurement Units``,
  ``Instrument Manufacturer``/``Model``, ``Processing Averaged`` …) are
  mapped (see :data:`~openspeclib.convert.common.DEFAULT_ALIASES`);
* empty cells and ``NA``, ``NaN``, ``#N/A``… in value cells are missing
  values (NaN), and the sentinels ``-9999``/``-999`` are masked to NaN (and
  counted in ``extra``). In metadata cells only empty text is missing;
* values may be percent or 0–10000 integers; they are scaled to fractions
  (see :mod:`openspeclib.convert.scale`).

**Long** has one row per (spectrum, wavelength): a spectrum key column
(``id``, ``original_id`` or ``name``), a ``wavelength`` column and a
``value`` column, optionally ``bandpass_fwhm``/``fwhm`` and
``values_stddev``/``stddev``, plus metadata columns whose value is taken
from the spectrum's rows that have one (blank cells are skipped). A key
that repeats a wavelength with a different value, or whose rows give a
metadata column different values, is an error: two spectra probably share
the key.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from openspeclib.convert.common import (
    ConversionError,
    FieldMapper,
    ImportReport,
    SourceSpectrum,
    is_blank,
    is_missing,
    normalize_column,
    normalize_wavelength_unit,
)

__all__ = [
    "LAYOUTS",
    "detect_layout",
    "parse_band_header",
    "read_csv_spectra",
    "read_table",
    "read_spectra_table",
    "write_long_csv",
    "write_metadata_csv",
    "write_wide_csv",
]

LAYOUTS: tuple[str, ...] = ("wide", "long")

_BAND_HEADER = re.compile(
    r"^\s*x?(?P<num>(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*"
    r"(?P<unit>nm|um|µm|μm|cm-1|cm\^-1|cm⁻¹)?\s*$",
    re.IGNORECASE,
)

# Long-layout per-point column names (normalised) and the unit they imply.
_POINT_ALIASES: dict[str, tuple[str, str | None]] = {
    "wavelength": ("wavelength", None),
    "wavelengths": ("wavelength", None),
    "wl": ("wavelength", None),
    "lambda": ("wavelength", None),
    "wavelength_nm": ("wavelength", "nm"),
    "wavelength_um": ("wavelength", "um"),
    "wavelength_micrometers": ("wavelength", "um"),
    "wavelength_microns": ("wavelength", "um"),
    "wavenumber": ("wavelength", "cm-1"),
    "wavenumber_cm_1": ("wavelength", "cm-1"),
    "value": ("value", None),
    "values": ("value", None),
    "y": ("value", None),
    "reflectance": ("value", None),
    "transmittance": ("value", None),
    "emissivity": ("value", None),
    "absorbance": ("value", None),
    "fwhm": ("bandpass_fwhm", None),
    "bandpass_fwhm": ("bandpass_fwhm", None),
    "bandwidth": ("bandpass_fwhm", None),
    "stddev": ("values_stddev", None),
    "std": ("values_stddev", None),
    "sd": ("values_stddev", None),
    "values_stddev": ("values_stddev", None),
    "uncertainty": ("values_stddev", None),
}

# Long-layout value-column names that also state the quantity.
_QUANTITY_COLUMNS = frozenset({"reflectance", "transmittance", "emissivity", "absorbance"})


def parse_band_header(name: str) -> tuple[float, str | None] | None:
    """Parse a wide-layout column header as a wavelength.

    Args:
        name: The header, e.g. ``"350"``, ``"350.5"``, ``"X350"``, ``"2.5um"``,
            ``"350 NM"``.

    Returns:
        ``(wavelength, unit)`` (unit ``None`` when not stated), or ``None``
        when the header is not a band.
    """
    match = _BAND_HEADER.match(name)
    if match is None:
        return None
    value = float(match.group("num"))
    if not math.isfinite(value):
        return None
    unit = match.group("unit")
    if unit is None:
        return value, None
    unit = unit.lower().replace("^", "").replace("⁻¹", "-1")
    return value, normalize_wavelength_unit(unit)


@dataclass
class Table:
    """A CSV file: its header and a one-pass iterator over its rows.

    Attributes:
        header: Column names.
        rows: ``(line_number, cells)`` per non-blank data row, padded to the
            header width. Consumed once.
        delimiter: The delimiter used.
    """

    header: list[str]
    rows: Iterator[tuple[int, list[str]]]
    delimiter: str = ","


def _decode(data: bytes, path: Path, encoding: str | None, report: ImportReport | None) -> str:
    """Decode a text file: the given encoding, else UTF-16 by BOM, UTF-8, then Latin-1.

    Args:
        data: The file content.
        path: The file, for messages.
        encoding: Encoding to use, or ``None`` to detect.
        report: Receives a warning when Latin-1 is used.

    Returns:
        The text.

    Raises:
        ConversionError: If the given encoding fails.
    """
    if encoding is not None:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ConversionError(f"cannot decode {path} as {encoding}: {exc}") from exc
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if report is not None:
            report.warn(f"{path.name} is not UTF-8; read as Latin-1 (pass --encoding to choose)")
        return data.decode("latin-1")


def read_table(
    path: Path,
    delimiter: str | None = None,
    *,
    encoding: str | None = None,
    report: ImportReport | None = None,
) -> Table:
    """Open a CSV/TSV file.

    Leading lines starting with ``#`` are skipped. The delimiter is sniffed
    among ``,``, ``;``, tab and ``|`` unless given. Blank rows are dropped.
    Rows are parsed lazily as :attr:`Table.rows` is iterated.

    Args:
        path: The file.
        delimiter: Field delimiter, or ``None`` to detect it.
        encoding: Text encoding, or ``None`` to detect (UTF-8, UTF-16 with
            BOM, else Latin-1 with a warning).
        report: Receives warnings.

    Returns:
        The table.

    Raises:
        ConversionError: If the file is unreadable, empty or has duplicate
            column names. Ragged rows raise while iterating.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConversionError(f"cannot read {path}: {exc}") from exc
    text = _decode(data, path, encoding, report)
    del data
    lines = text.splitlines(keepends=True)
    skipped = 0
    while skipped < len(lines) and lines[skipped].lstrip().startswith("#"):
        skipped += 1
    body = "".join(lines[skipped:])
    del lines, text
    if not body.strip():
        raise ConversionError(f"{path} holds no table")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(body[:65536], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration as exc:  # pragma: no cover - body is non-empty
        raise ConversionError(f"{path} holds no header row") from exc
    if len(set(header)) != len(header):
        dupes = sorted({h for h in header if header.count(h) > 1})
        raise ConversionError(f"{path}: duplicate column names {dupes}")
    width = len(header)

    def rows() -> Iterator[tuple[int, list[str]]]:
        """Yield the data rows.

        Yields:
            ``(line_number, cells)`` per non-blank row.

        Raises:
            ConversionError: If a row has more non-empty cells than columns.
        """
        for cells in reader:
            line = reader.line_num + skipped
            if not any(c.strip() for c in cells):
                continue
            if len(cells) > width and any(c.strip() for c in cells[width:]):
                raise ConversionError(
                    f"{path} line {line}: {len(cells)} cells but the header has {width} columns"
                )
            yield line, (cells + [""] * width)[:width] if len(cells) != width else cells

    return Table(header=header, rows=rows(), delimiter=delimiter)


def _monotonic_runs(points: list[tuple[int, float, str | None]]) -> list[list[int]]:
    """Split header candidates into contiguous, monotonic runs.

    Args:
        points: ``(column index, wavelength, unit)`` of numeric headers, in
            column order.

    Returns:
        Runs as lists of positions into ``points``: each run has adjacent
        column indices and a non-decreasing or non-increasing axis
        (repeats allowed; they are resolved later).
    """
    runs: list[list[int]] = []
    current: list[int] = []
    direction = 0
    for k, (index, wl, _) in enumerate(points):
        if current and index != points[current[-1]][0] + 1:
            runs.append(current)
            current, direction = [], 0
        if current:
            step = wl - points[current[-1]][1]
            sign = 0 if step == 0 else (1 if step > 0 else -1)
            if sign and direction and sign != direction:
                runs.append(current)
                current, direction = [current[-1]], 0
            if sign:
                direction = direction or sign
        current.append(k)
    if current:
        runs.append(current)
    return runs


def _band_columns(
    table: Table, mapper: FieldMapper, report: ImportReport | None = None
) -> list[tuple[int, float, str | None]]:
    """Find the wide-layout band columns.

    The bands are the longest contiguous, monotonic run of numeric headers;
    numeric-looking headers outside it are metadata. Columns the user
    mapping names are never bands.

    Args:
        table: The table.
        mapper: The field mapper.
        report: Receives a warning naming numeric headers kept as metadata.

    Returns:
        ``(column index, wavelength, unit)`` for each band column.
    """
    points = []
    for index, name in enumerate(table.header):
        parsed = parse_band_header(name)
        if parsed is not None and not mapper.has_override(name):
            points.append((index, parsed[0], parsed[1]))
    if not points:
        return []
    best = max(_monotonic_runs(points), key=len)
    chosen = [points[k] for k in best]
    others = [table.header[p[0]] for k, p in enumerate(points) if k not in set(best)]
    if others and report is not None and len(chosen) >= 2:
        report.warn(
            f"numeric column(s) outside the run of wavelength columns read as metadata: "
            f"{', '.join(others[:10])}"
        )
    return chosen


def _point_roles(table: Table, mapper: FieldMapper) -> dict[str, tuple[int, str | None]]:
    """Find the long-layout per-point columns.

    Args:
        table: The table.
        mapper: The field mapper (user targets ``wavelength``, ``value``,
            ``bandpass_fwhm``, ``values_stddev`` win).

    Returns:
        Role to ``(column index, implied unit)``.
    """
    roles: dict[str, tuple[int, str | None]] = {}
    for index, name in enumerate(table.header):
        target = mapper.target(name)
        if target in ("wavelength", "value", "bandpass_fwhm", "values_stddev"):
            roles.setdefault(target, (index, None))
    for index, name in enumerate(table.header):
        alias = _POINT_ALIASES.get(normalize_column(name))
        if alias is not None and mapper.target(name) in (None, alias[0]):
            roles.setdefault(alias[0], (index, alias[1]))
    return roles


def detect_layout(table: Table, mapper: FieldMapper) -> str:
    """Decide whether a table is wide or long.

    Args:
        table: The table.
        mapper: The field mapper.

    Returns:
        ``"wide"`` when at least two headers are wavelengths, ``"long"``
        when there are wavelength and value columns.

    Raises:
        ConversionError: If neither layout fits.
    """
    if len(_band_columns(table, mapper)) >= 2:
        return "wide"
    roles = _point_roles(table, mapper)
    if "wavelength" in roles and "value" in roles:
        return "long"
    raise ConversionError(
        "cannot tell the CSV layout: a wide table needs numeric wavelength headers "
        "(e.g. 350, 351, …); a long table needs 'wavelength' and 'value' columns. "
        "Rename the columns or map them with --mapping."
    )


def _parse_float(cell: str) -> float | None:
    """Parse a numeric cell.

    Args:
        cell: The text.

    Returns:
        The number, NaN for a missing-value token, or ``None`` when the cell
        is not a number.
    """
    try:
        return float(cell)
    except ValueError:
        return math.nan if is_missing(cell) else None


def _parse_row(cells: Sequence[str]) -> tuple[np.ndarray, int]:
    """Parse a row of value cells.

    Args:
        cells: The cells.

    Returns:
        ``(values, bad)``: float64 values (NaN for missing) and the number
        of non-numeric cells (also NaN).
    """
    try:
        return np.array(cells, dtype=np.float64), 0
    except ValueError:
        pass
    values = np.empty(len(cells), dtype=np.float64)
    bad = 0
    for k, cell in enumerate(cells):
        parsed = _parse_float(cell)
        if parsed is None:
            bad += 1
            parsed = math.nan
        values[k] = parsed
    return values, bad


def _read_wide(
    table: Table, mapper: FieldMapper, wavelength_unit: str | None, report: ImportReport
) -> list[SourceSpectrum]:
    """Read a wide table.

    Args:
        table: The table.
        mapper: The field mapper.
        wavelength_unit: Unit from ``--wavelength-unit``, if given.
        report: Collects warnings.

    Returns:
        One spectrum per row.

    Raises:
        ConversionError: If band headers disagree on their unit.
    """
    bands = _band_columns(table, mapper, report)
    if len(bands) < 2:
        raise ConversionError("a wide table needs at least two wavelength columns")
    units = {u for _, _, u in bands if u is not None}
    if len(units) > 1:
        raise ConversionError(f"band headers use several units: {sorted(units)}")
    if units and None in {u for _, _, u in bands}:
        report.warn("some band headers state a unit and some do not; all read in the stated one")
    header_unit = next(iter(units), None)
    if header_unit and wavelength_unit and header_unit != wavelength_unit:
        report.warn(f"band headers say {header_unit}; --wavelength-unit {wavelength_unit} ignored")
    unit = header_unit or wavelength_unit
    if unit is None:
        unit = "nm"
        if "wavelength_unit" not in {mapper.target(h) for h in table.header}:
            report.warn("band headers state no unit; read as nm (EcoSIS convention)")
    band_index = [i for i, _, _ in bands]
    band_set = set(band_index)
    wavelengths = np.asarray([w for _, w, _ in bands], dtype=np.float64)
    meta_columns = [(i, h) for i, h in enumerate(table.header) if i not in band_set]
    spectra = []
    bad_cells = 0
    for number, (line, cells) in enumerate(table.rows, start=1):
        values, bad = _parse_row([cells[i] for i in band_index])
        bad_cells += bad
        fields, extra = mapper.apply({h: cells[i] for i, h in meta_columns})
        spectra.append(
            SourceSpectrum(
                label=f"row {number} (line {line})",
                fallback_id=f"row-{number}",
                wavelengths=wavelengths,
                values=values,
                fields=fields,
                extra=extra,
                wavelength_unit=unit,
                wavelength_unit_stated_by="the band headers" if header_unit else None,
            )
        )
    if not spectra:
        raise ConversionError("the table has a header but no data rows")
    if bad_cells:
        report.warn(f"{bad_cells} non-numeric value cells read as NaN")
    return spectra


@dataclass
class _Group:
    """Points and metadata of one spectrum in a long table."""

    line: int
    wavelengths: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    fwhm: list[float] = field(default_factory=list)
    stddev: list[float] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    meta_lines: dict[str, int] = field(default_factory=dict)
    seen: dict[float, tuple[int, int]] = field(default_factory=dict)


def _optional_points(points: list[float]) -> np.ndarray | None:
    """Return an optional per-point column as an array.

    Args:
        points: The parsed cells (NaN for empty ones).

    Returns:
        The array, or ``None`` when the column is absent or entirely empty.
    """
    array = np.asarray(points, dtype=np.float64)
    return array if points and not np.isnan(array).all() else None


def _key_index(table: Table, mapper: FieldMapper) -> int | None:
    """Find the column that identifies spectra in a long or spectra table.

    Args:
        table: The table.
        mapper: The field mapper.

    Returns:
        The index of the first column mapped to ``id``, else ``original_id``,
        else ``name``; ``None`` when there is none.
    """
    for wanted in ("id", "original_id", "name"):
        for index, name in enumerate(table.header):
            if mapper.target(name) == wanted:
                return index
    return None


def read_spectra_table(
    path: Path,
    mapper: FieldMapper,
    delimiter: str | None = None,
    *,
    encoding: str | None = None,
    report: ImportReport | None = None,
) -> dict[str, dict[str, str]]:
    """Read a per-spectrum metadata table that accompanies a long table.

    Args:
        path: CSV with one row per spectrum and a key column (``id``,
            ``original_id`` or ``name``, as in the long table).
        mapper: The field mapper.
        delimiter: Field delimiter, or ``None`` to detect it.
        encoding: Text encoding, or ``None`` to detect it.
        report: Receives warnings.

    Returns:
        Each row's cells keyed by column name, keyed by the row's key.

    Raises:
        ConversionError: If the table has no key column or repeats a key.
    """
    table = read_table(path, delimiter, encoding=encoding, report=report)
    key_index = _key_index(table, mapper)
    if key_index is None:
        raise ConversionError(f"{path.name} needs an 'id', 'original_id' or 'name' column")
    rows: dict[str, dict[str, str]] = {}
    for line, cells in table.rows:
        key = cells[key_index].strip()
        if key in rows:
            raise ConversionError(f"{path.name} line {line}: spectrum {key!r} appears twice")
        rows[key] = dict(zip(table.header, cells))
    return rows


def _read_long(
    table: Table,
    mapper: FieldMapper,
    wavelength_unit: str | None,
    report: ImportReport,
    spectra_meta: dict[str, dict[str, str]] | None = None,
) -> list[SourceSpectrum]:
    """Read a long table.

    Args:
        table: The table.
        mapper: The field mapper.
        wavelength_unit: Unit from ``--wavelength-unit``, if given.
        report: Collects warnings.
        spectra_meta: Per-spectrum metadata rows by key (see
            :func:`read_spectra_table`); the long table's own metadata
            cells win over them.

    Returns:
        One spectrum per distinct key, in order of first appearance.

    Raises:
        ConversionError: If required columns are missing, a wavelength cell
            is not a number, a key repeats a wavelength with a different
            value, or a metadata column has different values within one key.
    """
    roles = _point_roles(table, mapper)
    for role in ("wavelength", "value"):
        if role not in roles:
            raise ConversionError(f"a long table needs a '{role}' column")
    key_index = _key_index(table, mapper)
    if key_index is None:
        raise ConversionError(
            "a long table needs a spectrum key column ('id', 'original_id' or 'name')"
        )
    wl_index, wl_unit = roles["wavelength"]
    if wl_unit and wavelength_unit and wl_unit != wavelength_unit:
        report.warn(f"the wavelength column says {wl_unit}; --wavelength-unit ignored")
    value_index, _ = roles["value"]
    fwhm_index = roles.get("bandpass_fwhm", (None, None))[0]
    std_index = roles.get("values_stddev", (None, None))[0]
    point_indices = {wl_index, value_index, fwhm_index, std_index}
    meta_columns = [(i, h) for i, h in enumerate(table.header) if i not in point_indices]
    value_name = normalize_column(table.header[value_index])

    groups: dict[str, _Group] = {}
    duplicates = 0
    bad_values = 0
    for line, cells in table.rows:
        key = cells[key_index].strip()
        if not key:
            raise ConversionError(f"line {line}: empty spectrum key")
        wavelength = _parse_float(cells[wl_index])
        if wavelength is None or math.isnan(wavelength):
            raise ConversionError(f"line {line}: wavelength {cells[wl_index]!r} is not a number")
        value = _parse_float(cells[value_index])
        if value is None:
            bad_values += 1
            value = math.nan
        extras = []
        for optional_index in (fwhm_index, std_index):
            if optional_index is not None:
                parsed = _parse_float(cells[optional_index])
                extras.append(math.nan if parsed is None else parsed)
        group = groups.setdefault(key, _Group(line=line))
        if wavelength in group.seen:
            k, first_line = group.seen[wavelength]
            previous = group.values[k]
            if not (value == previous or (math.isnan(value) and math.isnan(previous))):
                raise ConversionError(
                    f"spectrum {key!r}: wavelength {wavelength:g} appears on lines "
                    f"{first_line} and {line} with different values ({previous:g} and "
                    f"{value:g}); two spectra probably share the key. Give each its own id"
                )
            duplicates += 1
            continue
        group.seen[wavelength] = (len(group.wavelengths), line)
        group.wavelengths.append(wavelength)
        group.values.append(value)
        targets = [
            t for t, i in ((group.fwhm, fwhm_index), (group.stddev, std_index)) if i is not None
        ]
        for target, parsed in zip(targets, extras):
            target.append(parsed)
        for index, name in meta_columns:
            cell = cells[index]
            if is_blank(cell):
                continue
            if name not in group.meta:
                group.meta[name] = cell
                group.meta_lines[name] = line
            elif group.meta[name].strip() != cell.strip():
                raise ConversionError(
                    f"spectrum {key!r}: column {name!r} is {group.meta[name].strip()!r} on "
                    f"line {group.meta_lines[name]} but {cell.strip()!r} on line {line}; a "
                    f"spectrum's metadata must be the same on all its rows (two spectra "
                    f"probably share the key: give each its own id; if {name!r} varies per "
                    f'point, such as a band number, map it to "ignore" in a --mapping file)'
                )
    if not groups:
        raise ConversionError("the table has a header but no data rows")
    if duplicates:
        report.warn(f"{duplicates} repeated (spectrum, wavelength) rows with equal values dropped")
    if bad_values:
        report.warn(f"{bad_values} non-numeric value cells read as NaN")
    if spectra_meta is not None:
        unmatched = set(spectra_meta) - set(groups)
        missing = [k for k in groups if k not in spectra_meta]
        if unmatched:
            report.warn(f"{len(unmatched)} spectra-table row(s) match no spectrum; ignored")
        if missing:
            report.warn(f"{len(missing)} spectra have no row in the spectra table")
    spectra = []
    for key, group in groups.items():
        meta = {**(spectra_meta or {}).get(key, {}), **group.meta}
        fields, extra = mapper.apply(meta)
        if value_name in _QUANTITY_COLUMNS:
            fields.setdefault("quantity", value_name)
        spectra.append(
            SourceSpectrum(
                label=f"spectrum {key!r} (line {group.line})",
                fallback_id=key,
                wavelengths=np.asarray(group.wavelengths, dtype=np.float64),
                values=np.asarray(group.values, dtype=np.float64),
                fields=fields,
                extra=extra,
                bandpass_fwhm=_optional_points(group.fwhm),
                values_stddev=_optional_points(group.stddev),
                wavelength_unit=wl_unit or wavelength_unit,
                wavelength_unit_stated_by=(
                    f"the wavelength column name {table.header[wl_index]!r}" if wl_unit else None
                ),
            )
        )
    return spectra


def read_csv_spectra(
    path: Path,
    mapper: FieldMapper,
    *,
    layout: str = "auto",
    delimiter: str | None = None,
    wavelength_unit: str | None = None,
    report: ImportReport | None = None,
    spectra_table: Path | None = None,
    encoding: str | None = None,
) -> tuple[list[SourceSpectrum], str]:
    """Read spectra from a CSV file.

    Args:
        path: The file.
        mapper: Maps metadata columns to Format v1 columns.
        layout: ``"wide"``, ``"long"`` or ``"auto"``.
        delimiter: Field delimiter, or ``None`` to detect it.
        wavelength_unit: Unit of axes the table does not label.
        report: Collects warnings.
        spectra_table: Per-spectrum metadata CSV joined to a long table by
            its key column (as ``export --layout long`` writes it).
        encoding: Text encoding, or ``None`` to detect it.

    Returns:
        ``(spectra, layout used)``.

    Raises:
        ConversionError: If the file cannot be read in the layout.
    """
    report = report if report is not None else ImportReport()
    table = read_table(path, delimiter, encoding=encoding, report=report)
    if layout == "auto":
        layout = detect_layout(table, mapper)
    if spectra_table is not None and layout != "long":
        raise ConversionError("a spectra metadata table can only accompany a long table")
    if layout == "wide":
        return _read_wide(table, mapper, wavelength_unit, report), layout
    if layout == "long":
        meta = None
        if spectra_table is not None:
            meta = read_spectra_table(spectra_table, mapper, encoding=encoding, report=report)
        return _read_long(table, mapper, wavelength_unit, report, meta), layout
    raise ConversionError(f"unknown CSV layout {layout!r}; use wide, long or auto")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    """Format a metadata value for a CSV cell.

    Args:
        value: A string, number, date, list or ``None``.

    Returns:
        The text (lists joined with ``"; "``, ``None`` empty).
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _number_strings(values: np.ndarray, dtype: Any) -> list[str]:
    """Format numbers in their shortest exact form, NaN as an empty cell.

    Args:
        values: The numbers.
        dtype: ``np.float32`` for values, ``np.float64`` for wavelengths.

    Returns:
        One string per number.
    """
    texts = np.asarray(values, dtype=dtype).astype(str)
    return ["" if t == "nan" else t for t in texts.tolist()]


def write_wide_csv(
    path: Path,
    metadata: Sequence[dict[str, Any]],
    columns: Sequence[str],
    wavelengths: np.ndarray,
    values: np.ndarray,
) -> Path:
    """Write a wide CSV: metadata columns then one column per wavelength.

    Args:
        path: Output file.
        metadata: One metadata dict per spectrum.
        columns: Metadata columns to write, in order.
        wavelengths: The shared axis (column headers).
        values: ``(spectra, bands)`` array; NaN written as an empty cell.

    Returns:
        ``path``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [t[:-2] if t.endswith(".0") else t for t in _number_strings(wavelengths, np.float64)]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(columns) + headers)
        for meta, row in zip(metadata, values):
            writer.writerow(
                [_cell(meta.get(c)) for c in columns] + _number_strings(row, np.float32)
            )
    return path


def write_metadata_csv(
    path: Path, metadata: Sequence[dict[str, Any]], columns: Sequence[str]
) -> Path:
    """Write a per-spectrum metadata table (one row per spectrum, no bands).

    Args:
        path: Output file.
        metadata: One metadata dict per spectrum.
        columns: Columns to write, in order.

    Returns:
        ``path``.
    """
    return write_wide_csv(path, metadata, columns, np.empty(0), np.empty((len(metadata), 0)))


def write_long_csv(
    path: Path,
    metadata: Sequence[dict[str, Any]],
    columns: Sequence[str],
    spectra: Sequence[tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None]],
) -> Path:
    """Write a long CSV: one row per point, the given metadata repeated on each row.

    Args:
        path: Output file.
        metadata: One metadata dict per spectrum.
        columns: Metadata columns repeated on every row, in order (the first
            should be the key, ``id``). Keep it short: long tables are big.
        spectra: ``(wavelengths, values, bandpass_fwhm, values_stddev)`` per
            spectrum; the optional columns are written when any spectrum has them.

    Returns:
        ``path``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    has_fwhm = any(s[2] is not None for s in spectra)
    has_std = any(s[3] is not None for s in spectra)
    point_columns = ["wavelength", "value"]
    point_columns += ["bandpass_fwhm"] if has_fwhm else []
    point_columns += ["values_stddev"] if has_std else []
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(columns) + point_columns)
        for meta, (wl, vals, fwhm, std) in zip(metadata, spectra):
            prefix = [_cell(meta.get(c)) for c in columns]
            cols = [_number_strings(wl, np.float64), _number_strings(vals, np.float32)]
            for wanted, array in ((has_fwhm, fwhm), (has_std, std)):
                if wanted:
                    cols.append(
                        [""] * len(wl) if array is None else _number_strings(array, np.float32)
                    )
            for point in zip(*cols):
                writer.writerow(prefix + list(point))
    return path
