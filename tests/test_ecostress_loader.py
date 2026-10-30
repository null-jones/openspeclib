"""Tests for the ECOSTRESS spectral library loader."""

import logging
import math
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from openspeclib.cli import main
from openspeclib.loaders.ecostress import (
    EcostressLoader,
    EcostressUnitError,
    measurement_geometry,
    parse_ecostress_file,
    parse_y_units,
)
from openspeclib.models import Geometry, MaterialCategory, Quantity, WavelengthUnit

FIXTURES = Path(__file__).parent / "fixtures" / "ecostress"
Y_UNITS = FIXTURES / "y_units"
CALCITE = FIXTURES / "mineral.calcite.powder.tir.jhu.becknic.spectrum.txt"


class TestParseEcostressFile:
    def test_mineral_spectrum(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "mineral.calcite.powder.tir.jhu.becknic.spectrum.txt"
        )
        assert record.id.startswith("ecostress:")
        assert record.name == "Calcite WS272"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.material.subcategory == "Carbonate"
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert len(record.spectral_data.wavelengths) == 10
        assert min(record.spectral_data.wavelengths) == 2.080
        assert max(record.spectral_data.wavelengths) == 14.0
        assert len(record.spectral_data.wavelengths) == 10
        assert len(record.spectral_data.values) == 10

    def test_vegetation_spectrum(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "vegetation.deciduous.oak.vis.jhu.asd.spectrum.txt"
        )
        assert record.name == "Red Oak Leaf"
        assert record.material.category == MaterialCategory.VEGETATION
        assert record.material.subcategory == "Deciduous"
        assert len(record.spectral_data.wavelengths) == 8
        assert min(record.spectral_data.wavelengths) == 0.35
        assert max(record.spectral_data.wavelengths) == 2.5
        assert record.measurement.quantity == Quantity.REFLECTANCE

    def test_source_provenance(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "mineral.calcite.powder.tir.jhu.becknic.spectrum.txt"
        )
        assert record.collection == "ecostress"
        assert record.license == "LicenseRef-ECOSTRESS-Terms"
        assert "Meerdink" in EcostressLoader().collection_info().citation

    def test_sample_metadata(self) -> None:
        record = parse_ecostress_file(
            FIXTURES / "mineral.calcite.powder.tir.jhu.becknic.spectrum.txt"
        )
        assert record.sample.id == "WS272"
        assert record.sample.particle_size == "75-250 um"
        assert record.sample.origin == "Ward's Scientific"
        assert record.sample.owner == "JHU"


class TestEcostressLoader:
    def test_load_fixtures(self, caplog: pytest.LogCaptureFixture) -> None:
        loader = EcostressLoader()
        assert loader.source_name() == "ecostress"

        with caplog.at_level(logging.ERROR, logger="openspeclib.loaders.ecostress"):
            records = list(loader.load(FIXTURES))
        # 2 top-level fixtures + 4 recognised Y Units variants; the
        # unknown-unit fixture is skipped, not guessed.
        assert len(records) == 6
        assert not any("unknownunit" in r.id for r in records)
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("unknownunit_1" in m for m in errors)
        assert any("1 ECOSTRESS file(s) skipped" in m for m in errors)
        assert loader.unit_errors == 1
        assert loader.fatal_error_count == 1

        categories = {r.material.category for r in records}
        assert MaterialCategory.MINERAL in categories
        assert MaterialCategory.VEGETATION in categories
        for r in records:
            assert r.extra["source_reflectance_divisor"] == 100
            assert max(r.spectral_data.values) <= 1.0


# Every Y Units spelling observed in ECOSTRESS 1.0 (with counts across all
# 3,451 spectra, including the 13 cp1252-encoded files), plus a couple of
# defensive extras.
OBSERVED_Y_UNITS = [
    (" Reflectance (percentage)", Quantity.REFLECTANCE),  # 1159
    ("Reflectance (percent)", Quantity.REFLECTANCE),  # 2140
    (" Reflectance (percent)", Quantity.REFLECTANCE),  # 3
    ("  Reflectance (percent)", Quantity.REFLECTANCE),  # 4
    ("Reflectance (percent)\t", Quantity.REFLECTANCE),  # 1
    ("Reflectence (percent)", Quantity.REFLECTANCE),  # 16
    ("Transmittance (percent)", Quantity.TRANSMITTANCE),  # 123
    ("Transmittence (percent)", Quantity.TRANSMITTANCE),  # 2
    ("Transmission (percent)", Quantity.TRANSMITTANCE),  # 1
    ("transmittance (percent)", Quantity.TRANSMITTANCE),  # 1
    ("Transittance (percent)", Quantity.TRANSMITTANCE),  # 1 (olivine_10 fine)
    ("REFLECTANCE ( Percent )", Quantity.REFLECTANCE),
    ("Reflectance (%)", Quantity.REFLECTANCE),
]


class TestParseYUnits:
    @pytest.mark.parametrize(("raw", "technique"), OBSERVED_Y_UNITS)
    def test_observed_spellings_are_percent(self, raw: str, technique: Quantity) -> None:
        assert parse_y_units(raw) == (technique, 100)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   \t",
            "Reflectance",  # no scale stated: do not assume percent or unit
            "Reflectance (fraction)",
            "Reflectance (arbitrary units)",
            "Absorbance (percent)",
            "Counts (percent)",
            # Ambiguous: several quantities, or scale qualified / not anchored.
            "Transmission/Reflectance (percent)",
            "Absorbance, percent reflectance",
            "Reflectance (fraction, not percent)",
            "Reflectance (percent) / 100",
            "Reflectance percent",
            "Percent reflectance",
            "Hemispherical reflectance (percent)",
        ],
    )
    def test_missing_or_unrecognised_raises(self, raw: str | None) -> None:
        with pytest.raises(EcostressUnitError):
            parse_y_units(raw)


class TestPercentToUnit:
    def test_values_divided_by_100(self) -> None:
        record = parse_ecostress_file(CALCITE)
        # Raw file: 97.0, 95.0, ... percent.
        assert record.spectral_data.values[0] == pytest.approx(0.97)
        assert record.spectral_data.values[1] == pytest.approx(0.95)
        assert record.extra["source_reflectance_divisor"] == 100
        # Wavelengths are untouched.
        assert record.spectral_data.wavelengths[0] == 2.080

    @pytest.mark.parametrize(
        ("fixture", "technique"),
        [
            (
                "mineral.silicate.tectosilicate.fine.tir.percentage_1.jhu.nicolet.spectrum.txt",
                Quantity.REFLECTANCE,
            ),
            (
                "mineral.silicate.tectosilicate.fine.tir.reflectence_1.jhu.nicolet.spectrum.txt",
                Quantity.REFLECTANCE,
            ),
            (
                "mineral.silicate.tectosilicate.fine.tir.transmittence_1.jhu.nicolet.spectrum.txt",
                Quantity.TRANSMITTANCE,
            ),
        ],
    )
    def test_header_variants(self, fixture: str, technique: Quantity) -> None:
        record = parse_ecostress_file(Y_UNITS / fixture)
        assert record.spectral_data.values == pytest.approx([0.5, 1.0, 0.045, 0.0])
        assert record.measurement.quantity == technique
        assert record.measurement.quantity == technique
        assert record.extra["source_reflectance_divisor"] == 100
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 0
        assert record.quality.notes is None

    def test_measurement_header_conflict_uses_y_units(self) -> None:
        # Mirrors chabazite_1 (coarse): Measurement says Transmission but the
        # Y Units column is reflectance.
        record = parse_ecostress_file(
            Y_UNITS
            / "mineral.silicate.tectosilicate.coarse.tir.conflict_1.jhu.nicolet.spectrum.txt"
        )
        assert record.measurement.quantity == Quantity.REFLECTANCE
        assert record.quality.notes is not None
        assert "Transmission" in record.quality.notes

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(EcostressUnitError):
            parse_ecostress_file(
                Y_UNITS / "mineral.silicate.tectosilicate.fine.tir.unknownunit_1.jhu.nicolet"
                ".spectrum.txt"
            )

    def test_missing_y_units_skipped_by_loader(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        text = CALCITE.read_text()
        no_units = "\n".join(ln for ln in text.splitlines() if not ln.startswith("Y Units"))
        (tmp_path / "mineral.nounits.spectrum.txt").write_text(no_units + "\n")
        shutil.copy(CALCITE, tmp_path / CALCITE.name)

        with caplog.at_level(logging.ERROR, logger="openspeclib.loaders.ecostress"):
            records = list(EcostressLoader().load(tmp_path))
        assert [r.original_id for r in records] == [CALCITE.name.removesuffix(".spectrum.txt")]
        assert any("missing 'Y Units'" in r.getMessage() for r in caplog.records)

    def test_non_finite_values_become_nan(self, tmp_path: Path) -> None:
        text = CALCITE.read_text().replace("3.000\t95.0", "3.000\tnan")
        path = tmp_path / CALCITE.name
        path.write_text(text)
        record = parse_ecostress_file(path)
        assert math.isnan(record.spectral_data.values[1])
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 1


def _with_data(tmp_path: Path, name: str, data: str) -> Path:
    """Write CALCITE's header over new data rows."""
    head, _, _ = CALCITE.read_text().partition("2.080\t")
    path = tmp_path / name
    path.write_text(head + data)
    return path


class TestPercentHeaderHoldingFractions:
    """usgs.perknic rocks say "Reflectance (percent)" but hold fractions."""

    PERKNIC = "rock.sedimentary.limestone.solid.all.ro359.usgs.perknic.spectrum.txt"

    def test_fractions_not_divided(self, tmp_path: Path) -> None:
        data = "0.400\t0.541\n0.700\t0.60\n1.000\t0.65\n2.500\t0.55\n5.000\t0.10\n"
        record = parse_ecostress_file(_with_data(tmp_path, self.PERKNIC, data))
        assert record.spectral_data.values == pytest.approx([0.541, 0.60, 0.65, 0.55, 0.10])
        assert record.extra["source_reflectance_divisor"] == 1
        assert record.extra["declared_value_scale"] == "percent"
        assert record.extra["value_scale_rule"] == "declared_percent_level_below_1.5_vswir"
        assert record.extra["source_value_level_p90"] == pytest.approx(0.65)
        assert record.quality.notes is not None and "not divided" in record.quality.notes

    def test_dark_tir_only_spectrum_stays_percent(self, tmp_path: Path) -> None:
        # Carbonaceous chondrites sit near 1.3 % in the TIR: genuinely percent.
        data = "2.080\t1.20\n5.000\t1.30\n10.000\t1.25\n14.000\t1.10\n"
        name = "meteorites.carbonaceouschondrite.ci.fine.tir.orgueil_1.jhu.becknic.spectrum.txt"
        record = parse_ecostress_file(_with_data(tmp_path, name, data))
        assert record.extra["source_reflectance_divisor"] == 100
        assert record.spectral_data.values[0] == pytest.approx(0.012)
        assert "declared_value_scale" not in record.extra

    def test_dark_vswir_percent_spectrum_divided(self, tmp_path: Path) -> None:
        # Tap water, the darkest genuine percent VSWIR spectrum (level ~2.95).
        data = "0.400\t2.0\n1.000\t2.5\n2.000\t2.95\n5.000\t3.0\n"
        name = "water.tapwater.none.liquid.all.tapwater.jhu.becknic.spectrum.txt"
        record = parse_ecostress_file(_with_data(tmp_path, name, data))
        assert record.extra["source_reflectance_divisor"] == 100
        assert record.spectral_data.values[0] == pytest.approx(0.02)


def _classified(tmp_path: Path, *class_lines: str) -> Path:
    """Write a minimal spectrum whose classification header lines are given."""
    header = "\n".join(
        [
            "Name: Sample",
            *class_lines,
            "Measurement: Directional Hemispherical Reflectance",
            "Y Units: Reflectance (percent)",
        ]
    )
    path = tmp_path / "sample.x.jhu.nicolet.spectrum.txt"
    path.write_text(header + "\n2.080\t50.0\n3.000\t60.0\n")
    return path


class TestCategoryFromTypeAndClass:
    @pytest.mark.parametrize(
        "type_, class_, category, original",
        [
            ("Soil", "Lunar", MaterialCategory.LUNAR, "soil"),
            ("Water", "Snow", MaterialCategory.SNOW_ICE, "water"),
            ("Water", "Ice", MaterialCategory.SNOW_ICE, "water"),
            ("Water", "Frost", MaterialCategory.SNOW_ICE, "water"),
            ("Water", "Tap Water", MaterialCategory.WATER, None),
            ("Mineral", "Carbonate", MaterialCategory.MINERAL, None),
            ("non photosynthetic vegetation", "bark", MaterialCategory.NPV, None),
        ],
    )
    def test_specific_class_wins(
        self,
        tmp_path: Path,
        type_: str,
        class_: str,
        category: MaterialCategory,
        original: str | None,
    ) -> None:
        record = parse_ecostress_file(_classified(tmp_path, f"Type: {type_}", f"Class: {class_}"))
        assert record.material.category == category
        assert record.extra.get("original_category") == original
        assert record.extra["source_type"] == type_
        assert record.extra["source_class"] == class_
        assert class_.lower() in record.material.keywords

    def test_class_kept_when_subclass_present(self, tmp_path: Path) -> None:
        path = _classified(tmp_path, "Type: Mineral", "Class: Silicate", "Subclass: Tectosilicate")
        record = parse_ecostress_file(path)
        assert record.material.subcategory == "Tectosilicate"
        assert record.extra["source_class"] == "Silicate"
        assert record.material.keywords == ["sample", "mineral", "silicate", "Tectosilicate"]

    def test_swapped_type_and_class(self, tmp_path: Path) -> None:
        # Some records put the category in Class; Type is then not a category.
        record = parse_ecostress_file(_classified(tmp_path, "Type: ite", "Class: Mineral"))
        assert record.material.category == MaterialCategory.MINERAL
        assert "original_category" not in record.extra


class TestMeasurementSemantics:
    """Geometry from the Measurement header; instrument/lab from the filename."""

    @pytest.mark.parametrize(
        "measurement, geometry",
        [
            # Every spelling observed in the ECOSTRESS 1.0 archive.
            ("Hemispherical reflectance", Geometry.DIRECTIONAL_HEMISPHERICAL),
            ("Hemispherical Reflectance", Geometry.DIRECTIONAL_HEMISPHERICAL),
            ("Directional hemispherical reflectance", Geometry.DIRECTIONAL_HEMISPHERICAL),
            ("Directional Hemispherical Reflectance", Geometry.DIRECTIONAL_HEMISPHERICAL),
            (
                "Directional (10 Degree) Hemispherical Reflectance",
                Geometry.DIRECTIONAL_HEMISPHERICAL,
            ),
            (
                "Directional (10 degree) hemispherical reflectance",
                Geometry.DIRECTIONAL_HEMISPHERICAL,
            ),
            # Laboratory lamp/probe or FTIR-attachment set-ups the source calls
            # "bidirectional" are biconical (spec section 5).
            ("Bidirectional reflectance", Geometry.BICONICAL),
            ("Bidirectional Reflectance", Geometry.BICONICAL),
            ("Bidirectional reflectance Packed", Geometry.BICONICAL),
            # Not stated, transmission, or spliced from two geometries.
            ("Reflectance", Geometry.UNKNOWN),
            ("Transmission", Geometry.UNKNOWN),
            ("Tansmission", Geometry.UNKNOWN),
            ("Transmittance", Geometry.UNKNOWN),
            ("Bidirectional and directional hemispherical reflectance. ", Geometry.UNKNOWN),
            ("", Geometry.UNKNOWN),
        ],
    )
    def test_measurement_geometry(self, measurement: str, geometry: Geometry) -> None:
        assert measurement_geometry(measurement) == geometry

    def test_record_geometry_and_instrument(self, tmp_path: Path) -> None:
        text = CALCITE.read_text().replace(
            "Measurement: Directional Hemispherical Reflectance",
            "Measurement:  Bidirectional reflectance",
        )
        path = tmp_path / "mineral.oxide.none.coarse.tir.ilmenite_1.jhu.nicolet.spectrum.txt"
        path.write_text(text)
        record = parse_ecostress_file(path)
        assert record.measurement.geometry == Geometry.BICONICAL
        assert record.extra["source_geometry_term"] == "Bidirectional reflectance"
        assert record.measurement.instrument == "Nicolet FTIR spectrometer"
        assert record.measurement.instrument_type == "ftir"
        assert record.measurement.laboratory == "Johns Hopkins University"
        assert record.measurement.reference is None
        assert record.measurement.processing == []

    def test_fixture_is_directional_hemispherical(self) -> None:
        # jhu.becknic, but TIR only (from 2.08 um): measured by the Nicolet alone.
        record = parse_ecostress_file(CALCITE)
        assert record.measurement.geometry == Geometry.DIRECTIONAL_HEMISPHERICAL
        assert record.measurement.instrument == "Nicolet FTIR spectrometer"
        assert record.measurement.instrument_type == "ftir"
        assert record.measurement.processing == []
        assert record.extra["source_geometry_term"] == "Directional Hemispherical Reflectance"

    def test_spliced_vswir_tir_spectrum(self, tmp_path: Path) -> None:
        # becknic/perknic spectra covering VSWIR and TIR join two instruments.
        head, _, _ = CALCITE.read_text().partition("2.080\t")
        data = "0.400\t10.0\n1.000\t20.0\n2.500\t30.0\n5.000\t40.0\n14.000\t50.0\n"
        path = tmp_path / "rock.igneous.felsic.solid.all.ap-1.usgs.perknic.spectrum.txt"
        path.write_text(head + data)
        record = parse_ecostress_file(path)
        assert record.measurement.processing == ["spliced"]
        assert record.measurement.instrument_type is None
        assert record.measurement.instrument == (
            "Perkin-Elmer spectrophotometer and Nicolet FTIR spectrometer"
        )
        assert record.measurement.laboratory == "U.S. Geological Survey"

    def test_assembled_segments_have_no_instrument(self, tmp_path: Path) -> None:
        head, _, _ = CALCITE.read_text().partition("2.080\t")
        head = head.replace(
            "Description: Calcite, powdered, carbonate mineral",
            "Description: Dry grass. Spectra were assembled from two segments; the "
            "VNIR/SWIR spectrum was measured with a GER IRIS Mark IV.",
        ).replace(
            "Measurement: Directional Hemispherical Reflectance",
            "Measurement: Bidirectional and directional hemispherical reflectance.",
        )
        data = "0.400\t10.0\n1.000\t20.0\n2.500\t30.0\n5.000\t40.0\n14.000\t50.0\n"
        path = tmp_path / "vegetation.grass.unknown.unknown.all.grass.jhu.becknic.spectrum.txt"
        path.write_text(head + data)
        record = parse_ecostress_file(path)
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.instrument is None
        assert record.measurement.instrument_type is None
        assert record.measurement.processing == ["spliced"]

    def test_transmittance_never_gets_reflectance_geometry(self, tmp_path: Path) -> None:
        # Measurement names a reflectance geometry but Y Units is transmittance.
        text = CALCITE.read_text().replace(
            "Y Units: Reflectance (percent)", "Y Units: Transmittance (percent)"
        )
        path = tmp_path / CALCITE.name
        path.write_text(text)
        record = parse_ecostress_file(path)
        assert record.measurement.quantity == Quantity.TRANSMITTANCE
        assert record.measurement.geometry == Geometry.UNKNOWN

    def test_unrecognised_tokens_leave_fields_unset(self, tmp_path: Path) -> None:
        path = tmp_path / "mineral.x.y.z.tir.s1.lab.gizmo.spectrum.txt"
        shutil.copy(CALCITE, path)
        record = parse_ecostress_file(path)
        assert record.measurement.instrument is None
        assert record.measurement.instrument_type is None
        assert record.measurement.laboratory is None


class TestPlaceholderHeaders:
    """Header placeholders ("None", "N/A", "Unknown.", empty) become null."""

    def test_placeholders_are_null(self, tmp_path: Path) -> None:
        text = (
            CALCITE.read_text()
            .replace("SubClass: Carbonate", "Subclass: None")
            .replace("ParticleSize: 75-250 um", "Particle Size: none")
            .replace("SampleNo: WS272", "Sample No.: N/A")
            .replace("Origin: Ward's Scientific", "Origin: Unknown.")
            .replace("Owner: JHU", "Owner: ")
        )
        path = tmp_path / CALCITE.name
        path.write_text(text)
        record = parse_ecostress_file(path)
        assert record.material.subcategory is None
        assert "None" not in record.material.keywords
        assert record.sample.particle_size is None
        assert record.sample.id is None
        assert record.sample.origin is None
        assert record.sample.owner is None

    def test_real_values_kept(self) -> None:
        record = parse_ecostress_file(CALCITE)
        assert record.material.subcategory == "Carbonate"
        assert record.sample.particle_size == "75-250 um"
        assert record.sample.origin == "Ward's Scientific"


class TestDescriptionParts:
    """Parts the ECOSTRESS conversion appended to Description are separated."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (
                " white limestoneSample was Whole rock chips. Original ASTER Spectral Library "
                "name was usgs.perknic.rock.sedimentary.limestone.solid.ls359.spectrum.txt",
                "white limestone. Sample was Whole rock chips. Original ASTER Spectral Library "
                "name was usgs.perknic.rock.sedimentary.limestone.solid.ls359.spectrum.txt",
            ),
            (
                # No double full stop; a missing space is added.
                "Crysoprase. Particle size was 45-125um.Collected by JPL Original ASTER "
                "Spectral Library name was jpl.beckman.x.spectrum.txt",
                "Crysoprase. Particle size was 45-125um. Collected by JPL. Original ASTER "
                "Spectral Library name was jpl.beckman.x.spectrum.txt",
            ),
            (
                "Geologic age -  CambrianCollected by:  Ward's Original ASTER Spectral "
                "Library name was jpl.perkin.y.spectrum.txt",
                "Geologic age -  Cambrian. Collected by:  Ward's. Original ASTER Spectral "
                "Library name was jpl.perkin.y.spectrum.txt",
            ),
            (
                "It appeared quite pure.(Ring Silicates)Particle size was Less than 2  "
                "Micrometers.",
                "It appeared quite pure.(Ring Silicates). Particle size was Less than 2  "
                "Micrometers.",
            ),
            # Text without appended parts, or starting with one, is unchanged.
            ("Calcite, powdered, carbonate mineral", "Calcite, powdered, carbonate mineral"),
            ("Sample was a 19.5 g fragment.", "Sample was a 19.5 g fragment."),
            ("The sample was pure. Sample was earthy.", "The sample was pure. Sample was earthy."),
        ],
    )
    def test_parts_separated(self, tmp_path: Path, raw: str, expected: str) -> None:
        text = CALCITE.read_text().replace(
            "Description: Calcite, powdered, carbonate mineral", f"Description: {raw}"
        )
        path = tmp_path / CALCITE.name
        path.write_text(text)
        assert parse_ecostress_file(path).sample.description == expected


class TestArchiveQuirks:
    def test_macosx_metadata_ignored(self, tmp_path: Path) -> None:
        shutil.copy(CALCITE, tmp_path / CALCITE.name)
        mac = tmp_path / "__MACOSX"
        mac.mkdir()
        (mac / CALCITE.name).write_bytes(b"\x00\x05\x16\x07Mac OS X")
        (tmp_path / f"._{CALCITE.name}").write_bytes(b"\x00\x05\x16\x07Mac OS X")
        records = list(EcostressLoader().load(tmp_path))
        assert len(records) == 1

    def test_legacy_8bit_encoding(self, tmp_path: Path) -> None:
        text = CALCITE.read_text().replace("Name: Calcite WS272", "Name: Natron Na2CO3\u00b710H2O")
        path = tmp_path / CALCITE.name
        path.write_bytes(text.encode("cp1252"))
        record = parse_ecostress_file(path)
        assert record.name == "Natron Na2CO3\u00b710H2O"


class TestIngestCli:
    """``openspeclib ingest`` must fail the run when a file's unit is unknown."""

    def test_unknown_unit_exits_non_zero(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = CliRunner().invoke(
            main,
            ["ingest", "--source", "ecostress", "--input", str(FIXTURES), "--output", str(out)],
        )
        assert result.exit_code == 1, result.output
        assert "1 ecostress file(s) were rejected" in result.output
        # No partial output is left behind for a later ``combine`` to pick up.
        assert not (out / "ecostress.jsonl").exists()

    def test_clean_input_exits_zero(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        shutil.copy(CALCITE, src / CALCITE.name)
        out = tmp_path / "out"
        result = CliRunner().invoke(
            main, ["ingest", "--source", "ecostress", "--input", str(src), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert "Ingested 1 spectra" in result.output
        assert (out / "ecostress.jsonl").exists()
