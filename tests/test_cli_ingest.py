"""Tests for ``openspeclib ingest``: stale output from a previous run never survives."""

from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import make_collection_info, make_record

import openspeclib.cli as cli
from openspeclib.cli import main


class _Loader:
    def __init__(self, count: int = 3, fatal: int = 0, fail_after: int | None = None) -> None:
        self.count = count
        self.fatal_error_count = fatal
        self.fail_after = fail_after

    def source_name(self) -> str:
        return "usgs_splib07"

    def load(self, _input):
        for i in range(self.count):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("boom")
            yield make_record(f"r{i}")

    def collection_info(self):
        return make_collection_info()

    def datasets(self):
        return {}


def _run(tmp_path: Path, monkeypatch, loader: _Loader, *, previous: bool = True):
    out = tmp_path / "processed"
    out.mkdir()
    if previous:
        (out / "usgs_splib07.jsonl").write_text("old\n")
        (out / "usgs_splib07.collection.json").write_text("{}")
    monkeypatch.setattr(cli, "_get_loader", lambda source: loader)
    result = CliRunner().invoke(
        main, ["ingest", "--source", "usgs", "--input", str(tmp_path), "--output", str(out)]
    )
    return result, out


def test_fatal_ingest_removes_previous_output(tmp_path: Path, monkeypatch) -> None:
    result, out = _run(tmp_path, monkeypatch, _Loader(fatal=1))
    assert result.exit_code == 1
    assert list(out.iterdir()) == []
    assert "were removed" in result.output


def test_empty_ingest_removes_previous_output(tmp_path: Path, monkeypatch) -> None:
    result, out = _run(tmp_path, monkeypatch, _Loader(count=0))
    assert result.exit_code == 0, result.output
    assert list(out.iterdir()) == []
    assert "no spectra found" in result.output


@pytest.mark.parametrize("loader", [_Loader(fatal=1), _Loader(count=0)])
def test_failed_ingest_without_previous_output(tmp_path: Path, monkeypatch, loader) -> None:
    _result, out = _run(tmp_path, monkeypatch, loader, previous=False)
    assert list(out.iterdir()) == []


def test_crashed_ingest_keeps_previous_output(tmp_path: Path, monkeypatch) -> None:
    result, out = _run(tmp_path, monkeypatch, _Loader(fail_after=2))
    assert result.exit_code != 0
    assert sorted(p.name for p in out.iterdir()) == [
        "usgs_splib07.collection.json",
        "usgs_splib07.jsonl",
    ]
    assert (out / "usgs_splib07.jsonl").read_text() == "old\n"
