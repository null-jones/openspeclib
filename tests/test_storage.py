"""Tests for the Parquet-backed source library storage layer."""

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from openspeclib.models import (
    Material,
    MaterialCategory,
    Measurement,
    MeasurementTechnique,
    Quality,
    Sample,
    Source,
    SourceLibrary,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
)
from openspeclib.storage import (
    ARROW_SCHEMA,
    WAVELENGTHS_ARROW_SCHEMA,
    WAVELENGTHS_FILENAME,
    WavelengthRegistry,
    iter_records,
    read_chunk,
    read_wavelengths,
    validate_parquet_schema,
    write_source,
    write_wavelengths,
)


def _build_minimal_record(spectrum_id: str = "test:001") -> SpectrumRecord:
    """Return a minimal SpectrumRecord with only required fields populated."""
    return SpectrumRecord(
        id=spectrum_id,
        name="Minimal Test",
        source=Source(
            library=SourceLibrary.USGS_SPLIB07,
            library_version="7a",
            original_id="min001",
            url="https://example.com",
            license="Public Domain",
            citation="Test citation",
        ),
        material=Material(
            name="Quartz",
            category=MaterialCategory.MINERAL,
        ),
        sample=Sample(),
        measurement=Measurement(
            technique=MeasurementTechnique.REFLECTANCE,
        ),
        spectral_data=SpectralData(
            type=MeasurementTechnique.REFLECTANCE,
            wavelength_unit=WavelengthUnit.MICROMETERS,
            wavelength_min=0.4,
            wavelength_max=2.5,
            num_points=3,
            wavelengths=[0.4, 1.0, 2.5],
            values=[0.1, 0.2, 0.3],
        ),
        quality=Quality(
            has_bad_bands=False,
            bad_band_count=0,
            coverage_fraction=1.0,
        ),
    )


def test_write_read_roundtrip(tmp_path: Path, sample_spectrum: SpectrumRecord) -> None:
    path = tmp_path / "usgs_splib07.parquet"
    count = write_source([sample_spectrum], path, source="usgs_splib07")
    assert count == 1

    chunk = read_chunk(path)

    assert chunk.source == "usgs_splib07"
    assert chunk.spectrum_count == 1
    assert len(chunk.spectra) == 1

    out = chunk.spectra[0]
    assert out.model_dump() == sample_spectrum.model_dump()


def test_parquet_schema_matches_canonical(tmp_path: Path, sample_spectrum: SpectrumRecord) -> None:
    path = tmp_path / "usgs_splib07.parquet"
    write_source([sample_spectrum], path, source="usgs_splib07")

    actual = pq.read_schema(path)
    for expected_field in ARROW_SCHEMA:
        actual_field = actual.field(expected_field.name)
        assert actual_field.type.equals(expected_field.type), (
            f"Type mismatch for {expected_field.name}: "
            f"{actual_field.type} != {expected_field.type}"
        )


def test_footer_metadata_only_version_and_source(
    tmp_path: Path, sample_spectrum: SpectrumRecord
) -> None:
    path = tmp_path / "usgs_splib07.parquet"
    write_source([sample_spectrum, sample_spectrum], path, source="usgs_splib07")

    metadata = pq.read_schema(path).metadata
    assert metadata is not None
    assert metadata[b"openspeclib_version"]
    assert metadata[b"source"] == b"usgs_splib07"
    # spectrum_count is NOT stored in footer — it's derived from num_rows
    assert b"spectrum_count" not in metadata
    assert b"category" not in metadata


def test_spectrum_count_derived_from_num_rows(
    tmp_path: Path, sample_spectrum: SpectrumRecord
) -> None:
    path = tmp_path / "usgs_splib07.parquet"
    write_source([sample_spectrum] * 7, path, source="usgs_splib07")

    # Native Parquet row count
    assert pq.ParquetFile(path).metadata.num_rows == 7
    # Derived into LibraryChunkFile
    chunk = read_chunk(path)
    assert chunk.spectrum_count == 7
    assert len(chunk.spectra) == 7


def test_additional_properties_json_roundtrip(tmp_path: Path) -> None:
    record = _build_minimal_record()
    record.additional_properties = {
        "string_key": "value",
        "int_key": 42,
        "float_key": 3.14,
        "bool_key": True,
        "list_key": [1, 2, 3],
        "nested": {"inner_key": "inner_value", "deeper": {"x": 1}},
    }

    path = tmp_path / "usgs_splib07.parquet"
    write_source([record], path, source="usgs_splib07")
    chunk = read_chunk(path)

    assert chunk.spectra[0].additional_properties == record.additional_properties


def test_nullable_fields_preserved(tmp_path: Path) -> None:
    record = _build_minimal_record()
    # All optional fields default to None
    assert record.source.filename is None
    assert record.material.subcategory is None
    assert record.material.formula is None
    assert record.sample.id is None
    assert record.spectral_data.bandpass is None
    assert record.measurement.instrument is None
    assert record.quality.notes is None

    path = tmp_path / "usgs_splib07.parquet"
    write_source([record], path, source="usgs_splib07")
    chunk = read_chunk(path)

    out = chunk.spectra[0]
    assert out.source.filename is None
    assert out.material.subcategory is None
    assert out.material.formula is None
    assert out.sample.id is None
    assert out.sample.collection_date is None
    assert out.spectral_data.bandpass is None
    assert out.measurement.instrument is None
    assert out.measurement.date is None
    assert out.quality.notes is None


def test_date_fields_roundtrip(tmp_path: Path) -> None:
    record = _build_minimal_record()
    record.sample.collection_date = date(2010, 6, 15)
    record.measurement.date = date(2015, 11, 30)

    path = tmp_path / "usgs_splib07.parquet"
    write_source([record], path, source="usgs_splib07")
    chunk = read_chunk(path)

    out = chunk.spectra[0]
    assert out.sample.collection_date == date(2010, 6, 15)
    assert out.measurement.date == date(2015, 11, 30)


def test_validate_parquet_schema_clean(tmp_path: Path, sample_spectrum: SpectrumRecord) -> None:
    path = tmp_path / "usgs_splib07.parquet"
    write_source([sample_spectrum], path, source="usgs_splib07")

    errors = validate_parquet_schema(path)
    assert errors == []


def test_validate_parquet_schema_detects_drift(tmp_path: Path) -> None:
    # Hand-craft a Parquet file with a mismatched schema (id as int instead of string,
    # plus an unexpected extra column).
    bad_schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("unexpected_column", pa.string(), nullable=True),
        ]
    )
    table = pa.table({"id": [1, 2], "unexpected_column": ["a", "b"]}, schema=bad_schema)
    path = tmp_path / "bad.parquet"
    pq.write_table(table, path)

    errors = validate_parquet_schema(path)
    assert errors  # non-empty
    joined = "\n".join(errors)
    assert "id" in joined
    assert "unexpected_column" in joined
    # Most expected columns are missing
    assert any("missing column" in e for e in errors)


def test_compression_is_zstd(tmp_path: Path, sample_spectrum: SpectrumRecord) -> None:
    path = tmp_path / "usgs_splib07.parquet"
    write_source([sample_spectrum] * 10, path, source="usgs_splib07")

    pf = pq.ParquetFile(path)
    row_group = pf.metadata.row_group(0)
    for col_idx in range(row_group.num_columns):
        compression = row_group.column(col_idx).compression
        assert compression.upper() == "ZSTD", f"Column {col_idx} uses {compression}, not ZSTD"


def test_row_groups_honor_row_group_size(tmp_path: Path, sample_spectrum: SpectrumRecord) -> None:
    path = tmp_path / "usgs_splib07.parquet"
    write_source([sample_spectrum] * 25, path, source="usgs_splib07", row_group_size=10)

    pf = pq.ParquetFile(path)
    # 25 records at 10/group => 3 row groups (10, 10, 5)
    assert pf.metadata.num_row_groups == 3
    assert pf.metadata.num_rows == 25


def test_multiple_records_roundtrip(tmp_path: Path) -> None:
    records = [_build_minimal_record(f"test:{i:03d}") for i in range(5)]
    # Vary the spectral data so we can detect ordering bugs
    for i, r in enumerate(records):
        r.spectral_data.wavelengths = [float(i), float(i) + 0.5, float(i) + 1.0]
        r.spectral_data.values = [float(i) * 0.1, float(i) * 0.2, float(i) * 0.3]
        r.spectral_data.wavelength_min = float(i)
        r.spectral_data.wavelength_max = float(i) + 1.0

    path = tmp_path / "usgs_splib07.parquet"
    write_source(records, path, source="usgs_splib07")
    chunk = read_chunk(path)

    assert chunk.spectrum_count == 5
    for original, restored in zip(records, chunk.spectra):
        assert original.model_dump() == restored.model_dump()


def test_iter_records_streams_in_order(tmp_path: Path) -> None:
    records = [_build_minimal_record(f"test:{i:03d}") for i in range(12)]
    path = tmp_path / "usgs_splib07.parquet"
    write_source(records, path, source="usgs_splib07", row_group_size=5)

    streamed = list(iter_records(path, batch_size=3))
    assert [r.id for r in streamed] == [r.id for r in records]


def test_read_chunk_missing_metadata_raises(tmp_path: Path) -> None:
    # Write a Parquet file without the required footer metadata
    table = pa.table({"id": ["a"]}, schema=pa.schema([pa.field("id", pa.string())]))
    path = tmp_path / "no_meta.parquet"
    pq.write_table(table, path)

    with pytest.raises(ValueError, match="missing required footer metadata"):
        read_chunk(path)


def test_empty_stream_produces_no_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    count = write_source(iter([]), path, source="usgs_splib07")
    assert count == 0
    assert not path.exists()


def test_records_are_sorted_by_id_on_write(tmp_path: Path) -> None:
    # Feed records in scrambled order; expect the parquet to materialise them
    # in id-sorted order so DuckDB can prune row groups by id range.
    ids = ["test:003", "test:000", "test:002", "test:004", "test:001"]
    records = [_build_minimal_record(spectrum_id=i) for i in ids]

    path = tmp_path / "usgs_splib07.parquet"
    write_source(records, path, source="usgs_splib07")

    streamed = [r.id for r in iter_records(path)]
    assert streamed == sorted(ids)


def test_column_statistics_are_written(tmp_path: Path) -> None:
    # Multiple row groups with disjoint id ranges so we can also check that
    # the row group min/max statistics are populated.
    records = [_build_minimal_record(spectrum_id=f"test:{i:03d}") for i in range(20)]
    path = tmp_path / "usgs_splib07.parquet"
    write_source(records, path, source="usgs_splib07", row_group_size=5)

    pf = pq.ParquetFile(path)
    assert pf.metadata.num_row_groups == 4

    for rg_idx in range(pf.metadata.num_row_groups):
        row_group = pf.metadata.row_group(rg_idx)
        # Locate the id column by name
        id_col_idx = next(
            i for i in range(row_group.num_columns) if row_group.column(i).path_in_schema == "id"
        )
        stats = row_group.column(id_col_idx).statistics
        assert stats is not None, f"row group {rg_idx} has no id-column statistics"
        assert stats.has_min_max
        assert stats.min is not None
        assert stats.max is not None


def test_wavelengths_are_deduplicated_into_registry(tmp_path: Path) -> None:
    # Three records: two share a wavelength axis, one differs.
    shared_wl = [0.4, 1.0, 2.5]
    other_wl = [0.5, 1.5, 2.0]

    r1 = _build_minimal_record("test:001")
    r1.spectral_data.wavelengths = list(shared_wl)
    r2 = _build_minimal_record("test:002")
    r2.spectral_data.wavelengths = list(shared_wl)
    r3 = _build_minimal_record("test:003")
    r3.spectral_data.wavelengths = list(other_wl)
    r3.spectral_data.wavelength_min = other_wl[0]
    r3.spectral_data.wavelength_max = other_wl[-1]

    path = tmp_path / "usgs_splib07.parquet"
    write_source([r1, r2, r3], path, source="usgs_splib07")

    wavelengths_path = tmp_path / WAVELENGTHS_FILENAME
    assert wavelengths_path.exists()

    registry = read_wavelengths(wavelengths_path)
    assert len(registry) == 2  # two unique grids, not three


def test_wavelengths_file_schema_matches_canonical(tmp_path: Path) -> None:
    record = _build_minimal_record("test:001")
    path = tmp_path / "usgs_splib07.parquet"
    write_source([record], path, source="usgs_splib07")

    wavelengths_path = tmp_path / WAVELENGTHS_FILENAME
    actual = pq.read_schema(wavelengths_path)
    for expected_field in WAVELENGTHS_ARROW_SCHEMA:
        actual_field = actual.field(expected_field.name)
        assert actual_field.type.equals(expected_field.type)


def test_read_chunk_rehydrates_wavelengths_via_registry(tmp_path: Path) -> None:
    record = _build_minimal_record("test:001")
    record.spectral_data.wavelengths = [0.42, 1.1, 2.4]
    record.spectral_data.wavelength_min = 0.42
    record.spectral_data.wavelength_max = 2.4

    path = tmp_path / "usgs_splib07.parquet"
    write_source([record], path, source="usgs_splib07")

    chunk = read_chunk(path)
    assert chunk.spectra[0].spectral_data.wavelengths == [0.42, 1.1, 2.4]


def test_write_wavelengths_then_read_roundtrip(tmp_path: Path) -> None:
    registry = WavelengthRegistry()
    g1 = registry.register([0.4, 1.0, 2.5], "um", "usgs_splib07")
    g2 = registry.register([400.0, 700.0, 1000.0], "nm", "ecosis")
    g1_dup = registry.register([0.4, 1.0, 2.5], "um", "different_source_should_not_matter")
    assert g1 == g1_dup
    assert g1 != g2

    path = tmp_path / WAVELENGTHS_FILENAME
    write_wavelengths(registry, path)

    restored = read_wavelengths(path)
    assert len(restored) == 2
    assert restored.get(g1) == ([0.4, 1.0, 2.5], "um")
    assert restored.get(g2) == ([400.0, 700.0, 1000.0], "nm")


def test_reflectance_scale_roundtrips(tmp_path: Path) -> None:
    record = _build_minimal_record("test:001")
    # Default for new records is "unit" — check the default trip
    assert record.spectral_data.reflectance_scale == "unit"
    path = tmp_path / "usgs_splib07.parquet"
    write_source([record], path, source="usgs_splib07")

    chunk = read_chunk(path)
    assert chunk.spectra[0].spectral_data.reflectance_scale == "unit"
