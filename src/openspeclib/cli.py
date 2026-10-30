"""Command-line interface for OpenSpecLib."""

import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import click

from openspeclib import __version__
from openspeclib.loaders.base import BaseLoader

if TYPE_CHECKING:
    from openspeclib.models import SpectrumRecord

logger = logging.getLogger("openspeclib")

# Suffix of the collection-metadata file ``ingest`` writes next to the JSONL.
COLLECTION_METADATA_SUFFIX = ".collection.json"

LOADERS = {
    "usgs": "openspeclib.loaders.usgs:UsgsLoader",
    "ecostress": "openspeclib.loaders.ecostress:EcostressLoader",
    "relab": "openspeclib.loaders.relab:RelabLoader",
    "asu_tes": "openspeclib.loaders.asu_tes:AsuTesLoader",
    "bishop": "openspeclib.loaders.bishop:BishopLoader",
    "ecosis": "openspeclib.loaders.ecosis:EcosisLoader",
    "ossl": "openspeclib.loaders.ossl:OsslLoader",
}


def _get_loader(source: str) -> BaseLoader:
    """Dynamically import and instantiate a loader by source name.

    Args:
        source: Source library key (e.g. ``"usgs"``, ``"ecostress"``).

    Returns:
        An instantiated loader for the requested source.

    Raises:
        click.BadParameter: If the source name is not recognised.
    """
    if source not in LOADERS:
        raise click.BadParameter(f"Unknown source '{source}'. Available: {', '.join(LOADERS)}")
    module_path, class_name = LOADERS[source].rsplit(":", 1)
    import importlib

    module = importlib.import_module(module_path)
    cls: type[BaseLoader] = getattr(module, class_name)
    return cls()


@click.group()
@click.version_option(__version__)
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging.")
def main(verbose: bool) -> None:
    """OpenSpecLib: build, validate, convert and export spectral libraries.

    Every file is written in the OpenSpecLib Format (Format v1).
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option(
    "--source",
    required=True,
    type=click.Choice(list(LOADERS.keys())),
    help="Source library to download.",
)
@click.option(
    "--target",
    required=True,
    type=click.Path(path_type=Path),
    help="Directory to download/extract into.",
)
def download(source: str, target: Path) -> None:
    """Download a source spectral library."""
    loader = _get_loader(source)
    if not loader.supports_auto_download:
        click.echo(
            f"Note: {source} does not support automatic download. "
            f"Data must be placed manually in {target}."
        )
    else:
        click.echo(f"Downloading {source} to {target}...")
    result_path = loader.download(target)
    click.echo(f"Data directory: {result_path}")


@main.command()
@click.option(
    "--source",
    required=True,
    type=click.Choice(list(LOADERS.keys())),
    help="Source library to ingest.",
)
@click.option(
    "--input",
    "input_dir",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Directory containing the source data.",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory for processed records.",
)
def ingest(source: str, input_dir: Path, output: Path) -> None:
    """Ingest a source library into the standard format.

    Writes ``{collection}.jsonl`` (one record per line) and
    ``{collection}.collection.json`` (the collection and dataset metadata for
    the file footer). Both are written as ``.part`` files and renamed only
    when the run succeeds, so an interrupted or failed ingest never leaves a
    truncated or mismatched pair behind; an exception or interrupt mid-run
    leaves a previous ingest's pair untouched. Exits with status 1 when the
    loader reports any fatal skips via ``BaseLoader.fatal_error_count`` — e.g.
    an ECOSTRESS file whose ``Y Units`` scale is missing or unrecognised — so
    a release pipeline cannot silently ship a library with spectra missing.
    That run, and a run that finds no spectra (exit 0 with a warning), also
    delete any previous ``{collection}.jsonl`` and
    ``{collection}.collection.json``, so ``combine`` cannot pick up stale
    output.
    """
    from openspeclib.models import CollectionMetadata

    loader = _get_loader(source)
    output.mkdir(parents=True, exist_ok=True)

    # Name the outputs by the collection id (e.g. ``usgs_splib07``) rather
    # than the CLI source key (``usgs``): combine keys collections by it.
    count = 0
    collection_id = loader.source_name()
    records_file = output / f"{collection_id}.jsonl"
    metadata_file = output / f"{collection_id}{COLLECTION_METADATA_SUFFIX}"
    records_part = records_file.with_name(records_file.name + ".part")
    metadata_part = metadata_file.with_name(metadata_file.name + ".part")

    def _discard() -> None:
        records_part.unlink(missing_ok=True)
        metadata_part.unlink(missing_ok=True)

    def _discard_all() -> None:
        # The run finished but has nothing usable to publish: a previous
        # ingest's output must not survive for ``combine`` to pick up.
        _discard()
        records_file.unlink(missing_ok=True)
        metadata_file.unlink(missing_ok=True)

    click.echo(f"Ingesting {source} from {input_dir}...")
    try:
        with open(records_part, "w", encoding="utf-8") as f:
            for record in loader.load(input_dir):
                f.write(record.model_dump_json() + "\n")
                count += 1
    except BaseException:
        _discard()
        raise

    fatal = loader.fatal_error_count
    if fatal:
        _discard_all()
        click.echo(
            f"Error: {fatal} {source} file(s) were rejected (see ERROR log lines above); "
            f"{count} spectra parsed but not written, and any previous {records_file.name} "
            f"and {metadata_file.name} were removed. Fix the loader or the input and re-run.",
            err=True,
        )
        sys.exit(1)

    if count == 0:
        _discard_all()
        click.echo(
            f"Warning: no spectra found for {source} in {input_dir}; any previous "
            f"{records_file.name} and {metadata_file.name} were removed. "
            f"Ensure the source data has been downloaded and placed correctly.",
            err=True,
        )
        return

    try:
        metadata = CollectionMetadata(
            collection=loader.collection_info(), datasets=loader.datasets()
        )
        metadata_part.write_text(metadata.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except BaseException:
        _discard()
        raise
    records_part.replace(records_file)
    metadata_part.replace(metadata_file)
    click.echo(f"Ingested {count} spectra to {records_file}")


def _stream_jsonl(path: Path) -> Iterator["SpectrumRecord"]:
    """Stream records from an ingested JSONL file.

    Args:
        path: The ``{collection}.jsonl`` file.

    Yields:
        One record per non-empty line.
    """
    from openspeclib.models import SpectrumRecord

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield SpectrumRecord.model_validate_json(line)


@main.command()
@click.option(
    "--input",
    "input_dir",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Directory containing ingested .jsonl and .collection.json files.",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    help="Release directory to write (flat: collection files, index, checksums).",
)
@click.option(
    "--release",
    default=None,
    help="Release version recorded in the index footer (default: the package version).",
)
def combine(input_dir: Path, output: Path, release: str | None) -> None:
    """Build a release directory of Format v1 collection files and an index."""
    from openspeclib.combine import build_library
    from openspeclib.models import CollectionMetadata

    jsonl_files = sorted(input_dir.glob("*.jsonl"))
    if not jsonl_files:
        click.echo("No .jsonl files found in input directory.", err=True)
        sys.exit(1)

    sources = {}
    for jsonl_path in jsonl_files:
        collection_id = jsonl_path.stem
        metadata_path = input_dir / f"{collection_id}{COLLECTION_METADATA_SUFFIX}"
        if not metadata_path.exists():
            click.echo(
                f"Error: {metadata_path.name} is missing; re-run `openspeclib ingest` for "
                f"{collection_id} (ingest output from before release 0.2.0 is not usable).",
                err=True,
            )
            sys.exit(1)
        metadata = CollectionMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
        sources[collection_id] = (_stream_jsonl(jsonl_path), metadata)

    click.echo(f"Combining {len(sources)} collection(s) into {output}...")
    build = build_library(sources, output, release=release)
    for c in build.collections:
        click.echo(
            f"  {c.path.name}: {c.num_spectra} spectra, {len(c.migrations)} ids changed from v0"
        )
    click.echo(
        f"Built release {build.release}: {build.total_spectra} spectra in "
        f"{len(build.collections)} collection(s)"
    )


@main.command()
@click.argument(
    "path",
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--profile",
    type=click.Choice(["format", "openspeclib"]),
    default="format",
    show_default=True,
    help=(
        "format: OpenSpecLib Format v1 conformance only. openspeclib: conformance plus "
        "the OpenSpecLib quality profile (Q1-Q4) with the allowlist."
    ),
)
@click.option(
    "--allowlist",
    "allowlist_paths",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Additional allowlist JSON file (repeatable). Extends the allowlist shipped "
        "with the package; on equal specificity its entries take precedence."
    ),
)
@click.option(
    "--no-default-allowlist",
    is_flag=True,
    help="Do not load the allowlist shipped with the package (use only --allowlist files).",
)
@click.option(
    "--details",
    is_flag=True,
    help="Print every individual issue instead of the aggregated summary.",
)
@click.option(
    "--max-examples",
    default=5,
    show_default=True,
    type=click.IntRange(min=0),
    help="Example spectra listed per aggregated issue group.",
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a machine-readable JSON report with every issue to this path.",
)
def validate(
    path: Path,
    profile: str,
    allowlist_paths: tuple[Path, ...],
    no_default_allowlist: bool,
    details: bool,
    max_examples: int,
    report_path: Path | None,
) -> None:
    """Validate an OpenSpecLib Format v1 file, or a directory of them.

    PATH is a collection file, an index file (checked with every collection
    file it lists), or a directory searched recursively for *.parquet files.
    Exits 0 when there are no errors (warnings allowed), 1 when there is at
    least one error, and 2 on an unreadable or malformed allowlist. Repeated
    issues are aggregated by check and source; use --details or --report for
    the full list.
    """
    import json

    from openspeclib.validate import Allowlist
    from openspeclib.validate import validate as run_validation

    allowlist = None
    if profile == "openspeclib" or allowlist_paths:
        # Allowlist files are always parsed, so a malformed one is reported even
        # when the profile that uses them was forgotten.
        try:
            allowlist = Allowlist.load(allowlist_paths, include_default=not no_default_allowlist)
        except ValueError as e:
            # Exit 2 (usage/config error), distinct from exit 1 (validation failed).
            error = click.ClickException(str(e))
            error.exit_code = 2
            raise error from e
    if profile != "openspeclib":
        allowlist = None
        if allowlist_paths or no_default_allowlist:
            click.echo("Note: allowlists apply only to --profile openspeclib; ignored.")

    click.echo(f"Validating {path} (profile {profile})...")
    result = run_validation(path, profile=profile, allowlist=allowlist)
    click.echo(result.detailed_summary() if details else result.summary(max_examples))

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result.to_dict(max_examples), indent=2) + "\n", encoding="utf-8"
        )
        click.echo(f"Full report written to {report_path}")
    elif not details and (result.errors or result.warnings):
        click.echo("Use --details or --report PATH for every individual issue.")

    if not result.is_valid:
        sys.exit(1)


def _quiet_storage_logs() -> None:
    """Hide the storage layer's INFO lines unless ``-v`` was given."""
    if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
        logging.getLogger("openspeclib.storage").setLevel(logging.WARNING)


def _parse_value_scale(value: str) -> float | None:
    """Parse ``--value-scale``.

    Args:
        value: ``auto`` or a positive number.

    Returns:
        The divisor, or ``None`` for automatic detection.

    Raises:
        click.BadParameter: If the value is neither.
    """
    if value.strip().lower() == "auto":
        return None
    try:
        scale = float(value)
    except ValueError as exc:
        raise click.BadParameter("must be 'auto' or a number, e.g. 100") from exc
    if not scale > 0:
        raise click.BadParameter("must be positive")
    return scale


@main.command()
@click.argument(
    "input_path", metavar="INPUT", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.argument("output", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--from",
    "source_format",
    type=click.Choice(["auto", "envi", "csv"]),
    default="auto",
    show_default=True,
    help="Input format; auto decides from the file extension.",
)
@click.option("--collection-id", help="Collection id, e.g. my_lab_2026 ([a-z0-9_]).")
@click.option("--title", help="Collection title.")
@click.option(
    "--license",
    "license_",
    help="Collection licence: SPDX expression (CC-BY-4.0), NOASSERTION or LicenseRef-….",
)
@click.option("--license-name", help="Defines a LicenseRef- licence: its name.")
@click.option("--license-url", help="Defines a LicenseRef- licence: its URL.")
@click.option("--citation", help="Recommended citation for the collection.")
@click.option("--description", help="Collection description.")
@click.option("--collection-version", help="Version of the source collection.")
@click.option("--url", help="Landing page of the collection.")
@click.option("--doi", help="DOI of the collection (bare, 10.xxxx/...).")
@click.option("--publisher", help="Publishing organisation.")
@click.option(
    "--inclusion",
    type=click.Choice(["complete", "subset"]),
    help="Whether the file holds the whole source (default complete).",
)
@click.option("--inclusion-notes", help="How a subset was chosen (required for subset).")
@click.option(
    "--metadata-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "JSON with the footer 'collection' object (or {'collection': …, 'datasets': …}); "
        "flags override it."
    ),
)
@click.option(
    "--quantity",
    type=click.Choice(["reflectance", "transmittance", "emissivity", "absorbance"]),
    help="Quantity of spectra whose metadata does not state one.",
)
@click.option(
    "--geometry",
    type=click.Choice(
        [
            "bidirectional",
            "biconical",
            "directional_hemispherical",
            "hemispherical_directional",
            "hemispherical_conical",
            "bihemispherical",
            "directional",
            "unknown",
        ]
    ),
    default="unknown",
    show_default=True,
    help="Geometry of spectra whose metadata does not state one.",
)
@click.option(
    "--category",
    type=click.Choice(
        [
            "mineral",
            "rock",
            "soil",
            "vegetation",
            "npv",
            "water",
            "snow_ice",
            "man_made",
            "meteorite",
            "lunar",
            "organic_compound",
            "mixture",
            "other",
        ]
    ),
    help="Material category of spectra whose metadata does not state one (default other).",
)
@click.option(
    "--wavelength-unit",
    type=click.Choice(["nm", "um", "cm-1"]),
    help="Unit of the wavelength axis when the input does not state it (overrides ENVI's).",
)
@click.option(
    "--value-scale",
    default="auto",
    show_default=True,
    help=(
        "Divisor that turns reflectance, transmittance and emissivity into fractions "
        "(100 for percent); absorbance is never rescaled. auto: ENVI 'reflectance "
        "scale factor', else declared units or detection from the data."
    ),
)
@click.option(
    "--layout",
    type=click.Choice(["auto", "wide", "long"]),
    default="auto",
    show_default=True,
    help="CSV layout: wide (one row per spectrum, EcoSIS) or long (one row per point).",
)
@click.option(
    "--mapping",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help='JSON mapping of source columns to Format v1 columns, e.g. {"Mineral": "material_name"}.',
)
@click.option("--delimiter", help="CSV delimiter (default: detected).")
@click.option(
    "--spectra-table",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Long CSV only: per-spectrum metadata CSV joined by the key column "
        "(the NAME.spectra.csv that export --layout long writes)."
    ),
)
@click.option(
    "--encoding",
    help="Text encoding of the CSV or ENVI header (default: UTF-8, else Latin-1 with a warning).",
)
@click.option(
    "--allow-trailing-bytes",
    is_flag=True,
    help="Accept an ENVI data file longer than its header describes (extra bytes ignored).",
)
def convert(
    input_path: Path,
    output: Path,
    source_format: str,
    collection_id: str | None,
    title: str | None,
    license_: str | None,
    license_name: str | None,
    license_url: str | None,
    citation: str | None,
    description: str | None,
    collection_version: str | None,
    url: str | None,
    doi: str | None,
    publisher: str | None,
    inclusion: str | None,
    inclusion_notes: str | None,
    metadata_json: Path | None,
    quantity: str | None,
    geometry: str,
    category: str | None,
    wavelength_unit: str | None,
    value_scale: str,
    layout: str,
    mapping: Path | None,
    delimiter: str | None,
    spectra_table: Path | None,
    encoding: str | None,
    allow_trailing_bytes: bool,
) -> None:
    """Convert an ENVI spectral library or a CSV table to an OpenSpecLib Format v1 file.

    INPUT is an ENVI .hdr/.sli pair (either file) or a .csv/.tsv table.
    OUTPUT is the .parquet collection file to write. It is checked for
    conformance first and written only if it conforms; otherwise the
    command exits 1. Values are scaled to fractions; when the scale is
    ambiguous the command stops and asks for --value-scale. Metadata
    columns that map to no Format v1 field are kept in extra.source_metadata.
    """
    from openspeclib.convert import (
        ConversionError,
        ImportDefaults,
        NonConformantError,
        convert_file,
    )

    _quiet_storage_logs()
    defaults = ImportDefaults(
        quantity=quantity,
        geometry=geometry,
        material_category=category,
        wavelength_unit=wavelength_unit,
        value_scale=_parse_value_scale(value_scale),
    )
    flags = {
        "id": collection_id,
        "title": title,
        "license": license_,
        "citation": citation,
        "description": description,
        "version": collection_version,
        "url": url,
        "doi": doi,
        "publisher": publisher,
        "inclusion": inclusion,
        "inclusion_notes": inclusion_notes,
    }
    try:
        result = convert_file(
            input_path,
            output,
            source_format=source_format,
            layout=layout,
            collection=flags,
            metadata_json=metadata_json,
            license_name=license_name,
            license_url=license_url,
            defaults=defaults,
            mapping=mapping,
            delimiter=delimiter,
            spectra_table=spectra_table,
            encoding=encoding,
            allow_trailing_bytes=allow_trailing_bytes,
        )
    except NonConformantError as exc:
        click.echo(f"Error: {exc}", err=True)
        failed = exc.result.conformance
        for rule in failed.results if failed is not None else []:
            if rule.status == "failed":
                for example in rule.examples:
                    click.echo(f"  {rule.rule_id}: {example}", err=True)
        sys.exit(1)
    except ConversionError as exc:
        raise click.ClickException(str(exc)) from exc

    layout_note = f", {result.layout} layout" if result.layout else ""
    click.echo(
        f"Wrote {result.num_spectra} spectra from {input_path.name} "
        f"({result.source_format}{layout_note}) to {output}"
    )
    report = result.report
    if report.value_divisor != 1.0:
        click.echo(f"Values divided by {report.value_divisor:g} ({report.divisor_source}).")
    for warning in report.warnings:
        click.echo(f"Warning: {warning}", err=True)
    conformance = result.conformance
    assert conformance is not None
    warned = f" (recommendations not met: {', '.join(conformance.warned)})"
    click.echo(
        "Conformance: the file conforms to OpenSpecLib Format v1"
        + (warned if conformance.warned else "")
    )


@main.command(name="export")
@click.argument(
    "input_path", metavar="INPUT", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.argument("outdir", type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--to", "fmt", type=click.Choice(["envi", "csv"]), required=True, help="Output format."
)
@click.option(
    "--layout",
    type=click.Choice(["wide", "long"]),
    default="wide",
    show_default=True,
    help="CSV layout: wide (one row per spectrum) or long (one row per point).",
)
@click.option(
    "--ids",
    multiple=True,
    help="Spectrum ids to export (repeatable or comma-separated; local ids accepted).",
)
@click.option(
    "--search",
    multiple=True,
    help=(
        "Filter, repeatable: material=olivine, category=mineral, quantity=…, geometry=…, "
        "dataset_id=…, wavelength_range_nm=400:2500. A bare word searches material."
    ),
)
@click.option("--limit", type=click.IntRange(min=1), help="Export at most this many spectra.")
@click.option(
    "--resample-to",
    help=(
        "Interpolate linearly onto a grid: START:STOP:STEP in nm, an ENVI .hdr sensor "
        "definition, or a text file of band centres in nm. Required when the selected "
        "spectra do not share one axis (ENVI, wide CSV)."
    ),
)
@click.option("--name", "basename", help="Output file stem (default: the collection id).")
def export_spectra(
    input_path: Path,
    outdir: Path,
    fmt: str,
    layout: str,
    ids: tuple[str, ...],
    search: tuple[str, ...],
    limit: int | None,
    resample_to: str | None,
    basename: str | None,
) -> None:
    """Export spectra of an OpenSpecLib Format v1 file to ENVI or CSV.

    Writes the data files plus NAME.README.md (licences and citations of the
    exported spectra) and NAME.collection.json (metadata for re-import with
    `openspeclib convert --metadata-json`) into OUTDIR.
    """
    from openspeclib.convert import ConversionError, export_collection

    _quiet_storage_logs()
    id_list = [i.strip() for value in ids for i in value.split(",") if i.strip()]
    try:
        result = export_collection(
            input_path,
            outdir,
            to=fmt,
            layout=layout,
            ids=id_list,
            search=search,
            limit=limit,
            resample_to=resample_to,
            basename=basename,
        )
    except ConversionError as exc:
        raise click.ClickException(str(exc)) from exc
    what = "ENVI spectral library" if fmt == "envi" else f"{layout} CSV"
    resampled = " (resampled)" if result.resampled else ""
    click.echo(f"Exported {result.num_spectra} spectra as {what}{resampled}:")
    for path in result.files:
        click.echo(f"  {path}")
    for warning in result.warnings:
        click.echo(f"Warning: {warning}", err=True)


if __name__ == "__main__":
    main()
