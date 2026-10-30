"""Tests for building a release directory (``openspeclib combine``)."""

import csv
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from conftest import make_collection_info, make_record

from openspeclib import __version__
from openspeclib.cli import main
from openspeclib.combine import build_library, legacy_id, resolve_id_collisions
from openspeclib.conformance import check_index
from openspeclib.models import CollectionMetadata, DatasetInfo, Quantity
from openspeclib.storage import read_footer, read_index_footer, record_to_row


def _meta(collection_id: str = "usgs_splib07", **kwargs) -> CollectionMetadata:
    datasets = kwargs.pop("datasets", {})
    return CollectionMetadata(
        collection=make_collection_info(collection_id, **kwargs), datasets=datasets
    )


def _ecosis_meta() -> CollectionMetadata:
    return _meta(
        "ecosis",
        license="NOASSERTION",
        license_refs={},
        inclusion="subset",
        inclusion_notes="curated",
        datasets={"pkg": DatasetInfo(id="pkg", title="Package", license="CC-BY-4.0")},
    )


class TestBuildLibrary:
    def test_flat_release_layout(self, tmp_path: Path, sample_spectrum) -> None:
        out = tmp_path / "release"
        build = build_library(
            {"usgs_splib07": ([sample_spectrum, make_record("other")], _meta())},
            out,
            release="1.2.3",
        )
        assert sorted(p.name for p in out.iterdir()) == [
            "checksums.txt",
            "id-migration.csv",
            "openspeclib-index.parquet",
            "usgs_splib07.parquet",
        ]
        assert build.total_spectra == 2
        assert read_index_footer(out / "openspeclib-index.parquet")["release"] == "1.2.3"
        report = check_index(out / "openspeclib-index.parquet")
        assert report.conformant, report.to_dict()

    def test_release_defaults_to_package_version(self, tmp_path: Path) -> None:
        build_library({"usgs_splib07": ([make_record()], _meta())}, tmp_path)
        footer = read_index_footer(tmp_path / "openspeclib-index.parquet")
        assert footer["release"] == __version__
        assert footer["collections"][0]["metadata"] == read_footer(
            tmp_path / "usgs_splib07.parquet"
        )

    def test_checksums_cover_every_other_file(self, tmp_path: Path) -> None:
        build_library({"usgs_splib07": ([make_record()], _meta())}, tmp_path)
        lines = (tmp_path / "checksums.txt").read_text().splitlines()
        listed = {}
        for line in lines:
            digest, name = line.split("  ")
            listed[name] = digest
        others = {p.name for p in tmp_path.iterdir() if p.name != "checksums.txt"}
        assert set(listed) == others
        for name, digest in listed.items():
            assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest

    def test_multiple_collections(self, tmp_path: Path) -> None:
        build = build_library(
            {
                "usgs_splib07": ([make_record("u1"), make_record("u2")], _meta()),
                "ecostress": (
                    [make_record("e1", collection="ecostress")],
                    _meta("ecostress"),
                ),
            },
            tmp_path,
        )
        assert [c.collection_id for c in build.collections] == ["ecostress", "usgs_splib07"]
        index = pq.read_table(tmp_path / "openspeclib-index.parquet")
        assert sorted(set(index["file"].to_pylist())) == [
            "ecostress.parquet",
            "usgs_splib07.parquet",
        ]
        assert index.num_rows == 3

    def test_record_from_other_collection_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="belongs to"):
            build_library(
                {"usgs_splib07": ([make_record(collection="ecostress")], _meta())}, tmp_path
            )

    def test_aggregated_collection_footer(self, tmp_path: Path) -> None:
        recs = [make_record("s1", collection="ecosis", license="CC-BY-4.0", dataset_id="pkg")]
        build_library({"ecosis": (recs, _ecosis_meta())}, tmp_path)
        footer = read_footer(tmp_path / "ecosis.parquet")
        assert footer["collection"]["license"] == "NOASSERTION"
        assert footer["datasets"] == {"pkg": {"title": "Package", "license": "CC-BY-4.0"}}
        assert check_index(tmp_path / "openspeclib-index.parquet").conformant


class TestIds:
    def test_legacy_ids(self) -> None:
        assert legacy_id(make_record("a_b", original_id="a+b")) == "usgs_splib07:a+b"
        ecosis = make_record("x1", collection="ecosis", dataset_id="pkg")
        assert legacy_id(ecosis) == "ecosis:pkg:x1"
        vnir = make_record("u_vnir", collection="ossl", original_id="u")
        mir = make_record("u_mir", collection="ossl", original_id="u", quantity=Quantity.ABSORBANCE)
        assert legacy_id(vnir) == "ossl:u:vnir"
        assert legacy_id(mir) == "ossl:u:mir"

    def test_migration_csv(self, tmp_path: Path) -> None:
        recs = [make_record("a_b", original_id="a+b"), make_record("same")]
        ecosis = [make_record("x1", collection="ecosis", license="CC-BY-4.0", dataset_id="pkg")]
        build_library(
            {"usgs_splib07": (recs, _meta()), "ecosis": (ecosis, _ecosis_meta())}, tmp_path
        )
        with open(tmp_path / "id-migration.csv", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == ["old_id", "new_id"]
        assert sorted(rows[1:]) == [
            ["ecosis:pkg:x1", "ecosis:x1"],
            ["usgs_splib07:a+b", "usgs_splib07:a_b"],
        ]

    def test_collisions_resolved_deterministically(self, tmp_path: Path) -> None:
        # "a+b", "a b" and "a_b" all sanitise to "a_b".
        recs = [
            make_record("a_b", original_id="a b"),
            make_record("a_b", original_id="a+b"),
            make_record("a_b", original_id="a_b"),
            make_record("a_b~2", original_id="a_b~2"),
        ]
        rows = [record_to_row(r) for r in recs]
        assert resolve_id_collisions(rows, [legacy_id(r) for r in recs]) == 2
        ids = {row.meta["original_id"]: row.id for row in rows}
        # "a_b" needed no sanitising and keeps the plain id; the others are
        # ranked by original id ("a b" < "a+b"), skipping the taken "~2".
        assert ids == {
            "a_b": "usgs_splib07:a_b",
            "a b": "usgs_splib07:a_b~3",
            "a+b": "usgs_splib07:a_b~4",
            "a_b~2": "usgs_splib07:a_b~2",
        }
        # Input order does not matter.
        rows2 = [record_to_row(r) for r in reversed(recs)]
        resolve_id_collisions(rows2, [legacy_id(r) for r in reversed(recs)])
        assert {row.meta["original_id"]: row.id for row in rows2} == ids

        build = build_library({"usgs_splib07": (recs, _meta())}, tmp_path)
        assert build.collections[0].collisions == 2
        assert check_index(tmp_path / "openspeclib-index.parquet").conformant


class TestCli:
    def _ingested(self, tmp_path: Path) -> Path:
        processed = tmp_path / "processed"
        processed.mkdir()
        rec = make_record("cli", original_id="cli+1")
        (processed / "usgs_splib07.jsonl").write_text(rec.model_dump_json() + "\n")
        (processed / "usgs_splib07.collection.json").write_text(_meta().model_dump_json())
        return processed

    def test_combine_cli(self, tmp_path: Path) -> None:
        processed = self._ingested(tmp_path)
        out = tmp_path / "release"
        result = CliRunner().invoke(
            main,
            ["combine", "--input", str(processed), "--output", str(out), "--release", "0.2.0"],
        )
        assert result.exit_code == 0, result.output
        assert (out / "usgs_splib07.parquet").exists()
        assert read_index_footer(out / "openspeclib-index.parquet")["release"] == "0.2.0"
        assert "1 ids changed from v0" in result.output

    def test_combine_requires_collection_metadata(self, tmp_path: Path) -> None:
        processed = self._ingested(tmp_path)
        (processed / "usgs_splib07.collection.json").unlink()
        result = CliRunner().invoke(
            main, ["combine", "--input", str(processed), "--output", str(tmp_path / "o")]
        )
        assert result.exit_code == 1
        assert "collection.json" in result.output

    def test_ingest_writes_collection_metadata(self, tmp_path: Path) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "usgs"
        out = tmp_path / "processed"
        result = CliRunner().invoke(
            main, ["ingest", "--source", "usgs", "--input", str(fixtures), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        meta = json.loads((out / "usgs_splib07.collection.json").read_text())
        assert meta["collection"]["id"] == "usgs_splib07"
        assert meta["collection"]["license"] == "LicenseRef-USGS-PublicDomain"
        assert meta["datasets"] == {}
        CollectionMetadata.model_validate(meta)


def test_unsanitised_row_keeps_plain_id_even_if_it_sorts_last() -> None:
    # "a_b" sorts after "a+b" and "a b", yet it is the only upstream id that
    # equals the local id, so it must not be the one suffixed.
    recs = [make_record("a_b", original_id=o) for o in ("a_b", "a+b", "a b")]
    rows = [record_to_row(r) for r in recs]
    resolve_id_collisions(rows, [legacy_id(r) for r in recs])
    assert {row.meta["original_id"]: row.id for row in rows}["a_b"] == "usgs_splib07:a_b"
    # Without such a row the ranking falls back to the original id.
    recs = [make_record("a_b", original_id=o) for o in ("a+b", "a b")]
    rows = [record_to_row(r) for r in recs]
    resolve_id_collisions(rows, [legacy_id(r) for r in recs])
    assert {row.meta["original_id"]: row.id for row in rows} == {
        "a b": "usgs_splib07:a_b",
        "a+b": "usgs_splib07:a_b~2",
    }


def test_collision_suffix_respects_length_limit(tmp_path: Path) -> None:
    from openspeclib.loaders.base import make_local_id

    long_a = "x" * 199 + "+"  # sanitises to 200 chars
    long_b = "x" * 199 + "#"
    local = make_local_id(long_a)
    assert len(local) == 200 and local == make_local_id(long_b)
    recs = [make_record(local, original_id=o) for o in (long_a, long_b)]
    rows = [record_to_row(r) for r in recs]
    assert resolve_id_collisions(rows, [legacy_id(r) for r in recs]) == 1
    suffixed = sorted(row.id for row in rows)[1]
    local_part = suffixed.split(":", 1)[1]
    assert len(local_part) == 200 and local_part.endswith("~2")
    assert local_part == "x" * 198 + "~2"
    build_library({"usgs_splib07": (recs, _meta())}, tmp_path)
    assert check_index(tmp_path / "openspeclib-index.parquet").conformant


class _FakeLoader:
    def __init__(self, fail_after: int | None = None, fatal: int = 0) -> None:
        self.fail_after = fail_after
        self.fatal_error_count = fatal

    def source_name(self) -> str:
        return "usgs_splib07"

    def load(self, _input):
        for i in range(3):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("boom")
            yield make_record(f"r{i}")

    def collection_info(self):
        return make_collection_info()

    def datasets(self):
        return {}


# A fatal run removes the previous output instead (tests/test_cli_ingest.py).
@pytest.mark.parametrize("loader", [_FakeLoader(fail_after=2)])
def test_ingest_is_atomic(tmp_path: Path, monkeypatch, loader) -> None:
    import openspeclib.cli as cli

    out = tmp_path / "processed"
    out.mkdir()
    # A previous good ingest must survive a failed re-run untouched.
    (out / "usgs_splib07.jsonl").write_text("old\n")
    (out / "usgs_splib07.collection.json").write_text("{}")
    monkeypatch.setattr(cli, "_get_loader", lambda source: loader)
    result = CliRunner().invoke(
        main, ["ingest", "--source", "usgs", "--input", str(tmp_path), "--output", str(out)]
    )
    assert result.exit_code != 0
    assert sorted(p.name for p in out.iterdir()) == [
        "usgs_splib07.collection.json",
        "usgs_splib07.jsonl",
    ]
    assert (out / "usgs_splib07.jsonl").read_text() == "old\n"


def test_ingest_success_renames_both(tmp_path: Path, monkeypatch) -> None:
    import openspeclib.cli as cli

    out = tmp_path / "processed"
    monkeypatch.setattr(cli, "_get_loader", lambda source: _FakeLoader())
    result = CliRunner().invoke(
        main, ["ingest", "--source", "usgs", "--input", str(tmp_path), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "usgs_splib07.collection.json",
        "usgs_splib07.jsonl",
    ]
    assert len((out / "usgs_splib07.jsonl").read_text().splitlines()) == 3


def test_full_ties_broken_by_content_not_input_order() -> None:
    # Same original id, dataset and v0 id: only the content differs.
    recs = [
        make_record("dup", original_id="dup+", values=[0.1, 0.2, 0.3, 0.4, 0.5]),
        make_record("dup", original_id="dup+", values=[0.5, 0.4, 0.3, 0.2, 0.1]),
    ]

    def ranked(records: list) -> dict[str, tuple[float, ...]]:
        rows = [record_to_row(r) for r in records]
        resolve_id_collisions(rows, [legacy_id(r) for r in records])
        return {row.id: tuple(float(v) for v in row.values) for row in rows}

    forward = ranked(recs)
    assert sorted(forward) == ["usgs_splib07:dup", "usgs_splib07:dup~2"]
    assert ranked(list(reversed(recs))) == forward
