"""Tests for scripts/publish_huggingface.py with the Hugging Face client mocked."""

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_huggingface.py"


@pytest.fixture(scope="module")
def publish():
    spec = importlib.util.spec_from_file_location("publish_huggingface", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _footer(collection_id: str, license_id: str) -> dict:
    return {
        "format_version": "1.0.0",
        "collection": {
            "id": collection_id,
            "title": f"Test collection {collection_id}",
            "version": "1",
            "license": license_id,
            "citation": f"Citation for {collection_id}.",
            "inclusion": "complete",
        },
        "datasets": {},
        "extensions": [],
        "created": "2026-10-29T12:00:00Z",
        "generator": "openspeclib 0.2.0",
    }


def make_release(root: Path, with_index: bool = True) -> Path:
    """Write a tiny release: two collection files, optional index, checksums."""
    root.mkdir(parents=True, exist_ok=True)
    footers = {"alpha": _footer("alpha", "CC0-1.0"), "beta": _footer("beta", "CC-BY-4.0")}
    entries = []
    for cid, footer in footers.items():
        table = pa.table({"id": [f"{cid}:a", f"{cid}:b"]})
        table = table.replace_schema_metadata({"openspeclib": json.dumps(footer)})
        pq.write_table(table, root / f"{cid}.parquet")
        entries.append({"file": f"{cid}.parquet", "num_spectra": 2, "metadata": footer})
    if with_index:
        index_footer = {
            "format_version": "1.0.0",
            "release": "0.2.0",
            "created": "2026-10-29T12:00:00Z",
            "generator": "openspeclib 0.2.0",
            "collections": entries,
        }
        table = pa.table({"id": ["alpha:a", "alpha:b", "beta:a", "beta:b"]})
        table = table.replace_schema_metadata({"openspeclib_index": json.dumps(index_footer)})
        pq.write_table(table, root / "openspeclib-index.parquet")
    lines = []
    for path in sorted(root.glob("*.parquet")):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (root / "checksums.txt").write_text("\n".join(lines) + "\n")
    return root


def test_dry_run_prints_plan_and_never_touches_hub(publish, tmp_path, monkeypatch):
    release = make_release(tmp_path / "release")
    hf_api = MagicMock()
    monkeypatch.setattr(publish, "HfApi", hf_api)
    monkeypatch.setenv("HF_TOKEN", "should-not-be-used")
    out = io.StringIO()

    code = publish.main([str(release), "--repo-id", "someone/openspeclib"], out=out)

    text = out.getvalue()
    assert code == 0
    hf_api.assert_not_called()
    for name in ["alpha.parquet", "beta.parquet", "openspeclib-index.parquet", "checksums.txt"]:
        assert f"v0.2.0/{name}" in text
    assert "Dry run: nothing was uploaded" in text
    assert "license: other" in text  # two different licences
    assert "| `alpha` | Test collection alpha |" in text
    assert "Citation for beta." in text


def test_version_needed_without_index(publish, tmp_path, capsys):
    release = make_release(tmp_path / "release", with_index=False)
    assert publish.main([str(release), "--repo-id", "a/b"], out=io.StringIO()) == 1
    assert "pass --version" in capsys.readouterr().err
    out = io.StringIO()
    assert publish.main([str(release), "--repo-id", "a/b", "--version", "0.2.0"], out=out) == 0
    assert "v0.2.0/alpha.parquet" in out.getvalue()


def test_checksum_mismatch_is_refused(publish, tmp_path, capsys):
    release = make_release(tmp_path / "release")
    (release / "alpha.parquet").write_bytes(b"tampered")
    assert publish.main([str(release), "--repo-id", "a/b"], out=io.StringIO()) == 1
    assert "alpha.parquet: SHA-256 does not match" in capsys.readouterr().err


def test_yes_requires_token(publish, tmp_path, monkeypatch, capsys):
    release = make_release(tmp_path / "release")
    hf_api = MagicMock()
    monkeypatch.setattr(publish, "HfApi", hf_api)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert publish.main([str(release), "--repo-id", "a/b", "--yes"], out=io.StringIO()) == 1
    assert "HF_TOKEN" in capsys.readouterr().err
    hf_api.assert_not_called()


def test_yes_uploads_one_commit_and_tags(publish, tmp_path, monkeypatch):
    release = make_release(tmp_path / "release")
    api = MagicMock()
    api.list_repo_files.return_value = ["README.md", "v0.1.0/old.parquet"]
    api.list_repo_refs.return_value = SimpleNamespace(tags=[SimpleNamespace(name="v0.1.0")])
    api.create_commit.return_value = SimpleNamespace(oid="abc123")
    hf_api = MagicMock(return_value=api)
    monkeypatch.setattr(publish, "HfApi", hf_api)
    monkeypatch.setenv("HF_TOKEN", "test-token")

    code = publish.main([str(release), "--repo-id", "a/b", "--yes"], out=io.StringIO())

    assert code == 0
    hf_api.assert_called_once_with(token="test-token")
    args, kwargs = api.create_commit.call_args
    assert args[0] == "a/b"
    assert kwargs["repo_type"] == "dataset"
    paths = sorted(op.path_in_repo for op in args[1])
    assert paths == [
        "README.md",
        "v0.2.0/alpha.parquet",
        "v0.2.0/beta.parquet",
        "v0.2.0/checksums.txt",
        "v0.2.0/openspeclib-index.parquet",
    ]
    api.create_tag.assert_called_once()
    assert api.create_tag.call_args.kwargs["tag"] == "v0.2.0"
    assert api.create_tag.call_args.kwargs["revision"] == "abc123"


def test_existing_version_is_never_overwritten(publish, tmp_path, monkeypatch, capsys):
    release = make_release(tmp_path / "release")
    api = MagicMock()
    api.list_repo_files.return_value = ["v0.2.0/alpha.parquet"]
    monkeypatch.setattr(publish, "HfApi", MagicMock(return_value=api))
    monkeypatch.setenv("HF_TOKEN", "test-token")

    assert publish.main([str(release), "--repo-id", "a/b", "--yes"], out=io.StringIO()) == 1
    assert "immutable" in capsys.readouterr().err
    api.create_commit.assert_not_called()
