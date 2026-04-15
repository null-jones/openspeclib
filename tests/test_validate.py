"""Tests for the library validator."""

import json
from pathlib import Path

from openspeclib.combine import build_library
from openspeclib.models import SourceInfo, SpectrumRecord
from openspeclib.validate import ValidationResult, validate_library


def _make_source_info(name: str) -> SourceInfo:
    return SourceInfo(
        name=name,
        version="1.0",
        url="https://example.com",
        license="Public Domain",
        citation="Test citation",
        spectrum_count=0,
    )


class TestValidateLibrary:
    def test_valid_library(self, sample_spectrum: SpectrumRecord, tmp_path: Path) -> None:
        output_dir = tmp_path / "library"
        build_library(
            record_streams={"usgs_splib07": iter([sample_spectrum])},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        result = validate_library(output_dir)
        assert result.is_valid, result.summary()

    def test_missing_catalog(self, tmp_path: Path) -> None:
        result = validate_library(tmp_path)
        assert not result.is_valid
        assert any("catalog.json not found" in e for e in result.errors)

    def test_missing_chunk_file(self, sample_spectrum: SpectrumRecord, tmp_path: Path) -> None:
        output_dir = tmp_path / "library"
        build_library(
            record_streams={"usgs_splib07": iter([sample_spectrum])},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        # Delete the chunk file
        for chunk in (output_dir / "spectra").rglob("*.parquet"):
            chunk.unlink()

        result = validate_library(output_dir)
        assert not result.is_valid
        assert any("chunk file missing" in e for e in result.errors)

    def test_duplicate_ids_detected(self, sample_spectrum: SpectrumRecord, tmp_path: Path) -> None:
        output_dir = tmp_path / "library"

        # Create two records with the same ID
        records = [sample_spectrum, sample_spectrum]
        build_library(
            record_streams={"usgs_splib07": iter(records)},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        result = validate_library(output_dir)
        assert not result.is_valid
        assert any("Duplicate spectrum ID" in e for e in result.errors)

    def test_statistics_mismatch_detected(
        self, sample_spectrum: SpectrumRecord, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "library"
        build_library(
            record_streams={"usgs_splib07": iter([sample_spectrum])},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        # Tamper with the catalog statistics
        catalog_path = output_dir / "catalog.json"
        data = json.loads(catalog_path.read_text())
        data["statistics"]["total_spectra"] = 999
        catalog_path.write_text(json.dumps(data))

        result = validate_library(output_dir)
        assert not result.is_valid
        assert any("total_spectra" in e for e in result.errors)


class TestValidationResult:
    def test_empty_is_valid(self) -> None:
        result = ValidationResult()
        assert result.is_valid

    def test_with_errors_is_invalid(self) -> None:
        result = ValidationResult(errors=["something wrong"])
        assert not result.is_valid

    def test_warnings_still_valid(self) -> None:
        result = ValidationResult(warnings=["minor issue"])
        assert result.is_valid
