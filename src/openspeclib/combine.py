"""Combiner: merges processed spectral sources into a master library.

Orchestrates the full pipeline of loading records from each source,
writing one Parquet file per source (with row-group-based partial reads),
and building the catalog index.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from openspeclib import __version__, storage
from openspeclib.models import (
    CatalogFile,
    CatalogRecord,
    CatalogStatistics,
    SourceInfo,
    SpectrumRecord,
)

logger = logging.getLogger(__name__)


def build_library(
    record_streams: dict[str, Iterator[SpectrumRecord]],
    source_metadata: dict[str, SourceInfo],
    output_dir: Path,
) -> CatalogFile:
    """Build the master library from multiple record streams.

    Each source produces a single Parquet file at
    ``spectra/<source>.parquet``. Row groups inside the file give
    downstream consumers partial-read granularity via HTTP Range requests.

    Args:
        record_streams: Mapping of source name to an iterator of SpectrumRecord.
        source_metadata: Mapping of source name to SourceInfo.
        output_dir: Root directory for the output library.

    Returns:
        The completed CatalogFile object.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    spectra_dir = output_dir / "spectra"
    spectra_dir.mkdir(parents=True, exist_ok=True)

    catalog_entries: list[CatalogRecord] = []
    source_counts: dict[str, int] = defaultdict(int)
    category_counts: dict[str, int] = defaultdict(int)

    for source_name, records in record_streams.items():
        logger.info("Processing source: %s", source_name)
        chunk_rel_path = f"spectra/{source_name}.parquet"
        chunk_path = output_dir / chunk_rel_path

        def _tee(stream: Iterator[SpectrumRecord], name: str) -> Iterator[SpectrumRecord]:
            """Forward records to the Parquet writer while also building catalog entries.

            Args:
                stream: Upstream iterator of ``SpectrumRecord``.
                name: Source identifier used to key counters.

            Yields:
                Each record, unchanged.
            """
            for record in stream:
                catalog_entries.append(
                    CatalogRecord.from_spectrum(record, chunk_file=chunk_rel_path)
                )
                source_counts[name] += 1
                category_counts[record.material.category.value] += 1
                yield record

        storage.write_source(_tee(records, source_name), chunk_path, source=source_name)

    # Update source metadata with actual counts
    for name, info in source_metadata.items():
        source_metadata[name] = info.model_copy(
            update={"spectrum_count": source_counts.get(name, 0)}
        )

    # Build catalog
    catalog = CatalogFile(
        openspeclib_version=__version__,
        generated_at=datetime.now(timezone.utc),
        sources=source_metadata,
        statistics=CatalogStatistics(
            total_spectra=sum(source_counts.values()),
            categories=dict(category_counts),
        ),
        spectra=catalog_entries,
    )

    # Write catalog
    catalog_path = output_dir / "catalog.json"
    catalog_path.write_text(
        catalog.model_dump_json(indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Wrote catalog with %d entries to %s",
        len(catalog_entries),
        catalog_path,
    )

    # Write VERSION file
    version_path = output_dir / "VERSION"
    version_path.write_text(__version__ + "\n", encoding="utf-8")

    return catalog
