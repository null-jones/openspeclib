"""Combiner: builds an OpenSpecLib release directory from ingested collections.

``openspeclib combine`` writes a flat directory identical to the published
release layout:

* ``{collection_id}.parquet`` — one Format v1 collection file per source;
* ``openspeclib-index.parquet`` — the index over all of them (spec section 7),
  whose footer ``release`` is the release version;
* ``id-migration.csv`` — ``old_id,new_id`` for every spectrum whose id
  changed from the v0 layout (``{library}:{original_id}``, and
  ``ecosis:{dataset_id}:{spectrum_id}`` for EcoSIS);
* ``checksums.txt`` — ``sha256  filename`` for every other file.

Collection and dataset metadata (licences, citations, inclusion) come from
each collection's ``{collection}.collection.json`` written by ``ingest``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from openspeclib import storage
from openspeclib.models import LOCAL_ID_MAX_LENGTH, CollectionMetadata, SpectrumRecord

logger = logging.getLogger(__name__)

ID_MIGRATION_FILENAME = "id-migration.csv"
CHECKSUMS_FILENAME = "checksums.txt"


def legacy_id(record: SpectrumRecord) -> str:
    """Reproduce the v0 (OpenSpecLib 0.1) id of a record.

    v0 ids were ``{library}:{original_id}`` for every source except EcoSIS
    (``ecosis:{dataset_id}:{spectrum_id}``) and OSSL
    (``ossl:{layer_uuid}:{vnir|mir}``, VNIR being reflectance).

    Args:
        record: A v1 record.

    Returns:
        The id the same spectrum had in v0.
    """
    if record.collection == "ecosis" and record.dataset_id:
        return f"ecosis:{record.dataset_id}:{record.original_id}"
    if record.collection == "ossl":
        technique = "vnir" if record.measurement.quantity.value == "reflectance" else "mir"
        return f"ossl:{record.original_id}:{technique}"
    return f"{record.collection}:{record.original_id}"


def _row_digest(row: storage.SpectrumRow) -> str:
    """Hash a row's content, excluding its id, as a final collision tie-breaker.

    Args:
        row: A collection row.

    Returns:
        The SHA-256 hex digest of the row's metadata (as sorted JSON, without
        ``id``) and array bytes. Rows with equal digests are identical, so their
        relative order does not affect the output.
    """
    digest = hashlib.sha256()
    meta = {k: v for k, v in row.meta.items() if k != "id"}
    digest.update(json.dumps(meta, sort_keys=True, default=str).encode("utf-8"))
    for name in ("wavelengths", "values", "bandpass_fwhm", "values_stddev"):
        array = row.array(name)
        digest.update(name.encode("ascii"))
        if array is not None:
            digest.update(array.tobytes())
    return digest.hexdigest()


def resolve_id_collisions(rows: list[storage.SpectrumRow], order: list[str]) -> int:
    """Make spectrum ids unique by suffixing colliding ones with ``~2``, ``~3``, ….

    Sanitising upstream ids (see :func:`openspeclib.loaders.base.make_local_id`)
    can map two upstream ids to one local id. Within each group of rows
    sharing an id, a row whose original id *is* the local id (it needed no
    sanitising) ranks first; the rest are ranked by ``(original_id,
    dataset_id, order)``, and rows equal in all of these by a digest of their
    content (:func:`_row_digest`), never by input order. The first keeps the id
    and the others get the next free ``~N`` suffix (the local part is shortened
    so the id stays within 200 characters).
    The result depends only on the records, so rebuilding from the same raw
    data gives the same ids whatever order the records arrive in.

    Args:
        rows: The rows of one collection; ids are changed in place.
        order: A per-row tie-breaker (e.g. the v0 id), aligned with ``rows``.

    Returns:
        The number of ids changed.
    """
    groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[row.id].append(i)
    taken = set(groups)
    changed = 0
    for base in sorted(groups):
        members = groups[base]
        if len(members) == 1:
            continue
        _, base_local = base.split(":", 1)
        members.sort(
            key=lambda i: (
                # A row that needed no sanitising keeps the plain id.
                str(rows[i].meta["original_id"]) != base_local,
                str(rows[i].meta["original_id"]),
                str(rows[i].meta.get("dataset_id") or ""),
                order[i],
                _row_digest(rows[i]),
            )
        )
        prefix, local = base.split(":", 1)
        n = 2
        for i in members[1:]:
            while True:
                suffix = f"~{n}"
                candidate = f"{prefix}:{local[: LOCAL_ID_MAX_LENGTH - len(suffix)]}{suffix}"
                n += 1
                if candidate not in taken:
                    break
            taken.add(candidate)
            rows[i].meta["id"] = candidate
            changed += 1
    return changed


@dataclass
class CollectionBuild:
    """Result of building one collection file.

    Attributes:
        collection_id: The collection id.
        path: The written file.
        num_spectra: Rows written.
        collisions: Ids changed to resolve collisions.
        migrations: ``(old_id, new_id)`` for every spectrum whose id changed
            from v0.
    """

    collection_id: str
    path: Path
    num_spectra: int
    collisions: int
    migrations: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class LibraryBuild:
    """Result of building a release directory.

    Attributes:
        output_dir: The release directory.
        release: The release version recorded in the index.
        collections: One build result per collection.
        index_path: The index file.
        files: Every file written except ``checksums.txt``.
    """

    output_dir: Path
    release: str
    collections: list[CollectionBuild]
    index_path: Path
    files: list[Path]

    @property
    def total_spectra(self) -> int:
        """Return the number of spectra across all collections."""
        return sum(c.num_spectra for c in self.collections)


def build_collection(
    records: Iterable[SpectrumRecord],
    metadata: CollectionMetadata,
    output_dir: Path,
    *,
    created: str | None = None,
) -> CollectionBuild:
    """Write one collection file, resolving id collisions.

    Args:
        records: The collection's records.
        metadata: Its footer collection and datasets.
        output_dir: Directory to write ``{collection_id}.parquet`` into.
        created: RFC 3339 creation timestamp for the footer.

    Returns:
        The build result.

    Raises:
        ValueError: If a record belongs to another collection, or the
            collection has no records.
    """
    collection = metadata.collection
    interner = storage.AxisInterner()
    rows: list[storage.SpectrumRow] = []
    old_ids: list[str] = []
    for record in records:
        if record.collection != collection.id:
            raise ValueError(
                f"record {record.id} belongs to {record.collection!r}, not {collection.id!r}"
            )
        rows.append(storage.record_to_row(record, interner))
        old_ids.append(legacy_id(record))
    collisions = resolve_id_collisions(rows, old_ids)
    if collisions:
        logger.info("%s: resolved %d id collisions", collection.id, collisions)
    migrations = sorted((old, row.id) for old, row in zip(old_ids, rows) if old != row.id)
    path = output_dir / f"{collection.id}.parquet"
    result = storage.write_rows(rows, path, collection, metadata.datasets, created=created)
    return CollectionBuild(
        collection_id=collection.id,
        path=path,
        num_spectra=result.num_spectra,
        collisions=collisions,
        migrations=migrations,
    )


def _write_migrations(path: Path, builds: Iterable[CollectionBuild]) -> None:
    """Write ``id-migration.csv``.

    Args:
        path: Destination file.
        builds: Collection builds whose migrations to list.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["old_id", "new_id"])
        for build in builds:
            writer.writerows(build.migrations)


def _write_checksums(output_dir: Path, files: Iterable[Path]) -> Path:
    """Write ``checksums.txt`` (``sha256  filename``, sorted by name).

    Args:
        output_dir: The release directory.
        files: The files to list.

    Returns:
        The checksums file.
    """
    path = output_dir / CHECKSUMS_FILENAME
    lines = [f"{storage.sha256_file(f)}  {f.name}" for f in sorted(files, key=lambda p: p.name)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_library(
    sources: Mapping[str, tuple[Iterable[SpectrumRecord], CollectionMetadata]],
    output_dir: Path,
    *,
    release: str | None = None,
    created: str | None = None,
) -> LibraryBuild:
    """Build a flat release directory from ingested collections.

    Args:
        sources: Collection id -> (records, collection metadata).
        output_dir: The release directory (created if needed).
        release: Release version for the index footer; defaults to the
            package version.
        created: RFC 3339 timestamp shared by every footer; defaults to now.

    Returns:
        The build result.
    """
    from openspeclib import __version__

    release = release or __version__
    created = created or storage.utc_timestamp()
    output_dir.mkdir(parents=True, exist_ok=True)

    builds: list[CollectionBuild] = []
    for collection_id in sorted(sources):
        records, metadata = sources[collection_id]
        logger.info("Building collection %s", collection_id)
        builds.append(build_collection(records, metadata, output_dir, created=created))

    index_path = output_dir / storage.INDEX_FILENAME
    storage.write_index(
        [b.path for b in builds], index_path, release=release, created=created, with_sha256=True
    )
    migration_path = output_dir / ID_MIGRATION_FILENAME
    _write_migrations(migration_path, builds)

    files = [b.path for b in builds] + [index_path, migration_path]
    _write_checksums(output_dir, files)
    written = {f.name for f in files} | {CHECKSUMS_FILENAME}
    stale = sorted(p.name for p in output_dir.iterdir() if p.name not in written)
    if stale:
        logger.warning(
            "%s holds files this build did not write (not in checksums.txt): %s",
            output_dir,
            ", ".join(stale),
        )
    return LibraryBuild(
        output_dir=output_dir,
        release=release,
        collections=builds,
        index_path=index_path,
        files=files,
    )
