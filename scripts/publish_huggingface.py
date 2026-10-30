#!/usr/bin/env python3
"""Mirror a built OpenSpecLib release directory to a Hugging Face dataset repo.

Every file in the release directory is uploaded under a versioned prefix
(``v{version}/``) in one commit, and that commit is tagged ``v{version}``, so
a published URL never changes its content. The repo-root ``README.md`` (the
dataset card) is regenerated from the release's footer metadata and describes
the newest release.

The default is a dry run: the script verifies ``checksums.txt``, then prints
every file it would upload, with its size and SHA-256, and the dataset card.
Nothing is sent anywhere. To upload, pass ``--yes`` and put a token with write
access in the ``HF_TOKEN`` environment variable. The script refuses to upload
if the version prefix or tag already exists in the repo.

The dataset repo must already exist; this script does not create repos.

Usage:
    python scripts/publish_huggingface.py build/release --repo-id OWNER/NAME
    HF_TOKEN=... python scripts/publish_huggingface.py build/release \
        --repo-id OWNER/NAME --yes

Footer metadata is read with ``pyarrow.parquet.read_metadata``, which reads
only the Parquet footer, never the spectra.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import pyarrow.parquet as pq
from huggingface_hub import CommitOperationAdd, HfApi

COLLECTION_FOOTER_KEY = b"openspeclib"
INDEX_FOOTER_KEY = b"openspeclib_index"
INDEX_FILENAME = "openspeclib-index.parquet"
CHECKSUMS_FILENAME = "checksums.txt"
CARD_PATH_IN_REPO = "README.md"
TOKEN_ENV_VAR = "HF_TOKEN"
REPO_TYPE = "dataset"
GITHUB_REPO_URL = "https://github.com/null-jones/openspeclib"

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
SIMPLE_SPDX_RE = re.compile(r"^[A-Za-z0-9.+-]+$")


class PublishError(Exception):
    """Raised when the release directory or the target repo is not fit to publish."""


@dataclass(frozen=True)
class PlannedFile:
    local_path: Path
    path_in_repo: str
    size: int
    sha256: str


@dataclass
class Plan:
    repo_id: str
    revision: str
    version: str
    files: list[PlannedFile]
    collections: list[dict[str, Any]]
    card: str = field(default="")

    @property
    def prefix(self) -> str:
        return f"v{self.version}/"

    @property
    def tag(self) -> str:
        return f"v{self.version}"


# ---------------------------------------------------------------------------
# Reading the release directory
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_footer(path: Path, key: bytes) -> tuple[dict[str, Any] | None, int]:
    """Return the JSON footer under ``key`` (or None) and the file's row count."""
    meta = pq.read_metadata(path)
    kv = meta.metadata or {}
    raw = kv.get(key)
    if raw is None:
        return None, meta.num_rows
    try:
        return json.loads(raw), meta.num_rows
    except json.JSONDecodeError as exc:
        raise PublishError(f"{path.name}: footer key {key.decode()!r} is not JSON: {exc}") from exc


def load_collections(release_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Describe the release's collections from its footers.

    Uses the index file when present (it carries every collection footer);
    otherwise reads each collection file's ``openspeclib`` footer.

    Returns:
        A list of ``{"file", "num_spectra", "metadata"}`` entries, and the
        release version recorded in the index footer, if any.
    """
    index_path = release_dir / INDEX_FILENAME
    if index_path.is_file():
        index, _ = read_footer(index_path, INDEX_FOOTER_KEY)
        if index is None:
            raise PublishError(f"{INDEX_FILENAME} has no {INDEX_FOOTER_KEY.decode()!r} footer key")
        return list(index.get("collections", [])), index.get("release")

    collections = []
    for path in sorted(release_dir.glob("*.parquet")):
        footer, num_rows = read_footer(path, COLLECTION_FOOTER_KEY)
        if footer is None:
            continue
        collections.append({"file": path.name, "num_spectra": num_rows, "metadata": footer})
    return collections, None


def parse_checksums(path: Path) -> dict[str, str]:
    """Parse ``sha256sum`` output into ``{file name: hex digest}``."""
    sums: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise PublishError(f"{CHECKSUMS_FILENAME} line {lineno} is malformed: {line!r}")
        sums[parts[1].strip().lstrip("*")] = parts[0]
    return sums


def collect_files(release_dir: Path, prefix: str) -> list[PlannedFile]:
    """List the release's files and verify them against ``checksums.txt``."""
    checksums_path = release_dir / CHECKSUMS_FILENAME
    if not checksums_path.is_file():
        raise PublishError(f"{release_dir} has no {CHECKSUMS_FILENAME}; build the release first")
    expected = parse_checksums(checksums_path)

    files = []
    problems = []
    for path in sorted(p for p in release_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(release_dir).as_posix()
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        digest = sha256_of(path)
        if rel != CHECKSUMS_FILENAME:
            if rel not in expected:
                problems.append(f"{rel}: not listed in {CHECKSUMS_FILENAME}")
            elif expected[rel] != digest:
                problems.append(f"{rel}: SHA-256 does not match {CHECKSUMS_FILENAME}")
        files.append(PlannedFile(path, prefix + rel, path.stat().st_size, digest))

    present = {f.path_in_repo[len(prefix) :] for f in files}
    missing = [name for name in expected if name not in present]
    problems.extend(f"{name}: listed in {CHECKSUMS_FILENAME} but missing" for name in missing)
    if problems:
        raise PublishError("checksum verification failed:\n  " + "\n  ".join(problems))
    return files


# ---------------------------------------------------------------------------
# Dataset card
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _hf_license(collections: list[dict[str, Any]]) -> str:
    """Map the collections' licences to one Hub ``license`` id, or ``other``."""
    licences = {str(c["metadata"]["collection"].get("license", "")) for c in collections}
    if len(licences) == 1:
        (only,) = licences
        if SIMPLE_SPDX_RE.match(only) and only != "NOASSERTION":
            return only.lower()
    return "other"


def _size_category(total: int) -> str:
    bounds = [(1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K")]
    bounds += [(1_000_000, "100K<n<1M"), (10_000_000, "1M<n<10M")]
    for limit, label in bounds:
        if total < limit:
            return label
    return "10M<n<100M"


def render_card(plan: Plan) -> str:
    """Write a short dataset card from the release's footer metadata."""
    version, repo_id, prefix = plan.version, plan.repo_id, plan.prefix
    total = sum(int(c.get("num_spectra", 0)) for c in plan.collections)
    base = f"https://huggingface.co/datasets/{repo_id}/resolve/{plan.tag}/{prefix}"
    parquet_files = {f.path_in_repo[len(prefix) :] for f in plan.files}

    configs = []
    for c in plan.collections:
        configs += [
            f"- config_name: {c['metadata']['collection']['id']}",
            "  data_files:",
            "  - split: train",
            f"    path: {prefix}{c['file']}",
        ]
    if INDEX_FILENAME in parquet_files:
        configs += [
            "- config_name: index",
            "  data_files:",
            "  - split: train",
            f"    path: {prefix}{INDEX_FILENAME}",
        ]

    lines = [
        "---",
        f"license: {_hf_license(plan.collections)}",
        f"pretty_name: OpenSpecLib {version}",
        "size_categories:",
        f"- {_size_category(total)}",
        "tags:",
        "- spectroscopy",
        "- spectral-library",
        "- remote-sensing",
        "- hyperspectral",
        "- openspeclib",
        "configs:",
        *configs,
        "---",
        "",
        f"# OpenSpecLib {version}",
        "",
        f"Spectral libraries in the [OpenSpecLib Format]({GITHUB_REPO_URL}/blob/{plan.tag}/"
        "docs/spec/format-v1.md): one self-describing Parquet file per collection. "
        f"This is a mirror of the [GitHub release]({GITHUB_REPO_URL}/releases/tag/{plan.tag}).",
        "",
        f"Every release lives under its own folder (`{prefix}`) and is tagged `{plan.tag}`, so "
        "these URLs never change their content:",
        "",
        "```",
        f"{base}<file>",
        "```",
        "",
        "## Collections",
        "",
        "| Collection | Title | Source version | Spectra | Licence |",
        "|---|---|---|---|---|",
    ]
    for c in plan.collections:
        col = c["metadata"]["collection"]
        lines.append(
            f"| `{_cell(col.get('id'))}` | {_cell(col.get('title'))} | {_cell(col.get('version'))}"
            f" | {int(c.get('num_spectra', 0)):,} | {_cell(col.get('license'))} |"
        )
    lines += [
        "",
        "Each collection keeps the licence of its source. The per-row `license` column is "
        "authoritative: in an aggregated collection (licence `NOASSERTION`) each contributing "
        "dataset has its own licence and citation, listed in the file's `openspeclib` footer.",
        "",
        "## Citations",
        "",
    ]
    for c in plan.collections:
        col = c["metadata"]["collection"]
        lines.append(f"- **{col.get('title', col.get('id'))}**: {col.get('citation', '—')}")
    lines += [
        "",
        "## Files",
        "",
        "| File | Bytes | SHA-256 |",
        "|---|---|---|",
    ]
    for f in plan.files:
        lines.append(f"| `{f.path_in_repo}` | {f.size:,} | `{f.sha256}` |")
    lines += [
        "",
        "## Reading",
        "",
        "```sql",
        "INSTALL httpfs; LOAD httpfs;",
        'SELECT id, material_name, quantity, geometry, wavelength_unit, wavelengths, "values"',
        f"FROM '{base}{plan.collections[0]['file'] if plan.collections else '<file>'}'",
        "LIMIT 10;",
        "```",
        "",
        f"Specification, tools and conformance checker: <{GITHUB_REPO_URL}>.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planning, printing and uploading
# ---------------------------------------------------------------------------


def build_plan(release_dir: Path, repo_id: str, version: str | None, revision: str) -> Plan:
    """Check the release directory and work out exactly what would be uploaded."""
    if not release_dir.is_dir():
        raise PublishError(f"{release_dir} is not a directory")
    if not REPO_ID_RE.match(repo_id):
        raise PublishError(f"--repo-id must look like OWNER/NAME, got {repo_id!r}")

    collections, index_release = load_collections(release_dir)
    if not collections:
        raise PublishError(f"{release_dir} contains no OpenSpecLib collection files")

    if version is not None:
        version = version.removeprefix("v")
    if version and index_release and version != index_release.removeprefix("v"):
        raise PublishError(
            f"--version {version} disagrees with the index footer release {index_release}"
        )
    version = version or (index_release.removeprefix("v") if index_release else None)
    if not version:
        raise PublishError("cannot tell the release version; pass --version")
    if not VERSION_RE.match(version):
        raise PublishError(f"version {version!r} is not semver (e.g. 0.2.0)")

    plan = Plan(repo_id, revision, version, [], collections)
    plan.files = collect_files(release_dir, plan.prefix)
    plan.card = render_card(plan)
    return plan


def print_plan(plan: Plan, out: TextIO) -> None:
    """Print every file the plan would upload, then the dataset card."""
    total = sum(f.size for f in plan.files)
    print(f"Repository: datasets/{plan.repo_id} (revision {plan.revision})", file=out)
    print(f"Release:    {plan.version} -> folder {plan.prefix}, tag {plan.tag}", file=out)
    print(f"Files:      {len(plan.files)} ({total:,} bytes), checksums verified", file=out)
    print("", file=out)
    width = max(len(f.path_in_repo) for f in plan.files) if plan.files else 0
    for f in plan.files:
        print(f"  {f.path_in_repo:<{width}}  {f.size:>14,}  sha256:{f.sha256}", file=out)
    card_bytes = len(plan.card.encode())
    print(f"  {CARD_PATH_IN_REPO:<{width}}  {card_bytes:>14,}  (generated dataset card)", file=out)
    print("", file=out)
    print(f"----- {CARD_PATH_IN_REPO} -----", file=out)
    print(plan.card, file=out)
    print(f"----- end {CARD_PATH_IN_REPO} -----", file=out)


def upload(plan: Plan, api: HfApi, out: TextIO) -> str:
    """Upload the plan in one commit and tag it.

    Returns:
        The commit id.

    Raises:
        PublishError: If the version folder or tag already exists.
    """
    existing = api.list_repo_files(plan.repo_id, repo_type=REPO_TYPE, revision=plan.revision)
    clash = [p for p in existing if p.startswith(plan.prefix)]
    if clash:
        raise PublishError(
            f"{plan.prefix} already exists in {plan.repo_id} ({len(clash)} files); "
            "published releases are immutable, so bump the version instead"
        )
    refs = api.list_repo_refs(plan.repo_id, repo_type=REPO_TYPE)
    if any(t.name == plan.tag for t in refs.tags):
        raise PublishError(f"tag {plan.tag} already exists in {plan.repo_id}")

    operations = [CommitOperationAdd(f.path_in_repo, str(f.local_path)) for f in plan.files]
    operations.append(CommitOperationAdd(CARD_PATH_IN_REPO, plan.card.encode()))
    info = api.create_commit(
        plan.repo_id,
        operations,
        commit_message=f"Add OpenSpecLib {plan.version}",
        repo_type=REPO_TYPE,
        revision=plan.revision,
    )
    commit_id = str(getattr(info, "oid", "") or "")
    api.create_tag(
        plan.repo_id,
        tag=plan.tag,
        tag_message=f"OpenSpecLib {plan.version}",
        revision=commit_id or plan.revision,
        repo_type=REPO_TYPE,
    )
    print(f"Uploaded {len(plan.files)} files in commit {commit_id}, tagged {plan.tag}.", file=out)
    print(
        f"URLs: https://huggingface.co/datasets/{plan.repo_id}/resolve/{plan.tag}/{plan.prefix}"
        "<file>",
        file=out,
    )
    return commit_id


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mirror a built OpenSpecLib release directory to a Hugging Face dataset repo, "
            "under an immutable v{version}/ folder and tag. Dry run by default."
        ),
        epilog=(
            f"Uploading needs --yes and a write token in ${TOKEN_ENV_VAR}. The dataset repo "
            "must already exist. See docs/publishing.md."
        ),
    )
    parser.add_argument("release_dir", type=Path, help="Directory holding the release files")
    parser.add_argument(
        "--repo-id", required=True, help="Target dataset repo, OWNER/NAME (no default)"
    )
    parser.add_argument(
        "--version",
        help="Release version, e.g. 0.2.0. Defaults to the index footer's 'release' field.",
    )
    parser.add_argument("--revision", default="main", help="Branch to commit to (default: main)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"Actually upload. Without it nothing is sent. Reads the token from ${TOKEN_ENV_VAR}.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(args.release_dir, args.repo_id, args.version, args.revision)
        print_plan(plan, out)
        if not args.yes:
            print("", file=out)
            print("Dry run: nothing was uploaded. Re-run with --yes to upload.", file=out)
            return 0
        token = os.environ.get(TOKEN_ENV_VAR, "").strip()
        if not token:
            raise PublishError(f"--yes needs a Hugging Face write token in ${TOKEN_ENV_VAR}")
        upload(plan, HfApi(token=token), out)
        return 0
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
