"""Tests for the OSSL (Open Soil Spectral Library) loader."""

import gzip
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pa_csv

from openspeclib.loaders.ossl import OsslLoader
from openspeclib.models import (
    Geometry,
    MaterialCategory,
    Quantity,
    WavelengthUnit,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ossl"


class TestOsslLoader:
    def test_source_name(self):
        assert OsslLoader().source_name() == "ossl"

    def test_load_fixture_yields_one_record_per_measurement(self):
        # Fixture has uuid-001 (VNIR + MIR), uuid-002 (MIR only),
        # uuid-003 (VNIR only) → 4 records total.
        records = list(OsslLoader().load(FIXTURE_DIR))
        assert len(records) == 4
        ids = sorted(r.id for r in records)
        assert ids == [
            "ossl:uuid-001_mir",
            "ossl:uuid-001_vnir",
            "ossl:uuid-002_mir",
            "ossl:uuid-003_vnir",
        ]

    def test_vnir_record_is_reflectance_in_nm(self):
        records = list(OsslLoader().load(FIXTURE_DIR))
        vnir = next(r for r in records if r.id == "ossl:uuid-001_vnir")
        assert vnir.measurement.quantity == Quantity.REFLECTANCE
        assert vnir.spectral_data.wavelength_unit == WavelengthUnit.NANOMETERS
        assert min(vnir.spectral_data.wavelengths) == 350.0
        assert max(vnir.spectral_data.wavelengths) == 2500.0
        assert len(vnir.spectral_data.wavelengths) == 6
        # Values are unit-scale reflectance from the fixture.
        for v in vnir.spectral_data.values:
            assert 0.0 <= v <= 1.0

    def test_mir_record_is_absorbance_in_wavenumbers(self):
        records = list(OsslLoader().load(FIXTURE_DIR))
        mir = next(r for r in records if r.id == "ossl:uuid-001_mir")
        assert mir.measurement.quantity == Quantity.ABSORBANCE
        assert mir.spectral_data.wavelength_unit == WavelengthUnit.WAVENUMBERS
        assert min(mir.spectral_data.wavelengths) == 600.0
        assert max(mir.spectral_data.wavelengths) == 4000.0
        assert len(mir.spectral_data.wavelengths) == 6
        # MIR absorbance values can exceed 1 (log10 scale).
        assert max(mir.spectral_data.values) > 1.0
        # Spec 4.1: record the absorbance basis.
        assert mir.measurement.processing == ["absorbance_from_reflectance"]
        vnir = next(r for r in records if r.id == "ossl:uuid-001_vnir")
        assert "absorbance_from_reflectance" not in vnir.measurement.processing

    def test_measurement_semantics(self):
        # Geometry is not documented per spectrum; MIR is FTIR, VisNIR mixed.
        records = {r.id: r for r in OsslLoader().load(FIXTURE_DIR)}
        assert all(r.measurement.geometry == Geometry.UNKNOWN for r in records.values())
        assert records["ossl:uuid-001_mir"].measurement.instrument_type == "ftir"
        assert records["ossl:uuid-001_vnir"].measurement.instrument_type is None

    def test_records_inherit_soilsite_metadata(self):
        records = list(OsslLoader().load(FIXTURE_DIR))
        r = next(r for r in records if r.id == "ossl:uuid-001_vnir")
        assert r.collection == "ossl"
        assert r.license == "CC-BY-4.0"
        assert r.material.category == MaterialCategory.SOIL
        assert r.material.subcategory == "silt loam"  # USDA texture
        assert "USA" in (r.sample.origin or "")
        assert r.extra["dataset.code_ascii_txt"] == "KSSL"
        assert float(r.extra["location.latitude_wgs84_dd"]) == 42.03
        # Original layer uuid is preserved verbatim.
        assert r.original_id == "uuid-001"
        assert r.dataset_id == "KSSL"

    def test_datasets_collected(self):
        loader = OsslLoader()
        records = list(loader.load(FIXTURE_DIR))
        datasets = loader.datasets()
        assert {r.dataset_id for r in records if r.dataset_id} <= set(datasets)
        assert sum(d.spectra_included or 0 for d in datasets.values()) == sum(
            1 for r in records if r.dataset_id
        )
        assert all(d.license == "CC-BY-4.0" for d in datasets.values())

    def test_record_with_missing_site_metadata_still_loads(self, tmp_path: Path):
        # Copy only the spectral CSVs, omit the soilsite file — loader
        # should still build records with empty site metadata rather
        # than crashing.
        import shutil

        for f in ("ossl_visnir_L0_v1.2.csv.gz",):
            shutil.copy(FIXTURE_DIR / f, tmp_path / f)

        records = list(OsslLoader().load(tmp_path))
        # Both VNIR rows still emit.
        vnir = [r for r in records if r.id.endswith("_vnir")]
        assert len(vnir) == 2
        for r in vnir:
            assert r.material.category == MaterialCategory.SOIL
            assert r.material.subcategory is None  # no metadata available


class TestMissingValues:
    """Empty cells and sentinels become NaN and are counted in quality."""

    def test_missing_cells_counted(self, tmp_path: Path) -> None:
        table = pa.table(
            {
                "id.layer_uuid_txt": ["uuid-a", "uuid-b", "uuid-c"],
                "scan_visnir.350_ref": [0.1, None, None],
                "scan_visnir.700_ref": [0.2, -9999.0, None],
                "scan_visnir.1000_ref": [0.3, 0.25, None],
                "scan_visnir.2500_ref": [None, 0.35, None],
            }
        )
        pa_csv.write_csv(table, tmp_path / "ossl_visnir_L0_v1.2.csv")
        # The loader expects .csv.gz; pyarrow reads gzip transparently.
        raw = (tmp_path / "ossl_visnir_L0_v1.2.csv").read_bytes()
        (tmp_path / "ossl_visnir_L0_v1.2.csv.gz").write_bytes(gzip.compress(raw))
        (tmp_path / "ossl_visnir_L0_v1.2.csv").unlink()

        records = {r.id: r for r in OsslLoader().load(tmp_path)}
        # uuid-c is entirely missing and is skipped.
        assert sorted(records) == ["ossl:uuid-a_vnir", "ossl:uuid-b_vnir"]

        a = records["ossl:uuid-a_vnir"]
        assert math.isnan(a.spectral_data.values[3])
        assert sum(math.isnan(v) for v in a.spectral_data.values) == 1

        b = records["ossl:uuid-b_vnir"]
        assert [math.isnan(v) for v in b.spectral_data.values] == [True, True, False, False]
        assert sum(math.isnan(v) for v in b.spectral_data.values) == 2
        assert not any(math.isnan(w) for w in b.spectral_data.wavelengths)

    def test_fixture_records_have_full_coverage(self) -> None:
        for r in OsslLoader().load(FIXTURE_DIR):
            assert sum(math.isnan(v) for v in r.spectral_data.values) == 0


def test_rows_without_layer_id_make_a_subset(tmp_path: Path) -> None:
    table = pa.table(
        {
            "id.layer_uuid_txt": ["uuid-a", None],
            "scan_visnir.350_ref": [0.1, 0.2],
            "scan_visnir.700_ref": [0.2, 0.3],
        }
    )
    pa_csv.write_csv(table, tmp_path / "ossl_visnir_L0_v1.2.csv")
    raw = (tmp_path / "ossl_visnir_L0_v1.2.csv").read_bytes()
    (tmp_path / "ossl_visnir_L0_v1.2.csv.gz").write_bytes(gzip.compress(raw))
    (tmp_path / "ossl_visnir_L0_v1.2.csv").unlink()
    loader = OsslLoader()
    records = list(loader.load(tmp_path))
    assert len(records) == 1
    assert loader.stats["no_layer_uuid"] == 1
    info = loader.collection_info()
    assert info.inclusion.value == "subset" and "1 rows without a layer id" in info.inclusion_notes
    assert loader.fatal_error_count == 0


def test_full_fixture_is_complete() -> None:
    loader = OsslLoader()
    list(loader.load(FIXTURE_DIR))
    assert loader.collection_info().inclusion.value == "complete"
    assert loader.fatal_error_count == 0
