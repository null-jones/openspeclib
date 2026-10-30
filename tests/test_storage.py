"""Tests for the Format v1 Parquet storage layer."""

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import make_collection_info, make_record

from openspeclib import storage
from openspeclib.conformance import check_collection, check_index
from openspeclib.models import (
    DatasetInfo,
    Geometry,
    Material,
    MaterialCategory,
    Measurement,
    Quantity,
    Sample,
    SpectrumRecord,
    WavelengthUnit,
)
from openspeclib.storage import (
    SENTINEL_ABS_THRESHOLD,
    compute_value_stats,
    iter_records,
    read_footer,
    read_index_footer,
    write_collection,
    write_index,
)


def _write(tmp_path: Path, records, name="usgs_splib07", **kwargs) -> Path:
    info = kwargs.pop("info", None) or make_collection_info(name)
    path = tmp_path / f"{name}.parquet"
    write_collection(records, path, info, **kwargs)
    return path


def _assert_conformant(path: Path) -> None:
    report = check_collection(path)
    assert report.conformant, report.to_dict()
    assert not set(report.warned) - {"R5"}, report.warned


class TestWriteCollection:
    def test_roundtrip(self, tmp_path: Path, sample_spectrum: SpectrumRecord) -> None:
        path = _write(tmp_path, [sample_spectrum])
        (restored,) = list(iter_records(path))
        expected = sample_spectrum.model_copy(deep=True)
        assert restored.model_dump(exclude={"spectral_data"}) == expected.model_dump(
            exclude={"spectral_data"}
        )
        sd = restored.spectral_data
        assert sd.wavelengths == expected.spectral_data.wavelengths
        assert sd.values == pytest.approx(expected.spectral_data.values, rel=1e-6)
        _assert_conformant(path)

    def test_core_columns_and_types(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [make_record()])
        schema = pq.read_schema(path)
        for column in storage.CORE_COLUMNS:
            field = schema.field(column.name)
            assert field.type.equals(column.type) or str(field.type).replace(
                "element", "item"
            ) == str(column.type)
        assert schema.field("values").type.value_type == pa.float32()
        assert schema.field("wavelengths").type.value_type == pa.float64()

    def test_only_filled_extensions_declared(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [make_record()])
        footer = read_footer(path)
        names = pq.read_schema(path).names
        assert footer["extensions"] == []
        assert "sample_id" not in names and "extra" not in names
        assert "bandpass_fwhm" not in names and "quality_notes" not in names

    def test_extensions_follow_data(self, tmp_path: Path, sample_spectrum: SpectrumRecord) -> None:
        rec = make_record(
            "b", bandpass_fwhm=[0.01] * 5, values_stddev=[0.001] * 5, extra={"k": [1, None]}
        )
        path = _write(tmp_path, [sample_spectrum, rec])
        footer = read_footer(path)
        assert footer["extensions"] == [
            "material",
            "sample",
            "measurement",
            "bandpass",
            "uncertainty",
            "quality",
        ]
        names = pq.read_schema(path).names
        assert {"bandpass_fwhm", "values_stddev", "extra", "material_formula"} <= set(names)
        assert "sample_preparation" not in names
        rows = {r["id"]: r for r in pq.read_table(path).to_pylist()}
        assert rows[sample_spectrum.id]["bandpass_fwhm"] is None
        assert rows[sample_spectrum.id]["extra"] is None
        assert json.loads(rows[rec.id]["extra"]) == {"k": [1, None]}
        _assert_conformant(path)

    def test_footer(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [make_record()], created="2026-10-29T12:00:00Z")
        footer = read_footer(path)
        assert footer["format_version"] == "1.0.0"
        assert footer["created"] == "2026-10-29T12:00:00Z"
        assert footer["generator"].startswith("openspeclib ")
        assert footer["collection"]["id"] == "usgs_splib07"
        assert footer["datasets"] == {}
        metadata = pq.read_metadata(path).metadata
        assert b"openspeclib" in metadata and b"openspeclib_index" not in metadata

    def test_created_defaults_to_rfc3339_utc(self, tmp_path: Path) -> None:
        created = read_footer(_write(tmp_path, [make_record()]))["created"]
        assert dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")

    def test_source_date_epoch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
        assert storage.utc_timestamp() == "1970-01-01T00:00:00Z"

    def test_sorted_with_row_groups_and_stats(self, tmp_path: Path) -> None:
        records = [make_record(f"s{i:03d}") for i in reversed(range(250))]
        path = _write(tmp_path, records)
        pf = pq.ParquetFile(path)
        ids = pf.read(columns=["id"])["id"].to_pylist()
        assert ids == sorted(ids)
        assert pf.metadata.num_row_groups == 3
        assert pf.metadata.row_group(0).num_rows == storage.DEFAULT_ROW_GROUP_SIZE
        rg = pf.metadata.row_group(0)
        by_path = {rg.column(i).path_in_schema: rg.column(i) for i in range(rg.num_columns)}
        assert by_path["id"].is_stats_set
        assert by_path["wavelength_min_nm"].is_stats_set
        assert not by_path["name"].is_stats_set
        assert not by_path["license"].is_stats_set
        for leaf in ("values.list.element", "wavelengths.list.element"):
            assert "BYTE_STREAM_SPLIT" in by_path[leaf].encodings
            assert "RLE_DICTIONARY" not in by_path[leaf].encodings
            assert by_path[leaf].compression == "ZSTD"
        assert "RLE_DICTIONARY" in by_path["material_category"].encodings
        _assert_conformant(path)

    def test_nan_preserved(self, tmp_path: Path) -> None:
        rec = make_record(values=[0.1, float("nan"), 0.3, float("nan"), 0.5])
        path = _write(tmp_path, [rec])
        values = pq.read_table(path)["values"].to_pylist()[0]
        assert [math.isnan(v) for v in values] == [False, True, False, True, False]

    @pytest.mark.parametrize(
        ("unit", "axis", "expected"),
        [
            (WavelengthUnit.NANOMETERS, [400.0, 2500.0], (400.0, 2500.0)),
            (WavelengthUnit.MICROMETERS, [0.35, 2.5], (350.0, 2500.0)),
            (WavelengthUnit.WAVENUMBERS, [2000.0, 220.0], (5000.0, 1e7 / 220.0)),
        ],
    )
    def test_extent_in_nm(self, tmp_path: Path, unit, axis, expected) -> None:
        path = _write(tmp_path, [make_record(unit=unit, wavelengths=axis, values=[0.1, 0.2])])
        table = pq.read_table(path, columns=["wavelength_min_nm", "wavelength_max_nm"])
        assert table["wavelength_min_nm"][0].as_py() == pytest.approx(expected[0])
        assert table["wavelength_max_nm"][0].as_py() == pytest.approx(expected[1])
        _assert_conformant(path)

    def test_empty_strings_fall_back(self, tmp_path: Path) -> None:
        rec = make_record().model_copy(
            update={"name": "", "material": Material(name=" ", category=MaterialCategory.OTHER)}
        )
        table = pq.read_table(_write(tmp_path, [rec]))
        assert table["material_name"][0].as_py() == "olivine_gds70"
        assert table["name"][0].as_py() == "olivine_gds70"

    def test_empty_optional_values_are_null(self, tmp_path: Path) -> None:
        rec = make_record().model_copy(update={"sample": Sample(id="", description="d")})
        table = pq.read_table(_write(tmp_path, [rec]))
        assert "sample_id" not in table.column_names
        assert table["sample_description"][0].as_py() == "d"

    def test_dates_roundtrip(self, tmp_path: Path) -> None:
        rec = make_record().model_copy(
            update={
                "measurement": Measurement(
                    quantity=Quantity.EMISSIVITY,
                    geometry=Geometry.DIRECTIONAL,
                    date=dt.date(2020, 1, 2),
                    processing=["averaged"],
                )
            }
        )
        (restored,) = iter_records(_write(tmp_path, [rec]))
        assert restored.measurement.date == dt.date(2020, 1, 2)
        assert restored.measurement.processing == ["averaged"]
        assert restored.measurement.geometry == Geometry.DIRECTIONAL

    def test_duplicate_ids_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            _write(tmp_path, [make_record("a"), make_record("a")])

    def test_wrong_collection_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="collection"):
            _write(tmp_path, [make_record(collection="ecostress")])

    def test_unknown_dataset_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="dataset_id"):
            _write(tmp_path, [make_record(dataset_id="nope")])

    def test_undefined_license_ref_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="LicenseRef"):
            _write(tmp_path, [make_record(license="LicenseRef-Other")])

    def test_empty_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no spectra"):
            _write(tmp_path, [])

    def test_aggregated_collection(self, tmp_path: Path) -> None:
        info = make_collection_info(
            "ecosis",
            license="NOASSERTION",
            license_refs={},
            inclusion="subset",
            inclusion_notes="some",
        )
        datasets = {
            "p1": DatasetInfo(id="p1", title="P1", license="CC-BY-4.0", spectra_included=1),
            "p2": DatasetInfo(id="p2", title="P2", license="NOASSERTION", license_note="none"),
        }
        recs = [
            make_record("a", collection="ecosis", license="CC-BY-4.0", dataset_id="p1"),
            make_record("b", collection="ecosis", license="NOASSERTION", dataset_id="p2"),
        ]
        path = _write(tmp_path, recs, name="ecosis", info=info, datasets=datasets)
        footer = read_footer(path)
        assert footer["datasets"]["p1"] == {
            "title": "P1",
            "license": "CC-BY-4.0",
            "spectra_included": 1,
        }
        _assert_conformant(path)


class TestIndex:
    def test_index_over_two_collections(self, tmp_path: Path, sample_spectrum) -> None:
        a = _write(tmp_path, [sample_spectrum, make_record("x")])
        info = make_collection_info("ecostress")
        b = _write(
            tmp_path,
            [make_record("e1", collection="ecostress", extra={"divisor": 100})],
            name="ecostress",
            info=info,
        )
        index_path = tmp_path / storage.INDEX_FILENAME
        footer = write_index([a, b], index_path, release="9.9.9")
        assert footer == read_index_footer(index_path)
        assert footer["release"] == "9.9.9"
        assert [c["file"] for c in footer["collections"]] == [a.name, b.name]
        assert footer["collections"][0]["metadata"] == read_footer(a)
        assert footer["collections"][0]["num_spectra"] == 2
        assert len(footer["collections"][0]["sha256"]) == 64

        table = pq.read_table(index_path)
        names = set(table.column_names)
        assert {"num_points", "file", "extra", "sample_id"} <= names
        assert not names & set(storage.ARRAY_COLUMNS)
        assert table["id"].to_pylist() == sorted(table["id"].to_pylist())
        assert set(table["num_points"].to_pylist()) == {5}
        metadata = pq.read_metadata(index_path).metadata
        assert b"openspeclib" not in metadata

        report = check_index(index_path)
        assert report.conformant, report.to_dict()

    def test_read_footer_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.parquet"
        pq.write_table(pa.table({"a": [1]}), path)
        with pytest.raises(ValueError, match="openspeclib"):
            read_footer(path)
        with pytest.raises(ValueError, match="openspeclib_index"):
            read_index_footer(path)


def test_schema_matches_columns_json() -> None:
    spec_dir = Path(__file__).resolve().parent.parent / "schemas" / "format-v1"
    spec = json.loads((spec_dir / "columns.json").read_text())["columns"]
    by_name = {c["name"]: c for c in spec}
    assert set(by_name) == set(storage.COLUMNS)
    for name, column in storage.COLUMNS.items():
        assert by_name[name]["nullable"] == column.nullable, name
        assert by_name[name]["extension"] == column.extension, name


def test_wavelength_extent_rejects_unknown_unit() -> None:
    with pytest.raises(ValueError):
        storage.wavelength_extent_nm(np.array([1.0, 2.0]), "furlong")


# ---------------------------------------------------------------------------
# Vectorised value statistics
# ---------------------------------------------------------------------------


def _reference_stats(values, threshold=SENTINEL_ABS_THRESHOLD):
    """Pure-Python reference for compute_value_stats (one spectrum)."""
    if values is None:
        return 0, 0, 0, 0, None, None
    nan = sum(1 for v in values if v is not None and math.isnan(v))
    other = sum(1 for v in values if v is None or math.isinf(v))
    sentinel = sum(1 for v in values if v is not None and math.isfinite(v) and abs(v) > threshold)
    valid = [v for v in values if v is not None and math.isfinite(v) and abs(v) <= threshold]
    return (
        len(values),
        nan,
        other,
        sentinel,
        min(valid) if valid else None,
        max(valid) if valid else None,
    )


def _as_tuples(stats):
    return list(
        zip(
            stats.n_values,
            stats.n_nan,
            stats.n_non_finite,
            stats.n_sentinel,
            stats.min,
            stats.max,
        )
    )


def test_compute_value_stats_crafted_cases() -> None:
    nan, inf = float("nan"), float("inf")
    lists = [
        [0.1, 0.5, 0.9],  # clean
        [0.1, nan, nan, 0.4],  # NaN bands
        [-1.23e34, 0.2, -1.23e34, 0.3],  # USGS sentinel
        [1e11, 0.5],  # just above the threshold
        [9e9, 0.5],  # large but below the threshold — a range problem, not a sentinel
        [nan, nan],  # all missing
        [0.2, inf, -inf],  # infinities
        [0.2, None, 0.3],  # null element
        [None],  # only a null element
        [None, None, nan],  # nulls and NaN, nothing valid
        [],  # empty
        None,  # null list
        [55.0, 98.0, 12.0],  # percent scale
        [-0.02, 0.0, 1.05],  # noise near zero, slight overshoot
    ]
    array = pa.array(lists, type=pa.list_(pa.float32()))
    stats = compute_value_stats(array)

    expected = [_reference_stats(v) for v in lists]
    got = _as_tuples(stats)
    for g, e, raw in zip(got, expected, lists):
        assert g[:4] == e[:4], raw
        for actual, ref in zip(g[4:], e[4:]):
            if ref is None:
                assert actual is None, raw
            else:
                assert actual == pytest.approx(ref, rel=1e-6), raw

    # Spot-check the semantics the validator relies on.
    assert stats.n_sentinel[2] == 2 and stats.min[2] == pytest.approx(0.2)
    assert stats.n_sentinel[3] == 1 and stats.n_sentinel[4] == 0
    assert stats.max[4] == pytest.approx(9e9, rel=1e-6)
    assert stats.min[5] is None and stats.n_nan[5] == 2
    assert stats.n_non_finite[6] == 2 and stats.n_non_finite[7] == 1
    assert stats.n_non_finite[8] == 1 and stats.min[8] is None and stats.median[8] is None
    assert stats.n_non_finite[9] == 2 and stats.n_nan[9] == 1 and stats.max[9] is None
    assert stats.n_values[10] == 0 and stats.n_values[11] == 0


def test_compute_value_stats_all_null_lists_do_not_crash() -> None:
    for lists in ([[None]], [[None], [1.0]], [[1.0], [None, None]]):
        array = pa.array(lists, type=pa.list_(pa.float32()))
        stats = compute_value_stats(array, lower=[-0.2] * len(lists), upper=[1.5] * len(lists))
        assert stats.n_sentinel == [0] * len(lists)
        assert stats.n_below == [0] * len(lists) and stats.n_above == [0] * len(lists)
        assert stats.n_non_finite == [sum(v is None for v in row) for row in lists]


def test_compute_value_stats_bounds_and_median() -> None:
    lists = [
        [-0.2, 0.1, 1.5, 0.3],  # exactly at the (float32-rounded) bounds
        [-0.3, 0.1, 1.6, 2.0, float("nan"), -1.23e34],  # 1 below, 2 above; NaN/sentinel ignored
        [50.0, 60.0, 70.0],
        [0.1, 0.2, 0.3],  # unbounded row
    ]
    array = pa.array(lists, type=pa.list_(pa.float32()))
    inf = float("inf")
    stats = compute_value_stats(array, lower=[-0.2, -0.2, -0.2, -inf], upper=[1.5, 1.5, 1.5, inf])
    assert stats.n_below == [0, 1, 0, 0]
    assert stats.n_above == [0, 2, 3, 0]
    assert stats.median[2] == pytest.approx(60.0)
    assert stats.median[3] == pytest.approx(0.2)
    # Without bounds nothing is counted.
    unbounded = compute_value_stats(array)
    assert unbounded.n_below == [0] * 4 and unbounded.n_above == [0] * 4
    with pytest.raises(ValueError, match="entries"):
        compute_value_stats(array, lower=[0.0])


def test_compute_value_stats_matches_reference_on_random_data() -> None:
    import random

    rng = random.Random(1234)
    specials = [float("nan"), -1.23e34, float("inf"), 1e36]
    lists = []
    for _ in range(300):
        n = rng.randint(1, 60)
        row = [rng.uniform(-0.5, 2.0) * rng.choice([1, 1, 1, 100]) for _ in range(n)]
        for _ in range(rng.randint(0, 5)):
            row[rng.randrange(n)] = rng.choice(specials)
        lists.append(row)
    array = pa.chunked_array(
        [
            pa.array(lists[:150], type=pa.list_(pa.float32())),
            pa.array(lists[150:], type=pa.list_(pa.float32())),
        ]
    )
    stats = compute_value_stats(array)

    # Reference computed on the float32-rounded values the file would hold.
    rounded = pa.array(lists, type=pa.list_(pa.float32())).to_pylist()
    for got, ref in zip(_as_tuples(stats), [_reference_stats(v) for v in rounded]):
        assert got == ref


# ---------------------------------------------------------------------------
# The writer never writes a non-conformant file
# ---------------------------------------------------------------------------


def _bypass(record: SpectrumRecord, **spectral) -> SpectrumRecord:
    """Inject spectral data without validation (as model_copy(update=...) does)."""
    sd = record.spectral_data.model_copy(update=spectral)
    return record.model_copy(update={"spectral_data": sd})


class TestWriterRefusesNonConformant:
    @pytest.mark.parametrize(
        ("spectral", "rule"),
        [
            ({"wavelengths": [0.35, 0.45, 0.40, 0.50, 0.55]}, "F8"),
            ({"wavelengths": [0.35, 0.40, 0.40, 0.50, 0.55]}, "F8"),
            ({"values": [0.1, float("inf"), 0.3, 0.4, 0.5]}, "F9"),
            ({"values": [0.1, -1.23e34, 0.3, 0.4, 0.5]}, "F9"),
            ({"values": [0.1, 1e39, 0.3, 0.4, 0.5]}, "F9"),  # float32 overflow -> inf
            ({"bandpass_fwhm": [0.01, -0.01, 0.01, 0.01, 0.01]}, "F13"),
        ],
    )
    def test_bad_arrays_raise_and_leave_no_file(self, tmp_path: Path, spectral, rule) -> None:
        bad = _bypass(make_record(), **spectral)
        with pytest.raises(storage.NonConformantFileError) as excinfo:
            _write(tmp_path, [bad])
        assert rule in [r for r, _ in excinfo.value.failures]
        assert rule in str(excinfo.value)
        assert "usgs_splib07:olivine_gds70" in str(excinfo.value)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "license", ["MIT and Apache-2.0", "Creative Commons Attribution", "NOASSERTION OR MIT"]
    )
    def test_bad_licence_raises(self, tmp_path: Path, license: str) -> None:
        bad = make_record().model_copy(update={"license": license})
        with pytest.raises(ValueError, match="SPDX"):
            _write(tmp_path, [bad])
        assert list(tmp_path.iterdir()) == []

    def test_dataset_doi_as_url_raises(self, tmp_path: Path) -> None:
        info = make_collection_info("ecosis", license="NOASSERTION", license_refs={})
        ds = DatasetInfo(id="p", title="P").model_copy(
            update={"doi": "https://doi.org/10.21232/C22M27"}
        )
        rec = make_record(collection="ecosis", license="NOASSERTION", dataset_id="p")
        with pytest.raises(ValueError, match="doi"):
            _write(tmp_path, [rec], name="ecosis", info=info, datasets={"p": ds})

    def test_dataset_key_must_match_id(self, tmp_path: Path) -> None:
        info = make_collection_info("ecosis", license="NOASSERTION", license_refs={})
        rec = make_record(collection="ecosis", license="NOASSERTION", dataset_id="p")
        with pytest.raises(ValueError, match="holds DatasetInfo"):
            _write(
                tmp_path,
                [rec],
                name="ecosis",
                info=info,
                datasets={"p": DatasetInfo(id="q", title="Q")},
            )

    def test_unused_datasets_left_out(self, tmp_path: Path) -> None:
        info = make_collection_info("ecosis", license="NOASSERTION", license_refs={})
        datasets = {
            "p": DatasetInfo(id="p", title="P", spectra_included=1),
            "unused": DatasetInfo(id="unused", title="U"),
        }
        rec = make_record(collection="ecosis", license="NOASSERTION", dataset_id="p")
        path = _write(tmp_path, [rec], name="ecosis", info=info, datasets=datasets)
        assert set(read_footer(path)["datasets"]) == {"p"}
        _assert_conformant(path)

    def test_spectra_included_mismatch_raises(self, tmp_path: Path) -> None:
        info = make_collection_info("ecosis", license="NOASSERTION", license_refs={})
        datasets = {"p": DatasetInfo(id="p", title="P", spectra_included=5)}
        rec = make_record(collection="ecosis", license="NOASSERTION", dataset_id="p")
        with pytest.raises(ValueError, match="F12"):
            _write(tmp_path, [rec], name="ecosis", info=info, datasets=datasets)


class TestFooterAndEncoding:
    def test_footer_is_not_duplicated_in_arrow_schema(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [make_record()])
        kv = pq.read_metadata(path).metadata
        assert set(kv) == {b"openspeclib"}
        # Readers following spec section 9 still find it on the Arrow schema.
        assert b"openspeclib" in pq.ParquetFile(path).schema_arrow.metadata
        assert read_footer(path)["collection"]["id"] == "usgs_splib07"

    def test_index_footer_not_duplicated(self, tmp_path: Path) -> None:
        a = _write(tmp_path, [make_record()])
        index = tmp_path / storage.INDEX_FILENAME
        write_index([a], index)
        assert set(pq.read_metadata(index).metadata) == {b"openspeclib_index"}

    def test_statistics_exactly_on_listed_columns(self, tmp_path: Path, sample_spectrum) -> None:
        rec = make_record("d", dataset_id=None)
        path = _write(tmp_path, [sample_spectrum, rec])
        rg = pq.ParquetFile(path).metadata.row_group(0)
        with_stats = {
            rg.column(i).path_in_schema for i in range(rg.num_columns) if rg.column(i).is_stats_set
        }
        assert with_stats == set(storage.STATISTICS_COLUMNS)

    def test_bandpass_and_stddev_use_byte_stream_split(self, tmp_path: Path) -> None:
        rec = make_record(bandpass_fwhm=[0.01] * 5, values_stddev=[0.001] * 5)
        rg = pq.ParquetFile(_write(tmp_path, [rec])).metadata.row_group(0)
        by_path = {rg.column(i).path_in_schema: rg.column(i) for i in range(rg.num_columns)}
        for leaf in ("bandpass_fwhm.list.element", "values_stddev.list.element"):
            assert "BYTE_STREAM_SPLIT" in by_path[leaf].encodings
            assert "RLE_DICTIONARY" not in by_path[leaf].encodings

    def test_extra_is_sorted_strict_json(self) -> None:
        assert storage.encode_extra({"b": 1, "a": {"z": 2, "y": 3}}) == '{"a":{"y":3,"z":2},"b":1}'
        assert storage.encode_extra({"x": float("nan"), "y": [float("inf")]}) == (
            '{"x":null,"y":[null]}'
        )
        assert storage.encode_extra({}) is None

    def test_extra_rejects_unserialisable(self) -> None:
        with pytest.raises(TypeError):
            storage.encode_extra({"x": object()})

    def test_index_x_column_type_conflict(self, tmp_path: Path) -> None:
        a = _write(tmp_path, [make_record()])
        b = _write(
            tmp_path,
            [make_record("e", collection="ecostress")],
            name="ecostress",
            info=make_collection_info("ecostress"),
        )
        for path, value in ((a, pa.array([1], pa.int64())), (b, pa.array(["1"], pa.string()))):
            kv = pq.read_metadata(path).metadata
            table = pq.read_table(path).append_column("x_note", value)
            table = table.replace_schema_metadata({b"openspeclib": kv[b"openspeclib"]})
            pq.write_table(table, path)
        with pytest.raises(ValueError, match="x_note"):
            write_index([a, b], tmp_path / storage.INDEX_FILENAME)
