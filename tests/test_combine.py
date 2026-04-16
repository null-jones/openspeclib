"""Tests for the library combiner."""

import json
from pathlib import Path

from openspeclib.combine import build_library
from openspeclib.models import (
    SourceInfo,
    SpectrumRecord,
)
from openspeclib.storage import read_chunk


def _make_source_info(name: str) -> SourceInfo:
    return SourceInfo(
        name=name,
        version="1.0",
        url="https://example.com",
        license="Public Domain",
        citation="Test citation",
        spectrum_count=0,
    )


class TestBuildLibrary:
    def test_basic_combine(self, sample_spectrum: SpectrumRecord, tmp_path: Path) -> None:
        output_dir = tmp_path / "library"

        catalog = build_library(
            record_streams={"usgs_splib07": iter([sample_spectrum])},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        assert catalog.statistics.total_spectra == 1
        assert len(catalog.spectra) == 1
        assert catalog.spectra[0].id == sample_spectrum.id
        assert (output_dir / "catalog.json").exists()
        assert (output_dir / "VERSION").exists()

    def test_one_parquet_per_source(self, sample_spectrum: SpectrumRecord, tmp_path: Path) -> None:
        output_dir = tmp_path / "library"

        catalog = build_library(
            record_streams={"usgs_splib07": iter([sample_spectrum])},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        # Single chunk file per source, named <source>.parquet
        chunk_files = {e.chunk_file for e in catalog.spectra}
        assert chunk_files == {"spectra/usgs_splib07.parquet"}

        chunk_path = output_dir / "spectra/usgs_splib07.parquet"
        assert chunk_path.exists()

        chunk = read_chunk(chunk_path)
        assert chunk.source == "usgs_splib07"
        assert chunk.spectrum_count == 1
        assert chunk.spectra[0].id == sample_spectrum.id

    def test_multiple_sources_each_get_own_file(
        self, sample_spectrum: SpectrumRecord, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "library"

        # Create a second spectrum with a different source
        spectrum2 = sample_spectrum.model_copy(
            update={
                "id": "ecostress:calcite_001",
                "source": sample_spectrum.source.model_copy(
                    update={"library": "ecostress", "original_id": "calcite_001"}
                ),
            }
        )

        catalog = build_library(
            record_streams={
                "usgs_splib07": iter([sample_spectrum]),
                "ecostress": iter([spectrum2]),
            },
            source_metadata={
                "usgs_splib07": _make_source_info("USGS"),
                "ecostress": _make_source_info("ECOSTRESS"),
            },
            output_dir=output_dir,
        )

        assert catalog.statistics.total_spectra == 2
        assert len(catalog.spectra) == 2

        chunk_files = {e.chunk_file for e in catalog.spectra}
        assert chunk_files == {
            "spectra/usgs_splib07.parquet",
            "spectra/ecostress.parquet",
        }
        assert (output_dir / "spectra/usgs_splib07.parquet").exists()
        assert (output_dir / "spectra/ecostress.parquet").exists()

    def test_catalog_record_has_no_arrays(
        self, sample_spectrum: SpectrumRecord, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "library"

        build_library(
            record_streams={"usgs_splib07": iter([sample_spectrum])},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        # Read the catalog.json and verify no spectral arrays
        catalog_data = json.loads((output_dir / "catalog.json").read_text())
        entry = catalog_data["spectra"][0]
        assert "wavelengths" not in entry["spectral_data"]
        assert "values" not in entry["spectral_data"]

    def test_large_source_stays_one_file(
        self, sample_spectrum: SpectrumRecord, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "library"

        # Make 2500 records with distinct ids across different categories
        records = []
        for i in range(2500):
            rec = sample_spectrum.model_copy(update={"id": f"usgs_splib07:test_{i}"})
            records.append(rec)

        catalog = build_library(
            record_streams={"usgs_splib07": iter(records)},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        assert catalog.statistics.total_spectra == 2500
        # Single file per source regardless of size
        chunk_files = {e.chunk_file for e in catalog.spectra}
        assert chunk_files == {"spectra/usgs_splib07.parquet"}

        # Row groups should partition the file (default row_group_size=1000)
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(output_dir / "spectra/usgs_splib07.parquet")
        assert pf.metadata.num_rows == 2500
        assert pf.metadata.num_row_groups >= 2

    def test_source_counts_updated(self, sample_spectrum: SpectrumRecord, tmp_path: Path) -> None:
        output_dir = tmp_path / "library"

        catalog = build_library(
            record_streams={"usgs_splib07": iter([sample_spectrum])},
            source_metadata={"usgs_splib07": _make_source_info("USGS")},
            output_dir=output_dir,
        )

        assert catalog.sources["usgs_splib07"].spectrum_count == 1
