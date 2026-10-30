"""Tests for the v1 validator (conformance plus the OpenSpecLib quality profile).

Files are written here with pyarrow directly, following the Format v1 spec, so
these tests do not depend on ``openspeclib.storage``.
"""

import json
import math
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from openspeclib.validate import (
    DEFAULT_ALLOWLIST_PATH,
    WAIVABLE_CHECKS,
    Allowlist,
    AllowlistEntry,
    ValidationResult,
    quality_bounds,
    validate,
)

CORPUS = Path(__file__).parent / "conformance" / "fixtures"
CID = "usgs_splib07"
NAN = float("nan")
SPIKE = [0.04, 0.06, 2.0, 0.12, 0.15]
PERCENT = [4.0, 6.0, 8.0, 12.0, 15.0]

FIELDS = [
    ("id", pa.string()),
    ("collection", pa.string()),
    ("original_id", pa.string()),
    ("name", pa.string()),
    ("material_name", pa.string()),
    ("material_category", pa.string()),
    ("quantity", pa.string()),
    ("geometry", pa.string()),
    ("wavelength_unit", pa.string()),
    ("wavelengths", pa.list_(pa.float64())),
    ("values", pa.list_(pa.float32())),
    ("wavelength_min_nm", pa.float64()),
    ("wavelength_max_nm", pa.float64()),
    ("license", pa.string()),
    ("dataset_id", pa.string()),
]


def spectrum(local="olivine_gds70", values=(0.04, 0.06, 0.08, 0.12, 0.15), **kw):
    values = list(values)
    wavelengths = [400.0 + 100.0 * i for i in range(len(values))]
    row = {
        "id": f"{CID}:{local}",
        "collection": CID,
        "original_id": local,
        "name": local,
        "material_name": "Olivine",
        "material_category": "mineral",
        "quantity": "reflectance",
        "geometry": "directional_hemispherical",
        "wavelength_unit": "nm",
        "wavelengths": wavelengths,
        "values": values,
        "wavelength_min_nm": wavelengths[0],
        "wavelength_max_nm": wavelengths[-1],
        "license": "CC0-1.0",
        "dataset_id": None,
    }
    row.update(kw)
    return row


def write_collection(path, rows, *, cid=CID, datasets=None):
    footer = {
        "format_version": "1.0.0",
        "collection": {
            "id": cid,
            "title": "Test collection",
            "license": "CC0-1.0",
            "citation": "Test.",
            "inclusion": "complete",
        },
        "extensions": [],
        "created": "2026-10-29T12:00:00Z",
        "generator": "test_validate",
    }
    if datasets is not None:
        footer["datasets"] = {k: {"title": k} for k in datasets}
    rows = sorted(rows, key=lambda r: r["id"])
    table = pa.Table.from_pylist(rows, schema=pa.schema(FIELDS))
    table = table.replace_schema_metadata({"openspeclib": json.dumps(footer)})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        compression="zstd",
        use_dictionary=False,
        use_byte_stream_split=["wavelengths.list.element", "values.list.element"],
    )
    return path


def one(tmp_path, values, **kw):
    datasets = [kw["dataset_id"]] if kw.get("dataset_id") else None
    return write_collection(
        tmp_path / f"{CID}.parquet", [spectrum(values=values, **kw)], datasets=datasets
    )


def _allowlist(*entries):
    allowlist = Allowlist()
    for entry in entries:
        allowlist.add_entry(AllowlistEntry.model_validate(entry))
    return allowlist


def _oob(selector, bounds=None, reason="test"):
    entry = {**selector, "checks": ["value_out_of_bounds"], "reason": reason}
    if bounds is not None:
        entry["bounds"] = bounds
    return entry


def _checks(result, severity):
    return {i.check for i in result.issues if i.severity == severity}


def _empty():
    return Allowlist()


def check(path, **kwargs):
    """Validate with the quality profile (the API default is ``format``)."""
    kwargs.setdefault("profile", "openspeclib")
    return validate(path, **kwargs)


# ---------------------------------------------------------------------------
# Inputs and profiles
# ---------------------------------------------------------------------------


class TestInputs:
    def test_valid_file_passes_both_profiles(self, tmp_path):
        path = one(tmp_path, [0.04, 0.06, 0.08, 0.12, 0.15])
        for profile in ("format", "openspeclib"):
            result = check(path, profile=profile, allowlist=_empty())
            assert result.is_valid, result.summary()
            assert not result.warnings, result.summary()
            assert result.spectra_checked == 1
            assert result.files_checked == 1

    def test_format_profile_skips_quality(self, tmp_path):
        path = one(tmp_path, PERCENT)
        assert check(path, profile="format").is_valid
        result = check(path, profile="openspeclib", allowlist=_empty())
        assert _checks(result, "error") == {"value_scale"}
        assert result.issues[0].rule == "Q1"

    def test_default_profile_is_format(self, tmp_path):
        result = validate(one(tmp_path, PERCENT), allowlist=_empty())
        assert result.profile == "format"
        assert result.is_valid
        assert result.allowlist_files == []

    def test_unknown_profile(self, tmp_path):
        with pytest.raises(ValueError, match="profile"):
            check(one(tmp_path, SPIKE), profile="strict")

    def test_missing_path(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            check(tmp_path / "nope")

    def test_empty_directory_is_an_error(self, tmp_path):
        result = check(tmp_path, allowlist=_empty())
        assert _checks(result, "error") == {"no_files"}

    def test_conformance_failures_are_errors_with_rule_ids(self):
        result = check(CORPUS / "f9_sentinel.parquet", allowlist=_empty())
        assert _checks(result, "error") == {"F9"}
        issue = result.issues[0]
        assert issue.rule == "F9"
        assert issue.spectrum_id == "conformance_minimal:a1"
        assert issue.file == "f9_sentinel.parquet"

    def test_should_rules_are_warnings(self):
        result = check(CORPUS / "r3_default_encoding.parquet", allowlist=_empty())
        assert result.is_valid
        assert _checks(result, "warning") == {"R3"}

    def test_quality_skipped_when_values_unusable(self, tmp_path):
        rows = [spectrum(values=[50.0, 60.0, 70.0, 80.0, 90.0])]
        fields = [(n, pa.list_(pa.float64()) if n == "values" else t) for n, t in FIELDS]
        table = pa.Table.from_pylist(rows, schema=pa.schema(fields))
        good = pq.read_table(one(tmp_path / "ok", [0.1, 0.2, 0.3, 0.4, 0.5]))
        table = table.replace_schema_metadata(good.schema.metadata)
        pq.write_table(table, tmp_path / "f64.parquet")
        result = check(tmp_path / "f64.parquet", allowlist=_empty())
        assert "F2" in _checks(result, "error")
        assert "quality_skipped" in _checks(result, "warning")
        assert "value_scale" not in _checks(result, "error")

    def test_index_file_checks_its_collections(self):
        result = check(CORPUS / "index_valid.parquet", allowlist=_empty())
        assert result.is_valid, result.summary()
        assert result.files_checked == 3
        assert result.spectra_checked == 9
        bad = check(CORPUS / "index_value_mismatch.parquet", allowlist=_empty())
        assert _checks(bad, "error") == {"I1"}
        assert bad.issues[0].source == "index_value_mismatch.parquet"

    def test_directory_with_index(self, tmp_path):
        for name in ("index_valid.parquet", "valid_minimal.parquet", "valid_full.parquet"):
            shutil.copy(CORPUS / name, tmp_path / name)
        result = check(tmp_path, allowlist=_empty())
        assert result.is_valid, result.summary()
        # Each collection is checked once although the index also lists it.
        assert result.files_checked == 3
        assert result.spectra_checked == 9

        shutil.copy(CORPUS / "r4_all_nan.parquet", tmp_path / "extra.parquet")
        result = check(tmp_path, allowlist=_empty())
        assert "unindexed_file" in _checks(result, "warning")
        assert result.files_checked == 4

    def test_report_lists_conformance_verdicts(self, tmp_path):
        result = check(CORPUS / "index_valid.parquet", allowlist=_empty())
        data = json.loads(json.dumps(result.to_dict()))
        assert data["profile"] == "openspeclib"
        assert data["conformance"][0]["kind"] == "index"
        assert len(data["conformance"][0]["collections"]) == 2


# ---------------------------------------------------------------------------
# Quality profile
# ---------------------------------------------------------------------------


class TestQuality:
    def test_bounds_come_from_rules_json(self):
        rules = json.loads((Path(__file__).parents[1] / "schemas/format-v1/rules.json").read_text())
        bounds = quality_bounds()
        assert set(bounds) == set(rules["qa_bounds"])
        assert math.isclose(bounds["reflectance"].hard_max, 1.5)
        assert math.isclose(bounds["emissivity"].warn_max, 1.05, rel_tol=1e-6)

    def test_reflectance_slightly_above_one_and_noise_pass(self, tmp_path):
        path = one(tmp_path, [-0.03, 0.4, 1.1, 1.19, 0.9])
        assert check(path, allowlist=_empty()).is_valid

    def test_values_exactly_at_bounds_pass(self, tmp_path):
        # -0.2 and 1.5 are stored as float32; the bound is rounded the same way.
        result = check(one(tmp_path, [-0.2, 0.1, 0.2, 0.3, 1.5]), allowlist=_empty())
        assert result.is_valid, result.summary()
        assert _checks(result, "warning") == {"value_range_suspect"}

    def test_values_just_past_bounds_fail_with_readable_message(self, tmp_path):
        result = check(one(tmp_path, [0.1, 0.2, 0.3, 0.4, 1.5001]), allowlist=_empty())
        assert _checks(result, "error") == {"value_out_of_bounds"}
        assert "1 of 5 valid reflectance value(s)" in result.errors[0]
        assert "[-0.2, 1.5]" in result.errors[0]

    def test_reflectance_warn_band(self, tmp_path):
        result = check(one(tmp_path, [0.1, 0.2, 1.25, 0.3, 0.4]), allowlist=_empty())
        assert result.is_valid
        assert _checks(result, "warning") == {"value_range_suspect"}
        assert result.issues[0].rule == "Q3"

    def test_scale_error_vs_out_of_bounds_values(self, tmp_path):
        scale = check(one(tmp_path, PERCENT), allowlist=_empty())
        assert _checks(scale, "error") == {"value_scale"}
        assert "missed scale conversion" in scale.errors[0]
        spike = check(one(tmp_path, SPIKE), allowlist=_empty())
        assert _checks(spike, "error") == {"value_out_of_bounds"}
        low = check(one(tmp_path, [-0.5, -0.6, -0.4, 0.1, -0.7]), allowlist=_empty())
        assert _checks(low, "error") == {"value_scale"}
        assert "below the hard minimum" in low.errors[0]

    def test_nan_values_are_ignored(self, tmp_path):
        result = check(one(tmp_path, [0.1, NAN, 0.3, NAN, 0.2]), allowlist=_empty())
        assert result.is_valid and not result.warnings, result.summary()

    def test_emissivity_warns_above_1_05(self, tmp_path):
        result = check(
            one(tmp_path, [0.95, 0.97, 1.06, 0.99, 0.98], quantity="emissivity"),
            allowlist=_empty(),
        )
        assert result.is_valid
        assert _checks(result, "warning") == {"value_range_suspect"}

    def test_absorbance_uses_its_bounds(self, tmp_path):
        assert check(
            one(tmp_path, [0.5, 1.2, 2.9, 1.5, 0.7], quantity="absorbance"), allowlist=_empty()
        ).is_valid
        result = check(
            one(tmp_path, [0.5, 1.2, 5.5, 1.5, 0.7], quantity="absorbance"), allowlist=_empty()
        )
        assert _checks(result, "error") == {"value_out_of_bounds"}

    def test_bounds_share_warning(self, tmp_path):
        rows = [spectrum(f"s{i:02d}", dataset_id="ds-1") for i in range(10)]
        rows += [spectrum(f"t{i:02d}", SPIKE, dataset_id="ds-1") for i in range(2)]
        path = write_collection(tmp_path / "c.parquet", rows, datasets=["ds-1"])
        result = check(path, allowlist=_empty())
        share = [i for i in result.issues if i.check == "value_bounds_share"]
        assert len(share) == 2  # the collection and its dataset
        assert any(f"dataset '{CID}:ds-1'" in i.message for i in share)
        assert all(i.severity == "warning" for i in share)


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def _file(self, tmp_path, values):
        return one(tmp_path, values, dataset_id="ds-1")

    def test_out_of_bounds_waived_by_id(self, tmp_path):
        path = self._file(tmp_path, SPIKE)
        result = check(path, allowlist=_allowlist(_oob({"id": f"{CID}:olivine_gds70"})))
        assert result.is_valid, result.summary()
        assert not result.warnings, result.summary()
        assert [i.check for i in result.waived] == ["value_out_of_bounds"]
        assert "1 issue(s) waived" in result.summary()

    def test_value_scale_waivable_by_exact_id_only(self, tmp_path):
        path = self._file(tmp_path, PERCENT)
        sid = f"{CID}:olivine_gds70"
        by_id = _allowlist({"id": sid, "checks": ["value_scale"], "reason": "documented"})
        assert check(path, allowlist=by_id).is_valid
        assert not check(path, allowlist=_allowlist(_oob({"id": sid}))).is_valid
        for selector in ({"id_prefix": f"{CID}:o"}, {"dataset": f"{CID}:ds-1"}):
            with pytest.raises(ValidationError, match="exact 'id'"):
                AllowlistEntry.model_validate(
                    {**selector, "checks": ["value_scale"], "reason": "x"}
                )

    @pytest.mark.parametrize(
        "check",
        [
            "F9",
            "sentinel_value",
            "non_finite_value",
            "nan_accounting",
            "no_valid_values",
            "wavelength_range",
        ],
    )
    def test_structural_checks_are_not_waivable(self, check):
        with pytest.raises(ValidationError, match="non-waivable"):
            AllowlistEntry.model_validate({"id": f"{CID}:x", "checks": [check], "reason": "x"})

    def test_conformance_error_survives_broad_allowlist(self):
        broad = _allowlist(
            {
                "id_prefix": "conformance_minimal:a",
                "checks": sorted(WAIVABLE_CHECKS),
                "reason": "try to hide everything",
            }
        )
        result = check(CORPUS / "f9_sentinel.parquet", allowlist=broad)
        assert _checks(result, "error") == {"F9"}

    def test_bounds_max_override(self, tmp_path):
        path = self._file(tmp_path, SPIKE)
        sid = f"{CID}:olivine_gds70"
        assert check(path, allowlist=_allowlist(_oob({"id": sid}, {"max": 2.5}))).is_valid
        result = check(path, allowlist=_allowlist(_oob({"id": sid}, {"max": 1.8})))
        assert not result.is_valid
        assert any("allowlisted bounds [-0.2, 1.8]" in e for e in result.errors)
        assert "stale_allowlist_entry" not in _checks(result, "warning")

    def test_bounds_min_override(self, tmp_path):
        path = self._file(tmp_path, [0.04, 0.06, -0.5, 0.12, 0.15])
        sid = f"{CID}:olivine_gds70"
        assert check(path, allowlist=_allowlist(_oob({"id": sid}, {"min": -1.0}))).is_valid
        result = check(path, allowlist=_allowlist(_oob({"id": sid}, {"min": -0.4})))
        assert any("allowlisted bounds [-0.4, 1.5]" in e for e in result.errors)

    def test_prefix_dataset_and_quantity_selectors(self, tmp_path):
        path = self._file(tmp_path, SPIKE)
        for selector in (
            {"id_prefix": f"{CID}:olivine"},
            {"dataset": f"{CID}:ds-1"},
            {"id_prefix": f"{CID}:o", "quantity": "reflectance"},
            {"id_prefix": f"{CID}:o", "technique": "reflectance"},  # v0 field name
        ):
            assert check(path, allowlist=_allowlist(_oob(selector))).is_valid, selector
        other = _allowlist(_oob({"id_prefix": f"{CID}:o", "quantity": "transmittance"}))
        assert not check(path, allowlist=other).is_valid
        # A bare dataset id is not a selector: datasets are "{collection}:{dataset_id}".
        with pytest.raises(ValidationError):
            AllowlistEntry.model_validate(_oob({"dataset": "ds-1"}))

    def test_longest_prefix_wins(self, tmp_path):
        path = self._file(tmp_path, SPIKE)
        allowlist = _allowlist(
            _oob({"id_prefix": f"{CID}:olivine"}, bounds={"max": 2.5}),
            _oob({"id_prefix": f"{CID}:o"}, bounds={"max": 1.8}),
        )
        assert check(path, allowlist=allowlist).is_valid

    def test_precedence_id_then_prefix_then_dataset(self, tmp_path):
        path = self._file(tmp_path, SPIKE)
        by_dataset = _oob({"dataset": f"{CID}:ds-1"}, bounds={"max": 1.7})
        by_prefix = _oob({"id_prefix": f"{CID}:o"}, bounds={"max": 1.9})
        by_id = _oob({"id": f"{CID}:olivine_gds70"}, bounds={"max": 2.5})
        assert check(path, allowlist=_allowlist(by_id, by_prefix, by_dataset)).is_valid
        assert check(path, allowlist=_allowlist(by_dataset, by_prefix, by_id)).is_valid
        result = check(path, allowlist=_allowlist(by_dataset, by_prefix))
        assert any("allowlisted bounds [-0.2, 1.9]" in e for e in result.errors)
        lenient_dataset = _oob({"dataset": f"{CID}:ds-1"}, bounds={"max": 2.5})
        assert not check(path, allowlist=_allowlist(by_prefix, lenient_dataset)).is_valid

    def test_allowlist_only_waives_named_check(self, tmp_path):
        path = self._file(tmp_path, [0.1, 0.2, 1.25, 0.3, 0.4])
        result = check(path, allowlist=_allowlist(_oob({"id": f"{CID}:other"})))
        assert _checks(result, "warning") == {"value_range_suspect", "stale_allowlist_entry"}
        suspect = _allowlist(
            {"id": f"{CID}:olivine_gds70", "checks": ["value_range_suspect"], "reason": "x"}
        )
        result = check(path, allowlist=suspect)
        assert not result.warnings
        assert [i.check for i in result.waived] == ["value_range_suspect"]

    def test_stale_entries_warn(self, tmp_path):
        path = one(tmp_path, [0.04, 0.06, 0.08, 0.12, 0.15])
        allowlist = _allowlist(
            _oob({"id": f"{CID}:olivine_gds70"}),  # matches a clean spectrum: stale
            _oob({"id": f"{CID}:does_not_exist"}),
            _oob({"id_prefix": f"{CID}:zzz"}),
            # Collection absent from this run: could never match, not stale.
            _oob({"id": "ecostress:something"}),
        )
        result = check(path, allowlist=allowlist)
        assert result.is_valid
        stale = [i for i in result.issues if i.check == "stale_allowlist_entry"]
        assert len(stale) == 3
        assert all(i.severity == "warning" for i in stale)
        assert any(f"id_prefix={CID}:zzz" in i.message for i in stale)
        assert not any("ecostress:something" in i.message for i in stale)

    def test_format_profile_ignores_allowlist(self, tmp_path):
        path = one(tmp_path, [0.04, 0.06, 0.08, 0.12, 0.15])
        result = check(path, profile="format", allowlist=_allowlist(_oob({"id": f"{CID}:x"})))
        assert not result.warnings
        assert result.allowlist_files == []

    def test_entry_validation(self):
        ok = {"checks": ["value_out_of_bounds"], "reason": "x"}
        bad_entries = [
            {"id": f"{CID}:b", "checks": ["value_out_of_bounds"]},  # no reason
            {**ok, "id": f"{CID}:b", "reason": "  "},
            ok,  # no selector
            {**ok, "id": f"{CID}:b", "dataset": f"{CID}:d"},
            {**ok, "id_prefix": "usgs"},  # no colon: would silently disable stale detection
            {**ok, "id_prefix": "Bad-Collection:"},
            {**ok, "id_prefix": CID},  # collection id but no colon
            {**ok, "id": f"{CID}:"},
            {**ok, "dataset": "ds-1"},
            {**ok, "id": f"{CID}:b", "quantity": "Reflectance"},
            {**ok, "id": f"{CID}:b", "quantity": "radiance"},
            {**ok, "id": f"{CID}:b", "checks": ["I1"]},
            {**ok, "id": f"{CID}:b", "checks": ["value_range"]},  # renamed long ago
            {**ok, "id": f"{CID}:b", "checks": ["value_range_suspect"], "bounds": {"max": 2}},
            {**ok, "id": f"{CID}:b", "bounds": {"min": 1.0, "max": 0.5}},
            {**ok, "id": f"{CID}:b", "bounds": {}},
            {**ok, "id": f"{CID}:b", "reasons": "typo"},
        ]
        for entry in bad_entries:
            with pytest.raises(ValidationError):
                AllowlistEntry.model_validate(entry)
        AllowlistEntry.model_validate({**ok, "id_prefix": "ecosis:x", "quantity": "emissivity"})

    def test_load_file_errors_name_the_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"format_version": 2, "entries": []}))
        with pytest.raises(ValueError, match="bad.json"):
            Allowlist.load([path], include_default=False)

    def test_packaged_default_allowlist_is_valid_for_v1_ids(self):
        assert DEFAULT_ALLOWLIST_PATH.exists()
        allowlist = Allowlist.load()
        assert allowlist.files == [str(DEFAULT_ALLOWLIST_PATH)]
        data = json.loads(DEFAULT_ALLOWLIST_PATH.read_text())
        assert data["format_version"] == 1
        for entry in data["entries"]:
            assert entry["reason"].strip()
            if "id" in entry:
                collection, local = entry["id"].split(":", 1)
                assert all(c.isalnum() or c in "._~-" for c in local), entry["id"]

    def test_shipped_lawngrass_entry_matches(self, tmp_path):
        lawngrass = "splib07a_LawnGrass_GDS91b_-shiftd_3nm_BECKa_AREF"
        values = [1.0, 0.98, 2.014, 1.01, 0.99]
        path = write_collection(tmp_path / "u.parquet", [spectrum(lawngrass, values)])
        result = check(path)  # default allowlist
        assert result.is_valid, result.summary()
        assert [i.check for i in result.waived] == ["value_out_of_bounds"]

    def test_later_entry_wins_on_equal_specificity(self):
        allowlist = _allowlist(
            _oob({"id": f"{CID}:1"}, bounds={"max": 2}, reason="a"),
            _oob({"id": f"{CID}:1"}, bounds={"max": 3}, reason="b"),
        )
        entry = allowlist.match(f"{CID}:1", None, "reflectance", "value_out_of_bounds")
        assert entry is not None and entry.reason == "b"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class TestAggregatedOutput:
    def _many_bad(self, tmp_path, n=12):
        rows = [spectrum(f"s{i:02d}", SPIKE) for i in range(n)]
        return write_collection(tmp_path / "c.parquet", rows)

    def test_summary_aggregates_repeated_issues(self, tmp_path):
        result = check(self._many_bad(tmp_path), allowlist=_empty())
        summary = result.summary(max_examples=3)
        assert f"[value_out_of_bounds] {CID}: 12" in summary
        assert "… and 9 more" in summary
        assert len(summary.splitlines()) < 20
        assert "value_out_of_bounds" in result.detailed_summary()
        assert len(result.detailed_summary().splitlines()) > 12

    def test_report_dict(self, tmp_path):
        result = check(self._many_bad(tmp_path), allowlist=_empty())
        data = json.loads(json.dumps(result.to_dict(max_examples=2)))
        assert data["valid"] is False
        assert data["error_count"] == 12
        assert len(data["issues"]) == data["error_count"] + data["warning_count"]
        group = next(g for g in data["groups"] if g["check"] == "value_out_of_bounds")
        assert group["count"] == 12 and len(group["examples"]) == 2
        assert {i["rule"] for i in data["issues"]} == {"Q2"}

    def test_plain_string_errors_still_summarised(self):
        result = ValidationResult()
        result.errors.append("something odd")
        assert "[other]" in result.summary()

    def test_clean_summary(self, tmp_path):
        result = check(one(tmp_path, [0.1, 0.2, 0.3, 0.4, 0.5]), allowlist=_empty())
        assert result.summary().startswith("Validation passed with no issues.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestValidateCli:
    def _run(self, *args):
        from click.testing import CliRunner

        from openspeclib.cli import main

        return CliRunner().invoke(main, ["validate", *map(str, args)])

    def test_default_profile_is_format(self, tmp_path):
        path = one(tmp_path, PERCENT)
        result = self._run(path)
        assert result.exit_code == 0, result.output
        assert "profile format" in result.output
        result = self._run(path, "--profile", "openspeclib")
        assert result.exit_code == 1, result.output
        assert "[value_scale]" in result.output

    def test_conformance_failure_exits_1(self):
        result = self._run(CORPUS / "f8_not_monotonic.parquet")
        assert result.exit_code == 1
        assert "[F8]" in result.output

    def test_directory_and_report(self, tmp_path):
        library = tmp_path / "lib"
        library.mkdir()
        for name in ("index_valid.parquet", "valid_minimal.parquet", "valid_full.parquet"):
            shutil.copy(CORPUS / name, library / name)
        report = tmp_path / "out" / "report.json"
        result = self._run(library, "--profile", "openspeclib", "--report", report)
        assert result.exit_code == 0, result.output
        data = json.loads(report.read_text())
        assert data["valid"] is True
        assert data["files_checked"] == 3

    def test_allowlist_option(self, tmp_path):
        path = one(tmp_path, SPIKE)
        q = ("--profile", "openspeclib")
        assert self._run(path, *q, "--no-default-allowlist").exit_code == 1
        allowlist = tmp_path / "allow.json"
        allowlist.write_text(
            json.dumps({"format_version": 1, "entries": [_oob({"id": f"{CID}:olivine_gds70"})]})
        )
        result = self._run(path, *q, "--allowlist", allowlist)
        assert result.exit_code == 0, result.output
        assert "1 issue(s) waived" in result.output

    def test_invalid_allowlist_exits_2(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"format_version": 1, "entries": [{"id": "x"}]}))
        result = self._run(one(tmp_path, SPIKE), "--profile", "openspeclib", "--allowlist", bad)
        assert result.exit_code == 2
        assert "bad.json" in result.output

    def test_no_default_allowlist_and_details(self, tmp_path):
        rows = [spectrum(f"s{i:02d}", SPIKE) for i in range(3)]
        path = write_collection(tmp_path / "c.parquet", rows)
        result = self._run(path, "--profile", "openspeclib", "--no-default-allowlist", "--details")
        assert result.exit_code == 1
        assert result.output.count("ERROR [value_out_of_bounds]") == 3
        assert "Allowlist:" not in result.output

    def test_wavelength_extent_q4(self, tmp_path):
        path = write_collection(tmp_path / "c.parquet", [micron_axis_labelled_nm("bad")])
        assert self._run(path).exit_code == 0  # conformant: the format can't know
        result = self._run(path, "--profile", "openspeclib")
        assert result.exit_code == 1
        assert "[wavelength_extent]" in result.output

    def test_corrupt_file_next_to_valid_ones(self, tmp_path):
        shutil.copy(CORPUS / "valid_minimal.parquet", tmp_path / "a.parquet")
        shutil.copy(CORPUS / "f1_corrupt_page.parquet", tmp_path / "b.parquet")
        result = self._run(tmp_path, "--profile", "openspeclib")
        assert result.exit_code == 1, result.output
        assert "[F1]" in result.output
        assert "2 file(s)" in result.output


def micron_axis_labelled_nm(local, **kw):
    """A spectrum whose 0.4-0.8 um axis is labelled nm (conformant, fails Q4)."""
    row = spectrum(local, **kw)
    row["wavelengths"] = [0.4, 0.5, 0.6, 0.7, 0.8]
    row["wavelength_min_nm"], row["wavelength_max_nm"] = 0.4, 0.8
    return row


class TestRound3:
    def test_q2_entry_does_not_waive_q3_and_goes_stale(self, tmp_path):
        path = one(tmp_path, [0.1, 0.2, 1.25, 0.3, 0.4])  # Q3 only
        result = check(path, allowlist=_allowlist(_oob({"id": f"{CID}:olivine_gds70"})))
        assert _checks(result, "warning") == {"value_range_suspect", "stale_allowlist_entry"}
        assert result.waived == []

    def test_q2_waiver_suppresses_q3_for_the_same_spectrum(self, tmp_path):
        path = one(tmp_path, SPIKE)  # fails Q2 (and would be outside the warning band)
        result = check(path, allowlist=_allowlist(_oob({"id": f"{CID}:olivine_gds70"})))
        assert result.is_valid and not result.warnings
        assert [i.check for i in result.waived] == ["value_out_of_bounds"]

    def test_collection_wide_prefix_is_rejected(self):
        with pytest.raises(ValidationError, match="whole collection"):
            AllowlistEntry.model_validate(_oob({"id_prefix": f"{CID}:"}))
        AllowlistEntry.model_validate(_oob({"id_prefix": f"{CID}:o"}))

    def test_wavelength_extent(self, tmp_path):
        path = write_collection(tmp_path / "c.parquet", [micron_axis_labelled_nm("bad")])
        assert validate(path).is_valid  # format profile: conformant
        result = check(path, allowlist=_empty())
        assert _checks(result, "error") == {"wavelength_extent"}
        issue = result.issues[0]
        assert issue.rule == "Q4"
        assert "[0.4, 0.8] nm" in issue.message

    def test_wavelength_extent_bounds_come_from_rules_json(self, tmp_path):
        from openspeclib.conformance import load_qa_wavelength_nm

        low, high = load_qa_wavelength_nm()
        at_limits = spectrum("limits")
        at_limits["wavelengths"] = [low, high]
        at_limits["values"] = [0.1, 0.2]
        at_limits["wavelength_min_nm"], at_limits["wavelength_max_nm"] = low, high
        above = spectrum("above")
        above["wavelengths"] = [low, high * 1.01]
        above["values"] = [0.1, 0.2]
        above["wavelength_min_nm"], above["wavelength_max_nm"] = low, high * 1.01
        path = write_collection(tmp_path / "c.parquet", [at_limits, above])
        result = check(path, allowlist=_empty())
        assert [i.spectrum_id for i in result.issues if i.check == "wavelength_extent"] == [
            f"{CID}:above"
        ]

    def test_wavelength_extent_waivable_by_exact_id_only(self, tmp_path):
        path = write_collection(tmp_path / "c.parquet", [micron_axis_labelled_nm("bad")])
        entry = {"id": f"{CID}:bad", "checks": ["wavelength_extent"], "reason": "checked"}
        result = check(path, allowlist=_allowlist(entry))
        assert result.is_valid
        assert [i.check for i in result.waived] == ["wavelength_extent"]
        with pytest.raises(ValidationError, match="exact 'id'"):
            AllowlistEntry.model_validate(
                {"id_prefix": f"{CID}:b", "checks": ["wavelength_extent"], "reason": "x"}
            )

    def test_wavelength_extent_counts_towards_bounds_share(self, tmp_path):
        rows = [spectrum(f"s{i:02d}") for i in range(9)]
        rows += [micron_axis_labelled_nm(f"t{i}") for i in range(2)]
        path = write_collection(tmp_path / "c.parquet", rows)
        result = check(path, allowlist=_empty())
        assert "value_bounds_share" in _checks(result, "warning")

    def test_corrupt_file_does_not_stop_a_directory(self, tmp_path):
        shutil.copy(CORPUS / "valid_minimal.parquet", tmp_path / "a.parquet")
        shutil.copy(CORPUS / "f1_corrupt_page.parquet", tmp_path / "b.parquet")
        shutil.copy(CORPUS / "valid_full.parquet", tmp_path / "c.parquet")
        result = check(tmp_path, allowlist=_empty())
        assert result.files_checked == 3
        assert _checks(result, "error") == {"F1"}
        assert "quality_skipped" in _checks(result, "warning")

    @pytest.mark.parametrize(
        "name",
        [
            "valid_dictionary_string.parquet",
            "valid_fixed_size_list.parquet",
            "valid_null_dataset_id.parquet",
            "valid_large_types.parquet",
            "valid_empty.parquet",
        ],
    )
    def test_quality_reads_equivalent_types(self, name):
        result = check(CORPUS / name, allowlist=_empty())
        assert result.is_valid and not result.warnings, result.summary()


def test_cli_malformed_allowlist_fails_even_with_format_profile(tmp_path):
    from click.testing import CliRunner

    from openspeclib.cli import main

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format_version": 1, "entries": [{"id": "x"}]}))
    path = one(tmp_path, [0.1, 0.2, 0.3, 0.4, 0.5])
    result = CliRunner().invoke(main, ["validate", str(path), "--allowlist", str(bad)])
    assert result.exit_code == 2, result.output


def test_newer_minor_is_valid_with_warnings():
    result = validate(CORPUS / "valid_future_minor.parquet")
    assert result.is_valid, result.summary()
    assert _checks(result, "warning") == {"F1", "F3", "F7"}
    older = validate(CORPUS / "f1_f3_f7_unknowns_in_1_0.parquet")
    assert _checks(older, "error") == {"F1", "F3", "F7"}


def test_bounds_share_wording_matches_rule():
    from openspeclib.validate import CHECK_DESCRIPTIONS

    assert "at least 10 spectra of one quantity and at least 10%" in (
        CHECK_DESCRIPTIONS["value_bounds_share"]
    )


def test_directory_named_parquet_and_broken_link_are_errors(tmp_path):
    shutil.copy(CORPUS / "valid_minimal.parquet", tmp_path / "a.parquet")
    (tmp_path / "folder.parquet").mkdir()
    (tmp_path / "dangling.parquet").symlink_to(tmp_path / "missing.parquet")
    result = validate(tmp_path)
    assert result.files_checked == 1
    problems = [i for i in result.issues if i.check == "unreadable_path"]
    assert {i.file for i in problems} == {"folder.parquet", "dangling.parquet"}
    assert any("broken symbolic link" in i.message for i in problems)
    assert not result.is_valid


def test_invalid_utf8_does_not_crash_either_profile():
    for profile in ("format", "openspeclib"):
        result = validate(CORPUS / "f2_invalid_utf8_name.parquet", profile=profile)
        assert _checks(result, "error") == {"F2"}


def test_invalid_utf8_column_name_is_f1_in_a_directory(tmp_path):
    shutil.copy(CORPUS / "f1_invalid_utf8_column_name.parquet", tmp_path / "bad.parquet")
    shutil.copy(CORPUS / "valid_minimal.parquet", tmp_path / "good.parquet")
    for profile in ("format", "openspeclib"):
        result = validate(tmp_path, profile=profile)
        assert result.files_checked == 2
        assert _checks(result, "error") == {"F1"}
        assert "not UTF-8" in result.errors[0]


@pytest.mark.skipif(
    not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0,
    reason="needs a non-root POSIX user (root reads chmod 000 directories)",
)
def test_unreadable_directory_is_reported(tmp_path):
    shutil.copy(CORPUS / "valid_minimal.parquet", tmp_path / "good.parquet")
    locked = tmp_path / "locked"
    locked.mkdir()
    shutil.copy(CORPUS / "valid_full.parquet", locked / "hidden.parquet")
    locked.chmod(0)
    try:
        result = validate(tmp_path)
    finally:
        locked.chmod(0o755)
    assert not result.is_valid
    problems = [i for i in result.issues if i.check == "unreadable_path"]
    assert [i.file for i in problems] == ["locked"]
    assert "directory cannot be read" in problems[0].message
    assert result.files_checked == 1


def test_unexpected_exception_is_reported_per_file(tmp_path, monkeypatch):
    from openspeclib import conformance

    shutil.copy(CORPUS / "valid_minimal.parquet", tmp_path / "a.parquet")
    shutil.copy(CORPUS / "valid_full.parquet", tmp_path / "b.parquet")
    original = conformance.check_collection

    def flaky(path, max_examples=None):
        if Path(path).name == "a.parquet":
            raise RuntimeError("boom")
        return original(path, max_examples=max_examples)

    monkeypatch.setattr(conformance, "check_collection", flaky)
    result = validate(tmp_path)
    assert result.files_checked == 1
    problems = [i for i in result.issues if i.check == "unreadable_path"]
    assert [i.file for i in problems] == ["a.parquet"]
    assert "RuntimeError: boom" in problems[0].message


def test_index_over_newer_minor_collection_is_valid_with_warnings(tmp_path):
    for name in ("valid_future_minor.parquet", "index_over_future_minor.parquet"):
        shutil.copy(CORPUS / name, tmp_path / name)
    result = validate(tmp_path)
    assert result.is_valid, result.summary()
    assert _checks(result, "warning") == {"F1", "F3", "F7", "I1"}


def test_unexpected_quality_error_is_reported_not_raised(tmp_path, monkeypatch):
    import shutil

    from openspeclib import validate as validate_module

    shutil.copy(
        Path(__file__).parent / "conformance" / "fixtures" / "valid_full.parquet",
        tmp_path / "valid_full.parquet",
    )

    def boom(self, *args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(validate_module._QualityChecker, "_check_parquet", boom)
    result = validate_module.validate(tmp_path, profile="openspeclib")
    assert any(i.check == "unreadable_path" and "KeyError" in i.message for i in result.issues)


def test_exact_median_decides_q1(tmp_path):
    from openspeclib.validate import _exact_medians

    rows = pa.array([0, 0, 0, 0, 1, 1, 1, 2], pa.int64())
    values = pa.array([4.0, 1.0, None, 2.0, 1.0, 3.0, 2.0, None], pa.float32())
    medians = _exact_medians(rows, values, pa.array([0, 1, 2, 3], pa.int64())).to_pylist()
    assert medians == [2.0, 2.0, None, None]
