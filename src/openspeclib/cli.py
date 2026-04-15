"""Command-line interface for OpenSpecLib."""

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import click

from openspeclib import __version__
from openspeclib.loaders.base import BaseLoader

logger = logging.getLogger("openspeclib")

LOADERS = {
    "usgs": "openspeclib.loaders.usgs:UsgsLoader",
    "ecostress": "openspeclib.loaders.ecostress:EcostressLoader",
    "relab": "openspeclib.loaders.relab:RelabLoader",
    "asu_tes": "openspeclib.loaders.asu_tes:AsuTesLoader",
    "bishop": "openspeclib.loaders.bishop:BishopLoader",
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
    cls = getattr(module, class_name)
    return cls()


@click.group()
@click.version_option(__version__)
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging.")
def main(verbose: bool) -> None:
    """OpenSpecLib — amalgamated spectral library toolkit."""
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
    click.echo(f"Downloading {source} to {target}...")
    result_path = loader.download(target)
    click.echo(f"Downloaded to {result_path}")


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
    """Ingest a source library into the standard format."""
    loader = _get_loader(source)
    output.mkdir(parents=True, exist_ok=True)

    count = 0
    records_file = output / f"{source}.jsonl"

    click.echo(f"Ingesting {source} from {input_dir}...")
    with open(records_file, "w", encoding="utf-8") as f:
        for record in loader.load(input_dir):
            f.write(record.model_dump_json() + "\n")
            count += 1

    click.echo(f"Ingested {count} spectra to {records_file}")


@main.command()
@click.option(
    "--input",
    "input_dir",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Directory containing processed .jsonl files.",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory for the master library.",
)
@click.option(
    "--chunk-size",
    default=5000,
    type=int,
    help="Maximum spectra per chunk file.",
)
def combine(input_dir: Path, output: Path, chunk_size: int) -> None:
    """Combine processed sources into a master library."""
    from openspeclib.combine import build_library
    from openspeclib.models import SourceInfo, SpectrumRecord

    # Find all .jsonl files in the input directory
    jsonl_files = sorted(input_dir.glob("*.jsonl"))
    if not jsonl_files:
        click.echo("No .jsonl files found in input directory.", err=True)
        sys.exit(1)

    def _stream_jsonl(path: Path) -> Iterator[SpectrumRecord]:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield SpectrumRecord.model_validate_json(line)

    record_streams = {}
    source_metadata = {}

    for jsonl_path in jsonl_files:
        source_name = jsonl_path.stem
        record_streams[source_name] = _stream_jsonl(jsonl_path)

        # Build placeholder source metadata (will be updated with actual counts)
        source_metadata[source_name] = SourceInfo(
            name=source_name,
            version="",
            url="",
            license="",
            citation="",
            spectrum_count=0,
        )

    click.echo(f"Combining {len(record_streams)} source(s) into {output}...")
    catalog = build_library(record_streams, source_metadata, output, chunk_size)
    click.echo(
        f"Built library: {catalog.statistics.total_spectra} spectra, "
        f"{len(catalog.sources)} source(s)"
    )


@main.command()
@click.argument(
    "library_dir",
    type=click.Path(exists=True, path_type=Path),
)
def validate(library_dir: Path) -> None:
    """Validate a built library directory."""
    from openspeclib.validate import validate_library

    click.echo(f"Validating library at {library_dir}...")
    result = validate_library(library_dir)
    click.echo(result.summary())

    if not result.is_valid:
        sys.exit(1)


if __name__ == "__main__":
    main()
