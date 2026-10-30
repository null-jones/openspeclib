"""Tests for the ENVI/CSV converters (openspeclib.convert) and the convert/export CLI."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

import openspeclib
from openspeclib.cli import main
from openspeclib.conformance import check_collection
from openspeclib.convert import (
    ConversionError,
    ImportDefaults,
    NonConformantError,
    convert_file,
    export_collection,
    parse_envi_header,
    parse_resample_spec,
    parse_search_terms,
    read_envi_library,
)
from openspeclib.convert.common import normalize_quantity
from openspeclib.convert.envi import write_envi_library
from openspeclib.models import (
    CollectionInfo,
    DatasetInfo,
    Material,
    Measurement,
    Sample,
    SpectralData,
    SpectrumRecord,
)
from openspeclib.storage import write_collection

FIXTURES = Path(__file__).parent / "fixtures" / "convert"

COLLECTION = {
    "id": "test_lab",
    "title": "Test lab library",
    "license": "CC-BY-4.0",
    "citation": "Test Lab (2026). Test spectral library.",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    local: str,
    name: str,
    wavelengths: list[float],
    values: list[float],
    *,
    unit: str = "nm",
    license: str = "CC-BY-4.0",
    dataset_id: str | None = None,
    fwhm: list[float] | None = None,
    stddev: list[float] | None = None,
    category: str = "mineral",
) -> SpectrumRecord:
    return SpectrumRecord(
        id=f"src_lib:{local}",
        collection="src_lib",
        original_id=local.replace("_", " "),
        name=name,
        license=license,
        dataset_id=dataset_id,
        material=Material(name=name.split(",")[0], category=category, keywords=["a", "b"]),
        sample=Sample(id=f"S-{local}", description="desc, with comma {braces}"),
        measurement=Measurement(
            quantity="reflectance",
            geometry="directional_hemispherical",
            instrument="ASD FieldSpec",
            processing=["averaged"],
        ),
        spectral_data=SpectralData(
            wavelength_unit=unit,
            wavelengths=wavelengths,
            values=values,
            bandpass_fwhm=fwhm,
            values_stddev=stddev,
        ),
        extra={"note": "x,y", "n": 3},
    )


def _source_collection(path: Path, *, shared_axis: bool = True) -> Path:
    """Write a small v1 collection with NaNs, commas in names, fwhm and two licences."""
    wl = [400.0, 500.5, 600.25, 700.0, 800.125]
    fwhm = [10.0, 10.0, 10.0, 12.5, 12.5]
    records = [
        _record("q1", "Quartz, fine", wl, [0.1, 0.2, math.nan, 0.4, 0.5], fwhm=fwhm),
        _record("k_2", "Kaolinite {CM9}", wl, [0.31234567, 0.32, 0.33, 0.34, 0.35], fwhm=fwhm),
        _record(
            "c3",
            "Calcite",
            wl if shared_axis else [450.0, 550.0, 650.0, 750.0],
            [0.5, 0.6, 0.7, math.nan, 0.9][: 5 if shared_axis else 4],
            license="LicenseRef-Lab-Terms",
            dataset_id="ds1",
            fwhm=fwhm if shared_axis else None,
            stddev=None if shared_axis else [0.01, 0.01, 0.02, 0.02],
        ),
    ]
    info = CollectionInfo(
        id="src_lib",
        title="Source library",
        license="CC-BY-4.0 AND LicenseRef-Lab-Terms",
        license_refs={"LicenseRef-Lab-Terms": {"name": "Lab terms", "url": "https://lab.example"}},
        citation="Source Lab (2025). Source library.",
        inclusion="complete",
    )
    datasets = {
        "ds1": DatasetInfo(
            id="ds1",
            title="Carbonates",
            license="LicenseRef-Lab-Terms",
            citation="Carbonate team (2024). Carbonates.",
        )
    }
    write_collection(records, path, info, datasets)
    return path


def _assert_same_spectra(src: Path, dst: Path, *, same_ids: bool = True) -> None:
    a, b = openspeclib.open(src), openspeclib.open(dst)
    assert len(a) == len(b)
    for sid in a.search()["id"].to_pylist():
        x = a.get(sid)
        y = b.get(sid if same_ids else f"{b.collection_id}:{sid.split(':', 1)[1]}")
        assert x.wavelength_unit == y.wavelength_unit
        np.testing.assert_array_equal(x.wavelengths, y.wavelengths)
        np.testing.assert_allclose(x.values, y.values, rtol=1e-7, equal_nan=True)
        assert y.values.dtype == np.float32
        for key in ("name", "original_id", "material_name", "license", "quantity", "geometry"):
            assert x.metadata[key] == y.metadata[key], key


# ---------------------------------------------------------------------------
# ENVI header parsing
# ---------------------------------------------------------------------------


class TestEnviHeader:
    def test_case_insensitive_multiline_and_comments(self) -> None:
        header = parse_envi_header((FIXTURES / "lab_int16.hdr").read_text())
        assert header["samples"] == "6"
        assert header["byte order"] == "1"
        assert header["wavelength units"] == "Micrometers"
        assert header["wavelength"].startswith("{") and header["wavelength"].endswith("}")
        assert "0.90" in header["wavelength"]
        assert "Kaolinite CM9" in header["spectra names"]

    def test_first_line_must_be_envi(self) -> None:
        with pytest.raises(ConversionError, match="first line must be 'ENVI'"):
            parse_envi_header("samples = 3\n")

    def test_line_without_equals(self) -> None:
        with pytest.raises(ConversionError, match="malformed ENVI header line 3"):
            parse_envi_header("ENVI\nsamples = 3\nthis is junk\n")

    def test_unterminated_brace(self) -> None:
        with pytest.raises(ConversionError, match="never closed"):
            parse_envi_header("ENVI\nwavelength = {1, 2,\n 3\nlines = 2\n")


# ---------------------------------------------------------------------------
# ENVI reading and import
# ---------------------------------------------------------------------------


def _copy_envi(tmp_path: Path, header_text: str | None = None) -> Path:
    hdr = tmp_path / "lab.hdr"
    shutil.copy(FIXTURES / "lab_int16.sli", tmp_path / "lab.sli")
    hdr.write_text(header_text or (FIXTURES / "lab_int16.hdr").read_text())
    return hdr


class TestEnviImport:
    def test_read_library(self) -> None:
        lib = read_envi_library(FIXTURES / "lab_int16.sli")
        assert lib.values.shape == (3, 6)
        assert lib.wavelength_unit == "um"
        assert lib.names == ["Quartz GDS31", "Kaolinite CM9", "Calcite"]
        assert lib.scale_factor == 10000
        assert math.isnan(lib.values[0, 3])  # data ignore value
        assert np.isnan(lib.values[:, 5]).all()  # bbl = 0
        assert lib.values[1, 0] == 5000

    def test_convert_scales_and_masks(self, tmp_path: Path) -> None:
        out = tmp_path / "test_lab.parquet"
        result = convert_file(
            FIXTURES / "lab_int16.hdr",
            out,
            collection=COLLECTION,
            defaults=ImportDefaults(quantity="reflectance", material_category="mineral"),
        )
        assert result.conformance is not None and result.conformance.conformant
        assert result.report.value_divisor == 10000
        assert result.report.divisor_source == "header"
        lib = openspeclib.open(out)
        quartz = lib.get("test_lab:Quartz_GDS31")
        np.testing.assert_allclose(
            quartz.values, [0.1, 0.2, 0.3, np.nan, 0.5, np.nan], rtol=1e-6, equal_nan=True
        )
        np.testing.assert_allclose(quartz.wavelengths, [0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
        assert quartz.wavelength_unit == "um"
        assert quartz.bandpass_fwhm is not None
        assert quartz.metadata["material_category"] == "mineral"
        assert quartz.metadata["geometry"] == "unknown"

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("SAMPLES = 6", "SAMPLES = 4", "longer than the 24 the header describes"),
            ("lines   = 3", "lines   = 2", "--allow-trailing-bytes"),
            ("0.80, 0.90}", "0.80}", "has 5 values; samples = 6"),
            ("lines   = 3", "lines   = 4", "has 36 bytes"),
            ("bands = 1", "bands = 2", "bands = 2"),
            ("File Type = ENVI Spectral Library", "file type = ENVI Standard", "file type"),
            ("data type = 2", "data type = 6", "data type = 6 is not supported"),
            ("SAMPLES = 6\n", "", "missing the required field 'samples'"),
            ("Byte Order = 1", "Byte Order = 7", "byte order = 7"),
            ("Data Ignore Value = -1", "Data Ignore Value = none", "not a number"),
        ],
    )
    def test_malformed_headers(self, tmp_path: Path, old: str, new: str, message: str) -> None:
        text = (FIXTURES / "lab_int16.hdr").read_text()
        assert old in text
        hdr = _copy_envi(tmp_path, text.replace(old, new))
        with pytest.raises(ConversionError, match=message):
            read_envi_library(hdr)

    def test_missing_wavelength_list(self, tmp_path: Path) -> None:
        text = (FIXTURES / "lab_int16.hdr").read_text()
        start = text.index("wavelength = {")
        end = text.index("}", start) + 1
        hdr = _copy_envi(tmp_path, text[:start] + text[end:])
        with pytest.raises(ConversionError, match="no 'wavelength' list"):
            read_envi_library(hdr)

    def test_missing_unit_needs_flag(self, tmp_path: Path) -> None:
        text = (FIXTURES / "lab_int16.hdr").read_text().replace("Wavelength Units", "x_units")
        hdr = _copy_envi(tmp_path, text)
        with pytest.raises(ConversionError, match="--wavelength-unit"):
            convert_file(
                hdr,
                tmp_path / "test_lab.parquet",
                collection=COLLECTION,
                defaults=ImportDefaults(quantity="reflectance"),
            )
        result = convert_file(
            hdr,
            tmp_path / "test_lab.parquet",
            collection=COLLECTION,
            defaults=ImportDefaults(quantity="reflectance", wavelength_unit="um"),
        )
        assert result.conformance is not None and result.conformance.conformant

    def test_missing_data_file(self, tmp_path: Path) -> None:
        hdr = tmp_path / "lonely.hdr"
        hdr.write_text((FIXTURES / "lab_int16.hdr").read_text())
        with pytest.raises(ConversionError, match="no data file"):
            read_envi_library(hdr)


# ---------------------------------------------------------------------------
# Collection metadata
# ---------------------------------------------------------------------------


class TestCollectionMetadata:
    def test_missing_required_fields_listed(self, tmp_path: Path) -> None:
        with pytest.raises(ConversionError) as info:
            convert_file(
                FIXTURES / "lab_int16.hdr",
                tmp_path / "x.parquet",
                collection={"id": "test_lab"},
                defaults=ImportDefaults(quantity="reflectance"),
            )
        message = str(info.value)
        for flag in ("--title", "--license", "--citation"):
            assert flag in message
        assert "--collection-id" not in message

    def test_metadata_json_and_flag_override(self, tmp_path: Path) -> None:
        out = tmp_path / "test_lab.parquet"
        result = convert_file(
            FIXTURES / "lab_int16.hdr",
            out,
            metadata_json=FIXTURES / "metadata.json",
            collection={"title": "Overridden title"},
            defaults=ImportDefaults(quantity="reflectance"),
        )
        assert result.collection.title == "Overridden title"
        assert openspeclib.open(out).metadata["collection"]["license"] == "CC-BY-4.0"

    def test_invalid_licence(self, tmp_path: Path) -> None:
        with pytest.raises(ConversionError, match="not an SPDX expression"):
            convert_file(
                FIXTURES / "lab_int16.hdr",
                tmp_path / "x.parquet",
                collection={**COLLECTION, "license": "CC BY 4.0 or whatever"},
                defaults=ImportDefaults(quantity="reflectance"),
            )

    def test_licence_ref_needs_definition(self, tmp_path: Path) -> None:
        coll = {**COLLECTION, "license": "LicenseRef-My-Lab"}
        with pytest.raises(ConversionError, match="--license-name"):
            convert_file(
                FIXTURES / "lab_int16.hdr",
                tmp_path / "x.parquet",
                collection=coll,
                defaults=ImportDefaults(quantity="reflectance"),
            )
        out = tmp_path / "test_lab.parquet"
        convert_file(
            FIXTURES / "lab_int16.hdr",
            out,
            collection=coll,
            license_name="My lab terms",
            license_url="https://lab.example/terms",
            defaults=ImportDefaults(quantity="reflectance"),
        )
        refs = openspeclib.open(out).metadata["collection"]["license_refs"]
        assert refs == {
            "LicenseRef-My-Lab": {"name": "My lab terms", "url": "https://lab.example/terms"}
        }

    def test_invalid_collection_id(self, tmp_path: Path) -> None:
        with pytest.raises(ConversionError, match="id"):
            convert_file(
                FIXTURES / "lab_int16.hdr",
                tmp_path / "x.parquet",
                collection={**COLLECTION, "id": "Bad-ID"},
                defaults=ImportDefaults(quantity="reflectance"),
            )

    def test_missing_quantity(self, tmp_path: Path) -> None:
        with pytest.raises(ConversionError, match="--quantity"):
            convert_file(FIXTURES / "lab_int16.hdr", tmp_path / "x.parquet", collection=COLLECTION)

    def test_subset_needs_notes(self, tmp_path: Path) -> None:
        with pytest.raises(ConversionError, match="inclusion_notes"):
            convert_file(
                FIXTURES / "lab_int16.hdr",
                tmp_path / "x.parquet",
                collection={**COLLECTION, "inclusion": "subset"},
                defaults=ImportDefaults(quantity="reflectance"),
            )


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


class TestCsvImport:
    def test_ecosis_wide(self, tmp_path: Path) -> None:
        out = tmp_path / "test_lab.parquet"
        result = convert_file(FIXTURES / "ecosis_wide.csv", out, collection=COLLECTION)
        assert result.layout == "wide"
        assert result.conformance is not None and result.conformance.conformant
        assert result.report.value_divisor == 100  # percent detected
        assert result.report.unmapped_columns == ["Site"]
        lib = openspeclib.open(out)
        assert len(lib) == 3

        maple = lib.get("test_lab:leaf-001")
        np.testing.assert_allclose(maple.values, [0.051, 0.052, 0.053, 0.054, 0.055, 0.056])
        np.testing.assert_array_equal(maple.wavelengths, [350, 351, 352, 353, 354, 355])
        meta = maple.metadata
        assert meta["wavelength_unit"] == "nm"
        assert meta["material_name"] == "Red maple"
        assert meta["material_category"] == "vegetation"
        assert meta["material_subcategory"] == "Leaf"
        assert meta["quantity"] == "reflectance"
        assert meta["measurement_instrument"] == "ASD FieldSpec 3"
        assert meta["measurement_processing"] == ["averaged"]
        assert str(meta["sample_collection_date"]) == "2019-07-04"
        assert "Acer rubrum" in meta["material_keywords"]
        assert meta["extra"]["source_metadata"] == {"Site": "Plot A"}
        assert meta["extra"]["source_reflectance_divisor"] == 100

        oak = lib.get("test_lab:leaf-002")
        assert oak.metadata["material_name"] == "Quercus alba"  # from Latin names
        assert np.isnan(oak.values[[1, 3]]).all()  # -9999 and NA
        assert str(oak.metadata["sample_collection_date"]) == "2019-07-05"

        soil = lib.get("test_lab:soil-001")
        assert soil.metadata["material_category"] == "soil"
        assert soil.metadata.get("measurement_processing") is None

    def test_explicit_value_scale(self, tmp_path: Path) -> None:
        out = tmp_path / "test_lab.parquet"
        result = convert_file(
            FIXTURES / "ecosis_wide.csv",
            out,
            collection=COLLECTION,
            defaults=ImportDefaults(value_scale=1.0),
        )
        assert result.report.divisor_source == "flag"
        assert openspeclib.open(out).get("test_lab:soil-001").values[0] == pytest.approx(20.0)

    def test_long_needs_key_column(self, tmp_path: Path) -> None:
        with pytest.raises(ConversionError, match="spectrum key column"):
            convert_file(FIXTURES / "long.csv", tmp_path / "x.parquet", collection=COLLECTION)

    def test_long_with_mapping(self, tmp_path: Path) -> None:
        out = tmp_path / "test_lab.parquet"
        result = convert_file(
            FIXTURES / "long.csv",
            out,
            collection=COLLECTION,
            mapping=FIXTURES / "long_mapping.json",
            defaults=ImportDefaults(material_category="mineral"),
        )
        assert result.layout == "long"
        assert result.conformance is not None and result.conformance.conformant
        lib = openspeclib.open(out)
        gypsum = lib.get("test_lab:A-1")
        assert gypsum.wavelength_unit == "um"  # from the column name
        assert gypsum.metadata["quantity"] == "reflectance"  # from the value column name
        assert gypsum.metadata["material_name"] == "Gypsum"
        np.testing.assert_allclose(gypsum.values, [0.5, 0.55, 0.6])
        alunite = lib.get("test_lab:B-2")
        # Unsorted points are reordered (spec 4.3) and the reordering recorded.
        np.testing.assert_allclose(alunite.wavelengths, [2.05, 2.15, 2.25])
        np.testing.assert_allclose(alunite.values, [0.3, 0.32, 0.35])
        assert alunite.metadata["extra"]["bands_reordered"] is True
        assert alunite.metadata["quality_notes"] == "dry sample"
        np.testing.assert_allclose(alunite.bandpass_fwhm, [0.02, 0.02, 0.02])

    def test_mapping_dict_and_unknown_target(self, tmp_path: Path) -> None:
        with pytest.raises(ConversionError, match="unknown mapping target"):
            convert_file(
                FIXTURES / "long.csv",
                tmp_path / "x.parquet",
                collection=COLLECTION,
                mapping={"sample": "not_a_column"},
            )

    def test_layout_not_detected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.csv"
        bad.write_text("a,b,c\n1,2,3\n")
        with pytest.raises(ConversionError, match="cannot tell the CSV layout"):
            convert_file(bad, tmp_path / "x.parquet", collection=COLLECTION)

    def test_ids_from_names_and_rows(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "named.csv"
        csv_path.write_text(
            "name,400,500,600\n"
            "Olivine (Fo90),0.1,0.2,0.3\n"
            "Olivine (Fo90),0.2,0.3,0.4\n"
            ",0.3,0.4,0.5\n"
        )
        out = tmp_path / "test_lab.parquet"
        convert_file(
            csv_path,
            out,
            collection=COLLECTION,
            defaults=ImportDefaults(quantity="reflectance"),
        )
        ids = sorted(openspeclib.open(out).search()["id"].to_pylist())
        assert ids == ["test_lab:Olivine__Fo90_", "test_lab:Olivine__Fo90_~2", "test_lab:row-3"]
        assert check_collection(out).conformant

    def test_bad_dataset_reference(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "ds.csv"
        csv_path.write_text("id,dataset_id,400,500\na,nope,0.1,0.2\n")
        with pytest.raises(ConversionError, match="dataset_id 'nope' is not defined"):
            convert_file(
                csv_path,
                tmp_path / "x.parquet",
                collection=COLLECTION,
                defaults=ImportDefaults(quantity="reflectance"),
            )

    def test_bad_quantity_value(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "q.csv"
        csv_path.write_text("id,quantity,400,500\na,absorptance,0.1,0.2\n")
        with pytest.raises(ConversionError, match="row 1.*absorptance"):
            convert_file(csv_path, tmp_path / "x.parquet", collection=COLLECTION)

    def test_semicolon_delimiter_and_unit_suffix(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "semi.csv"
        csv_path.write_text("id;material;X1.5um;X2.0um;X2.5um\nm1;Talc;0.5;0.6;0.7\n")
        out = tmp_path / "test_lab.parquet"
        convert_file(
            csv_path,
            out,
            collection=COLLECTION,
            defaults=ImportDefaults(quantity="reflectance"),
        )
        spectrum = openspeclib.open(out).get("test_lab:m1")
        assert spectrum.wavelength_unit == "um"
        assert spectrum.metadata["material_name"] == "Talc"


# ---------------------------------------------------------------------------
# Export and round trips
# ---------------------------------------------------------------------------


class TestExport:
    def test_envi_round_trip(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet")
        result = export_collection(src, tmp_path / "out", to="envi")
        assert result.warnings == []
        hdr = tmp_path / "out" / "src_lib.hdr"
        header = parse_envi_header(hdr.read_text())
        assert header["file type"] == "ENVI Spectral Library"
        assert header["data ignore value"] == "-9999"
        assert header["wavelength units"] == "Nanometers"
        assert "fwhm" in header
        raw = np.fromfile(tmp_path / "out" / "src_lib.sli", dtype="<f4").reshape(3, 5)
        assert raw[2, 2] == -9999  # the NaN of q1 (rows in file order: c3, k_2, q1)
        back = tmp_path / "back" / "src_lib.parquet"
        res = convert_file(hdr, back, metadata_json=tmp_path / "out" / "src_lib.collection.json")
        assert res.conformance is not None and res.conformance.conformant
        _assert_same_spectra(src, back)
        b = openspeclib.open(back)
        q1 = b.get("src_lib:q1")
        assert q1.metadata["sample_description"] == "desc, with comma {braces}"
        assert q1.metadata["material_keywords"] == ["a", "b"]
        assert q1.metadata["extra"] == {"note": "x,y", "n": 3}
        np.testing.assert_allclose(q1.bandpass_fwhm, [10.0, 10.0, 10.0, 12.5, 12.5])
        assert b.get("src_lib:c3").metadata["dataset_id"] == "ds1"
        assert b.metadata["datasets"]["ds1"]["license"] == "LicenseRef-Lab-Terms"

    def test_envi_row_order_and_ignore_value(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet")
        export_collection(src, tmp_path / "out", to="envi", ids=["q1", "src_lib:c3"])
        raw = np.fromfile(tmp_path / "out" / "src_lib.sli", dtype="<f4").reshape(2, 5)
        np.testing.assert_allclose(raw[0], [0.1, 0.2, -9999, 0.4, 0.5], rtol=1e-6)
        np.testing.assert_allclose(raw[1], [0.5, 0.6, 0.7, -9999, 0.9], rtol=1e-6)

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_envi_readable_by_spectral_python(self, tmp_path: Path) -> None:
        envi = pytest.importorskip("spectral.io.envi")
        src = _source_collection(tmp_path / "src_lib.parquet")
        export_collection(src, tmp_path / "out", to="envi")
        lib = envi.open(
            str(tmp_path / "out" / "src_lib.hdr"), str(tmp_path / "out" / "src_lib.sli")
        )
        assert lib.spectra.shape == (3, 5)
        assert lib.names == ["Calcite", "Kaolinite (CM9)", "Quartz; fine"]
        np.testing.assert_allclose(lib.bands.centers, [400.0, 500.5, 600.25, 700.0, 800.125])
        assert lib.bands.band_unit == "Nanometers"
        assert lib.spectra[2, 2] == -9999
        np.testing.assert_allclose(lib.spectra[1], [0.31234567, 0.32, 0.33, 0.34, 0.35], rtol=1e-6)

    def test_envi_needs_shared_axis(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet", shared_axis=False)
        with pytest.raises(ConversionError, match="shared wavelength axis.*2 different axes"):
            export_collection(src, tmp_path / "out", to="envi")
        result = export_collection(src, tmp_path / "out", to="envi", resample_to="350:850:50")
        assert result.resampled
        assert any("bandpass_fwhm not written" in w for w in result.warnings)
        lib = read_envi_library(tmp_path / "out" / "src_lib.hdr")
        np.testing.assert_array_equal(lib.wavelengths, np.arange(350, 851, 50))
        c3 = lib.values[0]  # c3 has 450-750 nm: outside points are NaN (ignore value)
        assert np.isnan(c3[[0, 1, 9, 10]]).all()
        assert c3[2] == pytest.approx(0.5, rel=1e-6)
        assert c3[3] == pytest.approx(0.55, rel=1e-6)  # interpolated at 500 nm
        assert np.isnan(c3[[7, 8]]).all()  # next to the missing band at 750 nm
        q1 = lib.values[2]
        assert np.isnan(q1[0]) and q1[1] == pytest.approx(0.1)
        assert np.isnan(q1[5])  # next to the missing band
        back = tmp_path / "back" / "src_lib.parquet"
        convert_file(
            tmp_path / "out" / "src_lib.hdr",
            back,
            metadata_json=tmp_path / "out" / "src_lib.collection.json",
        )
        spectrum = openspeclib.open(back).get("src_lib:c3")
        assert "resampled" in spectrum.metadata["measurement_processing"]
        assert check_collection(back).conformant

    def test_csv_wide_round_trip(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet")
        result = export_collection(src, tmp_path / "out", to="csv")
        csv_path = tmp_path / "out" / "src_lib.csv"
        header = csv_path.read_text().splitlines()[0].split(",")
        assert {"license", "attribution", "id", "name"} <= set(header)
        assert header[-5:] == ["400", "500.5", "600.25", "700", "800.125"]
        assert any("bandpass_fwhm/values_stddev not written" in w for w in result.warnings)
        back = tmp_path / "back" / "src_lib.parquet"
        res = convert_file(
            csv_path, back, metadata_json=tmp_path / "out" / "src_lib.collection.json"
        )
        assert res.layout == "wide"
        assert res.report.unmapped_columns == []
        assert res.conformance is not None and res.conformance.conformant
        _assert_same_spectra(src, back)
        assert openspeclib.open(back).get("src_lib:q1").metadata["extra"] == {"note": "x,y", "n": 3}

    def test_csv_long_round_trip_keeps_axes_and_arrays(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet", shared_axis=False)
        with pytest.raises(ConversionError, match="--layout long"):
            export_collection(src, tmp_path / "out", to="csv")
        result = export_collection(src, tmp_path / "out", to="csv", layout="long")
        assert [f.name for f in result.files][:2] == ["src_lib.csv", "src_lib.spectra.csv"]
        points = (tmp_path / "out" / "src_lib.csv").read_text().splitlines()
        assert points[0] == (
            "id,wavelength_unit,license,wavelength,value,bandpass_fwhm,values_stddev"
        )
        assert len(points) == 1 + 5 + 5 + 4
        readme = (tmp_path / "out" / "src_lib.README.md").read_text()
        assert "--spectra-table src_lib.spectra.csv" in readme
        back = tmp_path / "back" / "src_lib.parquet"
        res = convert_file(
            tmp_path / "out" / "src_lib.csv",
            back,
            metadata_json=tmp_path / "out" / "src_lib.collection.json",
            spectra_table=tmp_path / "out" / "src_lib.spectra.csv",
        )
        assert res.layout == "long"
        assert res.conformance is not None and res.conformance.conformant
        _assert_same_spectra(src, back)
        c3 = openspeclib.open(back).get("src_lib:c3")
        np.testing.assert_allclose(c3.values_stddev, [0.01, 0.01, 0.02, 0.02])
        q1 = openspeclib.open(back).get("src_lib:q1")
        np.testing.assert_allclose(q1.bandpass_fwhm, [10.0, 10.0, 10.0, 12.5, 12.5])

    def test_long_points_alone_need_metadata(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet")
        export_collection(src, tmp_path / "out", to="csv", layout="long")
        with pytest.raises(ConversionError, match="--quantity"):
            convert_file(
                tmp_path / "out" / "src_lib.csv", tmp_path / "x.parquet", collection=COLLECTION
            )
        with pytest.raises(ConversionError, match="long CSV tables only"):
            convert_file(
                FIXTURES / "lab_int16.hdr",
                tmp_path / "x.parquet",
                collection=COLLECTION,
                spectra_table=tmp_path / "out" / "src_lib.spectra.csv",
            )

    def test_licences_travel_with_the_data(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet")
        export_collection(src, tmp_path / "out", to="csv", ids=["c3", "q1"])
        rows = (tmp_path / "out" / "src_lib.csv").read_text()
        assert "Carbonate team (2024). Carbonates. Via Source library." in rows
        assert "Licence: LicenseRef-Lab-Terms (Lab terms)." in rows
        assert "Source Lab (2025). Source library. Licence: CC-BY-4.0." in rows
        readme = (tmp_path / "out" / "src_lib.README.md").read_text()
        assert "| `CC-BY-4.0` | 1 |" in readme
        assert "| `LicenseRef-Lab-Terms` | 1 | LicenseRef-Lab-Terms: Lab terms" in readme
        assert "Carbonates (`ds1`): 1 spectra" in readme
        assert "Cite: Source Lab (2025). Source library." in readme
        meta = json.loads((tmp_path / "out" / "src_lib.collection.json").read_text())
        assert meta["collection"]["inclusion"] == "subset"
        assert meta["collection"]["inclusion_notes"].startswith("2 of the 3 spectra")
        assert list(meta["datasets"]) == ["ds1"]
        header = parse_envi_header(
            (export_collection(src, tmp_path / "e", to="envi").files[1]).read_text()
        )
        assert "LicenseRef-Lab-Terms" in header["openspeclib license"]
        assert "Source Lab (2025)" in header["description"]

    def test_selection_errors(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet")
        with pytest.raises(ConversionError, match="not found"):
            export_collection(src, tmp_path / "o", to="csv", ids=["nope"])
        with pytest.raises(ConversionError, match="no spectra match"):
            export_collection(src, tmp_path / "o", to="csv", search=["material=unobtainium"])
        result = export_collection(src, tmp_path / "o", to="csv", search=["quartz"])
        assert result.num_spectra == 1


class TestParsing:
    def test_resample_grid(self) -> None:
        np.testing.assert_array_equal(parse_resample_spec("400:420:10"), [400, 410, 420])
        np.testing.assert_allclose(parse_resample_spec("400:425:10"), [400, 410, 420])
        for bad in ("400:2500", "a:b:c", "500:400:10", "400:500:0"):
            with pytest.raises(ConversionError):
                parse_resample_spec(bad)

    def test_resample_sensor_files(self, tmp_path: Path) -> None:
        centres = tmp_path / "sensor.txt"
        centres.write_text("# band centres\nwavelength,fwhm\n450,10\n550,10\n650,10\n")
        np.testing.assert_array_equal(parse_resample_spec(str(centres)), [450, 550, 650])
        hdr = tmp_path / "sensor.hdr"
        hdr.write_text("ENVI\nwavelength units = Micrometers\nwavelength = {0.5, 0.6}\n")
        np.testing.assert_allclose(parse_resample_spec(str(hdr)), [500, 600])
        np.testing.assert_allclose(
            parse_resample_spec(str(FIXTURES / "lab_int16.hdr")),
            [400, 500, 600, 700, 800, 900],
        )

    def test_search_terms(self) -> None:
        assert parse_search_terms(["olivine", "category=mineral,rock", "range=400:"]) == {
            "material": "olivine",
            "category": ["mineral", "rock"],
            "wavelength_range_nm": (400.0, None),
        }
        with pytest.raises(ConversionError, match="unknown search key"):
            parse_search_terms(["colour=red"])

    def test_write_envi_validates_shapes(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not match"):
            write_envi_library(
                tmp_path / "x", np.arange(3.0) + 1, np.zeros((2, 4)), ["a", "b"], "nm"
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_convert_and_export(self, tmp_path: Path) -> None:
        runner = CliRunner()
        out = tmp_path / "test_lab.parquet"
        result = runner.invoke(
            main,
            [
                "convert",
                str(FIXTURES / "lab_int16.hdr"),
                str(out),
                "--collection-id",
                "test_lab",
                "--title",
                "Test lab",
                "--license",
                "CC0-1.0",
                "--citation",
                "Test Lab (2026).",
                "--quantity",
                "reflectance",
                "--category",
                "mineral",
                "--geometry",
                "directional_hemispherical",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Wrote 3 spectra" in result.output
        assert "conforms to OpenSpecLib Format v1" in result.output
        assert openspeclib.open(out).get("test_lab:Calcite").metadata["geometry"] == (
            "directional_hemispherical"
        )

        result = runner.invoke(
            main,
            [
                "export",
                str(out),
                str(tmp_path / "csv"),
                "--to",
                "csv",
                "--ids",
                "Calcite,Kaolinite_CM9",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Exported 2 spectra as wide CSV" in result.output
        assert (tmp_path / "csv" / "test_lab.csv").is_file()

    def test_convert_missing_metadata_exits_nonzero(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["convert", str(FIXTURES / "ecosis_wide.csv"), str(tmp_path / "x.parquet")],
        )
        assert result.exit_code == 1
        assert "missing required collection metadata" in result.output
        assert "--collection-id" in result.output

    def test_export_axis_error_message(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet", shared_axis=False)
        result = CliRunner().invoke(main, ["export", str(src), str(tmp_path / "o"), "--to", "envi"])
        assert result.exit_code == 1
        assert "--resample-to" in result.output

    def test_bad_value_scale(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "convert",
                str(FIXTURES / "ecosis_wide.csv"),
                str(tmp_path / "x.parquet"),
                "--value-scale",
                "lots",
            ],
        )
        assert result.exit_code == 2
        assert "auto" in result.output


# ---------------------------------------------------------------------------
# Real data: small committed extracts of the OpenSpecLib USGS and EcoSIS files
# ---------------------------------------------------------------------------


class TestRealData:
    def test_usgs_envi_and_csv(self, tmp_path: Path) -> None:
        src = FIXTURES / "usgs_sample.parquet"
        lib = openspeclib.open(src)
        ids = [i for i in lib.search()["id"].to_pylist() if "ASDFR" in i]
        assert len(ids) == 3
        for fmt in ("envi", "csv"):
            out = tmp_path / fmt
            result = export_collection(src, out, to=fmt, ids=ids)
            data = result.files[1] if fmt == "envi" else result.files[0]
            back = tmp_path / f"back_{fmt}" / "usgs_splib07.parquet"
            res = convert_file(data, back, metadata_json=out / "usgs_splib07.collection.json")
            assert res.conformance is not None and res.conformance.conformant, fmt
            assert res.report.divisor_source == "openspeclib export"
            for sid in ids:
                x, y = lib.get(sid), openspeclib.open(back).get(sid)
                np.testing.assert_array_equal(x.wavelengths, y.wavelengths)
                np.testing.assert_array_equal(x.values, y.values)
                assert x.metadata["name"] == y.metadata["name"]

    def test_usgs_mixed_axes_long(self, tmp_path: Path) -> None:
        src = FIXTURES / "usgs_sample.parquet"
        out = tmp_path / "long"
        export_collection(src, out, to="csv", layout="long")
        back = tmp_path / "back" / "usgs_splib07.parquet"
        convert_file(
            out / "usgs_splib07.csv",
            back,
            metadata_json=out / "usgs_splib07.collection.json",
            spectra_table=out / "usgs_splib07.spectra.csv",
        )
        _assert_same_spectra(src, back)

    def test_ecosis_long_and_resampled_envi(self, tmp_path: Path) -> None:
        src = FIXTURES / "ecosis_sample.parquet"
        out = tmp_path / "long"
        export_collection(src, out, to="csv", layout="long")
        back = tmp_path / "back_long" / "ecosis.parquet"
        res = convert_file(
            out / "ecosis.csv",
            back,
            metadata_json=out / "ecosis.collection.json",
            spectra_table=out / "ecosis.spectra.csv",
        )
        assert res.conformance is not None and res.conformance.conformant
        _assert_same_spectra(src, back)
        assert len(openspeclib.open(back).metadata["datasets"]) == 2

        out = tmp_path / "envi"
        export_collection(src, out, to="envi", resample_to="400:2400:10")
        back = tmp_path / "back_envi" / "ecosis.parquet"
        res = convert_file(out / "ecosis.hdr", back, metadata_json=out / "ecosis.collection.json")
        assert res.conformance is not None and res.conformance.conformant
        assert len(openspeclib.open(back)) == 4


# ---------------------------------------------------------------------------
# Review regressions: scale handling (B2)
# ---------------------------------------------------------------------------


def _csv(tmp_path: Path, text: str, name: str = "in.csv") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _envi(tmp_path: Path, data: np.ndarray, extra: str = "", dtype: int = 4) -> Path:
    lines, samples = data.shape
    wl = ", ".join(str(400 + 10 * i) for i in range(samples))
    hdr = tmp_path / "lib.hdr"
    hdr.write_text(
        f"ENVI\nsamples = {samples}\nlines = {lines}\nbands = 1\nheader offset = 0\n"
        f"file type = ENVI Spectral Library\ndata type = {dtype}\ninterleave = bsq\n"
        f"byte order = 0\nwavelength units = Nanometers\nwavelength = {{{wl}}}\n{extra}"
    )
    data.tofile(tmp_path / "lib.sli")
    return hdr


REFLECTANCE = ImportDefaults(quantity="reflectance", material_category="mineral")


def _convert(path: Path, tmp_path: Path, defaults: ImportDefaults = REFLECTANCE, **kw: object):
    return convert_file(
        path, tmp_path / "test_lab.parquet", collection=COLLECTION, defaults=defaults, **kw
    )


class TestScale:
    def test_bright_reflectance_round_trip(self, tmp_path: Path) -> None:
        bright = _record("b1", "Snow", [400.0, 500.0, 600.0], [1.8, 1.9, 2.0])
        info = CollectionInfo(
            id="src_lib", title="t", license="CC0-1.0", citation="c", inclusion="complete"
        )
        src = tmp_path / "src_lib.parquet"
        write_collection([bright], src, info)
        for to in ("envi", "csv"):
            out = tmp_path / to
            files = export_collection(src, out, to=to).files
            if to == "envi":
                header = parse_envi_header(files[1].read_text())
                assert header["reflectance scale factor"] == "1"
            back = tmp_path / f"{to}.parquet"
            res = convert_file(
                files[1] if to == "envi" else files[0],
                back,
                metadata_json=out / "src_lib.collection.json",
            )
            np.testing.assert_allclose(
                openspeclib.open(back).get("src_lib:b1").values, [1.8, 1.9, 2.0], rtol=1e-6
            )
            assert res.report.divisor_source == "openspeclib export"
            assert any("implausible" in w for w in res.report.warnings)
        # ENVI is recognised as an export from its header alone.
        res = _convert(tmp_path / "envi" / "src_lib.hdr", tmp_path)
        assert res.report.divisor_source == "openspeclib export"
        # A CSV without the sidecar is ambiguous: bright fractions or dark percent?
        with pytest.raises(ConversionError, match="cannot tell the value scale"):
            _convert(tmp_path / "csv" / "src_lib.csv", tmp_path)

    def test_declared_percent_wins_and_conflict_is_an_error(self, tmp_path: Path) -> None:
        good = _csv(tmp_path, "id,Measurement Units,400,500\na,%,20.5,30.5\n")
        assert _convert(good, tmp_path).report.divisor_source == "declared units"
        dark = _csv(tmp_path, "id,Measurement Units,400,500\na,%,0.5,0.8\n", "dark.csv")
        with pytest.raises(ConversionError, match="declared ÷100"):
            _convert(dark, tmp_path)
        result = _convert(dark, tmp_path, ImportDefaults(quantity="reflectance", value_scale=100))
        assert result.report.divisor_source == "flag"
        frac = _csv(tmp_path, "id,Measurement Units,400,500\na,0-1,0.5,0.8\n", "f.csv")
        assert _convert(frac, tmp_path).report.value_divisor == 1

    def test_integer_data(self, tmp_path: Path) -> None:
        dark = (np.array([[0.005, 0.008, 0.01, 0.012]]) * 10000).astype("<i2")
        with pytest.raises(ConversionError, match="integer, level"):
            _convert(_envi(tmp_path, dark, dtype=2), tmp_path)
        bright = (np.array([[0.1, 0.2, 0.3, 0.4]]) * 10000).astype("<i2")
        result = _convert(_envi(tmp_path, bright, dtype=2), tmp_path)
        assert result.report.value_divisor == 10000

    def test_mixed_scales_name_the_rows(self, tmp_path: Path) -> None:
        mixed = _csv(tmp_path, "id,400,500\na,0.1,0.2\nb,0.3,0.4\nc,30.5,40.5\n")
        with pytest.raises(ConversionError, match=r"mixes value scales.*÷1: row 1.*÷100: row 3"):
            _convert(mixed, tmp_path)

    def test_absorbance_is_not_rescaled(self, tmp_path: Path) -> None:
        path = _csv(
            tmp_path,
            "id,quantity,400,500\na,reflectance,30.5,40.5\nb,absorbance,0.5,0.6\n",
        )
        out = tmp_path / "test_lab.parquet"
        convert_file(
            path, out, collection=COLLECTION, defaults=ImportDefaults(material_category="mineral")
        )
        lib = openspeclib.open(out)
        np.testing.assert_allclose(lib.get("test_lab:a").values, [0.305, 0.405], rtol=1e-6)
        np.testing.assert_allclose(lib.get("test_lab:b").values, [0.5, 0.6], rtol=1e-6)

    def test_implausible_absorbance_and_header_scale(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,400,500\na,30,40\n")
        with pytest.raises(ConversionError, match="absorbance median 35"):
            _convert(path, tmp_path, ImportDefaults(quantity="absorbance"))
        percent = np.array([[25.0, 30.0, 35.0, 40.0]], dtype="<f4")
        hdr = _envi(tmp_path, percent, "reflectance scale factor = 1\n")
        with pytest.raises(ConversionError, match="implausible after scaling"):
            _convert(hdr, tmp_path)

    def test_float_ambiguous_band(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,400,500\na,1.9,2.1\n")
        with pytest.raises(ConversionError, match="float, level"):
            _convert(path, tmp_path)


# ---------------------------------------------------------------------------
# Review regressions: ENVI (B1 and nits)
# ---------------------------------------------------------------------------


class TestEnviReview:
    def test_data_file_larger_than_header(self, tmp_path: Path) -> None:
        data = np.array([[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]])
        hdr = _envi(tmp_path, data.astype("<f8"))  # header says float32
        with pytest.raises(ConversionError, match="longer than the 32 the header describes"):
            _convert(hdr, tmp_path)
        (tmp_path / "lib.sli").write_bytes(data.astype("<f4").tobytes() + b"\0" * 7)
        with pytest.raises(ConversionError, match="--allow-trailing-bytes"):
            _convert(hdr, tmp_path)
        result = _convert(hdr, tmp_path, allow_trailing_bytes=True)
        assert any("trailing bytes" in w for w in result.report.warnings)

    def test_header_offset_and_millimetres(self, tmp_path: Path) -> None:
        data = np.array([[0.1, 0.2, 0.3, 0.4]], dtype="<f4")
        hdr = _envi(tmp_path, data)
        text = hdr.read_text().replace("header offset = 0", "header offset = 16")
        text = text.replace("Nanometers", "Millimeters")
        text = text.replace("{400, 410, 420, 430}", "{0.0004, 0.0005, 0.0006, 0.0007}")
        hdr.write_text(text)
        (tmp_path / "lib.sli").write_bytes(b"X" * 16 + data.tobytes())
        _convert(hdr, tmp_path)
        spectrum = openspeclib.open(tmp_path / "test_lab.parquet").search()
        row = openspeclib.open(tmp_path / "test_lab.parquet").get(spectrum["id"][0].as_py())
        np.testing.assert_allclose(row.values, [0.1, 0.2, 0.3, 0.4], rtol=1e-6)
        np.testing.assert_allclose(row.wavelengths, [400, 500, 600, 700])
        assert row.wavelength_unit == "nm"

    def test_masking_counts_and_sentinels(self, tmp_path: Path) -> None:
        data = np.array([[0.1, -9999.0, 1e30, 0.4], [0.5, 0.6, -1.0, 0.8]], dtype="<f4")
        hdr = _envi(tmp_path, data, "data ignore value = -1\nbbl = {1, 1, 1, 0}\n")
        _convert(hdr, tmp_path)
        lib = openspeclib.open(tmp_path / "test_lab.parquet")
        first, second = (lib.get(i) for i in sorted(lib.search()["id"].to_pylist()))
        assert np.isnan(first.values[1:]).all()
        assert first.metadata["extra"]["masked_sentinel_count"] == 1
        assert first.metadata["extra"]["masked_fill_value_count"] == 1
        assert first.metadata["extra"]["masked_bad_band_count"] == 1
        assert second.metadata["extra"]["masked_ignore_value_count"] == 1

    def test_header_quirks(self, tmp_path: Path) -> None:
        data = np.array([[0.1, 0.2, 0.3, 0.4]], dtype="<f4")
        hdr = _envi(tmp_path, data, "description = {a = b, see {x}\n more}\n")
        text = hdr.read_text().replace("wavelength = {", "wavelength =\n{")
        text += "spectra names = {Épidote}\nwavelength units = Nanometers\n"
        hdr.write_bytes(text.encode("latin-1"))
        result = _convert(hdr, tmp_path)
        warnings = " ".join(result.report.warnings)
        assert "Latin-1" in warnings and "repeats 'wavelength units'" in warnings
        lib = openspeclib.open(tmp_path / "test_lab.parquet")
        assert lib.search()["name"].to_pylist() == ["Épidote"]

    def test_duplicate_wavelengths_dropped_and_recorded(self, tmp_path: Path) -> None:
        data = np.array([[0.1, 0.2, 0.3, 0.2]], dtype="<f4")
        hdr = _envi(tmp_path, data)
        hdr.write_text(hdr.read_text().replace("{400, 410, 420, 430}", "{400, 420, 410, 420}"))
        result = _convert(hdr, tmp_path)
        assert any("repeated wavelengths" in w for w in result.report.warnings)
        lib = openspeclib.open(tmp_path / "test_lab.parquet")
        row = lib.get(lib.search()["id"][0].as_py())
        np.testing.assert_array_equal(row.wavelengths, [400, 410, 420])
        np.testing.assert_allclose(row.values, [0.1, 0.3, 0.2], rtol=1e-6)
        extra = row.metadata["extra"]
        assert extra["dropped_duplicate_bands"] == [[420.0, pytest.approx(0.2)]]
        assert extra["source_band_index"] == [0, 2, 1]

    def test_duplicate_wavelengths_with_different_values_are_an_error(self, tmp_path: Path) -> None:
        data = np.array([[0.1, 0.2, 0.3, 0.4]], dtype="<f4")
        hdr = _envi(tmp_path, data)
        hdr.write_text(hdr.read_text().replace("{400, 410, 420, 430}", "{400, 420, 410, 420}"))
        with pytest.raises(ConversionError, match=r"wavelength 420 appears 2 times.*0\.2.*0\.4"):
            _convert(hdr, tmp_path)
        assert not (tmp_path / "test_lab.parquet").exists()

    def test_duplicate_wavelength_keeps_the_finite_band(self, tmp_path: Path) -> None:
        data = np.array([[0.1, np.nan, 0.3, 0.4]], dtype="<f4")
        hdr = _envi(tmp_path, data)
        hdr.write_text(hdr.read_text().replace("{400, 410, 420, 430}", "{400, 420, 410, 420}"))
        _convert(hdr, tmp_path)
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:spectrum-1")
        np.testing.assert_allclose(row.wavelengths, [400, 410, 420])
        np.testing.assert_allclose(row.values, [0.1, 0.3, 0.4], rtol=1e-6)
        assert row.metadata["extra"]["dropped_duplicate_bands"] == [[420.0, None]]

    def test_duplicate_wavelength_provenance_names_the_kept_band(self, tmp_path: Path) -> None:
        # Source bands: 430, 420 (NaN), 410, 420 (0.2), 420 (0.2).
        data = np.array([[0.4, np.nan, 0.1, 0.2, 0.2]], dtype="<f4")
        hdr = _envi(tmp_path, data)
        hdr.write_text(
            hdr.read_text().replace("{400, 410, 420, 430, 440}", "{430, 420, 410, 420, 420}")
        )
        _convert(hdr, tmp_path)
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:spectrum-1")
        np.testing.assert_allclose(row.wavelengths, [410, 420, 430])
        np.testing.assert_allclose(row.values, [0.1, 0.2, 0.4], rtol=1e-6)
        extra = row.metadata["extra"]
        assert extra["source_band_index"] == [2, 3, 0]
        assert sorted(extra["dropped_duplicate_bands"], key=str) == sorted(
            [[420.0, None], [420.0, pytest.approx(0.2)]], key=str
        )

    def test_export_header_carries_attribution_and_refs(self, tmp_path: Path) -> None:
        src = _source_collection(tmp_path / "src_lib.parquet")
        header = parse_envi_header(
            export_collection(src, tmp_path / "o", to="envi").files[1].read_text()
        )
        assert "Carbonate team (2024)" in header["openspeclib attribution"]
        assert (
            "LicenseRef-Lab-Terms: Lab terms (https://lab.example)"
            in header["openspeclib license refs"]
        )

    def test_resample_to_image_header(self, tmp_path: Path) -> None:
        hdr = tmp_path / "emit.hdr"
        hdr.write_text(
            "ENVI\nsamples = 1242\nlines = 1280\nbands = 3\nfile type = ENVI Standard\n"
            "data type = 4\nwavelength units = Millimeters\nwavelength = {0.0005, 0.0004, 0.0006}\n"
        )
        np.testing.assert_allclose(parse_resample_spec(str(hdr)), [400, 500, 600])


# ---------------------------------------------------------------------------
# Review regressions: CSV and metadata
# ---------------------------------------------------------------------------


class TestCsvReview:
    def test_numeric_sentinels_and_tokens(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,400,410,420,430,440\na,0.1,-9999.0,-999.00,NA,0.5\n")
        _convert(path, tmp_path)
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a")
        assert np.isnan(row.values[1:4]).all()
        assert row.metadata["extra"]["masked_sentinel_count"] == 2
        absorbance = _csv(tmp_path, "id,400,410,420\na,0.5,-9999,0.7\n", "abs.csv")
        _convert(absorbance, tmp_path, ImportDefaults(quantity="absorbance"))
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a")
        assert np.isnan(row.values[1]) and row.metadata["extra"]["masked_sentinel_count"] == 1

    def test_metadata_tokens_are_values(self, tmp_path: Path) -> None:
        path = _csv(
            tmp_path, "id,material_formula,sample_origin,foreoptic,400,500\na,Na,NA,none,0.1,0.2\n"
        )
        _convert(path, tmp_path)
        meta = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a").metadata
        assert meta["material_formula"] == "Na"
        assert meta["sample_origin"] == "NA"
        assert meta["measurement_foreoptic"] == "none"

    def test_extra_source_metadata_not_an_object(self, tmp_path: Path) -> None:
        path = _csv(
            tmp_path,
            'id,extra,source_metadata,400,500\na,"{""source_metadata"": ""x""}",y,0.1,0.2\n',
        )
        _convert(path, tmp_path)
        extra = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a").metadata["extra"]
        assert extra["source_metadata"] == {"extra.source_metadata": "x", "source_metadata": "y"}

    def test_alias_collision_keeps_both(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,material,Common Name,400,500\na,Olivine,Peridot,0.1,0.2\n")
        result = _convert(path, tmp_path)
        meta = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a").metadata
        assert meta["material_name"] == "Olivine"
        assert meta["extra"]["source_metadata"] == {"Common Name": "Peridot"}
        assert any("also maps to material_name" in w for w in result.report.warnings)

    def test_numeric_metadata_header_is_not_a_band(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,2019,350,351,352\na,7,0.1,0.2,0.3\n")
        result = _convert(path, tmp_path)
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a")
        np.testing.assert_array_equal(row.wavelengths, [350, 351, 352])
        assert row.metadata["extra"]["source_metadata"] == {"2019": "7"}
        assert any("outside the run" in w for w in result.report.warnings)

    def test_unit_suffix_case_and_greek_mu(self, tmp_path: Path) -> None:
        for header, unit in (("0.35μm,0.36μm", "um"), ("350 NM,351 NM", "nm")):
            path = _csv(tmp_path, f"id,{header}\na,0.1,0.2\n")
            _convert(path, tmp_path)
            assert (
                openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a").wavelength_unit
                == unit
            )

    def test_bom_and_encodings(self, tmp_path: Path) -> None:
        bom = tmp_path / "bom_site.csv"
        bom.write_text("Site,id,400,500\nPlot A,a,0.1,0.2\n", encoding="utf-8-sig")
        _convert(bom, tmp_path)
        extra = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a").metadata["extra"]
        assert extra["source_metadata"] == {"Site": "Plot A"}
        path = tmp_path / "bom.csv"
        path.write_text("id,name,400,500\na,Épidote,0.1,0.2\n", encoding="utf-8-sig")
        _convert(path, tmp_path)
        assert (
            openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a").metadata["name"]
            == "Épidote"
        )
        path.write_text("id,name,400,500\na,Épidote,0.1,0.2\n", encoding="latin-1")
        result = _convert(path, tmp_path)
        assert any("Latin-1" in w for w in result.report.warnings)
        result = _convert(path, tmp_path, encoding="cp1252")
        assert not any("Latin-1" in w for w in result.report.warnings)

    def test_long_key_repeats_wavelength(self, tmp_path: Path) -> None:
        conflict = _csv(tmp_path, "name,wavelength_nm,value\nQ,400,0.1\nQ,410,0.2\nQ,400,0.5\n")
        with pytest.raises(ConversionError, match="lines 2 and 4 with different values"):
            _convert(conflict, tmp_path)
        same = _csv(
            tmp_path, "name,wavelength_nm,value\nQ,400,0.1\nQ,410,0.2\nQ,400,0.1\n", "s.csv"
        )
        result = _convert(same, tmp_path)
        assert any("repeated" in w for w in result.report.warnings)

    def test_other_collection_prefix_kept_in_original_id(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,400,500\nother:abc,0.1,0.2\ntest_lab:xyz,0.1,0.2\n")
        _convert(path, tmp_path)
        lib = openspeclib.open(tmp_path / "test_lab.parquet")
        assert lib.get("test_lab:abc").metadata["original_id"] == "other:abc"
        assert lib.get("test_lab:xyz").metadata["original_id"] == "xyz"

    def test_licence_falls_back_to_dataset_then_collection(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta.json"
        meta.write_text(
            json.dumps(
                {
                    "collection": {**COLLECTION, "license": "NOASSERTION"},
                    "datasets": {
                        "ds1": {"title": "One", "license": "CC-BY-4.0"},
                        "ds2": {"title": "Two"},
                    },
                }
            )
        )
        path = _csv(
            tmp_path,
            "id,dataset_id,license,400,500\n"
            "a,ds1,,0.1,0.2\n"
            "b,ds1,CC0-1.0,0.1,0.2\n"
            "c,ds2,,0.1,0.2\n"
            "d,,,0.1,0.2\n",
        )
        convert_file(path, tmp_path / "test_lab.parquet", metadata_json=meta, defaults=REFLECTANCE)
        lib = openspeclib.open(tmp_path / "test_lab.parquet")
        licences = {i: lib.get(f"test_lab:{i}").metadata["license"] for i in "abcd"}
        assert licences == {
            "a": "CC-BY-4.0",  # its dataset's licence
            "b": "CC0-1.0",  # its own licence wins
            "c": "NOASSERTION",  # dataset without a licence: the collection's
            "d": "NOASSERTION",  # no dataset: the collection's
        }


class TestExportReview:
    def test_readme_tokenises_licence_refs(self, tmp_path: Path) -> None:
        records = [
            _record("a", "A", [400.0, 500.0], [0.1, 0.2], license="LicenseRef-Lab-Terms"),
            _record("b", "B", [400.0, 500.0], [0.1, 0.2], license="LicenseRef-Lab"),
        ]
        info = CollectionInfo(
            id="src_lib",
            title="t",
            license="NOASSERTION",
            license_refs={
                "LicenseRef-Lab": {"name": "Short terms"},
                "LicenseRef-Lab-Terms": {"name": "Long terms"},
            },
            citation="c",
            inclusion="complete",
        )
        src = tmp_path / "src_lib.parquet"
        write_collection(records, src, info)
        export_collection(src, tmp_path / "o", to="csv")
        readme = (tmp_path / "o" / "src_lib.README.md").read_text()
        assert "| `LicenseRef-Lab-Terms` | 1 | LicenseRef-Lab-Terms: Long terms |" in readme
        assert "| `LicenseRef-Lab` | 1 | LicenseRef-Lab: Short terms |" in readme

    def test_partial_dataset_marked_subset(self, tmp_path: Path) -> None:
        wl = [400.0, 500.0]
        records = [
            _record(f"r{i}", f"R{i}", wl, [0.1, 0.2], license="CC0-1.0", dataset_id="ds1")
            for i in range(3)
        ]
        info = CollectionInfo(
            id="src_lib", title="t", license="CC0-1.0", citation="c", inclusion="complete"
        )
        datasets = {
            "ds1": DatasetInfo(id="ds1", title="D", inclusion="complete", spectra_included=3)
        }
        src = tmp_path / "src_lib.parquet"
        write_collection(records, src, info, datasets)
        export_collection(src, tmp_path / "o", to="csv", ids=["r0"])
        meta = json.loads((tmp_path / "o" / "src_lib.collection.json").read_text())
        ds = meta["datasets"]["ds1"]
        assert ds["inclusion"] == "subset" and ds["spectra_included"] == 1
        assert ds["inclusion_notes"].startswith("1 of the 3 spectra of this dataset")
        assert "openspeclib_export" in meta
        back = tmp_path / "back.parquet"
        convert_file(
            tmp_path / "o" / "src_lib.csv",
            back,
            metadata_json=tmp_path / "o" / "src_lib.collection.json",
        )
        assert check_collection(back).conformant


class TestStrict:
    def test_non_conformant_output_is_not_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import openspeclib.convert.importer as importer

        real = importer.check_collection

        def failing(path: Path):  # type: ignore[no-untyped-def]
            report = real(path)
            report.results[0].status = "failed"
            report.results[0].findings = []
            return report

        monkeypatch.setattr(importer, "check_collection", failing)
        out = tmp_path / "test_lab.parquet"
        with pytest.raises(NonConformantError, match="does not conform"):
            convert_file(
                FIXTURES / "lab_int16.hdr", out, collection=COLLECTION, defaults=REFLECTANCE
            )
        assert not out.exists() and not list(tmp_path.glob("*.parquet"))
        # There is no way to keep a non-conformant file (the dead strict=False is gone).
        with pytest.raises(TypeError):
            convert_file(  # type: ignore[call-arg]
                FIXTURES / "lab_int16.hdr",
                out,
                collection=COLLECTION,
                defaults=REFLECTANCE,
                strict=False,
            )
        assert not out.exists()

        cli = CliRunner().invoke(
            main,
            [
                "convert",
                str(FIXTURES / "lab_int16.hdr"),
                str(out),
                "--collection-id",
                "test_lab",
                "--title",
                "t",
                "--license",
                "CC0-1.0",
                "--citation",
                "c",
                "--quantity",
                "reflectance",
            ],
        )
        assert cli.exit_code == 1
        assert "does not conform" in cli.output
        assert not out.exists()


# ---------------------------------------------------------------------------
# 0.2.0 release review
# ---------------------------------------------------------------------------


class TestStatedScaleSkipsNonFractions:
    def test_value_scale_flag_leaves_absorbance_alone(self, tmp_path: Path) -> None:
        path = _csv(
            tmp_path,
            "id,quantity,400,500\na,reflectance,30.5,40.5\nb,absorbance,0.5,0.6\n",
        )
        result = _convert(
            path, tmp_path, ImportDefaults(material_category="mineral", value_scale=100)
        )
        lib = openspeclib.open(tmp_path / "test_lab.parquet")
        np.testing.assert_allclose(lib.get("test_lab:a").values, [0.305, 0.405], rtol=1e-6)
        np.testing.assert_allclose(lib.get("test_lab:b").values, [0.5, 0.6], rtol=1e-6)
        assert lib.get("test_lab:a").metadata["extra"]["source_reflectance_divisor"] == 100
        assert "source_reflectance_divisor" not in (lib.get("test_lab:b").metadata["extra"] or {})
        assert result.report.value_divisor == 100
        assert any(
            "--value-scale 100 not applied to 1 spectra (1 absorbance)" in w
            for w in result.report.warnings
        )

    def test_envi_scale_factor_leaves_absorbance_alone(self, tmp_path: Path) -> None:
        data = np.array([[0.5, 0.6, 0.7, 0.8]], dtype="<f4")
        hdr = _envi(tmp_path, data, "reflectance scale factor = 100\n")
        result = _convert(hdr, tmp_path, ImportDefaults(quantity="absorbance"))
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:spectrum-1")
        np.testing.assert_allclose(row.values, [0.5, 0.6, 0.7, 0.8], rtol=1e-6)
        assert result.report.divisor_source == "header"
        assert result.report.value_divisor == 1
        assert any(
            "reflectance scale factor = 100 not applied" in w for w in result.report.warnings
        )

    def test_scaled_absorbance_is_an_error_without_a_value_scale_hint(self, tmp_path: Path) -> None:
        data = np.array([[500.0, 600.0, 700.0, 800.0]], dtype="<f4")
        hdr = _envi(tmp_path, data, "reflectance scale factor = 1000\n")
        with pytest.raises(ConversionError, match="absorbance values are stored as given") as exc:
            _convert(hdr, tmp_path, ImportDefaults(quantity="absorbance"))
        assert "--value-scale" not in str(exc.value)

    def test_value_scale_flag_does_not_vouch_for_absorbance(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,quantity,400,500\na,absorbance,50,70\n")
        with pytest.raises(ConversionError, match="absorbance values are stored as given"):
            _convert(path, tmp_path, ImportDefaults(material_category="mineral", value_scale=100))
        assert not (tmp_path / "test_lab.parquet").exists()

    def test_value_scale_flag_still_warns_for_reflectance(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,quantity,400,500\na,reflectance,50,70\n")
        result = _convert(
            path, tmp_path, ImportDefaults(material_category="mineral", value_scale=1)
        )
        assert any("implausible after scaling" in w for w in result.report.warnings)

    def test_envi_scale_factor_still_divides_reflectance(self, tmp_path: Path) -> None:
        data = np.array([[10.0, 20.0, 30.0, 40.0]], dtype="<f4")
        hdr = _envi(tmp_path, data, "reflectance scale factor = 100\n")
        result = _convert(hdr, tmp_path)
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:spectrum-1")
        np.testing.assert_allclose(row.values, [0.1, 0.2, 0.3, 0.4], rtol=1e-6)
        assert not any("not applied" in w for w in result.report.warnings)


class TestLongMetadataConflicts:
    @pytest.mark.parametrize(
        ("column", "first", "second"),
        [
            ("quantity", "reflectance", "transmittance"),
            ("license", "CC0-1.0", "CC-BY-4.0"),
            ("wavelength_unit", "nm", "um"),
            ("name", "Quartz", "Calcite"),
        ],
    )
    def test_conflict_is_an_error(self, tmp_path: Path, column, first, second) -> None:
        path = _csv(
            tmp_path,
            f"id,{column},wavelength,value\n"
            f"a,{first},400,0.1\na,{first},500,0.2\na,{second},600,0.3\n",
        )
        message = (
            rf"spectrum 'a': column '{column}' is '{first}' on line 2 but '{second}' on line 4"
        )
        with pytest.raises(ConversionError, match=message):
            _convert(path, tmp_path)
        assert not (tmp_path / "test_lab.parquet").exists()

    def test_blank_cells_and_spacing_are_not_conflicts(self, tmp_path: Path) -> None:
        path = _csv(
            tmp_path,
            "id,name,wavelength_nm,value\na,Quartz,400,0.1\na,,500,0.2\na, Quartz ,600,0.3\n",
        )
        _convert(path, tmp_path)
        assert (
            openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a").metadata["name"]
            == "Quartz"
        )


class TestStatedUnitConflicts:
    def test_band_header_unit_vs_row_unit(self, tmp_path: Path) -> None:
        bad = _csv(tmp_path, "id,wavelength_unit,400nm,500nm\na,um,0.1,0.2\n")
        with pytest.raises(
            ConversionError, match=r"row 1.*says 'um' but the band headers gives nm"
        ):
            _convert(bad, tmp_path)
        good = _csv(tmp_path, "id,wavelength_unit,0.4um,0.5um\na,µm,0.1,0.2\n", "good.csv")
        _convert(good, tmp_path)
        assert openspeclib.open(tmp_path / "test_lab.parquet").get(
            "test_lab:a"
        ).wavelength_unit == ("um")

    def test_wavelength_column_name_vs_row_unit(self, tmp_path: Path) -> None:
        bad = _csv(tmp_path, "id,wavelength_unit,wavelength_nm,value\na,um,400,0.1\na,um,500,0.2\n")
        with pytest.raises(ConversionError, match="column name 'wavelength_nm' gives nm"):
            _convert(bad, tmp_path)
        good = _csv(
            tmp_path,
            "id,wavelength_unit,wavelength_nm,value\na,nm,400,0.1\na,nm,500,0.2\n",
            "good.csv",
        )
        _convert(good, tmp_path)

    def test_envi_header_unit_vs_row_unit(self, tmp_path: Path) -> None:
        data = np.array([[0.1, 0.2, 0.3, 0.4]], dtype="<f4")
        hdr = _envi(tmp_path, data, "openspeclib wavelength_unit = {um}\n")
        with pytest.raises(ConversionError, match="ENVI header 'wavelength units' gives nm"):
            _convert(hdr, tmp_path)

    def test_unlabelled_axis_takes_the_row_unit(self, tmp_path: Path) -> None:
        # No unit in the headers: the wavelength_unit column labels the axis.
        path = _csv(tmp_path, "id,wavelength_unit,0.4,0.5\na,um,0.1,0.2\n")
        _convert(path, tmp_path)
        assert openspeclib.open(tmp_path / "test_lab.parquet").get(
            "test_lab:a"
        ).wavelength_unit == ("um")


class TestQuantityLabels:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Reflectance", "reflectance"),
            ("reflectance factor", "reflectance"),
            ("Reflectance-Factor", "reflectance"),
            ("Reflectance (%)", "reflectance"),
            ("  TRANSMITTANCE ", "transmittance"),
            ("transmission", "transmittance"),
            ("Emissivity", "emissivity"),
            ("absorbance", "absorbance"),
            ("Optical density", "absorbance"),
        ],
    )
    def test_recognised(self, label: str, expected: str) -> None:
        assert normalize_quantity(label) == expected

    @pytest.mark.parametrize(
        "label",
        [
            "first derivative reflectance",
            "reflectance (log 1/R)",
            "log(1/R)",
            "reflectance derivative",
            "continuum-removed reflectance",
            "apparent absorbance",
            "nonreflective",
        ],
    )
    def test_derived_forms_are_rejected(self, label: str) -> None:
        with pytest.raises(ConversionError, match="unknown quantity"):
            normalize_quantity(label)

    def test_absorptance_has_its_own_message(self) -> None:
        with pytest.raises(ConversionError, match="absorptance"):
            normalize_quantity("Absorptance")

    def test_derivative_column_stops_the_import(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,quantity,400,500\na,first derivative reflectance,0.1,0.2\n")
        with pytest.raises(ConversionError, match=r"row 1.*unknown quantity"):
            _convert(path, tmp_path, ImportDefaults(material_category="mineral"))


class TestReviewNits:
    def test_detected_percent_warns_about_dark_spectra(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "id,400,500\na,30.5,40.5\n")
        result = _convert(path, tmp_path)
        assert result.report.value_divisor == 100
        assert any(
            "detected as percent" in w and "--value-scale 10000" in w
            for w in result.report.warnings
        )
        declared = _csv(tmp_path, "id,Measurement Units,400,500\na,%,30.5,40.5\n", "d.csv")
        assert not any(
            "detected as percent" in w for w in _convert(declared, tmp_path).report.warnings
        )

    def test_legacy_divisor_key_is_renamed_on_reimport(self, tmp_path: Path) -> None:
        record = _record("a", "A", [400.0, 500.0], [0.1, 0.2])
        record = record.model_copy(update={"extra": {"source_value_divisor": 100, "n": 1}})
        info = CollectionInfo(
            id="src_lib", title="t", license="CC-BY-4.0", citation="c", inclusion="complete"
        )
        src = tmp_path / "src_lib.parquet"
        write_collection([record], src, info)
        out = tmp_path / "o"
        files = export_collection(src, out, to="csv").files
        back = tmp_path / "back.parquet"
        convert_file(files[0], back, metadata_json=out / "src_lib.collection.json")
        extra = openspeclib.open(back).get("src_lib:a").metadata["extra"]
        assert extra["source_reflectance_divisor"] == 100
        assert "source_value_divisor" not in extra

    def test_wide_duplicate_wavelengths(self, tmp_path: Path) -> None:
        same = _csv(tmp_path, "id,400,500,500.0,600\na,0.1,0.2,0.2,0.3\n")
        result = _convert(same, tmp_path)
        assert any("repeated wavelengths" in w for w in result.report.warnings)
        row = openspeclib.open(tmp_path / "test_lab.parquet").get("test_lab:a")
        np.testing.assert_array_equal(row.wavelengths, [400, 500, 600])
        differ = _csv(tmp_path, "id,400,500,500.0,600\na,0.1,0.2,0.25,0.3\n", "d.csv")
        with pytest.raises(ConversionError, match=r"row 1.*wavelength 500 appears 2 times"):
            _convert(differ, tmp_path)

    def test_negative_header_offset(self, tmp_path: Path) -> None:
        data = np.array([[0.1, 0.2, 0.3, 0.4]], dtype="<f4")
        hdr = _envi(tmp_path, data)
        hdr.write_text(hdr.read_text().replace("header offset = 0", "header offset = -8"))
        with pytest.raises(ConversionError, match="header offset = -8"):
            _convert(hdr, tmp_path)

    def test_resampled_export_drops_axis_indexed_extra(self, tmp_path: Path) -> None:
        record = _record("a", "A", [400.0, 500.0, 600.0], [0.1, 0.2, 0.3])
        extra = {
            "source_band_order": [0, 2, 1],
            "source_band_index": [0, 2, 1],
            "bands_reordered_by_wavelength": True,
            "n": 3,
        }
        record = record.model_copy(update={"extra": extra})
        info = CollectionInfo(
            id="src_lib", title="t", license="CC-BY-4.0", citation="c", inclusion="complete"
        )
        src = tmp_path / "src_lib.parquet"
        write_collection([record], src, info)

        def exported_extra(outdir: Path, **kw: object) -> dict:
            files = export_collection(src, outdir, to="csv", **kw).files
            back = tmp_path / f"{outdir.name}.parquet"
            convert_file(files[0], back, metadata_json=outdir / "src_lib.collection.json")
            return openspeclib.open(back).get("src_lib:a").metadata["extra"]

        resampled = exported_extra(tmp_path / "r", resample_to="400:600:50")
        assert "source_band_order" not in resampled and "source_band_index" not in resampled
        assert resampled["bands_reordered_by_wavelength"] is True and resampled["n"] == 3
        readme = (tmp_path / "r" / "src_lib.README.md").read_text()
        assert "source_band_order, source_band_index" in readme
        plain = exported_extra(tmp_path / "p")
        assert plain["source_band_order"] == [0, 2, 1]
