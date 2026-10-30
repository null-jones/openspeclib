"""Convert ENVI spectral libraries and CSV tables into Format v1 collection files."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openspeclib.conformance import ConformanceReport, check_collection
from openspeclib.convert.common import (
    ConversionError,
    FieldMapper,
    ImportDefaults,
    ImportReport,
    build_collection_info,
    build_datasets,
    iter_records,
    load_mapping_file,
    load_metadata_json,
)
from openspeclib.convert.csv import read_csv_spectra
from openspeclib.convert.envi import envi_to_spectra, read_envi_library
from openspeclib.models import CollectionInfo
from openspeclib.storage import write_collection

__all__ = [
    "ConvertResult",
    "NonConformantError",
    "SOURCE_FORMATS",
    "convert_file",
    "detect_format",
]

SOURCE_FORMATS: tuple[str, ...] = ("auto", "envi", "csv")

_ENVI_SUFFIXES = frozenset({".hdr", ".sli", ".esl", ".lib", ".spl"})
_CSV_SUFFIXES = frozenset({".csv", ".tsv", ".tab", ".txt"})


def detect_format(path: Path) -> str:
    """Guess the input format from the file name.

    Args:
        path: The input file.

    Returns:
        ``"envi"`` or ``"csv"``.

    Raises:
        ConversionError: If the format cannot be told.
    """
    suffix = path.suffix.lower()
    if suffix in _ENVI_SUFFIXES:
        return "envi"
    if suffix in _CSV_SUFFIXES:
        return "csv"
    if path.with_suffix(".hdr").is_file() or Path(f"{path}.hdr").is_file():
        return "envi"
    raise ConversionError(f"cannot tell the format of {path.name}; pass --from envi or --from csv")


@dataclass
class ConvertResult:
    """Outcome of :func:`convert_file`.

    Attributes:
        output: The collection file written.
        num_spectra: Spectra written.
        source_format: ``envi`` or ``csv``.
        layout: CSV layout used (``wide``/``long``), ``None`` for ENVI.
        collection: The footer ``collection`` object written.
        report: Warnings, unmapped columns and the value scale applied.
        conformance: The conformance report of the written file.
    """

    output: Path
    num_spectra: int
    source_format: str
    layout: str | None
    collection: CollectionInfo
    report: ImportReport = field(default_factory=ImportReport)
    conformance: ConformanceReport | None = None


class NonConformantError(ConversionError):
    """The converted file does not conform; it was not written to the output path.

    Attributes:
        result: The conversion result with its conformance report.
    """

    def __init__(self, message: str, result: ConvertResult) -> None:
        """Create the error.

        Args:
            message: Summary of the failed rules.
            result: The conversion result.
        """
        super().__init__(message)
        self.result = result


def convert_file(
    input_path: Path,
    output_path: Path,
    *,
    source_format: str = "auto",
    layout: str = "auto",
    collection: Mapping[str, Any] | None = None,
    metadata_json: Path | None = None,
    license_name: str | None = None,
    license_url: str | None = None,
    defaults: ImportDefaults | None = None,
    mapping: Mapping[str, str] | Path | None = None,
    delimiter: str | None = None,
    spectra_table: Path | None = None,
    encoding: str | None = None,
    allow_trailing_bytes: bool = False,
) -> ConvertResult:
    """Convert an ENVI spectral library or a CSV table to a Format v1 collection file.

    The file is written next to ``output_path`` under a temporary name,
    checked with :func:`openspeclib.conformance.check_collection`, and moved
    to ``output_path`` only if it conforms. There is no way to keep a
    non-conformant file: the storage writer refuses one too.

    Args:
        input_path: ``.hdr``/``.sli`` or ``.csv``/``.tsv`` file.
        output_path: The ``.parquet`` file to write.
        source_format: ``envi``, ``csv`` or ``auto`` (from the extension).
        layout: CSV layout: ``wide``, ``long`` or ``auto``.
        collection: Footer ``collection`` fields from flags (win over the JSON).
        metadata_json: JSON file with the ``collection`` object (and optional
            ``datasets``).
        license_name: Name defining a ``LicenseRef-`` collection licence.
        license_url: URL for that ``LicenseRef-``.
        defaults: Per-spectrum defaults (quantity, geometry, …).
        mapping: Column mapping, or a JSON file holding one.
        delimiter: CSV delimiter, or ``None`` to detect it.
        spectra_table: Per-spectrum metadata CSV for a long table, joined by
            the key column.
        encoding: Text encoding of the CSV or ENVI header; ``None`` tries
            UTF-8 (and UTF-16 with a BOM), then Latin-1 with a warning.
        allow_trailing_bytes: Accept an ENVI data file longer than its header
            describes.

    Returns:
        The result, including the conformance report.

    Raises:
        ConversionError: If the input or metadata cannot be converted.
        NonConformantError: If the result does not conform (nothing is written).
    """
    defaults = defaults or ImportDefaults()
    base: dict[str, Any] = {}
    raw_datasets: dict[str, Any] = {}
    from_export = False
    if metadata_json is not None:
        base, raw_datasets, from_export = load_metadata_json(metadata_json)
    info = build_collection_info(
        base, collection or {}, license_name=license_name, license_url=license_url
    )
    datasets = build_datasets(raw_datasets, info)
    overrides = load_mapping_file(mapping) if isinstance(mapping, Path) else mapping
    mapper = FieldMapper(overrides)
    report = ImportReport()

    fmt = detect_format(input_path) if source_format == "auto" else source_format
    used_layout: str | None = None
    header_divisor: float | None = None
    if fmt == "envi":
        if spectra_table is not None:
            raise ConversionError("--spectra-table applies to long CSV tables only")
        library = read_envi_library(
            input_path,
            wavelength_unit=defaults.wavelength_unit,
            report=report,
            allow_trailing_bytes=allow_trailing_bytes,
            encoding=encoding,
        )
        spectra = envi_to_spectra(library, mapper)
        header_divisor = library.scale_factor
        from_export = from_export or library.from_export
    elif fmt == "csv":
        spectra, used_layout = read_csv_spectra(
            input_path,
            mapper,
            layout=layout,
            delimiter=delimiter,
            wavelength_unit=defaults.wavelength_unit,
            report=report,
            spectra_table=spectra_table,
            encoding=encoding,
        )
    else:
        raise ConversionError(f"unknown input format {fmt!r}; use envi, csv or auto")

    records = iter_records(
        spectra,
        info,
        datasets,
        defaults,
        report,
        header_divisor=header_divisor,
        from_export=from_export,
    )
    for column, target in sorted(mapper.collisions):
        report.warn(
            f"column {column!r} also maps to {target} but an earlier column set it; its "
            f"different values were kept in extra.source_metadata"
        )
    report.unmapped_columns = sorted(mapper.unmapped)
    if report.unmapped_columns:
        report.warn(
            f"{len(report.unmapped_columns)} column(s) not mapped to Format v1 fields were "
            f"kept in extra.source_metadata: {', '.join(report.unmapped_columns[:10])}"
            + (" …" if len(report.unmapped_columns) > 10 else "")
        )
    if output_path.name != f"{info.id}.parquet":
        report.warn(f"the spec recommends naming the file {info.id}.parquet")
    tmp = output_path.with_name(f".{output_path.name}.converting.parquet")
    try:
        written = write_collection(records, tmp, info, datasets)
        conformance = check_collection(tmp)
    except ValueError as exc:
        tmp.unlink(missing_ok=True)
        raise ConversionError(str(exc)) from exc
    result = ConvertResult(
        output=output_path,
        num_spectra=written.num_spectra,
        source_format=fmt,
        layout=used_layout,
        collection=info,
        report=report,
        conformance=conformance,
    )
    if not conformance.conformant:
        tmp.unlink(missing_ok=True)
        details = "; ".join(
            f"{r.rule_id}: {r.examples[0] if r.examples else r.title}"
            for r in conformance.results
            if r.status == "failed"
        )
        raise NonConformantError(
            f"the converted file does not conform (failed {', '.join(conformance.failed)}: "
            f"{details}); nothing was written to {output_path}",
            result,
        )
    tmp.replace(output_path)
    return result
