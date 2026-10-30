"""ENVI spectral library (``.sli`` + ``.hdr``) reading and writing.

An ENVI spectral library is a headerless binary array of ``lines`` spectra
by ``samples`` bands (``bands = 1``) described by a text header
(``file type = ENVI Spectral Library``). Every spectrum shares the header's
``wavelength`` axis. The header format (ENVI documentation, "ENVI Header
Format"):

* the first line is ``ENVI``;
* each field is ``key = value``; keys are case-insensitive;
* list values are enclosed in ``{…}``, comma-separated, and may span lines;
* lines starting with ``;`` are comments; a ``{`` may start on the next
  line; a repeated key keeps its last value (with a warning).

On import, the data file must be exactly as large as the header says
(unless trailing bytes are explicitly allowed), ``data ignore value`` and
bands flagged 0 in ``bbl`` become NaN (counted in ``extra``), and
``reflectance scale factor`` is divided out so values become fractions
(see :mod:`openspeclib.convert.scale`). Headers are read as UTF-8, else
Latin-1 with a warning.

On export, NaN is written as ``data ignore value = -9999`` and
``reflectance scale factor = 1`` says the values are fractions. Besides the
standard fields, the writer adds ``openspeclib <column>`` fields holding
per-spectrum Format v1 metadata (one list item per spectrum, with ``%``,
``,``, ``{``, ``}`` and line breaks percent-encoded), so a library exported
from Format v1 converts back without loss. ENVI and other readers ignore
unknown header fields.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import numpy as np

from openspeclib.convert.common import (
    ConversionError,
    FieldMapper,
    ImportReport,
    SourceSpectrum,
    normalize_wavelength_unit,
)

__all__ = [
    "ENVI_IGNORE_VALUE",
    "EnviLibrary",
    "find_envi_files",
    "format_envi_header",
    "header_wavelengths_nm",
    "parse_envi_header",
    "read_envi_library",
    "read_header_text",
    "envi_list",
    "envi_to_spectra",
    "write_envi_library",
]

# ``data ignore value`` written on export (the ENVI convention for no-data).
ENVI_IGNORE_VALUE = -9999.0

# Prefix of the per-spectrum Format v1 metadata fields the writer adds.
CUSTOM_PREFIX = "openspeclib "
# Collection-level ``openspeclib`` fields (not one item per spectrum).
COLLECTION_LEVEL_FIELDS = frozenset({f"{CUSTOM_PREFIX}license refs"})

# ENVI ``data type`` codes this reader supports, as numpy type codes.
ENVI_DTYPES: dict[int, str] = {
    1: "u1",
    2: "i2",
    3: "i4",
    4: "f4",
    5: "f8",
    12: "u2",
    13: "u4",
    14: "i8",
    15: "u8",
}

# Data-file extensions tried next to a header, in order.
DATA_EXTENSIONS: tuple[str, ...] = (".sli", ".esl", ".lib", ".spl", ".dat", ".img", "")

# Wavelength units ENVI allows that the format does not; converted to nm.
_TO_NM: dict[str, float] = {
    "millimeters": 1e6,
    "millimetres": 1e6,
    "mm": 1e6,
    "centimeters": 1e7,
    "centimetres": 1e7,
    "cm": 1e7,
    "meters": 1e9,
    "metres": 1e9,
    "m": 1e9,
    "angstroms": 0.1,
    "angstrom": 0.1,
}

# Header label written for each Format v1 unit.
_UNIT_LABELS: dict[str, str] = {"nm": "Nanometers", "um": "Micrometers", "cm-1": "Wavenumber"}


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def _normalize_key(key: str) -> str:
    """Normalise a header key: lower case, single spaces.

    Args:
        key: The raw key.

    Returns:
        The normalised key.
    """
    return " ".join(key.lower().split())


def parse_envi_header(text: str, report: ImportReport | None = None) -> dict[str, str]:
    """Parse ENVI header text.

    A ``{`` may start on the line after ``key =``. A repeated key keeps its
    last value, with a warning.

    Args:
        text: The header file's content.
        report: Receives warnings, if given.

    Returns:
        Field values keyed by lower-case key. Brace values keep their
        braces (``"{a, b}"``) so lists can be told from scalars; use
        :func:`envi_list` to split them.

    Raises:
        ConversionError: If the first line is not ``ENVI``, a line is not
            ``key = value``, or a ``{`` is never closed.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    position = 0
    while position < len(lines) and not lines[position].strip():
        position += 1
    if position >= len(lines) or lines[position].strip().upper() != "ENVI":
        raise ConversionError("not an ENVI header: the first line must be 'ENVI'")
    header: dict[str, str] = {}
    position += 1
    while position < len(lines):
        line = lines[position]
        position += 1
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if "=" not in stripped:
            raise ConversionError(
                f"malformed ENVI header line {position}: expected 'key = value', got "
                f"{stripped[:60]!r}"
            )
        key, _, value = stripped.partition("=")
        key, value = _normalize_key(key), value.strip()
        if not key:
            raise ConversionError(f"malformed ENVI header line {position}: empty key")
        if not value:
            ahead = position
            while ahead < len(lines) and not lines[ahead].strip():
                ahead += 1
            if ahead < len(lines) and lines[ahead].strip().startswith("{"):
                value = lines[ahead].strip()
                position = ahead + 1
        if value.startswith("{"):
            # Collect lines until the braces balance; ENVI lists do not nest,
            # but free text (description) sometimes holds a {…} pair.
            start = position
            parts = [value]
            depth = value.count("{") - value.count("}")
            while depth > 0:
                if position >= len(lines):
                    raise ConversionError(
                        f"malformed ENVI header: '{{' opened for '{key}' on line {start} "
                        f"is never closed"
                    )
                parts.append(lines[position].strip())
                depth += parts[-1].count("{") - parts[-1].count("}")
                position += 1
            value = _balanced("\n".join(parts))
        if key in header and report is not None:
            report.warn(f"ENVI header repeats '{key}'; the last value is used")
        header[key] = value
    return header


def read_header_text(path: Path, encoding: str | None, report: ImportReport) -> str:
    """Read a header file, trying UTF-8 and then Latin-1.

    Args:
        path: The header file.
        encoding: Encoding to use; ``None`` tries UTF-8, then Latin-1.
        report: Receives a warning when Latin-1 is used.

    Returns:
        The text.

    Raises:
        ConversionError: If the file cannot be read or decoded.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConversionError(f"cannot read {path}: {exc}") from exc
    if encoding is not None:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ConversionError(f"cannot decode {path} as {encoding}: {exc}") from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        report.warn(f"{path.name} is not UTF-8; read as Latin-1 (pass --encoding to choose)")
        return data.decode("latin-1")


def header_wavelengths_nm(header: Mapping[str, str], where: str) -> np.ndarray:
    """Return the ``wavelength`` list of any ENVI header in nanometres, ascending.

    Works for image headers (e.g. EMIT, AVIRIS) as well as libraries, so a
    sensor's band centres can serve as a resampling grid.

    Args:
        header: The parsed header.
        where: The header's name, for messages.

    Returns:
        The band centres in nm.

    Raises:
        ConversionError: If the list is missing, non-numeric or its unit unknown.
    """
    items = envi_list(header.get("wavelength"))
    if not items:
        raise ConversionError(f"{where}: no 'wavelength' list")
    try:
        axis = np.asarray([float(i) for i in items], dtype=np.float64)
    except ValueError as exc:
        raise ConversionError(f"{where}: 'wavelength' holds a non-number: {exc}") from exc
    raw = _scalar(header, "wavelength units")
    unit = normalize_wavelength_unit(raw)
    if unit == "nm":
        nm = axis
    elif unit == "um":
        nm = axis * 1000.0
    elif unit == "cm-1":
        nm = 1e7 / axis
    elif raw is not None and raw.strip().lower() in _TO_NM:
        nm = axis * _TO_NM[raw.strip().lower()]
    else:
        raise ConversionError(f"{where}: wavelength units {raw!r} cannot be converted to nm")
    return np.sort(nm)


def _balanced(text: str) -> str:
    """Cut text after the ``}`` that closes its first ``{``.

    Args:
        text: Text starting with ``{``.

    Returns:
        The brace value, without anything after it (e.g. a trailing comment).
    """
    depth = 0
    for position, char in enumerate(text):
        depth += (char == "{") - (char == "}")
        if depth == 0:
            return text[: position + 1]
    return text


def envi_list(value: str | None) -> list[str] | None:
    """Split a ``{a, b, c}`` header value into stripped items.

    Args:
        value: A header value, with or without braces.

    Returns:
        The items, or ``None`` when ``value`` is ``None``. ``{}`` gives ``[]``.
    """
    if value is None:
        return None
    inner = value.strip()
    if inner.startswith("{"):
        inner = inner[1:]
    if inner.endswith("}"):
        inner = inner[:-1]
    if not inner.strip():
        return []
    return [item.strip() for item in inner.replace("\n", " ").split(",")]


def _scalar(header: Mapping[str, str], key: str) -> str | None:
    """Return a scalar header value with any braces removed.

    Args:
        header: The parsed header.
        key: The lower-case key.

    Returns:
        The value, or ``None`` when absent or empty.
    """
    value = header.get(key)
    if value is None:
        return None
    value = value.strip().strip("{}").strip()
    return value or None


def _int_field(header: Mapping[str, str], key: str, default: int | None = None) -> int:
    """Read an integer header field.

    Args:
        header: The parsed header.
        key: The lower-case key.
        default: Value when absent; ``None`` makes the field required.

    Returns:
        The value.

    Raises:
        ConversionError: If the field is missing (and required) or not an integer.
    """
    raw = _scalar(header, key)
    if raw is None:
        if default is None:
            raise ConversionError(f"ENVI header is missing the required field '{key}'")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConversionError(f"ENVI header field '{key}' is not an integer: {raw!r}") from exc


def _float_list(header: Mapping[str, str], key: str, expected: int) -> np.ndarray | None:
    """Read a numeric list field of a given length.

    Args:
        header: The parsed header.
        key: The lower-case key.
        expected: Required number of items.

    Returns:
        The values as float64, or ``None`` when the field is absent.

    Raises:
        ConversionError: If the field has the wrong length or a non-number.
    """
    items = envi_list(header.get(key))
    if items is None:
        return None
    if len(items) != expected:
        raise ConversionError(
            f"ENVI header field '{key}' has {len(items)} values; samples = {expected}"
        )
    try:
        return np.asarray([float(i) for i in items], dtype=np.float64)
    except ValueError as exc:
        raise ConversionError(f"ENVI header field '{key}' holds a non-number: {exc}") from exc


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass
class EnviLibrary:
    """An ENVI spectral library read into memory.

    Attributes:
        header: The parsed header (see :func:`parse_envi_header`).
        header_path: The ``.hdr`` file.
        data_path: The binary data file.
        wavelengths: The shared axis, in ``wavelength_unit`` (float64).
        wavelength_unit: ``nm``, ``um`` or ``cm-1``, or ``None`` when the
            header does not state a unit the format can hold.
        values: ``(spectra, bands)`` float64 array; ignore values and bad
            bands are NaN, the scale factor is *not* yet applied.
        names: One name per spectrum (``spectra names``, else empty strings).
        fwhm: Per-band FWHM in ``wavelength_unit``, if given.
        scale_factor: ``reflectance scale factor``, if given.
        wavelength_unit_stated_by: What gave ``wavelength_unit`` (the
            header or ``--wavelength-unit``), ``None`` when nothing did.
        integer_data: Whether the data type is an integer type.
        ignored_counts: Per spectrum, bands equal to ``data ignore value``.
        bad_band_count: Bands flagged 0 in ``bbl``.
    """

    header: dict[str, str]
    header_path: Path
    data_path: Path
    wavelengths: np.ndarray
    wavelength_unit: str | None
    values: np.ndarray
    names: list[str]
    fwhm: np.ndarray | None
    scale_factor: float | None
    wavelength_unit_stated_by: str | None = None
    integer_data: bool = False
    ignored_counts: np.ndarray | None = None
    bad_band_count: int = 0

    @property
    def from_export(self) -> bool:
        """Whether ``openspeclib export`` wrote the library (``openspeclib id`` field)."""
        return f"{CUSTOM_PREFIX}id" in self.header


def find_envi_files(path: Path) -> tuple[Path, Path]:
    """Locate the header and data file of an ENVI library.

    Args:
        path: Either file: ``lib.hdr`` or the data file (``lib.sli``).

    Returns:
        ``(header_path, data_path)``.

    Raises:
        ConversionError: If the companion file cannot be found.
    """
    if path.suffix.lower() == ".hdr":
        header = path
        for ext in DATA_EXTENSIONS:
            for candidate in (path.with_suffix(ext), path.with_suffix(ext.upper())):
                if candidate != path and candidate.is_file():
                    return header, candidate
        tried = ", ".join(e or "(none)" for e in DATA_EXTENSIONS)
        raise ConversionError(f"no data file next to {path} (tried {tried})")
    for candidate in (path.with_suffix(".hdr"), Path(f"{path}.hdr"), path.with_suffix(".HDR")):
        if candidate.is_file():
            return candidate, path
    raise ConversionError(f"no ENVI header (.hdr) found for {path}")


def _unit_from_header(
    header: Mapping[str, str], override: str | None, report: ImportReport
) -> tuple[str | None, float]:
    """Decide the axis unit and any conversion factor to nanometres.

    Args:
        header: The parsed header.
        override: Unit given on the command line (wins over the header).
        report: Collects warnings.

    Returns:
        ``(unit, factor)``: the Format v1 unit (or ``None`` if unknown) and
        the factor the axis must be multiplied by (1 unless the header
        unit is converted to nm).

    Raises:
        ConversionError: If ``override`` is not a supported unit.
    """
    raw = _scalar(header, "wavelength units")
    if override is not None:
        unit = normalize_wavelength_unit(override)
        if unit is None:
            raise ConversionError(f"--wavelength-unit {override!r} is not nm, um or cm-1")
        stated = normalize_wavelength_unit(raw)
        if stated is not None and stated != unit:
            report.warn(f"header says wavelength units = {raw}; using {unit} as requested")
        return unit, 1.0
    if raw is None:
        return None, 1.0
    unit = normalize_wavelength_unit(raw)
    if unit is not None:
        return unit, 1.0
    factor = _TO_NM.get(raw.strip().lower())
    if factor is not None:
        report.warn(f"wavelengths in {raw} converted to nm (the format holds nm, um or cm-1)")
        return "nm", factor
    return None, 1.0


def read_envi_library(
    path: Path,
    *,
    wavelength_unit: str | None = None,
    report: ImportReport | None = None,
    allow_trailing_bytes: bool = False,
    encoding: str | None = None,
) -> EnviLibrary:
    """Read an ENVI spectral library.

    The data file must be exactly as large as the header describes: a
    larger file usually means a wrong ``data type`` or dimensions, which
    would otherwise be read as plausible-looking garbage.

    Args:
        path: The ``.hdr`` or the data file.
        wavelength_unit: Unit of the axis, overriding ``wavelength units``.
        report: Collects warnings (a new one if omitted).
        allow_trailing_bytes: Accept a data file longer than described and
            ignore the extra bytes.
        encoding: Header text encoding; ``None`` tries UTF-8, then Latin-1.

    Returns:
        The library.

    Raises:
        ConversionError: If the header is malformed or inconsistent with the
            data file, or the file is not a single-band spectral library.
    """
    report = report if report is not None else ImportReport()
    header_path, data_path = find_envi_files(path)
    header = parse_envi_header(read_header_text(header_path, encoding, report), report)

    file_type = _scalar(header, "file type")
    if file_type is None:
        report.warn("ENVI header has no 'file type'; reading it as a spectral library")
    elif "spectral library" not in file_type.lower():
        raise ConversionError(
            f"{header_path}: file type = {file_type!r}; only ENVI Spectral Library files "
            f"are supported"
        )
    samples = _int_field(header, "samples")
    lines = _int_field(header, "lines")
    bands = _int_field(header, "bands", 1)
    if bands != 1:
        raise ConversionError(
            f"{header_path}: bands = {bands}; a spectral library has bands = 1 "
            f"(samples = points per spectrum, lines = spectra)"
        )
    if samples < 2 or lines < 1:
        raise ConversionError(f"{header_path}: samples = {samples}, lines = {lines}")
    data_type = _int_field(header, "data type")
    if data_type not in ENVI_DTYPES:
        raise ConversionError(
            f"{header_path}: data type = {data_type} is not supported "
            f"(supported: {', '.join(str(k) for k in ENVI_DTYPES)})"
        )
    byte_order = _int_field(header, "byte order", -1)
    if byte_order == -1:
        report.warn("ENVI header has no 'byte order'; assuming 0 (little-endian)")
        byte_order = 0
    if byte_order not in (0, 1):
        raise ConversionError(f"{header_path}: byte order = {byte_order} (must be 0 or 1)")
    offset = _int_field(header, "header offset", 0)
    if offset < 0:
        raise ConversionError(
            f"{header_path}: header offset = {offset}; it must be 0 or a positive number of bytes"
        )
    interleave = (_scalar(header, "interleave") or "bsq").lower()
    if interleave not in ("bsq", "bil", "bip"):
        raise ConversionError(f"{header_path}: interleave = {interleave!r}")

    dtype = np.dtype(ENVI_DTYPES[data_type]).newbyteorder("<" if byte_order == 0 else ">")
    needed = offset + samples * lines * dtype.itemsize
    size = data_path.stat().st_size
    if size < needed or (size > needed and not allow_trailing_bytes):
        what = "is shorter than" if size < needed else "is longer than"
        raise ConversionError(
            f"{data_path} has {size} bytes, which {what} the {needed} the header describes "
            f"({lines} spectra x {samples} bands of {dtype.itemsize}-byte data type "
            f"{data_type} + offset {offset}). Check 'data type', 'samples', 'lines' and "
            f"'header offset'"
            + ("; pass --allow-trailing-bytes to ignore extra bytes" if size > needed else "")
        )
    if size > needed:
        report.warn(f"{size - needed} trailing bytes after the data in {data_path.name} ignored")
    raw = np.fromfile(data_path, dtype=dtype, count=samples * lines, offset=offset)
    values = raw.reshape(lines, samples).astype(np.float64)

    ignored_counts = None
    ignore = _scalar(header, "data ignore value")
    if ignore is not None:
        try:
            ignore_value = float(ignore)
        except ValueError as exc:
            raise ConversionError(f"data ignore value is not a number: {ignore!r}") from exc
        mask = np.isclose(values, ignore_value, rtol=1e-6, atol=0.0) | (values == ignore_value)
        values[mask] = np.nan
        ignored_counts = mask.sum(axis=1)
    bbl = _float_list(header, "bbl", samples)
    bad_bands = 0
    if bbl is not None and (bbl == 0).any():
        bad_bands = int((bbl == 0).sum())
        values[:, bbl == 0] = np.nan
        report.warn(f"{bad_bands} bands flagged bad in 'bbl' set to NaN")

    wavelengths = _float_list(header, "wavelength", samples)
    if wavelengths is None:
        raise ConversionError(
            f"{header_path}: no 'wavelength' list; a spectral library needs its band axis"
        )
    unit, factor = _unit_from_header(header, wavelength_unit, report)
    fwhm = _float_list(header, "fwhm", samples)
    if factor != 1.0:
        wavelengths = wavelengths * factor
        fwhm = None if fwhm is None else fwhm * factor

    names = envi_list(header.get("spectra names")) or []
    if names and len(names) != lines:
        report.warn(f"'spectra names' has {len(names)} entries for {lines} spectra; ignored")
        names = []
    scale = _scalar(header, "reflectance scale factor")
    scale_factor = None
    if scale is not None:
        try:
            scale_factor = float(scale)
        except ValueError as exc:
            raise ConversionError(f"reflectance scale factor is not a number: {scale!r}") from exc
        if not scale_factor > 0:
            raise ConversionError(f"reflectance scale factor must be positive, got {scale!r}")
    return EnviLibrary(
        header=header,
        header_path=header_path,
        data_path=data_path,
        wavelengths=wavelengths,
        wavelength_unit=unit,
        values=values,
        names=names or [""] * lines,
        fwhm=fwhm,
        scale_factor=scale_factor,
        wavelength_unit_stated_by=(
            None
            if unit is None
            else (
                "--wavelength-unit"
                if wavelength_unit is not None
                else "the ENVI header 'wavelength units'"
            )
        ),
        integer_data=dtype.kind in "iu",
        ignored_counts=ignored_counts,
        bad_band_count=bad_bands,
    )


def _decode(value: str) -> str:
    """Undo the percent-encoding of a custom list item.

    Args:
        value: The encoded item.

    Returns:
        The original text.
    """
    return unquote(value)


def envi_to_spectra(library: EnviLibrary, mapper: FieldMapper) -> list[SourceSpectrum]:
    """Turn a library's spectra into :class:`SourceSpectrum` objects.

    Per-spectrum metadata comes from ``spectra names`` (the name) and from
    any ``openspeclib <column>`` fields, which win over ``spectra names``.

    Args:
        library: The library.
        mapper: Maps ``openspeclib <column>`` names to columns.

    Returns:
        One spectrum per library line.

    Raises:
        ConversionError: If a custom field has the wrong number of items.
    """
    lines = library.values.shape[0]
    custom: dict[str, list[str]] = {}
    for key, value in library.header.items():
        if key in COLLECTION_LEVEL_FIELDS:
            continue
        if key.startswith(CUSTOM_PREFIX) and value.strip().startswith("{"):
            items = envi_list(value) or []
            if len(items) != lines:
                raise ConversionError(
                    f"ENVI header field '{key}' has {len(items)} items for {lines} spectra"
                )
            custom[key[len(CUSTOM_PREFIX) :]] = [_decode(i) for i in items]
    spectra = []
    for i in range(lines):
        raw: dict[str, str] = {column: items[i] for column, items in custom.items()}
        raw.setdefault("name", library.names[i])
        fields, extra = mapper.apply(raw)
        # In an openspeclib export the ignore value stands for NaN, not masked data.
        if (
            not library.from_export
            and library.ignored_counts is not None
            and library.ignored_counts[i]
        ):
            extra["masked_ignore_value_count"] = int(library.ignored_counts[i])
        if library.bad_band_count:
            extra["masked_bad_band_count"] = library.bad_band_count
        spectra.append(
            SourceSpectrum(
                label=f"spectrum {i + 1} ({library.names[i] or 'unnamed'})",
                fallback_id=f"spectrum-{i + 1}",
                wavelengths=library.wavelengths,
                values=library.values[i],
                fields=fields,
                extra=extra,
                bandpass_fwhm=library.fwhm,
                wavelength_unit=library.wavelength_unit,
                wavelength_unit_stated_by=library.wavelength_unit_stated_by,
                integer_data=library.integer_data,
            )
        )
    return spectra


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _encode(value: str) -> str:
    """Percent-encode the characters that would break an ENVI list item.

    Args:
        value: The text.

    Returns:
        The text with ``%``, ``,``, ``{``, ``}``, CR and LF encoded.
    """
    for char, code in (
        ("%", "%25"),
        (",", "%2C"),
        ("{", "%7B"),
        ("}", "%7D"),
        ("\n", "%0A"),
        ("\r", "%0D"),
    ):
        value = value.replace(char, code)
    if value != value.strip():
        value = re.sub(r"^\s+|\s+$", lambda m: m.group(0).replace(" ", "%20"), value)
    return value


def _plain(value: str, *, commas: bool = True) -> str:
    """Make text safe for a standard list item or a free-text brace field.

    Args:
        value: The text.
        commas: Replace commas (list items) as well; free-text fields such
            as ``description`` may keep them.

    Returns:
        The text with braces as parentheses, line breaks as spaces and,
        if ``commas``, commas as ``;``.
    """
    value = value.replace("{", "(").replace("}", ")")
    if commas:
        value = value.replace(",", ";")
    return " ".join(value.split())


def _format_list(items: Sequence[str], per_line: int = 8) -> str:
    """Format a brace list, wrapping lines.

    Args:
        items: The items (already safe).
        per_line: Items per line.

    Returns:
        ``{a, b, …}`` text.
    """
    if not items:
        return "{}"
    rows = [", ".join(items[i : i + per_line]) for i in range(0, len(items), per_line)]
    return "{\n " + ",\n ".join(rows) + "}"


def format_envi_header(fields: Sequence[tuple[str, str | Sequence[str]]]) -> str:
    """Render ENVI header text.

    Args:
        fields: ``(key, value)`` pairs in order; a sequence value is written
            as a brace list.

    Returns:
        The header text, starting with ``ENVI``.
    """
    out = ["ENVI"]
    for key, value in fields:
        if isinstance(value, str):
            out.append(f"{key} = {value}")
        else:
            out.append(f"{key} = {_format_list(list(value))}")
    return "\n".join(out) + "\n"


def _number(value: float) -> str:
    """Format a float exactly (shortest round-trip form).

    Args:
        value: The number.

    Returns:
        Its text.
    """
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def write_envi_library(
    base: Path,
    wavelengths: np.ndarray,
    values: np.ndarray,
    names: Sequence[str],
    wavelength_unit: str,
    *,
    fwhm: np.ndarray | None = None,
    description: str | None = None,
    custom_fields: Mapping[str, Sequence[str | None]] | None = None,
    license_refs: Sequence[str] = (),
) -> tuple[Path, Path]:
    """Write an ENVI spectral library.

    Args:
        base: Output path without extension; ``base.sli`` and ``base.hdr``
            are written.
        wavelengths: The shared axis (``samples`` values).
        values: ``(spectra, bands)`` array; written as little-endian float32
            with NaN as :data:`ENVI_IGNORE_VALUE`.
        names: One name per spectrum (``spectra names``).
        wavelength_unit: ``nm``, ``um`` or ``cm-1``.
        fwhm: Optional per-band FWHM in ``wavelength_unit``.
        description: Text for the ``description`` field.
        custom_fields: Per-spectrum metadata written as ``openspeclib <key>``
            lists (``None`` items written empty).
        license_refs: Definitions of the ``LicenseRef-`` licences used, one
            text per licence, written as ``openspeclib license refs``.

    Returns:
        ``(data_path, header_path)``.

    Raises:
        ValueError: If array shapes disagree.
    """
    data = np.asarray(values, dtype=np.float32)
    if data.ndim != 2 or data.shape[1] != len(wavelengths) or data.shape[0] != len(names):
        raise ValueError(
            f"values shape {data.shape} does not match {len(names)} names x "
            f"{len(wavelengths)} wavelengths"
        )
    if fwhm is not None and len(fwhm) != len(wavelengths):
        raise ValueError("fwhm and wavelengths differ in length")
    data = np.where(np.isnan(data), np.float32(ENVI_IGNORE_VALUE), data).astype("<f4")
    base.parent.mkdir(parents=True, exist_ok=True)
    data_path = base.with_name(base.name + ".sli")
    header_path = base.with_name(base.name + ".hdr")
    data.tofile(data_path)

    fields: list[tuple[str, str | Sequence[str]]] = []
    if description:
        fields.append(("description", "{" + _plain(description, commas=False) + "}"))
    fields += [
        ("samples", str(data.shape[1])),
        ("lines", str(data.shape[0])),
        ("bands", "1"),
        ("header offset", "0"),
        ("file type", "ENVI Spectral Library"),
        ("data type", "4"),
        ("interleave", "bsq"),
        ("byte order", "0"),
        ("data ignore value", _number(ENVI_IGNORE_VALUE)),
        # Values are fractions; stating the factor stops readers rescaling them.
        ("reflectance scale factor", "1"),
        ("wavelength units", _UNIT_LABELS[wavelength_unit]),
        ("wavelength", [_number(w) for w in wavelengths]),
    ]
    if fwhm is not None:
        fields.append(("fwhm", [str(np.float32(w)) for w in fwhm]))
    fields.append(("spectra names", [_plain(n) or "unnamed" for n in names]))
    for key, items in (custom_fields or {}).items():
        fields.append((CUSTOM_PREFIX + key, [_encode(i or "") for i in items]))
    if license_refs:
        fields.append((f"{CUSTOM_PREFIX}license refs", [_encode(text) for text in license_refs]))
    header_path.write_text(format_envi_header(fields), encoding="utf-8")
    return data_path, header_path
