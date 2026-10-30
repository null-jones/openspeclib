"""Tests for the OpenSpecLib Format v1 conformance checker.

The bulk of the coverage comes from the shared corpus in
``tests/conformance``: every fixture is checked and compared with
``expected.json``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from openspeclib import conformance
from openspeclib.conformance import (
    ConformanceError,
    check_collection,
    check_index,
    check_path,
    check_spdx_expression,
    load_rules,
    spec_dir,
)

CORPUS = Path(__file__).parent / "conformance"
FIXTURES = CORPUS / "fixtures"
EXPECTED = json.loads((CORPUS / "expected.json").read_text(encoding="utf-8"))


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "conformance_build_fixtures", CORPUS / "build_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _outcome(report):
    return {"conformant": report.conformant, "failed": report.failed, "warned": report.warned}


def _expected(name):
    entry = EXPECTED[name]
    return {k: entry[k] for k in ("conformant", "failed", "warned")}


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def _check(name):
    """Check a fixture the way expected.json says (``check``: ``index`` forces I1)."""
    if EXPECTED[name].get("check") == "index":
        return check_index(FIXTURES / name)
    return check_path(FIXTURES / name)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_corpus_fixture(name):
    report = _check(name)
    assert _outcome(report) == _expected(name), json.dumps(report.to_dict(), indent=2)


@pytest.mark.parametrize(
    "name", sorted(n for n in EXPECTED if not n.startswith("index_") and "not_parquet" not in n)
)
def test_corpus_fixture_with_one_row_batches(name, monkeypatch):
    """Batch boundaries (R1 ordering, F5 duplicates, list offsets) must not change results."""
    monkeypatch.setattr(conformance, "BATCH_SIZE", 1)
    assert _outcome(check_collection(FIXTURES / name)) == _expected(name)


def test_corpus_covers_every_rule():
    rules = load_rules()
    failed = {r for e in EXPECTED.values() for r in e["failed"]}
    warned = {r for e in EXPECTED.values() for r in e["warned"]}
    must = {r.id for r in rules.values() if r.level == "must"}
    should = {r.id for r in rules.values() if r.level == "should"}
    assert must <= failed
    assert should <= warned


def test_corpus_has_no_unlisted_files():
    assert {p.name for p in FIXTURES.glob("*.parquet")} == set(EXPECTED)


def test_builder_is_deterministic(tmp_path):
    builder = _load_builder()
    first = builder.build(tmp_path / "a", tmp_path / "a.json")
    second = builder.build(tmp_path / "b", tmp_path / "b.json")
    assert first == second
    for name in first:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    # Expectations never depend on the pyarrow version.
    assert (tmp_path / "a.json").read_text() == (CORPUS / "expected.json").read_text()
    # Bytes do (``created_by``); compare with the committed files only when the
    # writer matches.
    committed = pq.ParquetFile(FIXTURES / "valid_minimal.parquet").metadata.created_by
    fresh = pq.ParquetFile(tmp_path / "a" / "valid_minimal.parquet").metadata.created_by
    if committed != fresh:
        pytest.skip(f"fixtures were written by {committed}, this is {fresh}")
    for name in first:
        assert (tmp_path / "a" / name).read_bytes() == (FIXTURES / name).read_bytes(), name


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_report_uses_rule_metadata_from_rules_json():
    rules = load_rules()
    report = check_collection(FIXTURES / "valid_full.parquet")
    expected_ids = [
        r.id for r in rules.values() if r.applies_to == "collection" and r.level != "qa"
    ]
    assert [r.rule_id for r in report.results] == expected_ids
    for result in report.results:
        assert result.title == rules[result.rule_id].title
        assert result.level == rules[result.rule_id].level
    assert report.kind == "collection"
    assert report.num_rows == 6
    assert report.format_version == "1.0.0"


def test_report_to_dict_is_json():
    report = check_index(FIXTURES / "index_valid.parquet")
    data = json.loads(json.dumps(report.to_dict()))
    assert data["kind"] == "index"
    assert data["conformant"] is True
    assert [r["rule"] for r in data["rules"]] == ["I1"]
    assert len(data["collections"]) == 2
    assert all(c["conformant"] for c in data["collections"])


def test_failure_examples_name_rows():
    report = check_collection(FIXTURES / "f9_sentinel.parquet")
    result = report.result("F9")
    assert result.status == "failed"
    assert result.count == 1
    assert result.examples[0].startswith("conformance_minimal:a1")
    assert not result.passed


def test_examples_are_capped_but_count_is_not():
    result = check_collection(FIXTURES / "r1_unsorted.parquet", max_examples=1).result("R1")
    assert result.status == "warned"
    assert result.count == 2
    assert len(result.examples) == 1
    uncapped = check_collection(FIXTURES / "r2_large_row_group.parquet", max_examples=None)
    assert uncapped.result("R2").count == 1


def test_findings_carry_row_ids():
    result = check_collection(FIXTURES / "f5_duplicate_id.parquet").result("F5")
    assert [f.row_id for f in result.findings] == ["conformance_minimal:a2"]
    footer = check_collection(FIXTURES / "f11_footer_licence.parquet").result("F11")
    assert footer.findings[0].row_id is None


def test_collection_id_is_reported():
    assert check_collection(FIXTURES / "valid_full.parquet").collection_id == "conformance_full"


def test_large_types_are_equivalent():
    schema = pq.read_schema(FIXTURES / "valid_large_types.parquet")
    assert str(schema.field("name").type) == "large_string"
    assert check_collection(FIXTURES / "valid_large_types.parquet").conformant


def test_index_of_nonconformant_collection_reports_nested_failure():
    report = check_index(FIXTURES / "index_of_nonconformant.parquet")
    assert report.failed == []
    assert not report.conformant
    assert report.collections[0].failed == ["F7"]


def test_check_path_dispatches_on_footer_key():
    assert check_path(FIXTURES / "index_valid.parquet").kind == "index"
    assert check_path(FIXTURES / "valid_minimal.parquet").kind == "collection"
    # Both keys: checked as a collection, where F1 fails it.
    assert conformance.is_index_file(FIXTURES / "f1_both_footer_keys.parquet") is False
    assert conformance.is_index_file(FIXTURES / "f1_not_parquet.parquet") is None


def test_check_index_on_collection_file_fails_i1():
    report = check_index(FIXTURES / "valid_minimal.parquet")
    assert report.failed == ["I1"]


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        check_collection(FIXTURES / "nope.parquet")
    with pytest.raises(FileNotFoundError):
        check_path(FIXTURES / "nope.parquet")


def test_url_without_fsspec_raises(monkeypatch):
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(conformance.importlib, "import_module", missing)
    with pytest.raises(ConformanceError, match="fsspec"):
        check_collection("https://example.org/collection.parquet")


def test_spec_dir_resolves():
    directory = spec_dir()
    for name in ("columns.json", "vocabularies.json", "footer.schema.json", "rules.json"):
        assert (directory / name).is_file()


# ---------------------------------------------------------------------------
# SPDX syntax (F11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "refs"),
    [
        ("CC-BY-4.0", set()),
        ("NOASSERTION", set()),
        ("GPL-2.0+", set()),
        ("MIT OR Apache-2.0", set()),
        ("MIT AND (Apache-2.0 OR BSD-3-Clause)", set()),
        ("GPL-2.0-or-later WITH Classpath-exception-2.0", set()),
        ("LicenseRef-Lab-1", {"LicenseRef-Lab-1"}),
        ("(CC-BY-4.0 AND LicenseRef-a) OR LicenseRef-b.c", {"LicenseRef-a", "LicenseRef-b.c"}),
        ("((MIT))", set()),
        ("NOASSERTION", set()),
        ("MIT AND (Apache-2.0)", set()),
        ("(MIT)AND(Apache-2.0)", set()),
    ],
)
def test_spdx_valid(expression, refs):
    assert check_spdx_expression(expression) == (None, refs)


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "   ",
        "CC BY 4.0",
        "MIT AND",
        "AND MIT",
        "MIT or Apache-2.0",
        "(MIT",
        "MIT)",
        "()",
        "MIT WITH",
        "MIT WITH LicenseRef-x",
        "(MIT OR Apache-2.0) WITH Classpath-exception-2.0",
        "LicenseRef-",
        "LicenseRef-x+",
        "DocumentRef-spdx:LicenseRef-x",
        "MIT/Apache",
        "MIT ++",
        "MIT AND NOASSERTION",
        "  NOASSERTION  ",
        " MIT",
        "MIT ",
        "MIT  OR Apache-2.0",
        "MIT\tOR Apache-2.0",
        "MIT\u00a0OR Apache-2.0",
        "MIT\nOR Apache-2.0",
        "(" * 33 + "MIT" + ")" * 33,
        "(" * 5000 + "MIT" + ")" * 5000,
        "(NOASSERTION)",
        "NOASSERTION OR MIT",
    ],
)
def test_spdx_invalid(expression):
    error, _ = check_spdx_expression(expression)
    assert error is not None


def test_spdx_nesting_limit_is_clean():
    assert check_spdx_expression("(" * 32 + "MIT" + ")" * 32) == (None, set())
    error, _ = check_spdx_expression("(" * 33 + "MIT" + ")" * 33)
    assert "nested deeper" in error


def test_index_with_both_keys_fails_i1_via_check_index():
    assert check_index(FIXTURES / "index_both_keys.parquet").failed == ["I1"]
    # check_path treats a file with both keys as a collection, where F1 fails it.
    assert "F1" in check_path(FIXTURES / "index_both_keys.parquet").failed


def test_corrupt_page_reports_f1_and_skips_row_rules():
    report = check_collection(FIXTURES / "f1_corrupt_page.parquet")
    assert report.failed == ["F1"]
    assert "not a valid Parquet file" in report.result("F1").examples[-1]
    assert report.result("F9").status == "skipped"


def test_index_over_corrupt_collection_does_not_crash(tmp_path):
    import shutil

    for name in ("valid_minimal.parquet",):
        shutil.copy(FIXTURES / name, tmp_path / name)
    shutil.copy(FIXTURES / "f1_corrupt_page.parquet", tmp_path / "valid_minimal.parquet")
    shutil.copy(FIXTURES / "index_unlisted_file.parquet", tmp_path / "index.parquet")
    report = check_index(tmp_path / "index.parquet")
    assert "I1" in report.failed
    assert not report.conformant


def _rewrite_index(source, target, edit):
    table = pq.read_table(source)
    meta = json.loads(table.schema.metadata[b"openspeclib_index"])
    table, meta = edit(table, meta)
    table = table.replace_schema_metadata({b"openspeclib_index": json.dumps(meta).encode()})
    pq.write_table(table, target)


def test_absolute_file_reference_fails_even_when_the_file_exists(tmp_path):
    import shutil

    import pyarrow as pa

    shutil.copy(FIXTURES / "valid_minimal.parquet", tmp_path / "valid_minimal.parquet")
    absolute = str((tmp_path / "valid_minimal.parquet").resolve())

    def edit(table, meta):
        meta["collections"][0]["file"] = absolute
        position = table.column_names.index("file")
        return table.set_column(position, "file", pa.array([absolute] * table.num_rows)), meta

    _rewrite_index(FIXTURES / "index_file_listed_twice.parquet", tmp_path / "i.parquet", edit)
    report = check_index(tmp_path / "i.parquet")
    messages = report.result("I1").examples
    assert report.failed == ["I1"]
    assert any("absolute paths are not allowed" in m for m in messages), messages
    assert report.collections and report.collections[0].conformant


def test_same_file_listed_twice_is_reported_by_resolved_path():
    messages = check_index(FIXTURES / "index_file_listed_twice.parquet").result("I1").examples
    assert messages == ["footer: file './valid_minimal.parquet' is listed more than once"]


def test_newer_minor_reports_unknowns_as_warnings():
    report = check_collection(FIXTURES / "valid_future_minor.parquet")
    assert report.conformant
    assert report.failed == []
    for rule_id in ("F1", "F3", "F7"):
        result = report.result(rule_id)
        assert result.status == "warned" and result.passed and result.count == 0
        assert result.warnings
    assert any("provenance" in w.text for w in report.result("F1").warnings)
    assert "warnings" in report.to_dict()["rules"][0]
    older = check_collection(FIXTURES / "f1_f3_f7_unknowns_in_1_0.parquet")
    assert older.failed == ["F1", "F3", "F7"]
    assert not older.conformant


def test_newer_minor_detection():
    assert conformance.is_newer_minor({"format_version": "1.1.0"})
    assert not conformance.is_newer_minor({"format_version": "1.0.7"})
    assert not conformance.is_newer_minor({"format_version": "2.3.0"})
    assert not conformance.is_newer_minor({"format_version": None})


def test_index_with_unfetchable_collection_is_not_conformant(monkeypatch):
    def unreachable(location, max_examples):
        raise OSError(f"cannot fetch {location}")

    monkeypatch.setattr(conformance, "_run_collection", unreachable)
    report = check_index(FIXTURES / "index_valid.parquet")
    assert report.failed == ["I1"]
    assert not report.conformant
    assert any("cannot be read" in e for e in report.result("I1").examples)


def test_extra_messages_name_the_json_type():
    examples = check_collection(FIXTURES / "f4_extra_string.parquet").result("F4").examples
    assert examples == ["conformance_minimal:a1: extra is a string, not a JSON object"]


@pytest.mark.parametrize(
    ("created", "ok"),
    [
        ("2026-10-29T12:00:00Z", True),
        ("2024-02-29T00:00:00Z", True),
        ("2016-12-31T23:59:60Z", True),
        ("2026-01-01T00:00:00.123456789+05:30", True),
        ("2026-02-30T00:00:00Z", False),
        ("2026-13-01T00:00:00Z", False),
        ("2026-01-01T24:00:00Z", False),
        ("2026-01-01T00:00:00+12:60", False),
        ("2026-01-01T00:00:00+24:00", False),
        ("2026-01-01T00:00:00Z\n", False),
    ],
)
def test_calendar_datetime(created, ok):
    assert conformance._is_calendar_datetime(created) is ok


@pytest.mark.parametrize(
    "expression",
    ["MIT WITH NOASSERTION", "licenseref-lab", "LICENSEREF-lab", "documentref-x:LicenseRef-y"],
)
def test_spdx_case_and_noassertion_nits(expression):
    error, _ = check_spdx_expression(expression)
    assert error is not None


def test_schema_patterns_match_the_whole_value():
    meta = json.loads(
        pq.ParquetFile(FIXTURES / "valid_minimal.parquet").metadata.metadata[b"openspeclib"]
    )
    meta["collection"]["id"] = "conformance_minimal\n"
    errors = conformance._schema_errors(meta, "collectionFooter")
    assert any(e.startswith("collection/id:") for e in errors)


def test_write_index_refuses_a_non_conformant_index(tmp_path):
    import shutil

    from openspeclib import storage

    for name in ("valid_minimal_subset.parquet", "valid_minimal_rest.parquet"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    target = tmp_path / "index.parquet"
    with pytest.raises(storage.NonConformantFileError, match="share a collection id"):
        storage.write_index(
            [tmp_path / "valid_minimal_subset.parquet", tmp_path / "valid_minimal_rest.parquet"],
            target,
            created="2026-10-29T12:00:00Z",
        )
    assert not target.exists()
    assert not (tmp_path / "index.parquet.part").exists()


def test_write_index_accepts_a_conformant_index(tmp_path):
    import shutil

    from openspeclib import storage

    for name in ("valid_minimal.parquet", "valid_full.parquet"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    target = tmp_path / "index.parquet"
    storage.write_index(
        [tmp_path / "valid_minimal.parquet", tmp_path / "valid_full.parquet"],
        target,
        created="2026-10-29T12:00:00Z",
    )
    assert check_index(target).conformant


def test_semantic_version_rejects_leading_zeros():
    assert not conformance.is_newer_minor({"format_version": "1.01.0"})
    assert not conformance.is_newer_minor({"format_version": "1.1.00"})
    assert conformance.is_newer_minor({"format_version": "1.10.0"})
    report = check_collection(FIXTURES / "f1_format_version_leading_zero.parquet")
    assert any("leading zeros" in e for e in report.result("F1").examples)


@pytest.mark.parametrize(
    "expression",
    ["noassertion", "NoAssertion", "MIT AND noassertion", "MIT WITH noassertion", "noassertion+"],
)
def test_spdx_noassertion_is_case_sensitive(expression):
    error, _ = check_spdx_expression(expression)
    assert error is not None


def test_invalid_utf8_extra_is_one_finding():
    result = check_collection(FIXTURES / "f4_invalid_utf8_extra.parquet").result("F4")
    assert result.count == 1
    assert result.examples == ["conformance_minimal:a1: invalid UTF-8 in extra"]


def test_invalid_utf8_column_name_is_f1_not_a_crash(tmp_path):
    import shutil

    name = "f1_invalid_utf8_column_name.parquet"
    assert conformance.is_index_file(FIXTURES / name) is None
    report = check_collection(FIXTURES / name)
    assert report.failed == ["F1"]
    assert "not UTF-8" in report.result("F1").examples[0]
    assert report.result("F3").status == "skipped"
    assert check_index(FIXTURES / name).failed == ["I1"]
    # Listed by an index: the index fails I1, the collection F1.
    shutil.copy(FIXTURES / name, tmp_path / "valid_minimal.parquet")
    shutil.copy(FIXTURES / "index_unlisted_file.parquet", tmp_path / "index.parquet")
    nested = check_index(tmp_path / "index.parquet")
    assert nested.failed == ["I1"]
    assert nested.collections[0].failed == ["F1"]


def test_newer_minor_unknown_column_must_be_flat():
    report = check_collection(FIXTURES / "f3_future_minor_nested_column.parquet")
    assert report.result("F3").examples == [
        "column 'spectral_meta' is not flat (only primitives or lists of primitives)"
    ]


def test_index_judges_embedded_metadata_by_its_own_version(tmp_path):
    import shutil

    report = check_index(FIXTURES / "index_over_future_minor.parquet")
    assert report.conformant and report.failed == []
    warnings = [w.text for w in report.result("I1").warnings]
    assert any("collections/0/metadata" in w and "provenance" in w for w in warnings)
    with_column = check_index(FIXTURES / "index_over_future_minor_with_column.parquet")
    assert any("'spectral_index'" in w.text for w in with_column.result("I1").warnings)

    # A 1.1.0 index does not excuse unknown members of a 1.0.0 collection footer.
    shutil.copy(FIXTURES / "valid_minimal.parquet", tmp_path / "valid_minimal.parquet")

    def edit(table, meta):
        meta["collections"][0]["metadata"]["provenance"] = "x"
        return table, meta

    _rewrite_index(FIXTURES / "index_future_minor.parquet", tmp_path / "i.parquet", edit)
    examples = check_index(tmp_path / "i.parquet").result("I1").examples
    assert any("collections/0/metadata" in e and "provenance" in e for e in examples), examples


def test_index_values_of_unknown_columns_are_compared(tmp_path):
    import shutil

    import pyarrow as pa

    shutil.copy(FIXTURES / "valid_future_minor.parquet", tmp_path / "valid_future_minor.parquet")

    def edit(table, meta):
        position = table.column_names.index("spectral_index")
        values = [v + 1.0 for v in table["spectral_index"].to_pylist()]
        return table.set_column(position, "spectral_index", pa.array(values)), meta

    source = FIXTURES / "index_over_future_minor_with_column.parquet"
    _rewrite_index(source, tmp_path / "i.parquet", edit)
    report = check_index(tmp_path / "i.parquet")
    assert report.failed == ["I1"]
    assert any("spectral_index differs" in e for e in report.result("I1").examples)


def test_write_index_over_newer_minor_collection(tmp_path):
    import shutil

    from openspeclib import storage

    shutil.copy(FIXTURES / "valid_future_minor.parquet", tmp_path / "valid_future_minor.parquet")
    target = tmp_path / "index.parquet"
    storage.write_index(
        [tmp_path / "valid_future_minor.parquet"], target, created="2026-10-29T12:00:00Z"
    )
    report = check_index(target)
    assert report.conformant and report.warned == ["I1"]
    # Section 7: every non-array column, including one a newer minor defines.
    collection_columns = set(pq.read_schema(tmp_path / "valid_future_minor.parquet").names)
    index_columns = set(pq.read_schema(target).names)
    assert "spectral_index" in collection_columns
    assert collection_columns - {"wavelengths", "values", "bandpass_fwhm", "values_stddev"} <= (
        index_columns
    )


def test_write_index_failure_keeps_previous_index_and_names_it(tmp_path):
    import shutil

    from openspeclib import storage

    for name in ("valid_minimal_subset.parquet", "valid_minimal_rest.parquet"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    target = tmp_path / "index.parquet"
    target.write_bytes(b"previous")
    with pytest.raises(storage.NonConformantFileError) as caught:
        storage.write_index(
            [tmp_path / "valid_minimal_subset.parquet", tmp_path / "valid_minimal_rest.parquet"],
            target,
            created="2026-10-29T12:00:00Z",
        )
    assert ".part" not in str(caught.value)
    assert "index.parquet: Index file" in str(caught.value)
    assert target.read_bytes() == b"previous"
    assert not (tmp_path / "index.parquet.part").exists()


class TestEcmaPatterns:
    """Schema patterns are evaluated with ECMA-262 semantics, as ajv does."""

    @pytest.mark.parametrize(
        ("doi", "ok"),
        [
            ("10.1234/abc", True),
            ("10.1234/ab\x1cc", True),  # not whitespace in ECMA, unlike Python
            ("10.1234/ab﻿c", False),  # whitespace in ECMA, unlike Python
            ("10.1234/ab c", False),
            ("10.1234/abc\n", False),
        ],
    )
    def test_doi_pattern(self, doi: str, ok: bool) -> None:
        import re

        pattern = conformance._ecma_pattern(r"^10\.[0-9]{4,}/\S+$")
        assert bool(re.search(pattern, doi)) is ok

    def test_whitespace_inside_a_class(self) -> None:
        import re

        pattern = conformance._ecma_pattern(r"^[a\s]+$")
        assert re.search(pattern, "a﻿a")
        assert not re.search(pattern, "a\x1ca")

    def test_error_message_shows_the_schema_pattern(self) -> None:
        pattern = r"^10\.[0-9]{4,}/\S+$"
        rewritten = conformance._ecma_pattern(pattern)
        assert conformance._SCHEMA_PATTERNS[rewritten] == pattern


def test_index_missing_a_newer_minor_column_warns(tmp_path):
    import shutil

    shutil.copy(FIXTURES / "valid_future_minor.parquet", tmp_path / "valid_future_minor.parquet")
    shutil.copy(FIXTURES / "index_over_future_minor_with_column.parquet", tmp_path / "with.parquet")

    def edit(table, meta):
        return table.drop_columns(["spectral_index"]), meta

    _rewrite_index(tmp_path / "with.parquet", tmp_path / "i.parquet", edit)
    report = check_index(tmp_path / "i.parquet")
    assert report.conformant and report.warned == ["I1"]
    i1 = next(r for r in report.to_dict()["rules"] if r["rule"] == "I1")
    assert any("'spectral_index'" in w and "not in the index" in w for w in i1["warnings"])


def test_duplicate_x_column_fails_f3(tmp_path):
    import pyarrow as pa

    table = pq.read_table(FIXTURES / "valid_full.parquet")
    column = pa.array([1] * table.num_rows, pa.int32())
    table = table.append_column("x_note", column).append_column("x_note", column)
    pq.write_table(table, tmp_path / "dup.parquet")
    report = check_collection(tmp_path / "dup.parquet")
    assert not report.conformant and "F3" in report.failed


def test_duplicate_newer_minor_column_in_index_fails_i1(tmp_path):
    import shutil

    shutil.copy(FIXTURES / "valid_future_minor.parquet", tmp_path / "valid_future_minor.parquet")

    def edit(table, meta):
        position = table.column_names.index("spectral_index")
        return table.append_column(table.field(position), table.column(position)), meta

    _rewrite_index(
        FIXTURES / "index_over_future_minor_with_column.parquet", tmp_path / "i.parquet", edit
    )
    report = check_index(tmp_path / "i.parquet")
    assert not report.conformant and "I1" in report.failed


@pytest.mark.parametrize("batch_size", [1, 2048])
def test_r1_skips_a_null_id_the_same_across_batches(tmp_path, monkeypatch, batch_size):
    import pyarrow as pa

    monkeypatch.setattr(conformance, "BATCH_SIZE", batch_size)
    table = pq.read_table(FIXTURES / "valid_minimal.parquet")
    assert table.num_rows >= 3
    ids = table["id"].to_pylist()
    # Descending around a null: b, null, a. Neither neighbour pair compares.
    new_ids = [ids[1], None, ids[0]] + ids[3:]
    table = table.set_column(table.column_names.index("id"), "id", pa.array(new_ids))
    pq.write_table(table, tmp_path / "t.parquet", use_byte_stream_split=False)
    report = check_collection(tmp_path / "t.parquet")
    assert "R1" not in report.warned and "R1" not in report.failed


@pytest.mark.parametrize(
    ("file", "ok"),
    [
        ("http://[::1]/x.parquet", True),
        ("a[1].parquet", True),
        ("http://[abc/x.parquet", False),
        ("http://a]/x.parquet", False),
        ("http://[zz]/x.parquet", False),
        ("//[x/y.parquet", False),
    ],
)
def test_file_reference_with_brackets_does_not_crash(file, ok):
    assert (conformance._file_reference_error(file) is None) is ok


def test_nan_in_a_footer_is_not_valid_json(tmp_path):
    table = pq.read_table(FIXTURES / "valid_future_minor.parquet")
    raw = table.schema.metadata[b"openspeclib"].decode()
    patched = raw[:-1] + ',"x_future":NaN}'
    table = table.replace_schema_metadata({b"openspeclib": patched.encode()})
    pq.write_table(table, tmp_path / "nan.parquet")
    report = check_collection(tmp_path / "nan.parquet")
    assert not report.conformant and "F1" in report.failed


def test_write_index_leaves_out_a_newer_minor_list_column(tmp_path):
    import pyarrow as pa

    from openspeclib import storage

    table = pq.read_table(FIXTURES / "valid_future_minor.parquet")
    lists = pa.array([[0.5, 0.5]] * table.num_rows, pa.list_(pa.float32()))
    table = table.append_column("band_quality", lists)
    pq.write_table(table, tmp_path / "valid_future_minor.parquet")
    target = tmp_path / "index.parquet"
    storage.write_index(
        [tmp_path / "valid_future_minor.parquet"], target, created="2026-10-30T12:00:00Z"
    )
    names = pq.read_schema(target).names
    assert "spectral_index" in names and "band_quality" not in names
    assert check_index(target).conformant
