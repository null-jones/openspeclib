"""Tests for the fast Format v1 reader (``openspeclib.open`` / ``open_index``)."""

import functools
import http.server
import math
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from conftest import make_collection_info, make_record

import openspeclib
from openspeclib import storage
from openspeclib.models import (
    CollectionMetadata,
    DatasetInfo,
    MaterialCategory,
    Quantity,
    WavelengthUnit,
)
from openspeclib.reader import Collection, Index


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """A small release: USGS-like (um), ECOSTRESS-like (emissivity) and EcoSIS-like."""
    from openspeclib.combine import build_library

    usgs = [
        make_record(f"olivine_{i:03d}", values=[0.1, 0.2, float("nan"), 0.4, 0.5])
        for i in range(150)
    ] + [
        make_record(
            "quartz",
            category=MaterialCategory.ROCK,
            bandpass_fwhm=[0.01] * 5,
            extra={"note": "x"},
        )
    ]
    usgs[0] = usgs[0].model_copy(
        update={"material": usgs[0].material.model_copy(update={"name": "Forsterite"})}
    )
    eco = [
        make_record(
            "calcite",
            collection="ecostress",
            quantity=Quantity.EMISSIVITY,
            unit=WavelengthUnit.WAVENUMBERS,
            wavelengths=[2000.0, 1000.0, 400.0],
            values=[0.9, 0.95, 0.97],
        )
    ]
    ecosis = [
        make_record(
            f"leaf{i}",
            collection="ecosis",
            unit=WavelengthUnit.NANOMETERS,
            wavelengths=[350.0, 1000.0, 2500.0],
            values=[0.05, 0.5, 0.2],
            category=MaterialCategory.VEGETATION,
            license="CC-BY-4.0",
            dataset_id="pkg",
        )
        for i in range(3)
    ]
    ecosis_meta = CollectionMetadata(
        collection=make_collection_info(
            "ecosis", license="NOASSERTION", inclusion="subset", inclusion_notes="n"
        ),
        datasets={"pkg": DatasetInfo(id="pkg", title="P", license="CC-BY-4.0")},
    )
    out = tmp_path / "release"
    build_library(
        {
            "usgs_splib07": (usgs, CollectionMetadata(collection=make_collection_info())),
            "ecostress": (
                eco,
                CollectionMetadata(collection=make_collection_info("ecostress")),
            ),
            "ecosis": (ecosis, ecosis_meta),
        },
        out,
        release="0.0.0-test",
    )
    return out


class TestCollection:
    def test_open_and_metadata(self, release: Path) -> None:
        lib = openspeclib.open(release / "usgs_splib07.parquet")
        assert isinstance(lib, Collection)
        assert lib.metadata["collection"]["id"] == "usgs_splib07"
        assert lib.collection_id == "usgs_splib07"
        assert len(lib) == 151

    def test_get(self, release: Path) -> None:
        lib = openspeclib.open(release / "usgs_splib07.parquet")
        s = lib.get("usgs_splib07:olivine_120")
        assert s.id == "usgs_splib07:olivine_120"
        assert s.wavelengths.dtype == np.float64
        assert s.values.dtype == np.float32
        assert s.wavelength_unit == "um"
        assert math.isnan(s.values[2])
        assert s.metadata["material_name"] == "Olivine"
        assert s.quantity == "reflectance"
        np.testing.assert_allclose(s.wavelengths_nm(), [350, 400, 450, 500, 550])
        assert s.bandpass_fwhm is None

        q = lib.get("usgs_splib07:quartz")
        assert q.bandpass_fwhm is not None and len(q.bandpass_fwhm) == 5
        assert q.metadata["extra"] == {"note": "x"}

    def test_get_missing(self, release: Path) -> None:
        lib = openspeclib.open(release / "usgs_splib07.parquet")
        with pytest.raises(KeyError):
            lib.get("usgs_splib07:nope")
        with pytest.raises(KeyError):
            lib.get("usgs_splib07:zzz")

    def test_get_uses_one_row_group(self, release: Path, monkeypatch) -> None:
        lib = openspeclib.open(release / "usgs_splib07.parquet")
        assert lib._pf is not None and lib._pf.metadata.num_row_groups == 2
        calls: list[int] = []
        original = lib._pf.read_row_group

        def spy(i, *args, **kwargs):
            calls.append(i)
            return original(i, *args, **kwargs)

        monkeypatch.setattr(lib._pf, "read_row_group", spy)
        lib.get("usgs_splib07:quartz")
        assert len(calls) == 1

    def test_wavenumber_axis(self, release: Path) -> None:
        s = openspeclib.open(release / "ecostress.parquet").get("ecostress:calcite")
        np.testing.assert_allclose(s.wavelengths_nm(), [5000.0, 10000.0, 25000.0])

    def test_search(self, release: Path) -> None:
        lib = openspeclib.open(release / "usgs_splib07.parquet")
        assert lib.search(material="forster").num_rows == 1
        assert lib.search(material="OLIVINE").num_rows == 151  # material_name or name
        assert lib.search(category="rock")["id"].to_pylist() == ["usgs_splib07:quartz"]
        assert lib.search(category=["rock", "mineral"]).num_rows == 151
        assert lib.search(quantity="emissivity").num_rows == 0
        assert lib.search(wavelength_range_nm=(360, 540)).num_rows == 151
        assert lib.search(wavelength_range_nm=(300, None)).num_rows == 0
        assert lib.search(dataset_id="pkg").num_rows == 0
        result = lib.search()
        assert result.num_rows == 151
        assert "values" not in result.column_names

    def test_to_table_and_pandas(self, release: Path) -> None:
        lib = openspeclib.open(release / "usgs_splib07.parquet")
        t = lib.to_table(columns=["id", "values"], filter=pc.field("material_category") == "rock")
        assert t.num_rows == 1 and t.column_names == ["id", "values"]
        pd = pytest.importorskip("pandas")
        df = lib.to_pandas()
        assert isinstance(df, pd.DataFrame) and "values" not in df.columns
        assert "values" in lib.to_pandas(include_arrays=True).columns

    def test_rejects_other_major_version(self, tmp_path: Path, release: Path) -> None:
        table = pq.read_table(release / "ecostress.parquet")
        meta = dict(table.schema.metadata)
        meta[b"openspeclib"] = meta[b"openspeclib"].replace(b'"1.0.0"', b'"2.0.0"')
        pq.write_table(table.replace_schema_metadata(meta), tmp_path / "v2.parquet")
        with pytest.raises(ValueError, match="format_version"):
            openspeclib.open(tmp_path / "v2.parquet")

    def test_rejects_non_collection(self, release: Path) -> None:
        with pytest.raises(ValueError, match="not an OpenSpecLib collection"):
            openspeclib.open(release / "openspeclib-index.parquet")


class TestIndex:
    def test_open_index(self, release: Path) -> None:
        index = openspeclib.open_index(release / storage.INDEX_FILENAME)
        assert isinstance(index, Index)
        assert index.metadata["release"] == "0.0.0-test"
        assert len(index) == 155
        assert set(index.collections) == {"usgs_splib07", "ecostress", "ecosis"}

    def test_search_across_collections(self, release: Path) -> None:
        index = openspeclib.open_index(release / storage.INDEX_FILENAME)
        assert index.search(quantity="emissivity")["id"].to_pylist() == ["ecostress:calcite"]
        assert index.search(dataset_id="pkg").num_rows == 3
        assert index.search(collection="ecosis").num_rows == 3
        # 400-500 nm is covered by the USGS (350-550) and EcoSIS (350-2500) axes only.
        hits = index.search(wavelength_range_nm=(400, 500))
        assert set(hits["collection"].to_pylist()) == {"usgs_splib07", "ecosis"}
        assert index.search(category="vegetation", material="olivine").num_rows == 3

    def test_get_opens_right_file(self, release: Path) -> None:
        index = openspeclib.open_index(release / storage.INDEX_FILENAME)
        s = index.get("ecosis:leaf1")
        assert s.metadata["dataset_id"] == "pkg"
        assert index.get("ecostress:calcite").wavelength_unit == "cm-1"
        with pytest.raises(KeyError):
            index.get("ecosis:nope")
        assert index.open_collection("ecosis") is index.open_collection("ecosis.parquet")


@pytest.fixture
def http_release(release: Path) -> Iterator[str]:
    """Serve the release directory over HTTP (with Range support via DuckDB's client)."""
    handler = functools.partial(_RangeHandler, directory=str(release))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


# (file name, bytes sent) for every GET the test server answers.
SERVED: list[tuple[str, int]] = []


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Minimal static handler that honours single byte-range requests."""

    def do_GET(self) -> None:  # noqa: N802
        f = self.send_head()
        if f is None:
            return
        data = f.read()
        f.close()
        if self.command == "GET":
            SERVED.append((Path(self.path).name, len(data)))
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def send_head(self):  # type: ignore[no-untyped-def]
        header = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not header or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        start_s, end_s = header.removeprefix("bytes=").split("-")
        start = int(start_s) if start_s else size - int(end_s)
        end = int(end_s) if start_s and end_s else size - 1
        fh = open(path, "rb")
        fh.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        import io

        data = fh.read(end - start + 1)
        fh.close()
        return io.BytesIO(data)

    def copyfile(self, source, outputfile):  # type: ignore[no-untyped-def]
        outputfile.write(source.read())


def _remote_available() -> bool:
    try:
        import duckdb

        duckdb.connect().execute("LOAD httpfs")
    except Exception:  # noqa: BLE001 - any failure means remote reads can't be tested here
        return False
    return True


@pytest.mark.skipif(not _remote_available(), reason="duckdb with httpfs not available")
class TestRemote:
    def test_remote_collection(self, http_release: str) -> None:
        lib = openspeclib.open(http_release + "usgs_splib07.parquet")
        assert lib.remote
        assert lib.metadata["collection"]["id"] == "usgs_splib07"
        s = lib.get("usgs_splib07:olivine_007")
        assert s.values.dtype == np.float32 and math.isnan(s.values[2])
        assert lib.search(category="rock").num_rows == 1
        with pytest.raises(KeyError):
            lib.get("usgs_splib07:nope")

    def test_remote_index(self, http_release: str) -> None:
        index = openspeclib.open_index(http_release + storage.INDEX_FILENAME)
        assert index.get("ecostress:calcite").wavelength_unit == "cm-1"
        assert index.search(collection="ecosis").num_rows == 3


def test_remote_without_duckdb(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("no duckdb")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="openspeclib\\[remote\\]"):
        openspeclib.open("https://example.invalid/x.parquet")


@pytest.fixture
def big_release(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A 2,000-spectrum collection (20 row groups) served over HTTP.

    The file is large enough that one row group is a small part of it: DuckDB 2.0
    fetches a remote file under 2 MiB whole and rounds remote reads up to larger blocks.
    """
    from openspeclib.storage import write_collection

    bands = 1000
    axis = [400.0 + i for i in range(bands)]
    rng = np.random.default_rng(7)
    records = [
        make_record(
            f"s{i:04d}",
            unit=WavelengthUnit.NANOMETERS,
            wavelengths=axis,
            values=rng.random(bands).tolist(),
        )
        for i in range(2000)
    ]
    root = tmp_path / "big"
    path = root / "usgs_splib07.parquet"
    write_collection(records, path, make_collection_info())
    handler = functools.partial(_RangeHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/usgs_splib07.parquet", path
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(not _remote_available(), reason="duckdb with httpfs not available")
def test_remote_get_prunes_row_groups(big_release: tuple[str, Path]) -> None:
    url, path = big_release
    size = path.stat().st_size
    lib = openspeclib.open(url)
    SERVED.clear()
    s = lib.get("usgs_splib07:s0537")
    expected = pq.read_table(path, filters=pc.field("id") == "usgs_splib07:s0537")
    assert s.values[0] == pytest.approx(expected["values"][0][0].as_py())
    fetched = sum(n for _, n in SERVED)
    # One row group of twenty plus footer reads: far less than the whole file.
    assert 0 < fetched < size / 4, (fetched, size, SERVED)
    # DuckDB 2.0 reads the footer twice in a file's first query.
    assert len(SERVED) <= 10, SERVED
    assert all(not name.endswith(".part") for name, _ in SERVED)


def test_search_criterion_on_missing_column_matches_nothing() -> None:
    import pyarrow as pa

    from openspeclib.reader import _search_mask

    table = pa.table(
        {
            "id": ["a", "b"],
            "name": ["x", "y"],
            "material_name": ["x", "y"],
            "quantity": ["reflectance", "reflectance"],
        }
    )
    common = dict(
        material=None,
        category=None,
        geometry=None,
        wavelength_range_nm=None,
        collection=None,
    )
    mask = _search_mask(table, quantity="reflectance", dataset_id="pkg", **common)
    assert table.filter(mask).num_rows == 0
    mask = _search_mask(table, quantity="reflectance", dataset_id=None, **common)
    assert table.filter(mask).num_rows == 2


def test_to_table_empty_columns(release: Path) -> None:
    lib = openspeclib.open(release / "usgs_splib07.parquet")
    assert lib.to_table(columns=[]).num_columns == 0
    index = openspeclib.open_index(release / storage.INDEX_FILENAME)
    assert index.to_table(columns=[]).num_columns == 0
    assert index.to_table().num_columns > 0


def test_get_with_list_column_before_id(release: Path, tmp_path: Path) -> None:
    """Row-group statistics must be found by leaf path, not field position."""
    source = release / "usgs_splib07.parquet"
    footer = pq.read_metadata(source).metadata[b"openspeclib"]
    table = pq.read_table(source)
    names = ["wavelengths", "values"] + [
        n for n in table.column_names if n not in ("wavelengths", "values")
    ]
    table = table.select(names).replace_schema_metadata({b"openspeclib": footer})
    moved = tmp_path / "moved.parquet"
    pq.write_table(table, moved, row_group_size=10)
    lib = openspeclib.open(moved)
    ids = table["id"].to_pylist()
    assert all(lib.get(i).id == i for i in ids)
