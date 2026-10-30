"""Tests for the USGS Spectral Library loader."""

import math
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from openspeclib.cli import main
from openspeclib.conformance import check_index
from openspeclib.loaders.usgs import (
    SampleMetadata,
    SampleName,
    UsgsLoader,
    _classify_from_path,
    _detect_spectrometer,
    _find_wavelength_file,
    _parse_name_from_filename,
    _read_single_column,
    parse_sample_name,
    parse_usgs_file,
    read_record_title,
    sample_metadata,
)
from openspeclib.models import (
    Geometry,
    Inclusion,
    MaterialCategory,
    Quantity,
    SpectrumRecord,
    WavelengthUnit,
)
from openspeclib.storage import iter_records

FIXTURES = Path(__file__).parent / "fixtures" / "usgs"
WAVELENGTHS_FILE = FIXTURES / "splib07a_Wavelengths_ASD_0.35-2.5_microns_10_ch.txt"
OLIVINE_FILE = FIXTURES / "ChapterM_Minerals" / "splib07a_Olivine_GDS70_ASDFRa_AREF.txt"
GRANITE_FILE = FIXTURES / "ChapterR_Rocks" / "splib07a_Granite_HS66_ASDFRa_AREF.txt"
# Real-format file: record header line plus deleted channels written as
# ``-1.2300000e+034`` (and one as ``-1.23e34``) at indices 0, 1, 7, 9.
KAOLINITE_FILE = FIXTURES / "ChapterM_Minerals" / "splib07a_Kaolinite_CM9_ASDFRa_AREF.txt"


def _without_rule(extra: dict) -> dict:
    """Return ``extra`` without the ``geometry_rule`` every USGS record carries."""
    return {k: v for k, v in extra.items() if k != "geometry_rule"}


class TestHelperFunctions:
    def test_detect_spectrometer(self) -> None:
        # Spectrometer is the segment immediately before the trailing data-type token.
        assert _detect_spectrometer("splib07a_Olivine_GDS70_ASDFRa_AREF.txt") == "ASDFRa"
        assert _detect_spectrometer("splib07a_Granite_HS66_BECKa_AREF.txt") == "BECKa"
        assert _detect_spectrometer("splib07a_Acmite_NMNH133746_Pyroxene_NIC4a_RREF.txt") == "NIC4a"

    def test_parse_name_from_filename(self) -> None:
        name, sample_id = _parse_name_from_filename("splib07a_Olivine_GDS70_ASDFRa_AREF.txt")
        assert name == "Olivine"
        assert "GDS70" in sample_id

        name, sample_id = _parse_name_from_filename(
            "splib07a_Acmite_NMNH133746_Pyroxene_BECKa_AREF.txt"
        )
        assert name == "Acmite"
        assert sample_id == "NMNH133746_Pyroxene"

    def test_filename_fallback_drops_temperature(self) -> None:
        # Without a record title the filename does not mark where the name
        # ends; the temperature is still not part of the sample id.
        assert _parse_name_from_filename("splib07a_Glycine_SA-G7126_85K_NIC4bb_RREF.txt") == (
            "Glycine",
            "SA-G7126",
        )

    def test_filename_fallback_glued_temperature(self) -> None:
        # ``lt75um85K``: the filename spells ``<`` as ``lt``.
        assert _parse_name_from_filename(
            "splib07a_L-Alanine_SA-26802_lt75um85K_NIC4aa_RREF.txt"
        ) == ("L-Alanine", "SA-26802_lt75um")

    @pytest.mark.parametrize(
        "words, html_title, html_sample_id, name, sample_id, temperature",
        [
            # The first record-title word is the name; locality words are the
            # sample id.
            (["Topaz", "Little_3_Mine_#41"], "", "", "Topaz", "Little_3_Mine_#41", None),
            (["Topaz", "Wigwam_Area_2_#12"], "", "", "Topaz", "Wigwam_Area_2_#12", None),
            (["Melting_snow", "mSnw01a"], "", "", "Melting snow", "mSnw01a", None),
            (
                ["Kaol+Muscov_intimate", "CU93-5C"],
                "",
                "",
                "Kaol + Muscov intimate",
                "CU93-5C",
                None,
            ),
            # cis-/trans- organics stay in the name; the temperature goes to
            # extra and the id comes from SAMPLE_ID.
            (
                ["BHVO-2F+4%_cis-2-Heptene", "85K"],
                "BHVO-2F+4%_cis-2-Heptene",
                "BHVO-2F, cis-2-Heptene SA-268941",
                "BHVO-2F + 4% cis-2-Heptene",
                "SA-268941",
                85.0,
            ),
            (
                ["1-2-3-Trimethylbenzene", "85K"],
                "1,2,3-Trimethylbenzene SA-T73202",
                "1,2,3-Trimethylbenzene SA-T73202",
                "1-2-3-Trimethylbenzene",
                "SA-T73202",
                85.0,
            ),
            (["Methane", "XCEL", "liquid", "T~77K"], "", "", "Methane", "XCEL_liquid", 77.0),
            # A heating temperature is part of the treatment, not a condition.
            (
                ["Basalt", "BHVO-2F", "Htd16hr", "480K"],
                "Basalt BHVO-2F",
                "BHVO-2F",
                "Basalt",
                "BHVO-2F_Htd16hr_480K",
                None,
            ),
            # The page title joins a name the record title splits.
            (
                ["Samarium", "Oxide", "GDS36"],
                "Samarium_Oxide GDS36",
                "GDS36",
                "Samarium Oxide",
                "GDS36",
                None,
            ),
            # A sample id written onto the name moves to the sample id ...
            (["Vermillion_GDS779"], "Vermillion_GDS779", "GDS779", "Vermillion", "GDS779", None),
            # ... but not a component's id when the title has its own id.
            (
                ["Alun366+.50PyroPYS1A", "GDS222"],
                "Alun366+.50PyroPYS1A GDS222",
                "Alunite WS366 + Pyrophyllite PYS1A",
                "Alun366 + .50PyroPYS1A",
                "GDS222",
                None,
            ),
            # Lower-case words before the page's sample id belong to the name.
            (
                ["Iron", "oxide_#4820", "GDS782"],
                "Iron oxide_#4820 GDS782",
                "GDS782",
                "Iron oxide #4820",
                "GDS782",
                None,
            ),
            (
                ["Gadolinium_Nitrate_Hydrate"],
                "GadoliniumNitrateH20 12917",
                "12917 Stock No.",
                "Gadolinium Nitrate Hydrate",
                "12917",
                None,
            ),
            # A title word SAMPLE_ID gives as material (not the id) stays in
            # the name, as in the 4% sibling ``BHVO-2F+4%_Decane 85K``.
            (
                ["BHVO-2F+7%", "Decane"],
                "BHVO-2F+7% Decane",
                "BHVO-2F, Decane SA-30550",
                "BHVO-2F + 7% Decane",
                "SA-30550",
                None,
            ),
            (
                ["BHVO-2F+4%_Decane", "85K"],
                "BHVO-2F+4%_Decane",
                "BHVO-2F, Decane SA-30550",
                "BHVO-2F + 4% Decane",
                "SA-30550",
                85.0,
            ),
            # A code SAMPLE_ID lists before its id is not material.
            (["Talc", "GDS23"], "Talc GDS23", "GDS23 74-250um", "Talc", "GDS23", None),
            (["Kaolinite", "CM9"], "Kaolinite CM9", "CM9 (API9)", "Kaolinite", "CM9", None),
            # A catalogue number written onto the name is the sample id, as in
            # the sibling ``Nickel_Powder SA-268283 <1um``.
            (
                ["Iron_Powder_SA-267953", "<10", "um"],
                "Iron_Powder_SA-267953",
                "Iron powder",
                "Iron Powder",
                "SA-267953_<10_um",
                None,
            ),
            (
                ["Nickel_Powder", "SA-268283", "<1um"],
                "Nickel_Powder SA-268283",
                "Nickel, submicron powder",
                "Nickel Powder",
                "SA-268283_<1um",
                None,
            ),
            # A temperature written onto a frost or grain-size descriptor is
            # the measurement temperature; the descriptor stays in the id.
            (
                ["Heptane", "SA-H2198", "MedFrost85K"],
                "Heptane SA-H2198 15um PL",
                "Heptane SA-H2198",
                "Heptane",
                "SA-H2198_MedFrost",
                85.0,
            ),
            (
                ["Hexane", "SA-139386", "CrsFrost85K"],
                "",
                "Hexane SA-139386",
                "Hexane",
                "SA-139386_CrsFrost",
                85.0,
            ),
            (
                ["Hexane", "SA-139386", "MedFrst87K"],
                "",
                "Hexane SA-139386",
                "Hexane",
                "SA-139386_MedFrst",
                87.0,
            ),
            (
                ["L-Alanine", "SA-26802", "<75um85K"],
                "L-Alanine SA-A26802",
                "L-Alanine SA-A26802",
                "L-Alanine",
                "SA-26802_<75um",
                85.0,
            ),
            (
                ["Pentane", "SA-76878", "fine", "fr86K"],
                "Pentane SA-76878",
                "Pentane SA-76878",
                "Pentane",
                "SA-76878_fine_fr",
                86.0,
            ),
            # A code ending in digits and K is not a temperature.
            (["Alunite", "CU91-217K"], "", "", "Alunite", "CU91-217K", None),
        ],
    )
    def test_parse_sample_name(
        self,
        words: list[str],
        html_title: str,
        html_sample_id: str,
        name: str,
        sample_id: str,
        temperature: float | None,
    ) -> None:
        assert parse_sample_name(words, html_title, html_sample_id) == SampleName(
            name, sample_id, temperature
        )

    def test_sample_metadata_fields(self) -> None:
        page = (
            "<H3>TITLE: Topaz Glen_Cove_#6 DESCRIPT</H3><p>DOCUMENTATION_FORMAT: MINERAL"
            "<p>SAMPLE_ID:   Glen_Cove_#6<p>MINERAL_TYPE: Nesosilicate<p>MINERAL: Topaz"
            "<p>SAMPLE_DESCRIPTION:<p>Pegmatitic topaz.<p>END_SAMPLE_DESCRIPTION."
        )
        assert sample_metadata(page) == SampleMetadata(
            description="Pegmatitic topaz.", title="Topaz Glen_Cove_#6", sample_id="Glen_Cove_#6"
        )

    def test_classify_from_path(self) -> None:
        assert _classify_from_path(OLIVINE_FILE) == MaterialCategory.MINERAL
        assert _classify_from_path(GRANITE_FILE) == MaterialCategory.ROCK

    def test_read_single_column(self) -> None:
        values = _read_single_column(OLIVINE_FILE)
        assert len(values) == 10
        assert values[0] == 0.042


class TestParseUsgsFile:
    def test_mineral_spectrum(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        record = parse_usgs_file(
            OLIVINE_FILE,
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.id == "usgs_splib07:splib07a_Olivine_GDS70_ASDFRa_AREF"
        assert record.material.category == MaterialCategory.MINERAL
        assert record.spectral_data.wavelength_unit == WavelengthUnit.MICROMETERS
        assert len(record.spectral_data.wavelengths) == 10
        assert min(record.spectral_data.wavelengths) == 0.35
        assert max(record.spectral_data.wavelengths) == 0.8
        assert record.measurement.instrument == "ASDFRa"

    def test_rock_spectrum(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        record = parse_usgs_file(
            GRANITE_FILE,
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.material.category == MaterialCategory.ROCK
        assert "Granite" in record.name

    def test_source_provenance(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        record = parse_usgs_file(
            OLIVINE_FILE,
            wavelengths=wavelengths,
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.collection == "usgs_splib07"
        assert record.original_id == "splib07a_Olivine_GDS70_ASDFRa_AREF"
        assert record.license == "LicenseRef-USGS-PublicDomain"
        # Laboratory ASD spectrum in the minerals chapter: BCRF per DS1035.
        assert record.measurement.geometry == Geometry.BICONICAL
        assert record.measurement.reference == "absolute"
        assert record.measurement.instrument_type == "field_spectrometer"
        info = UsgsLoader().collection_info()
        assert "Kokaly" in info.citation
        assert info.doi == "10.5066/F7RR1WDJ"
        assert record.license in info.license_refs
        assert info.inclusion == Inclusion.SUBSET and info.inclusion_notes

    def test_id_sanitised_original_kept(self, tmp_path: Path) -> None:
        odd = (
            tmp_path
            / "ChapterA_ArtificialMaterials"
            / "splib07a_Iron_oxide_#4820_(dk)_ASDFRa_AREF.txt"
        )
        odd.parent.mkdir()
        shutil.copy(OLIVINE_FILE, odd)
        record = parse_usgs_file(
            odd,
            wavelengths=_read_single_column(WAVELENGTHS_FILE),
            bandpass=None,
            source_dir=tmp_path,
        )
        assert record.original_id == "splib07a_Iron_oxide_#4820_(dk)_ASDFRa_AREF"
        assert record.id == "usgs_splib07:splib07a_Iron_oxide__4820__dk__ASDFRa_AREF"

    def test_non_monotonic_axis_is_reordered(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        # Swap two bands to mimic overlapping AVIRIS detectors.
        wavelengths[3], wavelengths[4] = wavelengths[4], wavelengths[3]
        bandpass = [float(i) for i in range(10)]
        raw = _read_single_column(OLIVINE_FILE)
        record = parse_usgs_file(
            OLIVINE_FILE, wavelengths=wavelengths, bandpass=bandpass, source_dir=FIXTURES
        )
        sd = record.spectral_data
        assert sd.wavelengths == sorted(wavelengths)
        assert sd.values[3] == raw[4] and sd.values[4] == raw[3]
        assert sd.bandpass_fwhm is not None and sd.bandpass_fwhm[3] == 4.0
        assert _without_rule(record.extra) == {
            "bands_reordered_by_wavelength": True,
            "source_band_order": [0, 1, 2, 4, 3, 5, 6, 7, 8, 9],
        }

    def test_equal_wavelengths_resolved(self) -> None:
        wavelengths = _read_single_column(WAVELENGTHS_FILE)
        wavelengths[4] = wavelengths[3]
        record = parse_usgs_file(
            OLIVINE_FILE, wavelengths=wavelengths, bandpass=None, source_dir=FIXTURES
        )
        assert len(record.spectral_data.wavelengths) == 9
        assert _without_rule(record.extra) == {
            "duplicate_wavelength_bands_dropped": [[4, wavelengths[3]]],
            "duplicate_wavelength_band_count": 1,
        }

    def test_deleted_channels_counted(self) -> None:
        record = parse_usgs_file(
            KAOLINITE_FILE,
            wavelengths=_read_single_column(WAVELENGTHS_FILE),
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert _without_rule(record.extra) == {"source_missing_band_count": 4}


class TestUsgsLoader:
    def test_load_fixtures(self) -> None:
        loader = UsgsLoader()
        assert loader.source_name() == "usgs_splib07"

        records = list(loader.load(FIXTURES))
        assert len(records) == 3

        categories = {r.material.category for r in records}
        assert MaterialCategory.MINERAL in categories
        assert MaterialCategory.ROCK in categories

    def test_wavelength_lookup_ignores_companion_gif(self, tmp_path: Path) -> None:
        """Regression: the USGS archive ships GIF plots next to axis files.

        ``GIFplots/splib07a_Wavelengths_ASD_*.gif`` shares the splib07a
        prefix and family token with the real ``.txt`` axis, so a glob
        that did not constrain the extension would pick the GIF first on
        Linux filesystems where ``rglob`` walks ``GIFplots/`` ahead of
        ``ASCIIdata/``. Reading the binary as ASCII silently truncated
        spectra to 0 or 1 "points" in the v0.0.2 release.

        Only the GIF is present here; if the glob allowed non-``.txt``
        files it would be returned and the binary would be parsed as an
        axis. The fix scopes the glob to ``.txt``, so no match is found.
        """
        gif_dir = tmp_path / "GIFplots"
        gif_dir.mkdir()
        gif_path = gif_dir / WAVELENGTHS_FILE.name.replace(".txt", ".gif")
        gif_path.write_bytes(b"GIF89a\x00\x00not-a-wavelength-axis\x00")

        spectrum = tmp_path / "splib07a_Olivine_GDS70_ASDFRa_AREF.txt"
        shutil.copy(OLIVINE_FILE, spectrum)

        assert _find_wavelength_file(spectrum, tmp_path) is None

    def test_wavelength_lookup_prefers_txt_over_gif(self, tmp_path: Path) -> None:
        """The real ``.txt`` axis wins when a GIF companion exists."""
        ascii_dir = tmp_path / "ASCIIdata"
        ascii_dir.mkdir()
        shutil.copy(WAVELENGTHS_FILE, ascii_dir / WAVELENGTHS_FILE.name)

        gif_dir = tmp_path / "GIFplots"
        gif_dir.mkdir()
        (gif_dir / WAVELENGTHS_FILE.name.replace(".txt", ".gif")).write_bytes(b"GIF89a")

        spectrum = ascii_dir / "splib07a_Olivine_GDS70_ASDFRa_AREF.txt"
        shutil.copy(OLIVINE_FILE, spectrum)

        resolved = _find_wavelength_file(spectrum, tmp_path)
        assert resolved is not None
        assert resolved.suffix == ".txt"


class TestMissingValues:
    def _parse(self, path: Path) -> SpectrumRecord:
        return parse_usgs_file(
            path,
            wavelengths=_read_single_column(WAVELENGTHS_FILE),
            bandpass=None,
            source_dir=FIXTURES,
        )

    def test_sentinel_becomes_nan(self) -> None:
        record = self._parse(KAOLINITE_FILE)
        values = record.spectral_data.values
        assert len(values) == 10
        nan_idx = [i for i, v in enumerate(values) if math.isnan(v)]
        assert nan_idx == [0, 1, 7, 9]
        # No sentinel survives as a finite magic number.
        assert all(abs(v) < 1e10 for v in values if not math.isnan(v))
        assert values[2] == 0.31

    def test_quality_matches_nan_count(self) -> None:
        record = self._parse(KAOLINITE_FILE)
        assert sum(math.isnan(v) for v in record.spectral_data.values) == 4

    def test_wavelengths_never_nan(self) -> None:
        record = self._parse(KAOLINITE_FILE)
        assert not any(math.isnan(w) for w in record.spectral_data.wavelengths)
        assert min(record.spectral_data.wavelengths) == 0.35

    def test_clean_spectrum_has_no_nan(self) -> None:
        record = self._parse(OLIVINE_FILE)
        assert not any(math.isnan(v) for v in record.spectral_data.values)


class TestQuantityFromDataType:
    def test_tran_is_transmittance(self, tmp_path: Path) -> None:
        tran = tmp_path / "ChapterA_ArtificialMaterials" / "splib07a_Mylar_RefStd_ASDFRa_TRAN.txt"
        tran.parent.mkdir()
        shutil.copy(OLIVINE_FILE, tran)
        record = parse_usgs_file(
            tran,
            wavelengths=_read_single_column(WAVELENGTHS_FILE),
            bandpass=None,
            source_dir=tmp_path,
        )
        assert record.measurement.quantity == Quantity.TRANSMITTANCE

    def test_aref_is_reflectance(self) -> None:
        record = parse_usgs_file(
            OLIVINE_FILE,
            wavelengths=_read_single_column(WAVELENGTHS_FILE),
            bandpass=None,
            source_dir=FIXTURES,
        )
        assert record.measurement.quantity == Quantity.REFLECTANCE


class TestRecordTitle:
    """Names come from the SPECPR record title on the file's first line."""

    @staticmethod
    def _write(tmp_path: Path, chapter: str, filename: str, title: str) -> Path:
        path = tmp_path / chapter / filename
        path.parent.mkdir(exist_ok=True)
        path.write_text(f" splib07a Record=1: {title}\n" + OLIVINE_FILE.read_text())
        return path

    def test_read_record_title(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "ChapterM_Minerals",
            "splib07a_Topaz_Glen_Cove_#6_BECKb_AREF.txt",
            "Topaz Glen_Cove_#6            BECKb AREF",
        )
        assert read_record_title(path) == "Topaz Glen_Cove_#6            BECKb AREF"
        assert read_record_title(OLIVINE_FILE) is None

    def test_locality_is_sample_id(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "ChapterM_Minerals",
            "splib07a_Topaz_Little_3_Mine_#41_BECKb_AREF.txt",
            "Topaz Little_3_Mine_#41       BECKb AREF",
        )
        record = parse_usgs_file(
            path, _read_single_column(WAVELENGTHS_FILE), None, tmp_path, description=""
        )
        assert record.material.name == "Topaz"
        assert record.sample.id == "Little_3_Mine_#41"
        assert record.name == "Topaz Little_3_Mine_#41"
        assert "sample_temperature_k" not in record.extra

    def test_temperature_goes_to_extra(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "ChapterO_OrganicCompounds",
            "splib07a_BHVO-2F+4%_cis-2-Heptene_85K_NIC4aa_RREF.txt",
            "BHVO-2F+4%_cis-2-Heptene 85K  NIC4aa RREF",
        )
        metadata = SampleMetadata(
            description="",
            title="BHVO-2F+4%_cis-2-Heptene",
            sample_id="BHVO-2F, cis-2-Heptene SA-268941",
        )
        record = parse_usgs_file(
            path, _read_single_column(WAVELENGTHS_FILE), None, tmp_path, metadata=metadata
        )
        assert record.material.name == "BHVO-2F + 4% cis-2-Heptene"
        assert record.sample.id == "SA-268941"
        assert record.extra["sample_temperature_k"] == 85

    def test_title_not_matching_filename_falls_back(self, tmp_path: Path) -> None:
        # A title that does not end with the filename's spectrometer and data
        # type is not this file's title; the filename is used instead.
        path = self._write(
            tmp_path,
            "ChapterM_Minerals",
            "splib07a_Olivine_GDS70_ASDFRa_AREF.txt",
            "Something_else X1            BECKb AREF",
        )
        record = parse_usgs_file(path, _read_single_column(WAVELENGTHS_FILE), None, tmp_path)
        assert (record.material.name, record.sample.id) == ("Olivine", "GDS70")


class TestMeasurementSemantics:
    """Geometry / reference / instrument type follow DS1035 (see usgs.py)."""

    @staticmethod
    def _parse(tmp_path: Path, chapter: str, filename: str) -> SpectrumRecord:
        path = tmp_path / chapter / filename
        path.parent.mkdir(exist_ok=True)
        shutil.copy(OLIVINE_FILE, path)
        return parse_usgs_file(
            path,
            wavelengths=_read_single_column(WAVELENGTHS_FILE),
            bandpass=None,
            source_dir=tmp_path,
        )

    @pytest.mark.parametrize(
        "chapter, filename, geometry",
        [
            # Nicolet: BCRF in every chapter.
            ("ChapterM_Minerals", "splib07a_Actinolite_HS22.3B_NIC4bbb_RREF.txt", "biconical"),
            ("ChapterA_ArtificialMaterials", "splib07a_Tape_GDS1_NIC4a_RREF.txt", "biconical"),
            # AVIRIS: HCRF.
            (
                "ChapterV_Vegetation",
                "splib07a_Wetland_YNP-WT-2_AVIRISb_RTGC.txt",
                "hemispherical_conical",
            ),
            # ASD: laboratory-only chapters are BCRF; mixed lab/field chapters unknown.
            (
                "ChapterO_OrganicCompounds",
                "splib07a_L-Proline_SA-P0380_ASDHRa_AREF.txt",
                "biconical",
            ),
            ("ChapterV_Vegetation", "splib07a_Aspen_Aspen-1_green-top_ASDFRa_AREF.txt", "unknown"),
            ("ChapterS_SoilsAndMixtures", "splib07a_Stonewall_Playa_ASDFRa_AREF.txt", "unknown"),
            # Beckman: powders (grain-size token) are BCRF; others may be BHR.
            ("ChapterM_Minerals", "splib07a_Albite_NMNHC5390_74-250um_BECKb_AREF.txt", "biconical"),
            ("ChapterM_Minerals", "splib07a_Chlorite_SMR-13.e_lt30um_BECKb_AREF.txt", "biconical"),
            ("ChapterM_Minerals", "splib07a_Acmite_NMNH133746_Pyroxene_BECKa_AREF.txt", "unknown"),
            # Transmittance: no documented geometry.
            ("ChapterA_ArtificialMaterials", "splib07a_Mylar_RefStd_NIC4aa_TRAN.txt", "unknown"),
        ],
    )
    def test_geometry(self, tmp_path: Path, chapter: str, filename: str, geometry: str) -> None:
        record = self._parse(tmp_path, chapter, filename)
        assert record.measurement.geometry == Geometry(geometry)

    @pytest.mark.parametrize(
        "filename, reference, instrument_type",
        [
            (
                "splib07a_Albite_NMNHC5390_74-250um_BECKb_AREF.txt",
                "absolute",
                "laboratory_spectrometer",
            ),
            ("splib07a_Albite_HS66_NIC4bb_RREF.txt", "stainless_steel_mirror", "ftir"),
            ("splib07a_Albite_HS66_NIC4bb_AREF.txt", "absolute", "ftir"),
            ("splib07a_Albite_HS66_ASDNGa_AREF.txt", "absolute", "field_spectrometer"),
            ("splib07a_Wetland_YNP-WT-2_AVIRISb_RTGC.txt", None, "imaging_spectrometer"),
            ("splib07a_Mylar_RefStd_NIC4aa_TRAN.txt", None, "ftir"),
        ],
    )
    def test_reference_and_instrument_type(
        self, tmp_path: Path, filename: str, reference: str | None, instrument_type: str
    ) -> None:
        record = self._parse(tmp_path, "ChapterM_Minerals", filename)
        assert record.measurement.reference == reference
        assert record.measurement.instrument_type == instrument_type


class TestDescriptionRules:
    """Per-spectrum rules on the HTMLmetadata description (usgs_geometry.py)."""

    @staticmethod
    def _parse(tmp_path: Path, chapter: str, filename: str, description: str) -> SpectrumRecord:
        path = tmp_path / chapter / filename
        path.parent.mkdir(exist_ok=True)
        shutil.copy(OLIVINE_FILE, path)
        return parse_usgs_file(
            path,
            wavelengths=_read_single_column(WAVELENGTHS_FILE),
            bandpass=None,
            source_dir=tmp_path,
            description=description,
        )

    @pytest.mark.parametrize(
        "chapter, filename, description, geometry, rule",
        [
            (
                "ChapterV_Vegetation",
                "splib07a_Buckbrush_CA01-CECU-3_bush_3_ASDFRa_AREF.txt",
                "This spectrum is the average reflectance of 10 field measurements of a "
                "buckbrush bush. The bare fiber of the spectrometer was used to measure "
                "the reflectance spectra of the bush.",
                "hemispherical_conical",
                "asd_field_natural_light",
            ),
            (
                "ChapterV_Vegetation",
                "splib07a_S.alternif._CRMS322v06_grn.a_ASDFRa_AREF.txt",
                "This spectrum is the average reflectance of 8 field measurements (using an "
                "artificial light source) of green leaves. The contact probe attachement "
                "was used.",
                "biconical",
                "asd_artificial_light",
            ),
            (
                # "field" alone does not make a field (HCRF) measurement.
                "ChapterV_Vegetation",
                "splib07a_Cheatgrass_ANPC1_field_calib_ASDFRa_AREF.txt",
                "Mature brown cheat grass collected from large field in early June, 1995.",
                "unknown",
                "asd_setup_not_stated",
            ),
            (
                "ChapterV_Vegetation",
                "splib07a_Lodgepole-Pine_LP-Needles-1_ASDFRa_AREF.txt",
                "Reflectance of needles of lodgepole pine measured in the laboratory.",
                "biconical",
                "asd_laboratory",
            ),
            (
                "ChapterA_ArtificialMaterials",
                "splib07a_Cobalt_blue_GDS790_ASDFRa_AREF.txt",
                "Dry powdered paint pigment from Kremer Pigments Inc.",
                "biconical",
                "asd_laboratory_powder",
            ),
            (
                "ChapterM_Minerals",
                "splib07a_Sphalerite_HS136.3B_BECKb_AREF.txt",
                "Grain size fractions are indicated by the extension after the sample number",
                "biconical",
                "beckman_powder_hs_size_fraction",
            ),
            (
                "ChapterM_Minerals",
                "splib07a_Elbaite_NMNH94217-1.a_659_BECKb_AREF.txt",
                "Sample is in the following sieve intervals: (a) 659um, (b) 196um",
                "biconical",
                "beckman_powder_description",
            ),
            (
                "ChapterS_SoilsAndMixtures",
                "splib07a_Basalt_fresh_BR93-46B_BECKb_AREF.txt",
                "Basalt - gray vesicular flow rock (Fresh interior surface).",
                "bihemispherical",
                "beckman_surface_over_port",
            ),
            (
                # Thin films on glass: powder wording does not mean a sample cup.
                "ChapterC_Coatings",
                "splib07a_Hematite_Thin_Film_GDS27_BECKa_AREF.txt",
                "Fine grained powder was mixed with water and an eye-dropper was used to "
                "deposit a thin coating on a microscope glass thin section.",
                "unknown",
                "beckman_setup_not_stated",
            ),
            (
                "ChapterM_Minerals",
                "splib07a_Gypsum_SU2202_BECKa_AREF.txt",
                "",
                "unknown",
                "beckman_setup_not_stated",
            ),
        ],
    )
    def test_rule(
        self,
        tmp_path: Path,
        chapter: str,
        filename: str,
        description: str,
        geometry: str,
        rule: str,
    ) -> None:
        record = self._parse(tmp_path, chapter, filename, description)
        assert record.measurement.geometry == Geometry(geometry)
        assert record.extra["geometry_rule"] == rule

    def test_computed_mixture_is_simulated(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path,
            "ChapterL_Liquids",
            "splib07a_Melting_snow_mSnw01a+0.5_veg_ASDFRa_AREF.txt",
            "Constructed mixtures sequence for use in mapping melting snow in forests. "
            "Because all spectra used to construct the mixture are spectrally pure, these "
            "computed spectra are rated spectrally pure.",
        )
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.processing == ["simulated"]
        assert record.material.category == MaterialCategory.MIXTURE
        assert record.extra["original_category"] == "snow_ice"
        assert record.extra["category_rule"] == "computed_mixture"
        assert record.extra["geometry_rule"] == "computed_spectrum"

    def test_computed_nicolet_mixture_has_no_geometry(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path,
            "ChapterS_SoilsAndMixtures",
            "splib07a_Kaol.5+MuscCU91-250A_AMX13_NIC4b_AREF.txt",
            "Calculated areal (linear) mixture of 50% kaolinite + 50% Muscovite.",
        )
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.processing == ["simulated"]
        assert record.material.category == MaterialCategory.MIXTURE
        # Chapter S is "Soils and Mixtures": a laboratory mixture there is a
        # mixture by its own rule, so no chapter category is overridden.
        assert record.extra["category_rule"] == "chapter_s_laboratory_mixture"
        assert "original_category" not in record.extra

    @pytest.mark.parametrize(
        "filename, description",
        [
            (
                "splib07a_Grass_dry.83+.17NaMont_AMX35_BECKb_AREF.txt",
                "Cheat Grass ANP92-11A is described in the plant section of this USGS spectral "
                "library, while Na-montmorillonite SWy-1 is described in the mineral section "
                "of this USGS spectral library. Computed areal mixture.",
            ),
            (
                "splib07a_Stonwll_Ply+.1grnGrass_AMX33_BECKa_AREF.txt",
                "Stonewall Playa CU93-52A + green Lawn_Grass GDS91 in two areal mixtures: "
                "Stonewall Playa 0.9 + 0.1 green Grass = AMX33. Computed areal mixture.",
            ),
        ],
    )
    def test_computed_areal_mixture_is_simulated(
        self, tmp_path: Path, filename: str, description: str
    ) -> None:
        record = self._parse(tmp_path, "ChapterV_Vegetation", filename, description)
        assert record.measurement.processing == ["simulated"]
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.extra["geometry_rule"] == "computed_spectrum"
        assert record.material.category == MaterialCategory.MIXTURE
        assert record.extra["original_category"] == "vegetation"
        assert record.extra["category_rule"] == "computed_mixture"

    @pytest.mark.parametrize(
        "chapter, filename, description, original",
        [
            # AMX areal-mixture series, measured, filed in chapter V.
            (
                "ChapterV_Vegetation",
                "splib07a_Antigorite+.33DryGrass_AMX25_ASDFRa_AREF.txt",
                "Grass from side of road just west of Bld. 20. Antigorite sample is pale "
                "green massive antigorite.",
                "vegetation",
            ),
            (
                "ChapterO_OrganicCompounds",
                "splib07a_BHVO-2F+4%_1-Decyne_NIC4aa_RREF.txt",
                "Sample was prepared by adding organic compound to a fine ground Basalt and "
                "grinding in an alumina mortar and pestle.",
                "organic_compound",
            ),
            (
                "ChapterA_ArtificialMaterials",
                "splib07a_Montmorill+Benzene_SWy_1ml_BECKa_AREF.txt",
                "1 ml of benzene solvent mixed with clay Spectrum has normal clay absorptions.",
                "man_made",
            ),
            (
                "ChapterA_ArtificialMaterials",
                "splib07a_Vermiculite+1wt%amph._GDS451_ASDFRb_AREF.txt",
                "Samples were prepared mixtures of Scotts expanded verimculite (GDS327) plus "
                "fibrous Libby amphibole in 0 wt%, 1wt%, and 5wt% increments.",
                "man_made",
            ),
        ],
    )
    def test_laboratory_mixture_in_any_chapter(
        self, tmp_path: Path, chapter: str, filename: str, description: str, original: str
    ) -> None:
        record = self._parse(tmp_path, chapter, filename, description)
        assert record.material.category == MaterialCategory.MIXTURE
        assert record.extra["original_category"] == original
        assert record.extra["category_rule"] == "laboratory_mixture"
        assert record.measurement.processing != ["simulated"]

    def test_unmixed_basalt_reference_is_a_rock(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path,
            "ChapterO_OrganicCompounds",
            "splib07a_Basalt_BHVO-2F_100__NIC4aa_RREF.txt",
            "Basalt from the Hawaiian Volcano Observatory, ground to a fine powder.",
        )
        assert record.material.category == MaterialCategory.ROCK
        assert record.extra["original_category"] == "organic_compound"
        assert record.extra["category_rule"] == "rock_in_title"

    @pytest.mark.parametrize(
        "filename",
        [
            "splib07a_Vermiculite+0wt%amph._GDS327_ASDFRb_AREF.txt",
            "splib07a_Vermiculite+0%amph._GDS327_ASDFRb_AREF.txt",
        ],
    )
    def test_zero_fraction_member_is_not_a_mixture(self, tmp_path: Path, filename: str) -> None:
        # The 0% member of the vermiculite + amphibole series is the pure
        # GDS327 vermiculite, though the series description says "prepared
        # mixtures".
        record = self._parse(
            tmp_path,
            "ChapterA_ArtificialMaterials",
            filename,
            "Samples were prepared mixtures of Scotts expanded verimculite (GDS327) plus "
            "fibrous Libby amphibole in 0 wt%, 1wt%, and 5wt% increments.",
        )
        assert record.material.category == MaterialCategory.MAN_MADE
        assert record.extra["category_rule"] == "zero_fraction_reference"
        assert "original_category" not in record.extra

    @pytest.mark.parametrize(
        "filename",
        [
            "splib07a_Vermiculite+5wt%amph._GDS454_ASDFRb_AREF.txt",
            "splib07a_Kaol_Wxl+0.5Musc_Ruby_AMX12_BECKa_AREF.txt",
            "splib07a_A+0wt%B+5wt%C_X1_ASDFRb_AREF.txt",
        ],
    )
    def test_non_zero_fraction_stays_a_mixture(self, tmp_path: Path, filename: str) -> None:
        record = self._parse(
            tmp_path,
            "ChapterA_ArtificialMaterials",
            filename,
            "Samples were prepared mixtures of two materials.",
        )
        assert record.material.category == MaterialCategory.MIXTURE
        assert record.extra["category_rule"] == "laboratory_mixture"

    def test_mineral_composition_is_not_a_mixture(self, tmp_path: Path) -> None:
        # Chapter S's wt% wording does not apply elsewhere.
        record = self._parse(
            tmp_path,
            "ChapterM_Minerals",
            "splib07a_Topaz_Glen_Cove_#6_BECKb_AREF.txt",
            "H2O+ contents (in wt. %) for rhyolitic topaz ranged from 0.06 to 0.11.",
        )
        assert record.material.category == MaterialCategory.MINERAL
        assert "category_rule" not in record.extra

    def test_field_spectra_convolved_to_aviris(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path,
            "ChapterV_Vegetation",
            "splib07a_Bacterial_mat_YNP-B1_AVIRISb_RTGC.txt",
            "Thirty spectra of the mat were measured in the field using a field "
            "spectrometer. These spectra were averaged and convolved to the wavelength and "
            "bandpass characteristics of the Airborne Visible and InfraRed Imaging "
            "Spectrometer (AVIRIS).",
        )
        assert record.measurement.processing == ["resampled"]
        assert record.measurement.instrument_type == "field_spectrometer"
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.extra["geometry_rule"] == "field_spectrometer_convolved_setup_not_stated"
        assert record.extra["resampled_to_channels_of"] == "AVIRISb"

    def test_aviris_pixel_average_stays_imaging(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path,
            "ChapterV_Vegetation",
            "splib07a_Aspen_YNP-AS-1_forest_AVIRISb_RTGC.txt",
            "Averaged spectral reflectance for an aspen stand. This average spectrum was "
            "computed from 53 pixels.",
        )
        assert record.measurement.processing == []
        assert record.measurement.instrument_type == "imaging_spectrometer"
        assert record.measurement.geometry == Geometry.HEMISPHERICAL_CONICAL

    def test_field_average_holding_fibre_above(self, tmp_path: Path) -> None:
        # Same description as the BECK-coded copy (GEOMETRY_EXCEPTIONS).
        record = self._parse(
            tmp_path,
            "ChapterS_SoilsAndMixtures",
            "splib07a_Stonewall_Playa_Dry_Mud_2001_ASDFRa_AREF.txt",
            "This is not a spectrum of a collected sample but it is an average of 174 "
            "spectra collected of the Stonewall Playa surface. The field spectra where "
            "collected in a polygonal area. Spectra were collected while holding the "
            "spectrometer fiber optic about 1 meter above the surface and walking.",
        )
        assert record.measurement.geometry == Geometry.HEMISPHERICAL_CONICAL
        assert record.extra["geometry_rule"] == "asd_field_natural_light"
        assert record.material.category == MaterialCategory.SOIL

    @pytest.mark.parametrize(
        "filename, description, category, rule",
        [
            (
                "splib07a_Calcite.33+Epidote.67_GDS311_ASDFRa_AREF.txt",
                "Samples constructed from ground and sieved pure minerals GDS297 (epidote).",
                "mixture",
                "chapter_s_laboratory_mixture",
            ),
            (
                "splib07a_Calcite.5+Dolomite.5_AMX8_BECKb_AREF.txt",
                "Calcite 50% dolomite 50% See WS272, HS102.3B",
                "mixture",
                "chapter_s_laboratory_mixture",
            ),
            (
                "splib07a_Jarosite_Rhyolite_CU91-20A_BECKa_AREF.txt",
                "This sample is of a jarosite coating on a hydrothermally altered rhyolite.",
                "mixture",
                "chapter_s_coating_or_precipitate",
            ),
            (
                "splib07a_Basalt_fresh_BR93-46B_BECKb_AREF.txt",
                "Basalt - gray vesicular flow rock (Fresh interior surface).",
                "rock",
                "chapter_s_rock_in_title",
            ),
            (
                "splib07a_Illite_CU00-5B_Hi-Al+Quartz_ASDFRa_AREF.txt",
                "This sample was collected from the southern part of the western alteration "
                "center at Cuprite, NV. The sample was collected from the altered lower "
                "Cambrian Harkless Phyllite that was hydrothermally altered about 7.5 Ma.",
                "rock",
                "chapter_s_rock_in_description",
            ),
            (
                "splib07a_Calcite+Talc_PC99-1G_ASDFRc_AREF.txt",
                "Talc conglomerate from waste rock pile (Empire Canyon, Utah)",
                "rock",
                "chapter_s_rock_in_description",
            ),
            (
                "splib07a_Sand_GrndIsle1_no_oil_ASDFRa_AREF.txt",
                "An area of bright clean dune sand that appeared to be oil-free.",
                "soil",
                "chapter_s_soil",
            ),
            (
                "splib07a_Montmorillonite-Na_CU93-52A_BECKc_AREF.txt",
                "This sample was collected from the surface of the dry Stonewall Mud playa.",
                "soil",
                "chapter_s_soil",
            ),
            (
                # Where the mineral occurs is not what the sample is.
                "splib07a_Natrolite+Zeolite_HS168.3B_BECKb_AREF.txt",
                'Dimorphous with Tetranatrolite. "T-21 Natrolite 168B. This Zeolite occurs '
                "in cavities in basalt's and related rocks.",
                "mixture",
                "chapter_s_mixture",
            ),
            (
                # Two phases of one mineral group (K- and Na-alunite).
                "splib07a_Alunite0.35K+.65Na_CU91-217H_ASDFRb_AREF.txt",
                "This sample contains two alunite phases from Alunite Hill. It was deposited "
                "by hot hydrothermal fluids that altered lower Cambrian Harkless Phyllite.",
                "mineral",
                "chapter_s_single_mineral",
            ),
            (
                "splib07a_Pyrite_LV95-6A_Weath_on_Tail_BECKb_AREF.txt",
                "Weathered Pyrite from the surface of the Strayhorse Gulch mine tailings 1 km "
                "east of the City of Leadville. Sample may possibly contain other sulfides.",
                "mineral",
                "chapter_s_single_mineral",
            ),
            (
                # A second mineral in the title: stays a mixture.
                "splib07a_ClinozoisiteEpidote_HS299.1B_ASDFRb_AREF.txt",
                "Clinozoisite is dimorphous with Zoisite. The sample contains some epidote and "
                "quartz impurities.",
                "mixture",
                "chapter_s_mixture",
            ),
            (
                "splib07a_Covellite-Pyrite_HS477.4B_ASDFRb_AREF.txt",
                "",
                "mixture",
                "chapter_s_mixture",
            ),
        ],
    )
    def test_chapter_s_categories(
        self, tmp_path: Path, filename: str, description: str, category: str, rule: str
    ) -> None:
        record = self._parse(tmp_path, "ChapterS_SoilsAndMixtures", filename, description)
        assert record.material.category == MaterialCategory(category)
        assert record.extra["category_rule"] == rule
        assert "original_category" not in record.extra

    def test_category_rule_only_in_chapter_s(self, tmp_path: Path) -> None:
        record = self._parse(tmp_path, "ChapterR_Rocks", "splib07a_Granite_HS66_BECKa_AREF.txt", "")
        assert record.material.category == MaterialCategory.ROCK
        assert "category_rule" not in record.extra

    def test_computed_non_mixture_keeps_category(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path,
            "ChapterV_Vegetation",
            "splib07a_LawnGrass_GDS91b_shifted_3nm_BECKa_AREF.txt",
            "The original sample was measured, and a series of shifted and ratioed "
            "spectra computed for spectrally testing chlorophylll absorption band shifts.",
        )
        assert record.measurement.processing == ["simulated"]
        assert record.material.category == MaterialCategory.VEGETATION
        assert "original_category" not in record.extra

    def test_nicolet_scaled_to_reference_level(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path, "ChapterM_Minerals", "splib07a_Albite_HS66_NIC4bb_RREF.txt", ""
        )
        assert record.measurement.processing == ["scaled_to_reference_level"]

    @pytest.mark.parametrize(
        "filename, category",
        [
            ("splib07a_Seawater_Coast_Chl_SW1_BECKa_AREF.txt", MaterialCategory.WATER),
            ("splib07a_Water+Montmor_SWy-2+0.50g-l_ASDFRa_AREF.txt", MaterialCategory.WATER),
            ("splib07a_H2O-Ice_GDS136_77K_BECKa_AREF.txt", MaterialCategory.SNOW_ICE),
            ("splib07a_Melting_snow_mSnw03_ASDFRa_AREF.txt", MaterialCategory.SNOW_ICE),
        ],
    )
    def test_liquids_categories(
        self, tmp_path: Path, filename: str, category: MaterialCategory
    ) -> None:
        record = self._parse(tmp_path, "ChapterL_Liquids", filename, "")
        assert record.material.category == category

    def test_seawater_is_interpolated_external_data(self, tmp_path: Path) -> None:
        record = self._parse(
            tmp_path,
            "ChapterL_Liquids",
            "splib07a_Seawater_Coast_Chl_SW1_BECKa_AREF.txt",
            "The spectrum shown is the original data interpolated to our wavelengths and "
            "water first surface reflectance added.",
        )
        assert record.measurement.geometry == Geometry.UNKNOWN
        assert record.measurement.processing == ["interpolated"]

    def test_loader_reads_html_metadata(self, tmp_path: Path) -> None:
        shutil.copytree(FIXTURES, tmp_path / "usgs_splib07", dirs_exist_ok=True)
        html_dir = tmp_path / "usgs_splib07" / "HTMLmetadata"
        html_dir.mkdir()
        (html_dir / "Granite_HS66_ASDFRa_AREF.html").write_text(
            "<html><body>SAMPLE_DESCRIPTION: Spectrum <b>measured in the laboratory</b>."
            "<br>END_SAMPLE_DESCRIPTION. CURRENT_SAMPLE_LOCATION: field lab</body></html>"
        )
        records = {r.original_id: r for r in UsgsLoader().load(tmp_path)}
        granite = records["splib07a_Granite_HS66_ASDFRa_AREF"]
        assert granite.measurement.geometry == Geometry.BICONICAL
        assert granite.extra["geometry_rule"] == "asd_laboratory"


class TestIngestCombineRoundTrip:
    def test_nan_survives_into_parquet(self, tmp_path: Path) -> None:
        """Deleted channels stay NaN through ingest JSONL and combine Parquet."""
        processed = tmp_path / "processed"
        library = tmp_path / "library"
        runner = CliRunner()

        result = runner.invoke(
            main,
            ["ingest", "--source", "usgs", "--input", str(FIXTURES), "--output", str(processed)],
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(
            main, ["combine", "--input", str(processed), "--output", str(library)]
        )
        assert result.exit_code == 0, result.output

        assert sorted(p.name for p in library.iterdir()) == [
            "checksums.txt",
            "id-migration.csv",
            "openspeclib-index.parquet",
            "usgs_splib07.parquet",
        ]
        assert check_index(library / "openspeclib-index.parquet").conformant
        records = {r.id: r for r in iter_records(library / "usgs_splib07.parquet")}
        kaolinite = records["usgs_splib07:splib07a_Kaolinite_CM9_ASDFRa_AREF"]
        values = kaolinite.spectral_data.values
        assert [i for i, v in enumerate(values) if math.isnan(v)] == [0, 1, 7, 9]
        assert all(abs(v) < 1e10 for v in values if not math.isnan(v))
        assert sum(math.isnan(v) for v in kaolinite.spectral_data.values) == 4
        assert not any(math.isnan(w) for w in kaolinite.spectral_data.wavelengths)
