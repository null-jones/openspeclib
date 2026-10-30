"""Tests for the Pydantic data models."""

import json
import math

import pytest
from conftest import make_collection_info, make_record
from pydantic import ValidationError

from openspeclib.models import (
    CollectionInfo,
    DatasetInfo,
    Geometry,
    Inclusion,
    MaterialCategory,
    Quantity,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
    is_valid_collection_id,
    is_valid_local_id,
)


class TestSpectrumRecord:
    def test_valid_record(self, sample_spectrum: SpectrumRecord) -> None:
        assert sample_spectrum.id == "usgs_splib07:olivine_gds70"
        assert sample_spectrum.collection == "usgs_splib07"
        assert sample_spectrum.material.category == MaterialCategory.MINERAL
        assert sample_spectrum.measurement.quantity == Quantity.REFLECTANCE
        assert sample_spectrum.measurement.geometry == Geometry.UNKNOWN
        assert len(sample_spectrum.spectral_data.wavelengths) == 5

    def test_serialization_roundtrip(self, sample_spectrum: SpectrumRecord) -> None:
        restored = SpectrumRecord.model_validate_json(sample_spectrum.model_dump_json())
        assert restored == sample_spectrum

    def test_extra(self, sample_spectrum: SpectrumRecord) -> None:
        record = sample_spectrum.model_copy(update={"extra": {"xrd_results": "quartz trace"}})
        assert record.extra["xrd_results"] == "quartz trace"

    def test_no_v0_fields(self, sample_spectrum: SpectrumRecord) -> None:
        data = sample_spectrum.model_dump()
        assert "additional_properties" not in data
        assert "source" not in data
        assert set(data["spectral_data"]) == {
            "wavelength_unit",
            "wavelengths",
            "values",
            "bandpass_fwhm",
            "values_stddev",
        }
        assert set(data["quality"]) == {"notes"}

    @pytest.mark.parametrize(
        "bad_id",
        ["olivine", "ecostress:olivine", "usgs_splib07:", "usgs_splib07:a b", "usgs_splib07:a:b"],
    )
    def test_id_grammar(self, bad_id: str) -> None:
        record = make_record()
        with pytest.raises(ValidationError):
            SpectrumRecord.model_validate({**record.model_dump(), "id": bad_id})

    @pytest.mark.parametrize("bad", ["USGS", "a", "-usgs", "usgs-07", ""])
    def test_collection_grammar(self, bad: str) -> None:
        record = make_record()
        with pytest.raises(ValidationError):
            SpectrumRecord.model_validate({**record.model_dump(), "collection": bad})

    def test_license_required(self) -> None:
        with pytest.raises(ValidationError):
            make_record(license="")


class TestSpectralData:
    def test_lengths_must_match(self) -> None:
        with pytest.raises(ValidationError):
            SpectralData(
                wavelength_unit=WavelengthUnit.NANOMETERS,
                wavelengths=[400.0, 500.0],
                values=[0.1],
            )

    def test_needs_two_points(self) -> None:
        with pytest.raises(ValidationError):
            SpectralData(wavelength_unit=WavelengthUnit.NANOMETERS, wavelengths=[1.0], values=[1])

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
    def test_axis_finite_and_positive(self, bad: float) -> None:
        with pytest.raises(ValidationError):
            SpectralData(
                wavelength_unit=WavelengthUnit.NANOMETERS,
                wavelengths=[bad, 500.0],
                values=[0.1, 0.2],
            )

    def test_bandpass_length(self) -> None:
        with pytest.raises(ValidationError):
            SpectralData(
                wavelength_unit=WavelengthUnit.NANOMETERS,
                wavelengths=[400.0, 500.0],
                values=[0.1, 0.2],
                bandpass_fwhm=[1.0],
            )

    def test_nan_values_survive_json_round_trip(self, sample_spectrum: SpectrumRecord) -> None:
        """Missing bands (NaN) must survive the ingest -> combine JSONL round trip."""
        record = sample_spectrum.model_copy(deep=True)
        record.spectral_data.values[2] = float("nan")
        text = record.model_dump_json()
        assert "NaN" in text
        restored = SpectrumRecord.model_validate_json(text)
        assert math.isnan(restored.spectral_data.values[2])
        assert restored.spectral_data.values[0] == record.spectral_data.values[0]
        assert math.isnan(json.loads(text)["spectral_data"]["values"][2])


class TestCollectionInfo:
    def test_subset_needs_notes(self) -> None:
        with pytest.raises(ValidationError):
            make_collection_info(inclusion=Inclusion.SUBSET)
        info = make_collection_info(inclusion=Inclusion.SUBSET, inclusion_notes="half")
        assert info.to_footer()["inclusion_notes"] == "half"

    def test_doi_pattern(self) -> None:
        with pytest.raises(ValidationError):
            make_collection_info(doi="doi:10.5066/F7RR1WDJ")
        assert make_collection_info(doi="10.5066/F7RR1WDJ").doi == "10.5066/F7RR1WDJ"

    def test_footer_omits_unset(self) -> None:
        footer = make_collection_info(license="CC0-1.0", license_refs={}).to_footer()
        assert set(footer) == {"id", "title", "license", "citation", "inclusion"}

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            CollectionInfo(
                id="abc",
                title="t",
                license="CC0-1.0",
                citation="c",
                inclusion="complete",
                spectrum_count=3,  # type: ignore[call-arg]
            )

    def test_dataset_footer_drops_id(self) -> None:
        ds = DatasetInfo(id="pkg", title="Package", license="CC-BY-4.0", spectra_included=3)
        assert ds.to_footer() == {"title": "Package", "license": "CC-BY-4.0", "spectra_included": 3}


class TestVocabularies:
    def test_material_categories(self) -> None:
        assert len(MaterialCategory) == 13

    def test_quantities(self) -> None:
        assert {q.value for q in Quantity} == {
            "reflectance",
            "transmittance",
            "emissivity",
            "absorbance",
        }

    def test_geometry_has_unknown(self) -> None:
        assert Geometry("unknown") is Geometry.UNKNOWN
        assert len(Geometry) == 8

    def test_wavelength_units(self) -> None:
        assert {u.value for u in WavelengthUnit} == {"um", "nm", "cm-1"}

    def test_id_helpers(self) -> None:
        assert is_valid_collection_id("usgs_splib07")
        assert not is_valid_collection_id("Usgs")
        assert is_valid_local_id("a.b_c~2-d")
        assert not is_valid_local_id("a+b")
        assert not is_valid_local_id("x" * 201)


class TestUpfrontValidation:
    @pytest.mark.parametrize(
        "spectral",
        [
            {"wavelengths": [0.35, 0.45, 0.40, 0.50, 0.55]},
            {"wavelengths": [0.35, 0.40, 0.40, 0.50, 0.55]},
            {"values": [0.1, float("inf"), 0.3, 0.4, 0.5]},
            {"values": [0.1, -1.23e34, 0.3, 0.4, 0.5]},
            {"values": [0.1, 1e39, 0.3, 0.4, 0.5]},
            {"bandpass_fwhm": [0.01, -0.01, 0.01, 0.01, 0.01]},
            {"values_stddev": [0.01, float("inf"), 0.01, 0.01, 0.01]},
        ],
    )
    def test_array_rules(self, spectral) -> None:
        with pytest.raises(ValidationError):
            make_record(**spectral)

    def test_nan_allowed_in_optional_arrays(self) -> None:
        rec = make_record(bandpass_fwhm=[float("nan")] * 5, values_stddev=[0.0] * 5)
        assert math.isnan(rec.spectral_data.bandpass_fwhm[0])

    @pytest.mark.parametrize(
        "license",
        [
            "MIT and Apache-2.0",
            "Creative Commons Attribution",
            "NOASSERTION OR MIT",
            "LicenseRef-USGS_PublicDomain",
            " ",
        ],
    )
    def test_record_licence_syntax(self, license: str) -> None:
        with pytest.raises(ValidationError):
            make_record(license=license)

    @pytest.mark.parametrize("license", ["MIT AND Apache-2.0", "CC-BY-4.0", "NOASSERTION"])
    def test_valid_licences(self, license: str) -> None:
        assert make_record(license=license).license == license

    def test_whitespace_original_id(self) -> None:
        with pytest.raises(ValidationError):
            make_record(original_id="  ")

    def test_dataset_doi_pattern(self) -> None:
        with pytest.raises(ValidationError):
            DatasetInfo(id="p", title="P", doi="https://doi.org/10.21232/C22M27")
        with pytest.raises(ValidationError):
            DatasetInfo(id="p", title="P", license="Creative Commons Attribution")
        assert DatasetInfo(id="p", title="P", doi="10.21232/C22M27").doi

    @pytest.mark.parametrize("key", ["LicenseRef-Foo_Bar", "Foo", "LicenseRef-"])
    def test_license_ref_key_pattern(self, key: str) -> None:
        with pytest.raises(ValidationError):
            make_collection_info(license="NOASSERTION", license_refs={key: {"name": "x"}})

    def test_collection_licence_ref_must_be_defined(self) -> None:
        with pytest.raises(ValidationError, match="undefined"):
            make_collection_info(license="LicenseRef-Missing")
