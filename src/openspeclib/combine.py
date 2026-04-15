"""Combiner: merges processed spectral sources into a master library.

Orchestrates the full pipeline of loading records from each source,
partitioning them into chunk files, and building the catalog index.
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

DEFAULT_CHUNK_SIZE = 5000


def _write_chunk(
    records: list[SpectrumRecord],
    output_path: Path,
    source_name: str,
    category_label: str,
) -> None:
    """Write a list of spectrum records to a Parquet library chunk file.

    Args:
        records: Spectrum records to include in the chunk.
        output_path: Destination Parquet file path.
        source_name: Source library identifier (stored in footer metadata).
        category_label: Material category or chunk label (stored in footer metadata).
    """
    storage.write_chunk(records, output_path, source=source_name, category=category_label)


def build_library(
    record_streams: dict[str, Iterator[SpectrumRecord]],
    source_metadata: dict[str, SourceInfo],
    output_dir: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> CatalogFile:
    """Build the master library from multiple record streams.

    Args:
        record_streams: Mapping of source name to an iterator of SpectrumRecord.
        source_metadata: Mapping of source name to SourceInfo.
        output_dir: Root directory for the output library.
        chunk_size: Maximum spectra per chunk file (for large sources).

    Returns:
        The completed CatalogFile object.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    spectra_dir = output_dir / "spectra"

    catalog_entries: list[CatalogRecord] = []
    source_counts: dict[str, int] = defaultdict(int)
    category_counts: dict[str, int] = defaultdict(int)

    for source_name, records in record_streams.items():
        logger.info("Processing source: %s", source_name)
        source_dir = spectra_dir / source_name
        source_dir.mkdir(parents=True, exist_ok=True)

        # Buffer records by category for chunking
        buffers: dict[str, list[SpectrumRecord]] = defaultdict(list)
        chunk_counters: dict[str, int] = defaultdict(int)

        for record in records:
            cat = record.material.category.value
            buffers[cat].append(record)
            source_counts[source_name] += 1
            category_counts[cat] += 1

            # Flush buffer if it reaches chunk_size
            if len(buffers[cat]) >= chunk_size:
                chunk_idx = chunk_counters[cat]
                chunk_label = f"{cat}_chunk_{chunk_idx:03d}"
                chunk_path = source_dir / f"{chunk_label}.parquet"
                rel_path = f"spectra/{source_name}/{chunk_label}.parquet"

                _write_chunk(buffers[cat], chunk_path, source_name, chunk_label)

                for rec in buffers[cat]:
                    catalog_entries.append(CatalogRecord.from_spectrum(rec, chunk_file=rel_path))

                buffers[cat] = []
                chunk_counters[cat] += 1

        # Flush remaining buffers
        for cat, remaining in buffers.items():
            if not remaining:
                continue

            if chunk_counters[cat] > 0:
                # Continuation of a chunked category
                chunk_idx = chunk_counters[cat]
                chunk_label = f"{cat}_chunk_{chunk_idx:03d}"
            else:
                # Small enough for a single file
                chunk_label = cat

            chunk_path = source_dir / f"{chunk_label}.parquet"
            rel_path = f"spectra/{source_name}/{chunk_label}.parquet"

            _write_chunk(remaining, chunk_path, source_name, chunk_label)

            for rec in remaining:
                catalog_entries.append(CatalogRecord.from_spectrum(rec, chunk_file=rel_path))

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
