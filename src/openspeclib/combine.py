"""Combiner: merges processed spectral sources into a master library.

Orchestrates the full pipeline of loading records from each source,
writing one Parquet file per source (with row-group-based partial reads),
and building the catalog index. Also generates ``licenses.json`` alongside
the catalog for easy consumption by web applications.
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
    LicenseEntry,
    LicensesFile,
    SourceInfo,
    SpectrumRecord,
)

logger = logging.getLogger(__name__)

_LICENSES_NOTICE = (
    "Licensing terms differ between source spectral libraries. "
    "Users must review and comply with the license for each source library "
    "from which they use data. The 'license' field for each source below "
    "summarises the terms; consult the source URL for full details."
)

# Extra license/citation metadata not carried on the per-spectrum Source model.
# Keyed by SourceLibrary enum value (e.g. "usgs_splib07").
_SOURCE_EXTRA: dict[str, dict[str, str | None]] = {
    "usgs_splib07": {
        "license_url": None,
        "citation_doi": "10.3133/ds1035",
    },
    "ecostress": {
        "license_url": None,
        "citation_doi": "10.1016/j.rse.2019.05.015",
    },
    "relab": {
        "license_url": None,
        "citation_doi": None,
    },
    "asu_tes": {
        "license_url": None,
        "citation_doi": "10.1029/1999JE001138",
    },
    "bishop": {
        "license_url": None,
        "citation_doi": "10.1180/claymin.2008.043.1.03",
    },
    "ecosis": {
        "license_url": "https://ecosis.org",
        "citation_doi": None,
    },
}


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

    # Track the first record per source so we can enrich placeholder SourceInfo.
    first_records: dict[str, SpectrumRecord] = {}

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
                if name not in first_records:
                    first_records[name] = record
                catalog_entries.append(
                    CatalogRecord.from_spectrum(record, chunk_file=chunk_rel_path)
                )
                source_counts[name] += 1
                category_counts[record.material.category.value] += 1
                yield record

        storage.write_source(_tee(records, source_name), chunk_path, source=source_name)

    # Enrich source metadata from the first record of each source and update counts.
    for name, info in source_metadata.items():
        updates: dict[str, object] = {"spectrum_count": source_counts.get(name, 0)}
        first = first_records.get(name)
        if first is not None:
            src = first.source
            # Fill in empty placeholder fields from the actual record data.
            if not info.name:
                updates["name"] = src.library.value
            if not info.version:
                updates["version"] = src.library_version
            if not info.url:
                updates["url"] = src.url
            if not info.license:
                updates["license"] = src.license
            if not info.citation:
                updates["citation"] = src.citation
            # Populate extra fields from the registry if not already set.
            extras = _SOURCE_EXTRA.get(name, {})
            if info.license_url is None and extras.get("license_url"):
                updates["license_url"] = extras["license_url"]
            if info.citation_doi is None and extras.get("citation_doi"):
                updates["citation_doi"] = extras["citation_doi"]
        source_metadata[name] = info.model_copy(update=updates)

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

    # Write licenses.json
    now = datetime.now(timezone.utc)
    license_entries: dict[str, LicenseEntry] = {}
    for name, info in source_metadata.items():
        extras = _SOURCE_EXTRA.get(name, {})
        license_entries[name] = LicenseEntry(
            name=info.name,
            version=info.version,
            url=info.url,
            license=info.license,
            license_url=info.license_url or extras.get("license_url"),
            citation=info.citation,
            citation_doi=info.citation_doi or extras.get("citation_doi"),
        )

    licenses_file = LicensesFile(
        openspeclib_version=__version__,
        generated_at=now,
        notice=_LICENSES_NOTICE,
        sources=license_entries,
    )
    licenses_path = output_dir / "licenses.json"
    licenses_path.write_text(
        licenses_file.model_dump_json(indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote licenses.json to %s", licenses_path)

    return catalog
