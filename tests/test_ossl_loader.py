"""Tests for the OSSL (Open Soil Spectral Library) loader."""

from pathlib import Path

from openspeclib.loaders.ossl import OsslLoader
from openspeclib.models import (
    MaterialCategory,
    MeasurementTechnique,
    SourceLibrary,
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
            "ossl:uuid-001:mir",
            "ossl:uuid-001:vnir",
            "ossl:uuid-002:mir",
            "ossl:uuid-003:vnir",
        ]

    def test_vnir_record_is_reflectance_in_nm(self):
        records = list(OsslLoader().load(FIXTURE_DIR))
        vnir = next(r for r in records if r.id == "ossl:uuid-001:vnir")
        assert vnir.spectral_data.type == MeasurementTechnique.REFLECTANCE
        assert vnir.spectral_data.wavelength_unit == WavelengthUnit.NANOMETERS
        assert vnir.spectral_data.wavelength_min == 350.0
        assert vnir.spectral_data.wavelength_max == 2500.0
        assert vnir.spectral_data.num_points == 6
        # Values are unit-scale reflectance from the fixture.
        for v in vnir.spectral_data.values:
            assert 0.0 <= v <= 1.0
        assert vnir.spectral_data.reflectance_scale == "unit"

    def test_mir_record_is_absorbance_in_wavenumbers(self):
        records = list(OsslLoader().load(FIXTURE_DIR))
        mir = next(r for r in records if r.id == "ossl:uuid-001:mir")
        assert mir.spectral_data.type == MeasurementTechnique.ABSORBANCE
        assert mir.spectral_data.wavelength_unit == WavelengthUnit.WAVENUMBERS
        assert mir.spectral_data.wavelength_min == 600.0
        assert mir.spectral_data.wavelength_max == 4000.0
        assert mir.spectral_data.num_points == 6
        # MIR absorbance values can exceed 1 (log10 scale).
        assert max(mir.spectral_data.values) > 1.0

    def test_records_inherit_soilsite_metadata(self):
        records = list(OsslLoader().load(FIXTURE_DIR))
        r = next(r for r in records if r.id == "ossl:uuid-001:vnir")
        assert r.source.library == SourceLibrary.OSSL
        assert r.source.license == "CC-BY 4.0"
        assert r.material.category == MaterialCategory.SOIL
        assert r.material.subcategory == "silt loam"  # USDA texture
        assert "USA" in (r.sample.origin or "")
        assert r.additional_properties["dataset.code_ascii_txt"] == "KSSL"
        assert float(r.additional_properties["location.latitude_wgs84_dd"]) == 42.03
        # Original layer uuid is preserved verbatim.
        assert r.source.original_id == "uuid-001"

    def test_record_with_missing_site_metadata_still_loads(self, tmp_path: Path):
        # Copy only the spectral CSVs, omit the soilsite file — loader
        # should still build records with empty site metadata rather
        # than crashing.
        import shutil

        for f in ("ossl_visnir_L0_v1.2.csv.gz",):
            shutil.copy(FIXTURE_DIR / f, tmp_path / f)

        records = list(OsslLoader().load(tmp_path))
        # Both VNIR rows still emit.
        vnir = [r for r in records if r.id.endswith(":vnir")]
        assert len(vnir) == 2
        for r in vnir:
            assert r.material.category == MaterialCategory.SOIL
            assert r.material.subcategory is None  # no metadata available
