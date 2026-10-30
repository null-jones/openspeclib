"""Export spectra from a Format v1 collection file to ENVI or CSV.

Licence obligations travel with the exported data:

* a wide CSV carries the per-spectrum ``license`` and an ``attribution``
  column (citation plus licence); a long CSV carries ``license`` on every
  point and both columns in its ``{name}.spectra.csv`` table;
* ENVI headers carry the licence summary in ``description``, the
  per-spectrum ``openspeclib license`` and ``openspeclib attribution``, and
  the ``LicenseRef-`` definitions in ``openspeclib license refs``;
* every export writes ``{name}.README.md`` (who to cite, under which
  licences, which datasets, what processing was applied) and
  ``{name}.collection.json`` (the collection and dataset metadata, ready
  for ``openspeclib convert --metadata-json``; partly exported datasets
  are marked ``subset``, and an ``openspeclib_export`` key tells
  ``convert`` the values are already fractions).
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from openspeclib.conformance import check_spdx_expression
from openspeclib.convert.common import EXPORT_MARKER_KEY, ConversionError, ImportReport
from openspeclib.convert.csv import write_long_csv, write_metadata_csv, write_wide_csv
from openspeclib.convert.envi import (
    ENVI_IGNORE_VALUE,
    header_wavelengths_nm,
    parse_envi_header,
    read_header_text,
    write_envi_library,
)
from openspeclib.reader import Collection, Spectrum
from openspeclib.reader import open as open_collection
from openspeclib.storage import ARRAY_COLUMNS, utc_timestamp

__all__ = [
    "EXPORT_FORMATS",
    "ExportResult",
    "export_collection",
    "parse_resample_spec",
    "parse_search_terms",
    "resample_spectrum",
    "select_spectra",
]

EXPORT_FORMATS: tuple[str, ...] = ("envi", "csv")

# Metadata columns first in every export, in this order.
_LEADING_COLUMNS: tuple[str, ...] = (
    "id",
    "original_id",
    "name",
    "material_name",
    "material_category",
    "quantity",
    "geometry",
    "wavelength_unit",
    "license",
    "attribution",
    "dataset_id",
)
# Columns recomputed on import and therefore not exported.
_DERIVED_COLUMNS = frozenset({"collection", "wavelength_min_nm", "wavelength_max_nm"})

# Search keys accepted by ``--search`` and the reader argument each sets.
_SEARCH_KEYS: dict[str, str] = {
    "material": "material",
    "category": "category",
    "material_category": "category",
    "quantity": "quantity",
    "geometry": "geometry",
    "dataset": "dataset_id",
    "dataset_id": "dataset_id",
    "wavelength_range_nm": "wavelength_range_nm",
    "range": "wavelength_range_nm",
}

# Most points a resampling grid may have.
_MAX_GRID_POINTS = 1_000_000

# ``extra`` keys holding one item per stored band, in axis order: written by
# the loaders (``source_band_order``) and by ``convert`` (``source_band_index``).
# They describe the stored axis, so a resampled export drops them.
AXIS_INDEXED_EXTRA_KEYS: tuple[str, ...] = ("source_band_order", "source_band_index")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def parse_search_terms(terms: Sequence[str]) -> dict[str, Any]:
    """Parse ``--search`` terms into :meth:`Collection.search` arguments.

    Each term is ``key=value``; a term without ``=`` is a material search.
    Keys: ``material``, ``category``, ``quantity``, ``geometry``,
    ``dataset_id`` (comma-separated values allowed except for material),
    and ``wavelength_range_nm=LO:HI`` (either end may be empty).

    Args:
        terms: The terms.

    Returns:
        Keyword arguments for ``search``.

    Raises:
        ConversionError: On an unknown key or a malformed range.
    """
    criteria: dict[str, Any] = {}
    for term in terms:
        key, sep, value = term.partition("=")
        if not sep:
            key, value = "material", term
        key = key.strip().lower()
        if key not in _SEARCH_KEYS:
            raise ConversionError(
                f"unknown search key {key!r}; use one of {', '.join(sorted(_SEARCH_KEYS))}"
            )
        arg = _SEARCH_KEYS[key]
        value = value.strip()
        if arg == "material":
            criteria[arg] = value
        elif arg == "wavelength_range_nm":
            lo, colon, hi = value.replace(",", ":").partition(":")
            if not colon:
                raise ConversionError(f"wavelength_range_nm must be LO:HI, got {value!r}")
            try:
                criteria[arg] = (
                    float(lo) if lo.strip() else None,
                    float(hi) if hi.strip() else None,
                )
            except ValueError as exc:
                raise ConversionError(f"wavelength_range_nm: {exc}") from exc
        else:
            criteria[arg] = [v.strip() for v in value.split(",") if v.strip()]
    return criteria


def _list_arrays(column: Any) -> list[np.ndarray | None]:
    """Split an Arrow list column into one numpy array per row.

    Args:
        column: A (chunked) list array.

    Returns:
        The per-row arrays (``None`` for null lists).
    """
    array = column.combine_chunks() if isinstance(column, pa.ChunkedArray) else column
    if len(array) == 0:
        return []
    offsets = np.asarray(array.offsets.to_numpy(zero_copy_only=False), dtype=np.int64)
    flat = np.asarray(array.values.to_numpy(zero_copy_only=False))
    valid = np.asarray(array.is_valid().to_numpy(zero_copy_only=False), dtype=bool)
    return [flat[offsets[i] : offsets[i + 1]] if valid[i] else None for i in range(len(array))]


def _table_spectra(table: pa.Table) -> list[Spectrum]:
    """Build :class:`Spectrum` objects from a collection table.

    Args:
        table: Rows of a collection file (all columns).

    Returns:
        One spectrum per row, in table order; ``metadata`` keeps ``extra``
        as its JSON text.
    """
    names = [n for n in table.column_names if n not in ARRAY_COLUMNS]
    metas: list[dict[str, Any]] = table.select(names).to_pylist()
    arrays = {n: _list_arrays(table[n]) for n in ARRAY_COLUMNS if n in table.column_names}
    spectra = []
    for i, meta in enumerate(metas):
        fwhm = arrays.get("bandpass_fwhm", [None] * len(metas))[i]
        stddev = arrays.get("values_stddev", [None] * len(metas))[i]
        wavelengths = arrays["wavelengths"][i]
        values = arrays["values"][i]
        spectra.append(
            Spectrum(
                id=str(meta["id"]),
                wavelengths=np.asarray(wavelengths, dtype=np.float64),
                values=np.asarray(values, dtype=np.float32),
                wavelength_unit=str(meta["wavelength_unit"]),
                metadata=meta,
                bandpass_fwhm=None if fwhm is None else np.asarray(fwhm, np.float32),
                values_stddev=None if stddev is None else np.asarray(stddev, np.float32),
            )
        )
    return spectra


def select_spectra(
    collection: Collection,
    ids: Sequence[str] = (),
    search: Mapping[str, Any] | None = None,
    limit: int | None = None,
) -> list[Spectrum]:
    """Read the spectra an export selects.

    Args:
        collection: The open collection file.
        ids: Spectrum ids (full ``collection:local`` or bare local ids), in
            output order. When given, they must all match the search.
        search: :meth:`Collection.search` arguments.
        limit: Keep at most this many spectra.

    Returns:
        The spectra, in the order of ``ids`` or else file order.

    Raises:
        ConversionError: If an id is unknown or nothing is selected.
    """
    try:
        meta = collection.search(**dict(search or {}))
    except (TypeError, KeyError, pa.ArrowInvalid) as exc:
        raise ConversionError(f"invalid search: {exc}") from exc
    matching = [str(i) for i in meta["id"].to_pylist()]
    if ids:
        prefix = f"{collection.collection_id}:"
        wanted = [i if ":" in i else prefix + i for i in ids]
        known = set(matching)
        missing = [i for i in wanted if i not in known]
        if missing:
            raise ConversionError(
                f"{len(missing)} id(s) not found{' or not matching --search' if search else ''}: "
                f"{', '.join(missing[:5])}"
            )
        selected = list(dict.fromkeys(wanted))
    else:
        selected = matching
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ConversionError("no spectra match the selection")
    table = collection.to_table(filter=pc.field("id").isin(selected))
    by_id = {s.id: s for s in _table_spectra(table)}
    return [by_id[i] for i in selected]


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------


def parse_resample_spec(spec: str) -> np.ndarray:
    """Parse a ``--resample-to`` target into a wavelength grid in nm.

    Accepted forms:

    * ``START:STOP:STEP`` in nm, e.g. ``400:2500:10`` (STOP included when
      it falls on the grid);
    * a sensor definition: an ENVI header (``.hdr``) whose ``wavelength``
      list gives the band centres, in its stated unit;
    * a text/CSV file of band centres in nm, one per line (first column;
      lines starting with ``#`` and non-numeric lines are skipped).

    Args:
        spec: The target.

    Returns:
        A strictly increasing float64 grid in nm.

    Raises:
        ConversionError: If the spec is malformed or the grid is not
            strictly increasing and positive.
    """
    path = Path(spec)
    if path.is_file():
        if path.suffix.lower() == ".hdr":
            grid = _header_grid(path)
        else:
            centres = []
            for line in path.read_text(encoding="utf-8").splitlines():
                first = line.replace(";", ",").replace("\t", ",").split(",")[0].strip()
                if not first or first.startswith("#"):
                    continue
                try:
                    centres.append(float(first))
                except ValueError:
                    continue
            grid = np.asarray(centres, dtype=np.float64)
    else:
        parts = spec.split(":")
        if len(parts) != 3:
            raise ConversionError(
                f"--resample-to {spec!r}: use START:STOP:STEP in nm (e.g. 400:2500:10) or "
                f"a sensor file (ENVI .hdr, or a text file of band centres in nm)"
            )
        try:
            start, stop, step = (float(p) for p in parts)
        except ValueError as exc:
            raise ConversionError(f"--resample-to {spec!r}: {exc}") from exc
        if not (step > 0 and stop > start):
            raise ConversionError(f"--resample-to {spec!r}: need STOP > START and STEP > 0")
        count = int(np.floor((stop - start) / step + 1e-9)) + 1
        if count > _MAX_GRID_POINTS:
            raise ConversionError(f"--resample-to {spec!r} gives {count} points; too many")
        grid = start + step * np.arange(count, dtype=np.float64)
    if grid.size < 2 or not np.all(np.isfinite(grid)) or not np.all(grid > 0):
        raise ConversionError(f"--resample-to {spec!r}: need at least 2 positive wavelengths")
    if not np.all(np.diff(grid) > 0):
        raise ConversionError(f"--resample-to {spec!r}: wavelengths must be strictly increasing")
    return grid


def _header_grid(path: Path) -> np.ndarray:
    """Return the band centres of any ENVI header (library or image), in nm.

    Args:
        path: The ``.hdr`` file, e.g. an EMIT or AVIRIS image header.

    Returns:
        The grid, ascending.

    Raises:
        ConversionError: If the header has no usable wavelength list.
    """
    report = ImportReport()
    header = parse_envi_header(read_header_text(path, None, report), report)
    return header_wavelengths_nm(header, str(path))


def resample_spectrum(spectrum: Spectrum, grid_nm: np.ndarray) -> np.ndarray:
    """Linearly interpolate a spectrum onto a grid in nm.

    Grid points outside the spectrum's range, and points next to a missing
    band, are NaN. No spectral response function is applied.

    Args:
        spectrum: The spectrum.
        grid_nm: Target wavelengths in nm (ascending).

    Returns:
        float32 values on the grid.
    """
    x = spectrum.wavelengths_nm()
    y = spectrum.values.astype(np.float64)
    order = np.argsort(x)
    out = np.interp(grid_nm, x[order], y[order], left=np.nan, right=np.nan)
    return np.asarray(out, dtype=np.float32)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@dataclass
class ExportResult:
    """Outcome of :func:`export_collection`.

    Attributes:
        files: Every file written.
        num_spectra: Spectra exported.
        resampled: Whether values were resampled onto a grid.
        warnings: Notes on what could not be exported.
    """

    files: list[Path] = field(default_factory=list)
    num_spectra: int = 0
    resampled: bool = False
    warnings: list[str] = field(default_factory=list)


def _shares_axis(spectra: Sequence[Spectrum]) -> bool:
    """Tell whether all spectra have the same axis and unit.

    Args:
        spectra: The spectra.

    Returns:
        ``True`` when every axis equals the first.
    """
    first = spectra[0]
    return all(
        s.wavelength_unit == first.wavelength_unit
        and np.array_equal(s.wavelengths, first.wavelengths)
        for s in spectra[1:]
    )


def _drop_axis_extra(extra: Any) -> Any:
    """Remove the per-band ``extra`` fields that index the original axis.

    Args:
        extra: The ``extra`` column value: JSON text, a dict or ``None``.

    Returns:
        The same kind of value without :data:`AXIS_INDEXED_EXTRA_KEYS`
        (``None`` when nothing is left).
    """
    if extra is None:
        return None
    parsed = json.loads(extra) if isinstance(extra, str) else dict(extra)
    if not isinstance(parsed, dict) or not any(k in parsed for k in AXIS_INDEXED_EXTRA_KEYS):
        return extra
    for key in AXIS_INDEXED_EXTRA_KEYS:
        parsed.pop(key, None)
    if not parsed:
        return None
    return json.dumps(parsed, ensure_ascii=False) if isinstance(extra, str) else parsed


def _attribution(meta: Mapping[str, Any], footer: Mapping[str, Any]) -> str:
    """Compose a one-line attribution for a spectrum.

    Args:
        meta: The spectrum's metadata.
        footer: The collection footer.

    Returns:
        Citation text followed by the licence.
    """
    collection = footer["collection"]
    dataset = (footer.get("datasets") or {}).get(meta.get("dataset_id") or "")
    licence = str(meta["license"])
    ref = (collection.get("license_refs") or {}).get(licence)
    licence_text = f"{licence} ({ref['name']})" if ref else licence
    if dataset:
        cite = dataset.get("citation") or dataset.get("title") or ""
        return f"{cite} Via {collection['title']}. Licence: {licence_text}.".strip()
    return f"{collection['citation']} Licence: {licence_text}."


def _export_columns(metas: Sequence[Mapping[str, Any]]) -> list[str]:
    """Choose the metadata columns to export.

    Args:
        metas: The exported spectra's metadata.

    Returns:
        Core columns first, then every other column with a value, ``extra``
        last; derived columns left out.
    """
    present: list[str] = []
    for meta in metas:
        for name, value in meta.items():
            if value is not None and name not in present and name not in _DERIVED_COLUMNS:
                present.append(name)
    columns = [c for c in _LEADING_COLUMNS if c in present]
    columns += [c for c in present if c not in columns and c != "extra"]
    if "extra" in present:
        columns.append("extra")
    return columns


def _custom_value(value: Any) -> str | None:
    """Format a metadata value for an ENVI ``openspeclib`` field.

    Args:
        value: The value.

    Returns:
        Text, or ``None`` for null.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _collection_json(
    footer: Mapping[str, Any],
    spectra: Sequence[Spectrum],
    total: int,
    source: str,
    dataset_totals: Mapping[str, int],
) -> dict[str, Any]:
    """Build the ``.collection.json`` sidecar.

    The collection, and each dataset only partly exported, is marked
    ``subset`` with a note, and ``spectra_included`` counts what was
    exported. The ``openspeclib_export`` key tells ``convert`` that the
    values are already fractions.

    Args:
        footer: The source footer.
        spectra: The exported spectra.
        total: Spectra in the source file.
        source: Source file name.
        dataset_totals: Spectra per dataset id in the source file.

    Returns:
        ``{"collection": …, "datasets": …, "openspeclib_export": …}`` for
        ``convert --metadata-json``.
    """
    from openspeclib import __version__

    collection = dict(footer["collection"])
    if len(spectra) < total:
        note = (
            f"{len(spectra)} of the {total} spectra of {source} exported with "
            f"openspeclib export."
        )
        previous = collection.get("inclusion_notes")
        collection["inclusion"] = "subset"
        collection["inclusion_notes"] = f"{note} {previous}" if previous else note
    used = Counter(str(s.metadata["dataset_id"]) for s in spectra if s.metadata.get("dataset_id"))
    datasets = {}
    for key, entry in (footer.get("datasets") or {}).items():
        if key not in used:
            continue
        entry = dict(entry)
        entry["spectra_included"] = used[key]
        available = dataset_totals.get(key, used[key])
        if used[key] < available:
            note = f"{used[key]} of the {available} spectra of this dataset in {source} exported."
            previous = entry.get("inclusion_notes")
            entry["inclusion"] = "subset"
            entry["inclusion_notes"] = f"{note} {previous}" if previous else note
        datasets[key] = entry
    return {
        "collection": collection,
        "datasets": datasets,
        EXPORT_MARKER_KEY: {
            "generator": f"openspeclib {__version__}",
            "source": source,
            "values": "fractions as stored in Format v1 (no rescaling on re-import)",
        },
    }


def _licence_terms(licence: str, refs: Mapping[str, Any]) -> str:
    """Describe the terms of a licence expression for the README.

    Args:
        licence: An SPDX expression, ``LicenseRef-`` or ``NOASSERTION``.
        refs: The footer ``license_refs``.

    Returns:
        The definition of every ``LicenseRef-`` the expression uses (found
        by parsing it), else the SPDX licence page.
    """
    if licence == "NOASSERTION":
        return "No licence stated by the source; check the dataset terms below."
    error, used = check_spdx_expression(licence)
    parts = []
    for ref_id in sorted(used):
        ref = refs.get(ref_id) or {}
        text = f"{ref_id}: {ref.get('name', 'not defined in the source')}"
        text += f" ({ref['url']})" if ref.get("url") else ""
        text += f". {ref['text']}" if ref.get("text") else ""
        parts.append(text)
    if not used and error is None and " " not in licence.strip():
        parts.append(f"https://spdx.org/licenses/{licence.strip().rstrip('+')}.html")
    return " ".join(parts)


def _ref_texts(spectra_licences: Sequence[str], refs: Mapping[str, Any]) -> list[str]:
    """Describe the ``LicenseRef-`` licences used, for the ENVI header.

    Args:
        spectra_licences: The licences of the exported spectra.
        refs: The footer ``license_refs``.

    Returns:
        ``"LicenseRef-X: name (url)"`` per licence used.
    """
    used: set[str] = set()
    for licence in spectra_licences:
        used |= check_spdx_expression(licence)[1]
    texts = []
    for ref_id in sorted(used):
        ref = refs.get(ref_id) or {}
        text = f"{ref_id}: {ref.get('name', '')}"
        texts.append(text + (f" ({ref['url']})" if ref.get("url") else ""))
    return texts


def _dataset_totals(collection: Collection) -> dict[str, int]:
    """Count the spectra of each dataset in a collection file.

    Args:
        collection: The open collection.

    Returns:
        Dataset id to number of spectra.
    """
    if "dataset_id" not in collection.column_names:
        return {}
    ids = collection.to_table(columns=["dataset_id"])["dataset_id"].to_pylist()
    return dict(Counter(str(i) for i in ids if i is not None))


def _readme(
    footer: Mapping[str, Any],
    spectra: Sequence[Spectrum],
    *,
    source: str,
    fmt: str,
    files: Sequence[Path],
    processing: Sequence[str],
) -> str:
    """Write the attribution README for an export.

    Args:
        footer: The source footer.
        spectra: The exported spectra.
        source: Source file name.
        fmt: Format description.
        files: Data files written.
        processing: Notes on processing applied during export.

    Returns:
        Markdown text.
    """
    from openspeclib import __version__

    c = footer["collection"]
    refs = c.get("license_refs") or {}
    lines = [
        f"# {c['title']}: exported spectra",
        "",
        f"{len(spectra)} spectra exported from `{source}` (collection `{c['id']}`, "
        f"OpenSpecLib Format {footer.get('format_version')}) as {fmt} on {utc_timestamp()} "
        f"with openspeclib {__version__}.",
        "",
        "## Licence and citation",
        "",
        "These spectra stay under their original licences. Keep this file with the data "
        "when you share it, and cite the sources below when you use it.",
        "",
        f"- Collection licence: `{c['license']}`",
    ]
    if c.get("license_note"):
        lines.append(f"- Licence note: {c['license_note']}")
    lines.append(f"- Cite: {c['citation']}")
    for key, label in (
        ("doi", "DOI"),
        ("url", "URL"),
        ("version", "Version"),
        ("publisher", "Publisher"),
    ):
        if c.get(key):
            lines.append(f"- {label}: {c[key]}")
    lines += ["", "| Licence | Spectra | Terms |", "|---|---|---|"]
    for licence, count in sorted(Counter(str(s.metadata["license"]) for s in spectra).items()):
        lines.append(f"| `{licence}` | {count} | {_licence_terms(licence, refs)} |")
    datasets = footer.get("datasets") or {}
    used = Counter(
        str(s.metadata.get("dataset_id")) for s in spectra if s.metadata.get("dataset_id")
    )
    if used:
        lines += ["", "## Contributing datasets", ""]
        for key, count in sorted(used.items()):
            d = datasets.get(key, {})
            lines.append(f"### {d.get('title', key)} (`{key}`): {count} spectra")
            lines.append("")
            for label, name in (
                ("Licence", "license"),
                ("Licence note", "license_note"),
                ("Cite", "citation"),
                ("Authors", "authors"),
                ("Organisation", "organization"),
                ("URL", "url"),
                ("DOI", "doi"),
            ):
                if d.get(name):
                    lines.append(f"- {label}: {d[name]}")
            lines.append("")
    lines += [
        "",
        "Each spectrum's licence is in the `license` column (wide CSV, and every row of "
        "a long CSV) or the `openspeclib license` header field (ENVI). Its attribution "
        "(citation and licence) is in the `attribution` column (wide CSV, and the "
        "`.spectra.csv` table of a long CSV) or `openspeclib attribution` (ENVI).",
        "",
        "## Processing",
        "",
    ]
    lines += [f"- {p}" for p in processing] or ["- None: values and wavelengths as stored."]
    lines += ["", "## Files", ""]
    spectra_tables = [f.name for f in files if f.name.endswith(".spectra.csv")]
    join = f" --spectra-table {spectra_tables[0]}" if spectra_tables else ""
    for f in files:
        if f.name.endswith(".collection.json"):
            lines.append(
                f"- `{f.name}`: collection metadata; re-import with "
                f"`openspeclib convert {files[0].name} OUT.parquet --metadata-json {f.name}{join}`"
            )
        elif f.name.endswith(".spectra.csv"):
            lines.append(f"- `{f.name}`: one row per spectrum: metadata, licence, attribution")
        else:
            lines.append(f"- `{f.name}`")
    return "\n".join(lines) + "\n"


def export_collection(
    input_path: Path,
    outdir: Path,
    *,
    to: str,
    layout: str = "wide",
    ids: Sequence[str] = (),
    search: Sequence[str] = (),
    limit: int | None = None,
    resample_to: str | None = None,
    basename: str | None = None,
) -> ExportResult:
    """Export spectra of a Format v1 collection file to ENVI or CSV.

    Args:
        input_path: The collection file.
        outdir: Output directory (created if needed).
        to: ``envi`` or ``csv``.
        layout: CSV layout: ``wide`` or ``long``.
        ids: Spectrum ids to export (default: all matching ``search``).
        search: ``--search`` terms (see :func:`parse_search_terms`).
        limit: Export at most this many spectra.
        resample_to: Grid spec (see :func:`parse_resample_spec`).
        basename: Output file stem; defaults to the collection id.

    Returns:
        The files written and any warnings.

    Raises:
        ConversionError: If the selection is empty, the axes differ and no
            grid is given (ENVI and wide CSV), or an argument is invalid.
    """
    if to not in EXPORT_FORMATS:
        raise ConversionError(f"unknown export format {to!r}; use envi or csv")
    if to == "csv" and layout not in ("wide", "long"):
        raise ConversionError(f"unknown CSV layout {layout!r}; use wide or long")
    try:
        collection = open_collection(input_path)
    except (OSError, ValueError) as exc:
        raise ConversionError(f"cannot open {input_path}: {exc}") from exc
    footer = collection.metadata
    grid = parse_resample_spec(resample_to) if resample_to else None
    spectra = select_spectra(collection, ids, parse_search_terms(search), limit)
    result = ExportResult(num_spectra=len(spectra), resampled=grid is not None)
    base = outdir / (basename or collection.collection_id)
    processing: list[str] = []

    metas = []
    for s in spectra:
        meta = dict(s.metadata)
        meta["attribution"] = _attribution(meta, footer)
        metas.append(meta)

    needs_shared_axis = to == "envi" or layout == "wide"
    matrix: np.ndarray | None = None
    if grid is not None:
        matrix = np.vstack([resample_spectrum(s, grid) for s in spectra])
        axis, unit = grid, "nm"
        for meta, row in zip(metas, matrix):
            meta["wavelength_unit"] = "nm"
            meta["extra"] = _drop_axis_extra(meta.get("extra"))
            meta["measurement_processing"] = list(meta.get("measurement_processing") or []) + [
                "resampled"
            ]
            if not np.isfinite(row).any():
                result.warnings.append(f"{meta['id']}: no valid value on the resampling grid")
        processing.append(
            f"Linearly interpolated onto {len(grid)} wavelengths from {grid[0]:g} to "
            f"{grid[-1]:g} nm (no spectral response function applied); grid points outside "
            f"a spectrum's range are missing. 'resampled' added to measurement_processing; "
            f"per-band extra fields of the original axis ({', '.join(AXIS_INDEXED_EXTRA_KEYS)}) "
            f"dropped."
        )
    elif needs_shared_axis and not _shares_axis(spectra):
        distinct = len({(s.wavelength_unit, s.wavelengths.tobytes()) for s in spectra})
        what = "An ENVI spectral library" if to == "envi" else "A wide CSV"
        raise ConversionError(
            f"{what} needs one shared wavelength axis, but the {len(spectra)} selected "
            f"spectra have {distinct} different axes. Pass --resample-to START:STOP:STEP "
            f"(nm, e.g. 400:2500:10) or a sensor file"
            + (", or use --layout long" if to == "csv" else "")
            + "."
        )
    else:
        axis, unit = spectra[0].wavelengths, spectra[0].wavelength_unit
        matrix = np.vstack([s.values for s in spectra]) if needs_shared_axis else None

    columns = _export_columns(metas)

    if to == "envi":
        assert matrix is not None
        fwhm: np.ndarray | None = None
        widths = [s.bandpass_fwhm for s in spectra]
        present = [w for w in widths if w is not None]
        if grid is None and len(present) == len(widths):
            if all(np.array_equal(w, present[0], equal_nan=True) for w in present[1:]):
                fwhm = present[0]
        if fwhm is None and any(w is not None for w in widths):
            result.warnings.append(
                "bandpass_fwhm not written: it differs between spectra or was resampled"
            )
        if any(s.values_stddev is not None for s in spectra):
            result.warnings.append("values_stddev not written: ENVI libraries have no place for it")
        custom = {
            c: [_custom_value(m.get(c)) for m in metas] for c in columns if c != "wavelength_unit"
        }
        description = (
            f"{footer['collection']['title']} ({collection.collection_id}): {len(spectra)} "
            f"spectra exported by openspeclib. Licence: {footer['collection']['license']}. "
            f"Per-spectrum licences in 'openspeclib license'. Cite: "
            f"{footer['collection']['citation']} See {base.name}.README.md."
        )
        data_path, header_path = write_envi_library(
            base,
            axis,
            matrix,
            [str(m["name"]) for m in metas],
            unit,
            fwhm=fwhm,
            description=description,
            custom_fields=custom,
            license_refs=_ref_texts(
                [str(m["license"]) for m in metas],
                footer["collection"].get("license_refs") or {},
            ),
        )
        result.files += [data_path, header_path]
        processing.append(
            f"Missing values (NaN) written as data ignore value = {ENVI_IGNORE_VALUE:g}."
        )
        fmt = "an ENVI spectral library"
    elif layout == "wide":
        assert matrix is not None
        if any(s.bandpass_fwhm is not None or s.values_stddev is not None for s in spectra):
            result.warnings.append(
                "bandpass_fwhm/values_stddev not written: a wide CSV has one row per "
                "spectrum (use --layout long to keep them)"
            )
        result.files.append(
            write_wide_csv(base.with_name(base.name + ".csv"), metas, columns, axis, matrix)
        )
        fmt = "a wide CSV (one row per spectrum)"
    else:
        points: list[tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None]]
        if grid is not None:
            assert matrix is not None
            points = [(grid, row, None, None) for row in matrix]
        else:
            points = [(s.wavelengths, s.values, s.bandpass_fwhm, s.values_stddev) for s in spectra]
        # Spectrum metadata goes to a companion table instead of being
        # repeated on every point: a long table is already ~points x larger.
        result.files.append(
            write_long_csv(
                base.with_name(base.name + ".csv"),
                metas,
                ["id", "wavelength_unit", "license"],
                points,
            )
        )
        result.files.append(
            write_metadata_csv(base.with_name(base.name + ".spectra.csv"), metas, columns)
        )
        fmt = (
            "a long CSV (one row per point) with a per-spectrum metadata table "
            f"({base.name}.spectra.csv)"
        )

    sidecar = base.with_name(base.name + ".collection.json")
    result.files.append(sidecar)
    sidecar.write_text(
        json.dumps(
            _collection_json(
                footer,
                spectra,
                len(collection),
                os.path.basename(input_path),
                _dataset_totals(collection),
            ),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    readme = base.with_name(base.name + ".README.md")
    readme.write_text(
        _readme(
            footer,
            spectra,
            source=os.path.basename(input_path),
            fmt=fmt,
            files=result.files,
            processing=processing,
        ),
        encoding="utf-8",
    )
    result.files.append(readme)
    return result
