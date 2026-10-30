"""Tests for the EcoSIS loader."""

import io
import json
import math
import random
import urllib.error
from pathlib import Path

import pytest

import openspeclib.loaders.ecosis as ecosis
from openspeclib.loaders.ecosis import (
    DatasetPolicy,
    EcosisLoader,
    SamplingSpec,
    _allocate_strata,
    _bundle_is_complete,
    _classify_target_type,
    _detect_quantity,
    _iter_bundle,
    _parse_datapoints,
    _parse_policy,
    load_dataset_policies,
)
from openspeclib.loaders.ecosis_scales import (
    dominant_value_fraction,
    infer_dataset_divisor,
    resolve_spectrum_divisor,
    scale_and_mask,
    spectrum_level,
)
from openspeclib.models import Geometry, MaterialCategory, Quantity

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ecosis"


class TestParseDatapoints:
    def test_valid_datapoints(self):
        dp = {"350": "0.05", "400": "0.10", "450": "0.15"}
        wl, vals = _parse_datapoints(dp)
        assert wl == [350.0, 400.0, 450.0]
        assert vals == [0.05, 0.10, 0.15]

    def test_filters_non_numeric_keys(self):
        dp = {"350": "0.05", "Common Name": "oak", "400": "0.10"}
        wl, vals = _parse_datapoints(dp)
        assert len(wl) == 2
        assert wl == [350.0, 400.0]

    def test_sorts_by_wavelength(self):
        dp = {"500": "0.3", "350": "0.1", "400": "0.2"}
        wl, vals = _parse_datapoints(dp)
        assert wl == [350.0, 400.0, 500.0]
        assert vals == [0.1, 0.2, 0.3]

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="No valid spectral data"):
            _parse_datapoints({})

    def test_all_metadata_raises(self):
        dp = {"Common Name": "oak", "Latin Genus": "Quercus"}
        with pytest.raises(ValueError, match="No valid spectral data"):
            _parse_datapoints(dp)

    def test_handles_float_wavelengths(self):
        dp = {"350.5": "0.05", "400.2": "0.10"}
        wl, vals = _parse_datapoints(dp)
        assert wl == [350.5, 400.2]


class TestClassifyTargetType:
    def test_leaf(self):
        assert _classify_target_type(["leaf"]) == MaterialCategory.VEGETATION

    def test_canopy(self):
        assert _classify_target_type(["canopy"]) == MaterialCategory.VEGETATION

    def test_soil(self):
        assert _classify_target_type(["soil"]) == MaterialCategory.SOIL

    def test_water(self):
        assert _classify_target_type(["water"]) == MaterialCategory.WATER

    def test_npv(self):
        assert _classify_target_type(["bark"]) == MaterialCategory.NPV

    def test_unknown_returns_other(self):
        assert _classify_target_type(["something_weird"]) == MaterialCategory.OTHER

    def test_empty_returns_other(self):
        assert _classify_target_type([]) == MaterialCategory.OTHER

    def test_case_insensitive(self):
        assert _classify_target_type(["Leaf"]) == MaterialCategory.VEGETATION

    def test_first_match_wins(self):
        assert _classify_target_type(["leaf", "soil"]) == MaterialCategory.VEGETATION


class TestDetectTechnique:
    def test_reflectance(self):
        assert _detect_quantity(["reflectance"]) == Quantity.REFLECTANCE

    def test_transmittance(self):
        assert _detect_quantity(["transmittance"]) == Quantity.TRANSMITTANCE

    def test_absorptance_is_not_absorbance(self):
        # Absorptance (1 - R - T) is a fraction, not log10 absorbance: it has
        # no technique and spectra of it are dropped by the loader.
        assert "absorptance" not in ecosis.ECOSIS_QUANTITY_MAP
        assert "absorptance" in ecosis.UNSUPPORTED_QUANTITIES
        assert _detect_quantity(["absorptance"]) == Quantity.REFLECTANCE
        assert _detect_quantity(["absorptance", "transmittance"]) == (Quantity.TRANSMITTANCE)

    def test_default_reflectance(self):
        assert _detect_quantity([]) == Quantity.REFLECTANCE


class TestInferDatasetDivisor:
    def test_unit_scale_dataset(self):
        # Typical reflectance values, all under 1.0 → divisor 1.
        spectra = [[0.05, 0.1, 0.2, 0.5], [0.04, 0.08, 0.18, 0.45]]
        assert infer_dataset_divisor(spectra) == 1

    def test_percent_scale_dataset(self):
        # Typical 0-100 reflectance values → divisor 100.
        spectra = [[5.0, 10.0, 25.0, 60.0], [3.0, 8.0, 22.0, 55.0]]
        assert infer_dataset_divisor(spectra) == 100

    def test_scaled_int_dataset(self):
        # Typical 0-10000 scaled-integer reflectance → divisor 10000.
        spectra = [[500, 1500, 3000, 5500], [400, 1400, 2800, 5300]]
        assert infer_dataset_divisor(spectra) == 10000

    def test_unit_scale_with_one_outlier_spectrum(self):
        # A single mis-scaled spectrum shouldn't flip the classification —
        # the dataset decision uses the median of per-spectrum levels (90th
        # percentiles), which still sits below the unit ceiling.
        spectra = [
            [0.05, 0.1, 0.2, 0.5],
            [0.04, 0.08, 0.18, 0.45],
            [0.04, 0.08, 0.18, 0.45],
            [0.04, 0.08, 0.18, 99.0],  # one bad spectrum
            [0.04, 0.08, 0.18, 0.45],
        ]
        assert infer_dataset_divisor(spectra) == 1

    def test_emissivity_above_one_still_unit(self):
        # Emissivity / absorbance can legitimately exceed 1.0 in some
        # bands but stays well below the 1.5 unit ceiling.
        spectra = [[0.5, 0.9, 1.1, 1.2], [0.4, 0.85, 1.05, 1.15]]
        assert infer_dataset_divisor(spectra) == 1

    def test_empty_dataset_defaults_to_one(self):
        assert infer_dataset_divisor([]) == 1
        assert infer_dataset_divisor([[], []]) == 1

    def test_picks_smallest_divisor_that_normalises(self):
        # A dataset whose median level sits at ~70 fits divisor 100 (0.7)
        # but not divisor 1 (70 > 1.5). The function must pick the smallest
        # divisor that brings the median level under the unit ceiling.
        spectra = [[10.0, 30.0, 50.0, 70.0]] * 5
        assert infer_dataset_divisor(spectra) == 100


class TestEcosisLoader:
    def test_source_name(self):
        loader = EcosisLoader()
        assert loader.source_name() == "ecosis"

    def test_load_fixture(self):
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        assert len(records) == 2

        r = records[0]
        # The local id is the spectrum's EcoSIS ``_id`` alone.
        assert r.id == f"ecosis:{r.original_id}"
        assert r.collection == "ecosis"
        # The package's licence label is mapped to SPDX.
        assert r.license == "ODbL-1.0"
        assert r.dataset_id == "03e46f54-7d68-4a8c-896a-fcea87e9cf10"
        assert r.spectral_data.wavelength_unit.value == "nm"
        assert len(r.spectral_data.wavelengths) > 0
        assert len(r.spectral_data.wavelengths) == len(r.spectral_data.wavelengths)
        assert len(r.spectral_data.values) == len(r.spectral_data.wavelengths)
        assert r.material.category == MaterialCategory.VEGETATION
        # Dataset traceability lives in the footer datasets, not in extra. The
        # USDA PLANTS symbol names the species, so it is not the sample id.
        assert r.extra == {"usda_symbol": "POTR5"}
        assert r.sample is None or r.sample.id is None

    def test_dataset_metadata_populated(self):
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        ds = loader.datasets()[records[0].dataset_id]
        assert ds.id == "03e46f54-7d68-4a8c-896a-fcea87e9cf10"
        assert ds.license == "ODbL-1.0"
        assert ds.license_note is None
        assert ds.doi == "10.21232/C22M27"
        assert ds.citation
        assert ds.url and ds.url.startswith("https://ecosis.org/package/")
        assert ds.description and "fresh leaf spectra" in ds.description
        assert ds.spectra_included == len(records)
        assert ds.spectra_available is not None and ds.spectra_available >= len(records)
        assert ds.inclusion is not None
        assert ds.inclusion.value == ("complete" if ds.spectra_available == 2 else "subset")

    def test_measurement_context_populated(self):
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        r = records[0]
        assert r.measurement.light_source == "lamp"
        assert r.measurement.venue == "greenhouse"
        assert r.measurement.acquisition_method == "contact"
        assert r.measurement.foreoptic == "leaf clip"
        # The fixture has Processing Averaged=["yes"], Interpolated=["no"],
        # Resampled=["no"], so only "averaged" is surfaced.
        assert r.measurement.processing == ["averaged"]

    def test_fixture_provenance(self):
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        for r in records:
            assert r.collection == "ecosis"
            assert r.dataset_id == "03e46f54-7d68-4a8c-896a-fcea87e9cf10"
            # Biotron: lamp-illuminated ASD leaf clip, per the package metadata.
            assert r.measurement.geometry.value == "biconical"

    def test_fixture_is_auto_detected_as_unit_scale(self):
        # The bundled fixture's spectrum levels (90th percentiles) are well
        # under the 1.5 unit ceiling, so the dataset divisor is 1 and the
        # loader applies no normalisation.
        loader = EcosisLoader()
        records = list(loader.load(FIXTURE_DIR))

        for r in records:
            assert "source_reflectance_divisor" not in r.extra


class TestDownloadCompleteness:
    """The downloader must fetch complete datasets or fail loudly.

    Releases v0.0.6/v0.0.7 shipped 16–21% of the curated EcoSIS spectra
    because pagination silently truncated on transient failures and saved
    partial bundles that were then skipped forever.
    """

    def _spectra(self, n):
        return [{"_id": f"s{i}", "datapoints": {"350": "0.1"}} for i in range(n)]

    def test_fetch_all_spectra_survives_empty_pages(self, monkeypatch):
        pages = iter(
            [
                {"items": self._spectra(100)},
                {"items": []},  # rate-limited soft failure
                {"items": []},  # again
                {"items": self._spectra(100)},
                {"items": self._spectra(50)},
            ]
        )
        monkeypatch.setattr(ecosis, "_api_get", lambda path, retries=3: next(pages))
        monkeypatch.setattr(ecosis.time, "sleep", lambda s: None)

        spectra = ecosis._fetch_all_spectra("ds1", 250)
        assert len(spectra) == 250

    def test_fetch_all_spectra_raises_on_persistent_empty_pages(self, monkeypatch):
        monkeypatch.setattr(ecosis, "_api_get", lambda path, retries=3: {"items": []})
        monkeypatch.setattr(ecosis.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="empty pages"):
            ecosis._fetch_all_spectra("ds1", 250)

    def test_bundle_is_complete(self, tmp_path):
        complete = tmp_path / "complete.json"
        complete.write_text(
            json.dumps(
                {
                    "metadata": {"ecosis": {"spectra_count": 2}},
                    "spectra": self._spectra(2),
                }
            )
        )
        truncated = tmp_path / "truncated.json"
        truncated.write_text(
            json.dumps(
                {
                    "metadata": {"ecosis": {"spectra_count": 100}},
                    "spectra": self._spectra(40),
                }
            )
        )
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not json")

        assert ecosis._bundle_is_complete(complete)
        assert not ecosis._bundle_is_complete(truncated)
        assert not ecosis._bundle_is_complete(corrupt)

    def test_download_refetches_incomplete_and_fails_loudly(self, tmp_path, monkeypatch):
        curated = [
            _policy("good", name="Good Dataset"),
            _policy("bad", name="Bad Dataset"),
        ]
        monkeypatch.setattr(ecosis, "_load_curated_datasets", lambda: curated)
        monkeypatch.setattr(ecosis.time, "sleep", lambda s: None)

        # Pre-existing truncated bundle for "good" must be refetched.
        out_dir = tmp_path / "ecosis"
        out_dir.mkdir()
        (out_dir / "good.json").write_text(
            json.dumps(
                {
                    "metadata": {"ecosis": {"spectra_count": 3}},
                    "spectra": self._spectra(1),
                }
            )
        )

        def fake_api_get(path, retries=3):
            if path.startswith("/package/good"):
                return {"ecosis": {"spectra_count": 3}}
            if path.startswith("/package/bad"):
                return {"ecosis": {"spectra_count": 5}}
            if path.startswith("/spectra/search/good"):
                return {"items": self._spectra(3)}
            raise RuntimeError("network down")

        monkeypatch.setattr(ecosis, "_api_get", fake_api_get)

        loader = ecosis.EcosisLoader()
        with pytest.raises(RuntimeError, match="Bad Dataset"):
            loader.download(tmp_path)

        # "good" was refetched to completion; "bad" left no partial file.
        good = json.loads((out_dir / "good.json").read_text())
        assert len(good["spectra"]) == 3
        assert not (out_dir / "bad.json").exists()
        assert not list(out_dir.glob("*.part"))


# ---------------------------------------------------------------------------
# Helpers for synthetic bundles and policies
# ---------------------------------------------------------------------------

WAVELENGTHS = [400 + 10 * i for i in range(20)]
CEDAR_CREEK = "e7459b49-06d3-4e1d-beaa-cab0e012cc23"
SALT_MARSH = "ef2bc7ee-095c-4a76-bb5d-b16150667e14"
MULTISCALE = "b02e3e78-006c-4eed-a3d6-bfd593d15708"
FRACTIONAL = "c7b6affd-8b15-4f95-abf1-9956d8af1d52"


def _policy(dataset_id, name=None, inclusion="full", **extra):
    entry = {
        "id": dataset_id,
        "name": name or dataset_id,
        "inclusion": inclusion,
        "reason": "test",
    }
    entry.update(extra)
    return _parse_policy(entry)


def _spec(sid, values, distinct=True, **fields):
    """Synthetic spectrum item.

    ``distinct`` tags the datapoints with a non-numeric per-id key (ignored as
    a band, but part of the duplicate key) so spectra with equal values are
    not collapsed as duplicates.
    """
    item = {
        "_id": sid,
        "datapoints": {str(w): str(v) for w, v in zip(WAVELENGTHS, values)},
        "ecosis": {"filename": fields.pop("filename", "spectra.csv")},
    }
    if distinct:
        item["datapoints"]["Sample"] = sid
    item.update(fields)
    return item


def _flat(level, n=len(WAVELENGTHS)):
    return [level] * n


def _veg(nir, n=len(WAVELENGTHS)):
    """Vegetation-like shape: dark visible, red-edge ramp, NIR plateau at ``nir``.

    Its 90th-percentile level is ``nir``.
    """
    return [0.15 * nir] * 8 + [0.5 * nir] * 2 + [nir] * (n - 10)


def _write_bundle(directory, dataset_id, spectra, indent=None, **metadata):
    meta = {
        "_id": dataset_id,
        "ecosis": {"package_title": f"Title {dataset_id}", "spectra_count": len(spectra)},
        "Target Type": ["leaf"],
    }
    meta.update(metadata)
    path = directory / f"{dataset_id}.json"
    path.write_text(json.dumps({"metadata": meta, "spectra": spectra}, indent=indent))
    return path


def _load(tmp_path, policies):
    loader = EcosisLoader(policies=policies)
    records = list(loader.load(tmp_path))
    return loader, records


class TestPolicyFile:
    def test_curated_file_parses(self):
        policies = load_dataset_policies()
        assert len(policies) == 19
        by_id = {p.id: p for p in policies}
        assert len(by_id) == 19
        for p in policies:
            assert p.reason
            assert p.inclusion in ("full", "sample", "exclude")
            assert (p.sampling is not None) == (p.inclusion == "sample")

    def test_curated_decisions(self):
        by_id = {p.id: p for p in load_dataset_policies()}
        assert by_id[CEDAR_CREEK].inclusion == "exclude"
        # Fractional cover: measured field spectra that were only resampled.
        assert not by_id[FRACTIONAL].simulated
        assert by_id["38942519-e925-4cbc-bbbd-ce6dcf276f73"].simulated  # EMIT model output
        lma = by_id["7433af7d-fbbd-4617-8df4-4d892f0d4357"]
        assert lma.sampling == SamplingSpec("functional_type", 200, None, 10, one_per="id")
        fusarium = by_id["2a9d952a-5a28-47b3-9f85-8aa6e6808991"]
        assert fusarium.sampling.stratify_by == "Inoculation"
        assert fusarium.sampling.max_total == 200
        multiscale = by_id[MULTISCALE]
        assert multiscale.sampling.max_per_stratum == 25
        assert multiscale.identity.material_name_field == "Tree Species"
        salt = by_id[SALT_MARSH]
        assert any(
            p.search("All_PLSR_coefficients_to_model_N_14c.csv") for p in salt.exclude_resources
        )
        assert not any(
            p.search("Nutrients_CSHRS_Salt_Marsh_Categories.csv") for p in salt.exclude_resources
        )

    @pytest.mark.parametrize(
        "entry, match",
        [
            ({"inclusion": "maybe"}, "inclusion"),
            ({"inclusion": "sample"}, "requires a sampling"),
            ({"sampling": {"stratify_by": "x", "max_total": 5}}, "only valid"),
            ({"inclusion": "sample", "sampling": {"stratify_by": "x"}}, "max_total"),
            (
                {"inclusion": "sample", "sampling": {"stratify_by": "x", "max_total": 0}},
                "max_total",
            ),
            (
                {
                    "inclusion": "sample",
                    "sampling": {"stratify_by": "x", "max_per_stratum": 2, "min_per_stratum": 3},
                },
                "min_per_stratum",
            ),
            ({"scale_divisor": 7}, "scale_divisor"),
            ({"exclude_resources": ["("]}, "regex"),
            ({"typo_key": 1}, "unknown key"),
            ({"identity": {"material_nam": "x"}}, "unknown key"),
            ({"identity": {"material_name_template": "no placeholder"}}, "template"),
            ({"resource_techniques": {"^t": "emission"}}, "resource_techniques"),
            ({"drop_spectra": {"abc": ""}}, "drop_spectra"),
            ({"reason": ""}, "reason"),
            ({"simulated": "yes"}, "simulated"),
            ({"dedupe_key": "ids"}, "dedupe_key"),
            ({"units_field": "Units"}, "together"),
            ({"units_field": "Units", "units_divisors": {"%": 7}}, "units_divisors"),
            ({"identity": {"category": "plastic"}}, "category"),
            ({"identity": {"category_map": {"leaf": "tree"}}}, "category_map"),
            ({"resource_techniques": {"^a": "absorptance"}, "typo": 1}, "unknown key"),
        ],
    )
    def test_invalid_entries_rejected(self, entry, match):
        raw = {"id": "ds", "name": "n", "inclusion": "full", "reason": "r"}
        raw.update(entry)
        with pytest.raises(ValueError, match=match):
            _parse_policy(raw)

    def test_duplicate_ids_rejected(self, tmp_path):
        entry = {"id": "ds", "name": "n", "inclusion": "full", "reason": "r"}
        path = tmp_path / "datasets.json"
        path.write_text(json.dumps([entry, entry]))
        with pytest.raises(ValueError, match="duplicate"):
            load_dataset_policies(path)


class TestAllocation:
    def test_under_cap_keeps_everything(self):
        spec = SamplingSpec("f", max_total=100)
        assert _allocate_strata({"a": 10, "b": 5}, spec) == {"a": 10, "b": 5}

    def test_proportional_with_minimum(self):
        spec = SamplingSpec("f", max_total=200, min_per_stratum=10)
        alloc = _allocate_strata({"tree": 30000, "forb": 8000, "vine": 3000, "grass": 50}, spec)
        assert sum(alloc.values()) == 200
        assert alloc["grass"] >= 10
        assert alloc["tree"] > alloc["forb"] > alloc["vine"] >= 10

    def test_minimum_capped_by_stratum_size(self):
        spec = SamplingSpec("f", max_total=50, min_per_stratum=10)
        alloc = _allocate_strata({"big": 1000, "tiny": 3}, spec)
        assert alloc == {"big": 47, "tiny": 3}

    def test_more_strata_than_budget(self):
        spec = SamplingSpec("f", max_total=3, min_per_stratum=2)
        alloc = _allocate_strata({"a": 10, "b": 9, "c": 8}, spec)
        assert sum(alloc.values()) == 3
        assert all(v <= 2 for v in alloc.values())

    def test_minimums_go_to_small_strata_first(self):
        # When minimums exceed the budget, rare strata get theirs before the
        # big ones, so they are represented at all.
        spec = SamplingSpec("f", max_total=4, min_per_stratum=2)
        alloc = _allocate_strata({"big": 1000, "mid": 100, "rare": 3}, spec)
        assert alloc == {"big": 0, "mid": 2, "rare": 2}

    def test_per_stratum_cap(self):
        spec = SamplingSpec("f", max_per_stratum=25)
        assert _allocate_strata({"a": 100, "b": 7}, spec) == {"a": 25, "b": 7}


class TestSampling:
    def _spectra(self):
        spectra = []
        for i in range(60):
            spectra.append(_spec(f"tree-{i}", _veg(0.3), functional_type="tree"))
        for i in range(30):
            spectra.append(_spec(f"forb-{i}", _veg(0.3), functional_type="forb"))
        for i in range(4):
            spectra.append(_spec(f"vine-{i}", _veg(0.3), functional_type="vine"))
        spectra.append(_spec("anon", _veg(0.3)))  # no stratum -> not eligible
        return spectra

    def _policy(self):
        return _policy(
            "lma",
            inclusion="sample",
            sampling={"stratify_by": "functional_type", "max_total": 20, "min_per_stratum": 5},
            identity={
                "material_name_field": "functional_type",
                "material_name_template": "{} leaf",
                "keyword_fields": ["functional_type"],
            },
            carry_fields=["functional_type"],
        )

    def test_sample_is_independent_of_input_order(self, tmp_path):
        spectra = self._spectra()
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        _write_bundle(a_dir, "lma", spectra)
        shuffled = list(spectra)
        random.Random(42).shuffle(shuffled)
        _write_bundle(b_dir, "lma", shuffled)

        _, rec_a = _load(a_dir, [self._policy()])
        _, rec_b = _load(b_dir, [self._policy()])
        assert {r.id for r in rec_a} == {r.id for r in rec_b}

    def test_sample_respects_caps_and_strata(self, tmp_path):
        _write_bundle(tmp_path, "lma", self._spectra())
        loader, records = _load(tmp_path, [self._policy()])
        assert len(records) == 20
        by_stratum = {}
        for r in records:
            by_stratum.setdefault(r.material.name, []).append(r)
        assert set(by_stratum) == {"tree leaf", "forb leaf", "vine leaf"}
        assert len(by_stratum["vine leaf"]) == 4  # all of a small stratum
        assert len(by_stratum["forb leaf"]) >= 5
        assert len(by_stratum["tree leaf"]) > len(by_stratum["forb leaf"])
        r = by_stratum["tree leaf"][0]
        assert "tree" in r.material.keywords
        assert r.extra["ecosis_metadata"] == {"functional_type": "tree"}
        sampling = r.extra["ecosis_sampling"]
        assert sampling["stratum"] == "tree"
        assert sampling["stratum_available"] == 60
        stats = loader.ingest_stats[0]
        assert stats.available == 95
        assert stats.missing_stratum == 1
        assert stats.not_sampled == 94 - 20
        assert stats.included == 20

    def test_sample_takes_lowest_hash_ranks(self, tmp_path):
        import hashlib

        _write_bundle(tmp_path, "lma", self._spectra())
        _, records = _load(tmp_path, [self._policy()])
        chosen_trees = sorted(r.original_id for r in records if r.material.name == "tree leaf")
        all_trees = [f"tree-{i}" for i in range(60)]
        expected = sorted(
            sorted(all_trees, key=lambda s: hashlib.sha256(s.encode()).hexdigest())[
                : len(chosen_trees)
            ]
        )
        assert chosen_trees == expected


class TestIdentityMapping:
    def test_tree_species_becomes_material_name(self, tmp_path):
        spectra = [
            _spec(f"p{i}", _veg(0.2), **{"Tree Species": species})
            for i, species in enumerate(["Acacia", "Olive ", "Olive", "Zizphus", "Acacia"])
        ]
        policy = _policy(
            MULTISCALE,
            inclusion="sample",
            sampling={"stratify_by": "Tree Species", "max_per_stratum": 1},
            identity={
                "material_name_field": "Tree Species",
                "material_name_map": {"Zizphus": "Ziziphus"},
                "keyword_fields": ["Tree Species"],
            },
        )
        _write_bundle(tmp_path, MULTISCALE, spectra)
        _, records = _load(tmp_path, [policy])
        names = sorted(r.material.name for r in records)
        # "Olive " and "Olive" are one stratum after whitespace trimming.
        assert names == ["Acacia", "Olive", "Ziziphus"]
        zizi = next(r for r in records if r.material.name == "Ziziphus")
        assert "ziziphus" in zizi.material.keywords
        assert "Title" not in zizi.material.name

    def test_constant_name_replaces_dataset_title(self, tmp_path):
        _write_bundle(tmp_path, "water", [_spec("w1", _veg(0.02))])
        policy = _policy("water", identity={"material_name": "water", "keywords": ["water"]})
        _, records = _load(tmp_path, [policy])
        assert records[0].material.name == "water"
        assert "water" in records[0].material.keywords

    def test_fallback_fields_after_default_chain(self, tmp_path):
        spectra = [
            _spec("a", _veg(0.3), **{"Common Name": "red maple", "USDA Symbol": "ACRU"}),
            _spec("b", _veg(0.3), **{"USDA Symbol": "CAGL", "Functional_type": "broadleaf"}),
            _spec("c", _veg(0.3), **{"Functional_type": "forb"}),
        ]
        _write_bundle(tmp_path, "neon", spectra)
        policy = _policy(
            "neon", identity={"fallback_name_fields": ["USDA Symbol", "Functional_type"]}
        )
        _, records = _load(tmp_path, [policy])
        assert [r.material.name for r in records] == ["red maple", "CAGL", "forb"]


class TestExclusion:
    def test_loader_ignores_excluded_and_uncurated_bundles(self, tmp_path):
        _write_bundle(tmp_path, "excluded", [_spec("e1", _veg(0.3))])
        _write_bundle(tmp_path, "unknown", [_spec("u1", _veg(0.3))])
        _write_bundle(tmp_path, "kept", [_spec("k1", _veg(0.3))])
        loader, records = _load(
            tmp_path, [_policy("excluded", inclusion="exclude"), _policy("kept")]
        )
        assert [r.original_id for r in records] == ["k1"]
        stats = {s.dataset_id: s for s in loader.ingest_stats}
        assert stats["excluded"].inclusion == "exclude"
        assert stats["excluded"].included == 0
        assert stats["unknown"].inclusion == "uncurated"

    def test_downloader_skips_excluded(self, tmp_path, monkeypatch):
        calls = []

        def fake_api_get(path, retries=3):
            calls.append(path)
            if path.startswith("/package/"):
                return {"_id": "kept", "ecosis": {"spectra_count": 1}}
            return {"items": [_spec("k1", _veg(0.3))]}

        monkeypatch.setattr(ecosis, "_api_get", fake_api_get)
        monkeypatch.setattr(ecosis.time, "sleep", lambda s: None)
        loader = EcosisLoader(policies=[_policy("cedar", inclusion="exclude"), _policy("kept")])
        out = loader.download(tmp_path)
        assert not any("cedar" in c for c in calls)
        assert (out / "kept.json").exists()
        assert not (out / "cedar.json").exists()
        assert _bundle_is_complete(out / "kept.json")

    def test_excluded_resources_and_drop_list(self, tmp_path):
        spectra = [
            _spec("coef", _veg(150.0), filename="All_PLSR_coefficients_to_model_N_14c.csv"),
            _spec("meas", _veg(0.2), filename="Nutrients_CSHRS_Salt_Marsh_Categories.csv"),
            _spec("bad", _veg(0.2), filename="Nutrients_CSHRS_Salt_Marsh_Categories.csv"),
        ]
        _write_bundle(tmp_path, SALT_MARSH, spectra)
        policy = _policy(
            SALT_MARSH,
            exclude_resources=["^All_PLSR_coefficients_"],
            drop_spectra={"bad": "failed measurement"},
        )
        loader, records = _load(tmp_path, [policy])
        assert [r.original_id for r in records] == ["meas"]
        stats = loader.ingest_stats[0]
        assert stats.excluded_resource == 1
        assert stats.dropped_by_policy == 1
        # The coefficient rows must not influence the dataset scale.
        assert stats.base_divisor == 1

    def test_resource_techniques(self, tmp_path):
        spectra = [
            _spec("r", _veg(0.4), filename="ref_spec.csv"),
            _spec("t", _veg(0.4), filename="trans_spec.csv"),
            _spec("a", _veg(0.2), filename="abs_spec.csv"),
        ]
        _write_bundle(tmp_path, "cabo", spectra)
        policy = _policy(
            "cabo",
            exclude_resources=["^abs_spec\\.csv$"],
            resource_techniques={
                "^ref_spec\\.csv$": "reflectance",
                "^trans_spec\\.csv$": "transmittance",
            },
        )
        _, records = _load(tmp_path, [policy])
        techniques = {r.original_id: r.measurement.quantity for r in records}
        assert techniques == {
            "r": Quantity.REFLECTANCE,
            "t": Quantity.TRANSMITTANCE,
        }


class TestScaleHandling:
    def test_percent_dataset_normalised_and_recorded(self, tmp_path):
        spectra = [_spec(f"s{i}", _veg(40.0 + i)) for i in range(5)]
        _write_bundle(tmp_path, "pct", spectra)
        _, records = _load(tmp_path, [_policy("pct")])
        for i, r in enumerate(sorted(records, key=lambda r: r.original_id)):
            assert r.spectral_data.values == pytest.approx([v / 100 for v in _veg(40.0 + i)])
            assert r.extra["source_reflectance_divisor"] == 100

    def test_water_band_spikes_do_not_flip_scale(self, tmp_path):
        # NGEE Arctic pattern: percent-scale canopy spectra with a few noise
        # spikes of several hundred percent. The spikes are masked as bad
        # bands with provenance; the spectrum stays on the percent scale.
        spiky = _veg(40.0)
        spiky[5] = 860.0
        spiky[-1] = 5220.0
        spectra = [_spec(f"s{i}", _veg(45.0)) for i in range(4)] + [_spec("spiky", spiky)]
        _write_bundle(tmp_path, "ngee", spectra)
        loader, records = _load(tmp_path, [_policy("ngee")])
        r = next(r for r in records if r.original_id == "spiky")
        ap = r.extra
        assert ap["source_reflectance_divisor"] == 100
        assert math.isnan(r.spectral_data.values[5])
        assert math.isnan(r.spectral_data.values[-1])
        assert r.spectral_data.values[0] == pytest.approx(0.06)
        assert sum(math.isnan(v) for v in r.spectral_data.values) == 2
        assert ap["masked_band_count"] == 2
        assert ap["source_missing_band_count"] == 0
        assert ap["masked_bands"] == [[450.0, 860.0], [590.0, 5220.0]]
        assert loader.ingest_stats[0].escalated == 0
        assert loader.ingest_stats[0].masked_out_of_range == 2

    def test_mixed_scale_spectrum_escalated(self, tmp_path):
        spectra = [_spec(f"s{i}", _veg(40.0 + i)) for i in range(4)] + [_spec("int", _veg(4000.0))]
        _write_bundle(tmp_path, "mixed", spectra)
        loader, records = _load(tmp_path, [_policy("mixed")])
        r = next(r for r in records if r.original_id == "int")
        assert r.spectral_data.values == pytest.approx(_veg(0.4))
        assert r.extra["source_reflectance_divisor"] == 10000
        assert r.extra["dataset_reflectance_divisor"] == 100
        assert loader.ingest_stats[0].escalated == 1

    def test_unit_minority_in_percent_dataset_dropped(self, tmp_path):
        # Reviewer probe: unit-scale spectra inside a percent-scale dataset
        # would otherwise be divided by 100 and kept ~100x too dark.
        spectra = [_spec(f"p{i}", _veg(40.0 + i)) for i in range(6)] + [
            _spec("u0", _veg(0.45)),
            _spec("u1", _veg(0.5)),
        ]
        _write_bundle(tmp_path, "pct", spectra)
        loader, records = _load(tmp_path, [_policy("pct")])
        kept = {r.original_id for r in records}
        assert kept == {f"p{i}" for i in range(6)}
        stats = loader.ingest_stats[0]
        assert stats.base_divisor == 100
        assert stats.implausibly_dark == 2

    def test_predivided_copy_in_unit_dataset_dropped(self, tmp_path):
        # Madison PSR pattern: a unit-scale dataset (leaves at ~0.47) holding
        # copies the uploader already divided by 100 (~0.005). The darkness
        # check applies whatever the divisor, before sampling, so the dark
        # copy is never kept or chosen as a leaf's sampled replicate.
        spectra = [_spec(f"v{i}", _veg(0.4 + 0.01 * i), id=f"L{i}", ft="tree") for i in range(5)]
        spectra.append(_spec("dark", _veg(0.0045), id="L0", ft="tree"))
        _write_bundle(tmp_path, "unit", spectra)
        policy = _policy(
            "unit",
            inclusion="sample",
            sampling={"stratify_by": "ft", "max_total": 5, "one_per": "id"},
        )
        loader, records = _load(tmp_path, [policy])
        assert {r.original_id for r in records} == {f"v{i}" for i in range(5)}
        stats = loader.ingest_stats[0]
        assert stats.base_divisor == 1
        assert stats.implausibly_dark == 1

    def test_dark_target_within_ratio_kept(self, tmp_path):
        # A dark target (water, shade) above 1/DARK_LEVEL_RATIO of the dataset
        # median is a real measurement and is kept.
        spectra = [_spec(f"v{i}", _veg(0.4 + 0.01 * i)) for i in range(5)] + [
            _spec("water", _veg(0.02))
        ]
        _write_bundle(tmp_path, "unit", spectra)
        loader, records = _load(tmp_path, [_policy("unit")])
        assert "water" in {r.original_id for r in records}
        assert loader.ingest_stats[0].implausibly_dark == 0

    def test_dark_dataset_judged_against_its_own_median(self, tmp_path):
        # EMIT water pattern: every spectrum is dark, none is implausible.
        spectra = [_spec(f"w{i}", _veg(0.008 + 0.001 * i)) for i in range(6)]
        _write_bundle(tmp_path, "water", spectra)
        loader, records = _load(tmp_path, [_policy("water")])
        assert len(records) == 6
        assert loader.ingest_stats[0].implausibly_dark == 0

    def test_units_field_is_authoritative(self, tmp_path):
        spectra = [
            _spec("pct", _veg(40.0), Spectra_Units="Percent_Reflectance_0_100"),
            _spec("ratio", _veg(0.4), Spectra_Units="Ratio"),
            _spec("liar", _veg(40.0), Spectra_Units="Ratio"),  # contradicts its units
            _spec("unknown", _veg(30.0)),  # no units -> inferred
        ]
        _write_bundle(tmp_path, "units", spectra)
        policy = _policy(
            "units",
            units_field="Spectra_Units",
            units_divisors={"Percent_Reflectance_0_100": 100, "Ratio": 1},
        )
        loader, records = _load(tmp_path, [policy])
        by_id = {r.original_id: r for r in records}
        assert set(by_id) == {"pct", "ratio", "unknown"}
        assert by_id["pct"].extra["source_reflectance_divisor"] == 100
        assert "source_reflectance_divisor" not in by_id["ratio"].extra
        assert by_id["ratio"].spectral_data.values == pytest.approx(_veg(0.4))
        assert loader.ingest_stats[0].units_conflict == 1

    def test_implausible_scale_dropped(self, tmp_path):
        spectra = [_spec(f"s{i}", _veg(0.3 + 0.01 * i)) for i in range(4)] + [
            _spec("junk", _veg(1.0e7)),  # fits no supported scale
            _spec("dim", _veg(1.6)),  # /100 would be a fake dark spectrum
        ]
        _write_bundle(tmp_path, "unit", spectra)
        loader, records = _load(tmp_path, [_policy("unit")])
        assert {r.original_id for r in records} == {"s0", "s1", "s2", "s3"}
        assert loader.ingest_stats[0].no_plausible_scale == 2

    def test_mostly_invalid_spectrum_dropped(self, tmp_path):
        bad = [-0.5 - 0.01 * i for i in range(15)] + [0.3] * 5
        spectra = [_spec(f"s{i}", _veg(0.3)) for i in range(4)] + [_spec("neg", bad)]
        _write_bundle(tmp_path, "unit", spectra)
        loader, records = _load(tmp_path, [_policy("unit")])
        assert "neg" not in {r.original_id for r in records}
        assert loader.ingest_stats[0].low_coverage == 1

    def test_scale_override(self, tmp_path):
        _write_bundle(tmp_path, "dark", [_spec("d", _veg(1.2))])
        _, records = _load(tmp_path, [_policy("dark", scale_divisor=100)])
        assert records[0].spectral_data.values == pytest.approx(_veg(0.012))
        assert records[0].extra["source_reflectance_divisor"] == 100

    def test_masking_window(self, tmp_path):
        values = _veg(0.5)
        values[0] = -0.01  # near-zero noise: inside the window, kept
        values[1] = -0.2  # beyond the window: masked
        values[12] = 1.1  # bright but plausible: kept
        values[13] = 2.0  # Multiscale-style fill/saturation value: masked
        _write_bundle(tmp_path, "ms", [_spec("x", values)])
        _, records = _load(tmp_path, [_policy("ms")])
        r = records[0]
        vals = r.spectral_data.values
        assert vals[0] == pytest.approx(-0.01)
        assert vals[12] == pytest.approx(1.1)
        assert math.isnan(vals[1]) and math.isnan(vals[13])
        assert all(-0.05 <= v <= 1.2 for v in vals if not math.isnan(v))
        assert sum(math.isnan(v) for v in r.spectral_data.values) == 2
        assert r.extra["masked_bands"] == [[410.0, -0.2], [530.0, 2.0]]
        assert r.quality.notes is None

    def test_missing_band_kept_as_nan(self, tmp_path):
        spectrum = _spec("m", _veg(0.3))
        spectrum["datapoints"]["500"] = "NA"
        _write_bundle(tmp_path, "gap", [spectrum])
        _, records = _load(tmp_path, [_policy("gap")])
        r = records[0]
        assert len(r.spectral_data.wavelengths) == len(WAVELENGTHS)
        idx = r.spectral_data.wavelengths.index(500.0)
        assert math.isnan(r.spectral_data.values[idx])
        assert sum(math.isnan(v) for v in r.spectral_data.values) == 1
        assert r.extra["source_missing_band_count"] == 1
        assert r.extra["masked_band_count"] == 0
        assert r.extra["masked_bands"] == []

    def test_clean_spectrum_has_no_band_provenance(self, tmp_path):
        _write_bundle(tmp_path, "clean", [_spec("c", _veg(0.3))])
        _, records = _load(tmp_path, [_policy("clean")])
        assert "masked_bands" not in records[0].extra

    def test_scale_primitives(self):
        values = [float(i) for i in range(1, 21)] + [float("nan")]
        assert spectrum_level(values) == 18.0  # nearest-rank p90 of 20 finite values
        assert spectrum_level([float("nan")]) is None
        assert resolve_spectrum_divisor(0.5, 1) == 1
        assert resolve_spectrum_divisor(50.0, 1) == 100
        assert resolve_spectrum_divisor(5000.0, 100) == 10000
        assert resolve_spectrum_divisor(1.6, 1) is None  # would be 0.016 at /100
        assert resolve_spectrum_divisor(1e9, 1) is None
        scaled, missing, masked = scale_and_mask([50.0, float("nan"), 150.0, -10.0, -1.0], 100)
        assert scaled[0] == 0.5
        assert scaled[4] == pytest.approx(-0.01)
        assert math.isnan(scaled[1]) and math.isnan(scaled[2]) and math.isnan(scaled[3])
        assert (missing, masked) == (1, [2, 3])


class TestSimulatedFlag:
    def test_simulated_dataset_flagged(self, tmp_path):
        _write_bundle(tmp_path, "model", [_spec("sim", _veg(0.3))])
        _, records = _load(tmp_path, [_policy("model", simulated=True)])
        assert "simulated" in records[0].measurement.processing

    def test_measured_dataset_not_flagged(self, tmp_path):
        _write_bundle(tmp_path, "real", [_spec("m", _veg(0.3))])
        _, records = _load(tmp_path, [_policy("real")])
        assert "simulated" not in records[0].measurement.processing


class TestStreamingReader:
    def test_reads_pretty_and_compact_bundles(self, tmp_path, monkeypatch):
        # Tiny chunks force values to straddle chunk boundaries.
        monkeypatch.setattr(ecosis, "_READ_CHUNK_CHARS", 7)
        spectra = [_spec(f"s{i}", _veg(0.123456789)) for i in range(3)]
        for indent, name in ((None, "compact"), (2, "pretty")):
            d = tmp_path / name
            d.mkdir()
            path = _write_bundle(d, "ds", spectra, indent=indent)
            events = list(_iter_bundle(path))
            assert events[0][0] == "metadata"
            assert [v["_id"] for k, v in events if k == "spectrum"] == ["s0", "s1", "s2"]
            assert _bundle_is_complete(path)

    def test_empty_spectra_list(self, tmp_path):
        path = _write_bundle(tmp_path, "ds", [])
        assert [k for k, _ in _iter_bundle(path)] == ["metadata"]

    def test_truncated_bundle_is_incomplete_and_skipped(self, tmp_path):
        path = _write_bundle(tmp_path, "ds", [_spec("s0", _veg(0.3))])
        path.write_text(path.read_text()[:-40])
        assert not _bundle_is_complete(path)
        loader, records = _load(tmp_path, [_policy("ds")])
        assert records == []
        assert loader.ingest_stats[0].unreadable


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, headers=None):
    import email.message

    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://ecosis.org/api/x", code, "err", msg, None)


class TestApiRetries:
    def _run(self, monkeypatch, outcomes, retries=None):
        sleeps = []
        calls = iter(outcomes)

        def fake_urlopen(req, timeout=60):
            outcome = next(calls)
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(json.dumps(outcome).encode())

        monkeypatch.setattr(ecosis.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(ecosis.time, "sleep", sleeps.append)
        kwargs = {} if retries is None else {"retries": retries}
        return ecosis._api_get("/package/x", **kwargs), sleeps

    def test_retries_5xx_and_429_then_succeeds(self, monkeypatch):
        result, sleeps = self._run(
            monkeypatch,
            [_http_error(503), _http_error(502), _http_error(429), {"ok": True}],
        )
        assert result == {"ok": True}
        assert len(sleeps) == 3
        assert all(0 < s <= ecosis._MAX_BACKOFF_S for s in sleeps)

    def test_honours_retry_after(self, monkeypatch):
        result, sleeps = self._run(
            monkeypatch, [_http_error(503, {"Retry-After": "17"}), {"ok": 1}]
        )
        assert result == {"ok": 1}
        assert sleeps == [17.0]

    def test_retry_after_is_capped(self, monkeypatch):
        _, sleeps = self._run(monkeypatch, [_http_error(429, {"Retry-After": "3600"}), {}])
        assert sleeps == [ecosis._MAX_RETRY_AFTER_S]
        assert ecosis._MAX_RETRY_AFTER_S >= 300

    def test_long_retry_after_honoured(self, monkeypatch):
        _, sleeps = self._run(monkeypatch, [_http_error(503, {"Retry-After": "240"}), {}])
        assert sleeps == [240.0]

    def test_does_not_retry_other_4xx(self, monkeypatch):
        with pytest.raises(urllib.error.HTTPError):
            self._run(monkeypatch, [_http_error(404), {"never": "reached"}])

    def test_retries_network_errors(self, monkeypatch):
        result, sleeps = self._run(
            monkeypatch, [urllib.error.URLError("reset"), TimeoutError(), {"ok": 2}]
        )
        assert result == {"ok": 2}
        assert len(sleeps) == 2

    def test_backoff_grows_and_caps(self):
        for attempt in range(12):
            delay = ecosis._backoff_delay(attempt)
            base = min(2.0**attempt, ecosis._MAX_BACKOFF_S)
            assert base / 2 <= delay <= base

    def test_gives_up_after_retries(self, monkeypatch):
        with pytest.raises(urllib.error.HTTPError):
            self._run(monkeypatch, [_http_error(503)] * 3, retries=3)


def test_policy_type_exported():
    assert isinstance(_policy("x"), DatasetPolicy)


class TestFatalErrors:
    def test_missing_curated_bundle_is_fatal(self, tmp_path):
        _write_bundle(tmp_path, "kept", [_spec("k1", _veg(0.3))])
        loader, records = _load(
            tmp_path,
            [_policy("kept"), _policy("absent"), _policy("skipped", inclusion="exclude")],
        )
        assert len(records) == 1
        assert loader.missing_datasets == ["absent"]
        assert loader.fatal_error_count == 1

    def test_clean_run_has_no_fatal_errors(self, tmp_path):
        _write_bundle(tmp_path, "kept", [_spec("k1", _veg(0.3))])
        loader, _ = _load(tmp_path, [_policy("kept")])
        assert loader.fatal_error_count == 0


class TestDuplicates:
    def test_content_duplicates_collapsed_before_sampling(self, tmp_path):
        # CABO pattern: the same sample uploaded twice with identical metadata
        # and datapoints; only the EcoSIS _id differs.
        spectra = []
        for i in range(6):
            values = _veg(0.3 + 0.01 * i)
            for copy in ("a", "b"):
                spectra.append(
                    _spec(f"{copy}{i}", values, distinct=False, sample_id=f"S{i}", ft="tree")
                )
        _write_bundle(tmp_path, "cabo", spectra)
        policy = _policy(
            "cabo",
            inclusion="sample",
            sampling={"stratify_by": "ft", "max_total": 4},
            carry_fields=["sample_id"],
        )
        loader, records = _load(tmp_path, [policy])
        stats = loader.ingest_stats[0]
        assert stats.duplicate == 6
        # The cap counts distinct spectra: 4 picks are 4 different samples.
        assert len(records) == 4
        assert len({r.extra["ecosis_metadata"]["sample_id"] for r in records}) == 4
        assert stats.not_sampled == 2  # 6 distinct spectra, cap 4
        assert all(len(r.extra["duplicate_ids"]) == 1 for r in records)

    def test_lowest_ranked_copy_kept_regardless_of_order(self, tmp_path):
        import hashlib

        values = _veg(0.4)
        ids = ["x1", "x2", "x3"]
        expected = min(ids, key=lambda s: hashlib.sha256(s.encode()).hexdigest())
        for order, sub in ((ids, "fwd"), (list(reversed(ids)), "rev")):
            d = tmp_path / sub
            d.mkdir()
            _write_bundle(d, "dup", [_spec(i, values, distinct=False) for i in order])
            loader, records = _load(d, [_policy("dup")])
            assert [r.original_id for r in records] == [expected]
            assert records[0].extra["duplicate_ids"] == sorted(set(ids) - {expected})
            assert loader.ingest_stats[0].duplicate == 2

    def test_content_key_keeps_different_metadata(self, tmp_path):
        values = _veg(0.4)
        spectra = [
            _spec("p1", values, distinct=False, Pixel="1"),
            _spec("p2", values, distinct=False, Pixel="2"),
        ]
        _write_bundle(tmp_path, "ms", spectra)
        _, records = _load(tmp_path, [_policy("ms", dedupe_key="content")])
        assert len(records) == 2

    def test_datapoints_key_collapses_identical_pixels(self, tmp_path):
        # Multiscale pattern: adjacent pixels with identical spectra but
        # different pixel ids/coordinates.
        values = _veg(0.4)
        spectra = [
            _spec("p1", values, distinct=False, Pixel="1"),
            _spec("p2", values, distinct=False, Pixel="2"),
        ]
        _write_bundle(tmp_path, "ms", spectra)
        loader, records = _load(tmp_path, [_policy("ms")])  # datapoints is the default
        assert len(records) == 1
        assert loader.ingest_stats[0].duplicate == 1

    def test_conflicting_labels_drop_whole_group(self, tmp_path):
        # NGEE pattern: identical datapoints uploaded under different species
        # (a copy error) — no copy's label can be trusted.
        values = _veg(0.4)
        spectra = [
            _spec("a", values, distinct=False, **{"Common Name": "arctic sweet coltsfoot"}),
            _spec("b", values, distinct=False, **{"Common Name": "stone bramble"}),
            _spec("c", values, distinct=False, **{"Common Name": "water sedge"}),
            _spec("ok", _veg(0.5), **{"Common Name": "dwarf birch"}),
        ]
        _write_bundle(tmp_path, "ngee", spectra)
        loader, records = _load(tmp_path, [_policy("ngee")])
        assert [r.original_id for r in records] == ["ok"]
        stats = loader.ingest_stats[0]
        assert stats.conflicting_duplicate == 3
        assert stats.duplicate == 0

    def test_conflicting_strata_drop_whole_group(self, tmp_path):
        values = _veg(0.4)
        spectra = [
            _spec("a", values, distinct=False, ft="tree"),
            _spec("b", values, distinct=False, ft="forb"),
            _spec("c", _veg(0.3), ft="tree"),
        ]
        _write_bundle(tmp_path, "lma", spectra)
        policy = _policy(
            "lma", inclusion="sample", sampling={"stratify_by": "ft", "max_per_stratum": 5}
        )
        loader, records = _load(tmp_path, [policy])
        assert [r.original_id for r in records] == ["c"]
        assert loader.ingest_stats[0].conflicting_duplicate == 2

    def test_large_duplicate_group_warns(self, tmp_path, caplog):
        values = _veg(0.4)
        spectra = [_spec(f"s{i}", values, distinct=False, Plant="238") for i in range(12)]
        _write_bundle(tmp_path, "fill", spectra)
        with caplog.at_level("WARNING", logger="openspeclib.loaders.ecosis"):
            loader, records = _load(tmp_path, [_policy("fill")])
        assert len(records) == 1
        assert loader.ingest_stats[0].duplicate == 11
        assert any("12 rows share identical datapoints" in m for m in caplog.messages)

    def test_reflectance_and_transmittance_never_collide(self, tmp_path):
        values = _veg(0.4)
        spectra = [
            _spec("r", values, distinct=False, filename="ref_spec.csv"),
            _spec("t", values, distinct=False, filename="trans_spec.csv"),
        ]
        _write_bundle(tmp_path, "rt", spectra)
        _, records = _load(tmp_path, [_policy("rt", dedupe_key="datapoints")])
        assert len(records) == 2


class TestUnsupportedQuantity:
    def test_absorptance_resource_dropped_and_counted(self, tmp_path):
        spectra = [
            _spec("r", _veg(0.4), filename="ref_spec.csv"),
            _spec("a", _veg(0.2), filename="abs_spec.csv"),
        ]
        _write_bundle(tmp_path, "cabo", spectra)
        policy = _policy(
            "cabo",
            resource_techniques={
                "^ref_spec\\.csv$": "reflectance",
                "^abs_spec\\.csv$": "absorptance",
            },
        )
        loader, records = _load(tmp_path, [policy])
        assert [r.original_id for r in records] == ["r"]
        assert loader.ingest_stats[0].unsupported_quantity == 1

    def test_per_spectrum_absorptance_dropped(self, tmp_path):
        spectra = [
            _spec("r", _veg(0.4), **{"Measurement Quantity": "Reflectance"}),
            _spec("a", _veg(0.2), **{"Measurement Quantity": "Absorptance"}),
        ]
        _write_bundle(tmp_path, "mq", spectra)
        loader, records = _load(tmp_path, [_policy("mq")])
        assert [r.original_id for r in records] == ["r"]
        assert loader.ingest_stats[0].unsupported_quantity == 1


class TestSamplingUnits:
    def test_one_replicate_per_leaf(self, tmp_path):
        spectra = []
        for leaf in range(30):
            for rep in range(3):
                spectra.append(
                    _spec(
                        f"L{leaf}-r{rep}",
                        _veg(0.3 + 0.001 * (leaf * 3 + rep)),
                        functional_type="tree" if leaf < 20 else "forb",
                        id=f"leaf{leaf}",
                    )
                )
        _write_bundle(tmp_path, "lma", spectra)
        policy = _policy(
            "lma",
            inclusion="sample",
            sampling={
                "stratify_by": "functional_type",
                "max_total": 12,
                "min_per_stratum": 2,
                "one_per": "id",
            },
            carry_fields=["id"],
        )
        _, records = _load(tmp_path, [policy])
        leaves = [r.extra["ecosis_metadata"]["id"] for r in records]
        assert len(records) == 12
        assert len(set(leaves)) == 12  # 12 picks are 12 distinct leaves
        assert records[0].extra["ecosis_sampling"]["stratum_available"] in (
            20,
            10,
        )

    def test_stratum_keys_are_normalised(self, tmp_path):
        spectra = [
            _spec("a", _veg(0.31), ft="Tree"),
            _spec("b", _veg(0.32), ft="tree "),
            _spec("c", _veg(0.33), ft="TREE"),
        ]
        _write_bundle(tmp_path, "norm", spectra)
        policy = _policy(
            "norm", inclusion="sample", sampling={"stratify_by": "ft", "max_per_stratum": 1}
        )
        _, records = _load(tmp_path, [policy])
        assert len(records) == 1
        assert records[0].extra["ecosis_sampling"]["stratum"] == "tree"

    def test_name_map_merges_strata(self, tmp_path):
        spectra = [
            _spec("a", _veg(0.31), **{"Tree Species": "Cypress"}),
            _spec("b", _veg(0.32), **{"Tree Species": "Cupressus"}),
        ]
        _write_bundle(tmp_path, MULTISCALE, spectra)
        policy = _policy(
            MULTISCALE,
            inclusion="sample",
            sampling={"stratify_by": "Tree Species", "max_per_stratum": 5},
            identity={
                "material_name_field": "Tree Species",
                "material_name_map": {"Cypress": "Cupressus"},
            },
        )
        _, records = _load(tmp_path, [policy])
        assert {r.material.name for r in records} == {"Cupressus"}
        assert {r.extra["ecosis_sampling"]["stratum"] for r in records} == {"cupressus"}


class TestIdentityRules:
    def test_fixed_name_and_dominant_fraction_category(self, tmp_path):
        spectra = [
            _spec(
                "g",
                _veg(0.3),
                Spectra="Resop1",
                gv_fraction="0.7",
                npv_fraction="0.2",
                soil_fraction="0.1",
            ),
            _spec(
                "n",
                _veg(0.3),
                Spectra="Resop2",
                gv_fraction="0.1",
                npv_fraction="0.6",
                soil_fraction="0.3",
            ),
            _spec(
                "s",
                _veg(0.3),
                Spectra="Resop3",
                gv_fraction="0",
                npv_fraction="0.1",
                soil_fraction="0.9",
            ),
        ]
        _write_bundle(tmp_path, FRACTIONAL, spectra)
        policy = _policy(
            FRACTIONAL,
            identity={
                "fixed_material_name": "GV/NPV/soil field plot",
                "category_from_max_field": {
                    "gv_fraction": "vegetation",
                    "npv_fraction": "npv",
                    "soil_fraction": "soil",
                },
            },
            carry_fields=["gv_fraction", "npv_fraction", "soil_fraction"],
        )
        _, records = _load(tmp_path, [policy])
        by_id = {r.original_id: r for r in records}
        assert {r.material.name for r in records} == {"GV/NPV/soil field plot"}
        assert by_id["g"].material.category == MaterialCategory.VEGETATION
        assert by_id["n"].material.category == MaterialCategory.NPV
        assert by_id["s"].material.category == MaterialCategory.SOIL
        assert by_id["s"].extra["ecosis_metadata"]["soil_fraction"] == "0.9"

    def test_category_map_and_constant_category(self, tmp_path):
        spectra = [
            _spec("leaf", _veg(0.3), **{"Target Type": "Green Leaf", "Sample Name": "Sycamore"}),
            _spec("bark", _veg(0.3), **{"Target Type": "Grey Bark", "Sample Name": "Pine bark"}),
            _spec("pow", _veg(0.3), **{"Target Type": "Purified Powder", "Sample Name": "Lignin"}),
        ]
        _write_bundle(tmp_path, "fd", spectra)
        policy = _policy(
            "fd",
            identity={
                "material_name_field": "Sample Name",
                "category_map": {
                    "green leaf": "vegetation",
                    "grey bark": "npv",
                    "purified powder": "other",
                },
            },
        )
        _, records = _load(tmp_path, [policy])
        cats = {r.material.name: r.material.category for r in records}
        assert cats == {
            "Sycamore": MaterialCategory.VEGETATION,
            "Pine bark": MaterialCategory.NPV,
            "Lignin": MaterialCategory.OTHER,
        }
        _write_bundle(tmp_path, "plastic", [_spec("p", _veg(0.3), **{"Target Type": "nylon"})])
        _, recs = _load(tmp_path, [_policy("plastic", identity={"category": "man_made"})])
        assert recs[0].material.category == MaterialCategory.MAN_MADE

    def test_placeholder_values_ignored(self, tmp_path):
        spectra = [
            _spec("v", _veg(0.3), Dominant_Species="-9999", **{"Target Type": "Vegetation"}),
            _spec("r", _veg(0.3), Dominant_Species="SAPU15", **{"Target Type": "Road/gravel"}),
        ]
        _write_bundle(tmp_path, "ngee", spectra)
        policy = _policy(
            "ngee",
            identity={
                "material_name_field": "Target Type",
                "material_name_map": {"Vegetation": "arctic tundra canopy"},
                "keyword_fields": ["Dominant_Species"],
            },
        )
        _, records = _load(tmp_path, [policy])
        by_id = {r.original_id: r for r in records}
        assert by_id["v"].material.name == "arctic tundra canopy"
        assert "-9999" not in by_id["v"].material.keywords
        assert "sapu15" in by_id["r"].material.keywords
        assert by_id["r"].material.category == MaterialCategory.MAN_MADE


class TestDownloaderGuards:
    def test_missing_spectra_count_fails_loudly(self, tmp_path, monkeypatch):
        def fake_api_get(path, retries=3):
            if path.startswith("/package/"):
                return {"_id": "ds", "ecosis": {}}
            raise AssertionError("must not page spectra without a declared count")

        monkeypatch.setattr(ecosis, "_api_get", fake_api_get)
        monkeypatch.setattr(ecosis.time, "sleep", lambda s: None)
        loader = EcosisLoader(policies=[_policy("ds")])
        with pytest.raises(RuntimeError, match="1 EcoSIS dataset"):
            loader.download(tmp_path)
        assert not (tmp_path / "ecosis" / "ds.json").exists()
        assert not list((tmp_path / "ecosis").glob("*.part"))

    def test_completeness_counts_unique_ids(self, tmp_path):
        repeated = tmp_path / "repeated.json"
        repeated.write_text(
            json.dumps(
                {
                    "metadata": {"ecosis": {"spectra_count": 3}},
                    "spectra": [_spec("s0", _veg(0.3))] * 3,
                }
            )
        )
        assert not _bundle_is_complete(repeated)
        no_count = tmp_path / "no_count.json"
        no_count.write_text(json.dumps({"metadata": {"ecosis": {}}, "spectra": []}))
        assert not _bundle_is_complete(no_count)


class TestConstantFill:
    def test_all_zero_pixel_dropped(self, tmp_path):
        spectra = [_spec("ok", _veg(0.3)), _spec("zero", _flat(0.0))]
        _write_bundle(tmp_path, "fill", spectra)
        loader, records = _load(tmp_path, [_policy("fill")])
        assert [r.original_id for r in records] == ["ok"]
        assert loader.ingest_stats[0].constant_fill == 1


class TestCategoryPrecedence:
    def test_dataset_targets_used_only_when_unambiguous(self, tmp_path):
        _write_bundle(tmp_path, "one", [_spec("a", _veg(0.3))], **{"Target Type": ["leaf"]})
        _, records = _load(tmp_path, [_policy("one")])
        assert records[0].material.category == MaterialCategory.VEGETATION

        mixed = tmp_path / "mixed"
        mixed.mkdir()
        _write_bundle(mixed, "two", [_spec("b", _veg(0.3))], **{"Target Type": ["soil", "leaf"]})
        _, records = _load(mixed, [_policy("two")])
        assert records[0].material.category == MaterialCategory.OTHER

    def test_own_target_beats_constant_category(self, tmp_path):
        spectra = [
            _spec("road", _veg(0.3), **{"Target Type": "Road/gravel"}),
            _spec("none", _veg(0.31)),
        ]
        _write_bundle(tmp_path, "ngee", spectra, **{"Target Type": ["road/gravel", "vegetation"]})
        _, records = _load(tmp_path, [_policy("ngee", identity={"category": "vegetation"})])
        cats = {r.original_id: r.material.category for r in records}
        assert cats == {"road": MaterialCategory.MAN_MADE, "none": MaterialCategory.VEGETATION}

    def test_target_type_field_override(self, tmp_path):
        spectra = [_spec("s", _veg(0.3), **{"Surface Type": "soil"})]
        _write_bundle(tmp_path, "hawaii", spectra, **{"Target Type": ["canopy", "soil"]})
        policy = _policy(
            "hawaii",
            identity={"target_type_field": "Surface Type", "category_map": {"soil": "soil"}},
        )
        _, records = _load(tmp_path, [policy])
        assert records[0].material.category == MaterialCategory.SOIL
        assert records[0].material.subcategory == "soil"


class TestFormatV1Metadata:
    """Collection, dataset and licence metadata for the Format v1 footer."""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Open Data Commons Open Database License (ODbL)", "ODbL-1.0"),
            ("Open Data Commons Attribution License", "ODC-By-1.0"),
            ("Creative Commons Attribution", "LicenseRef-EcoSIS-CC-BY"),
            ("Creative Commons Attribution Share-Alike", "LicenseRef-EcoSIS-CC-BY-SA"),
            ("Creative Commons Non-Commercial (Any)", "LicenseRef-EcoSIS-CC-NC"),
            ("Other (Open)", "LicenseRef-EcoSIS-Other-Open"),
        ],
    )
    def test_license_map(self, label, expected):
        assert ecosis.map_ecosis_license(label) == (expected, None)

    @pytest.mark.parametrize("label", [None, "", "License not specified", "Some Custom Terms"])
    def test_unmapped_license_is_noassertion_with_note(self, label):
        license, note = ecosis.map_ecosis_license(label)
        assert license == "NOASSERTION"
        assert note
        if label:
            assert label in note

    def test_every_license_ref_is_defined(self):
        info = EcosisLoader().collection_info()
        refs = {v for v in ecosis.ECOSIS_LICENSE_MAP.values() if v.startswith("LicenseRef-")}
        assert refs <= set(info.license_refs)
        assert info.license == "NOASSERTION"
        assert info.license_note
        assert info.inclusion.value == "subset" and info.inclusion_notes

    def test_records_carry_dataset_license(self, tmp_path):
        _write_bundle(
            tmp_path,
            "cc",
            [_spec("a", _veg(0.4)), _spec("b", _veg(0.5))],
            ecosis={
                "package_title": "CC package",
                "spectra_count": 2,
                "license": "Creative Commons Attribution",
            },
        )
        _write_bundle(tmp_path, "none", [_spec("c", _veg(0.4))])
        loader, records = _load(tmp_path, [_policy("cc"), _policy("none")])
        by_id = {r.original_id: r for r in records}
        assert by_id["a"].license == "LicenseRef-EcoSIS-CC-BY"
        assert by_id["a"].id == "ecosis:a"
        assert by_id["c"].license == "NOASSERTION"
        datasets = loader.datasets()
        assert set(datasets) == {"cc", "none"}
        assert datasets["none"].license == "NOASSERTION"
        assert datasets["none"].license_note
        assert datasets["cc"].inclusion.value == "complete"
        assert datasets["cc"].inclusion_notes is None
        assert (datasets["cc"].spectra_available, datasets["cc"].spectra_included) == (2, 2)

    def test_subset_dataset_explains_drops(self, tmp_path):
        spectra = [_spec(f"s{i}", _veg(0.4), functional_type="tree") for i in range(5)]
        spectra.append(_spec("dup", _veg(0.4), distinct=False))
        spectra.append(_spec("dup2", _veg(0.4), distinct=False))
        _write_bundle(tmp_path, "samp", spectra)
        policy = _policy(
            "samp",
            inclusion="sample",
            sampling={"stratify_by": "functional_type", "max_total": 2},
        )
        loader, records = _load(tmp_path, [policy])
        ds = loader.datasets()["samp"]
        assert ds.inclusion.value == "subset"
        assert ds.spectra_included == len(records) == 2
        assert ds.spectra_available == 7
        assert "stratified sample by 'functional_type'" in ds.inclusion_notes
        assert "not selected by sampling" in ds.inclusion_notes

    def test_datasets_without_included_spectra_are_omitted(self, tmp_path):
        _write_bundle(tmp_path, "kept", [_spec("k", _veg(0.4))])
        _write_bundle(tmp_path, "empty", [_spec("x", _veg(0.4))])
        loader, _ = _load(tmp_path, [_policy("kept"), _policy("empty", drop_spectra={"x": "test"})])
        assert set(loader.datasets()) == {"kept"}


class TestSetup:
    def test_policy_setup_parsed_and_validated(self):
        policies = load_dataset_policies()
        for p in policies:
            if p.inclusion != "exclude":
                assert p.setup.evidence
        by_id = {p.id[:8]: p for p in policies}
        assert by_id["a12286ac"].setup.default["geometry"] == Geometry.DIRECTIONAL_HEMISPHERICAL
        assert by_id["38942519"].setup.default["instrument"] is None  # model output
        assert by_id["c7b6affd"].setup.default["geometry"] == Geometry.HEMISPHERICAL_CONICAL
        assert by_id["13658942"].reference == "spectralon"
        assert by_id["2074ef11"].conflicting_duplicates.action == "keep_one"

    @pytest.mark.parametrize(
        "setup, match",
        [
            ({"default": {"geometry": "biconical"}}, "evidence"),
            ({"default": {"geometry": "sideways"}, "evidence": "x"}, "geometry"),
            ({"default": {"colour": "red"}, "evidence": "x"}, "unknown key"),
            ({"field": "Acquisition_Method", "evidence": "x"}, "go together"),
            ({"field_map": {"LC": {"geometry": "biconical"}}, "evidence": "x"}, "go together"),
            ({"instrument_map": {"A": {"instrument": "a"}}, "evidence": "x"}, "instrument_field"),
            ({"resources": {"^a": {"geometry": "flat"}}, "evidence": "x"}, "geometry"),
            ({"evidence": "x", "typo": 1}, "unknown key"),
        ],
    )
    def test_bad_setup_blocks_rejected(self, setup, match):
        raw = {"id": "ds", "name": "n", "inclusion": "full", "reason": "r", "setup": setup}
        with pytest.raises(ValueError, match=match):
            _parse_policy(raw)

    def test_field_instrument_and_default_mapping(self, tmp_path):
        # HyspIRI pattern: setup by Acquisition_Method, spectrometer by Instrument.
        spectra = [
            _spec("lc", _veg(0.3), Acquisition_Method="LC", Instrument="ASD_FS3"),
            _spec("is", _veg(0.31), Acquisition_Method="IS", Instrument="SpecEvo_PSM3500"),
            _spec("odd", _veg(0.32), Acquisition_Method="XYZ", Instrument="Mystery"),
            _spec("none", _veg(0.33)),
        ]
        _write_bundle(
            tmp_path,
            "hy",
            spectra,
            **{
                "Light Source": ["lamp", "sun"],
                "Foreoptic Type": ["leaf clip", "integrating sphere"],
            },
        )
        policy = _policy(
            "hy",
            setup={
                "default": {
                    "geometry": "hemispherical_conical",
                    "instrument_type": "field_spectrometer",
                },
                "field": "Acquisition_Method",
                "field_map": {
                    "LC": {
                        "geometry": "biconical",
                        "light_source": "lamp",
                        "foreoptic": "leaf clip",
                    },
                    "IS": {
                        "geometry": "directional_hemispherical",
                        "foreoptic": "integrating sphere",
                    },
                },
                "instrument_field": "Instrument",
                "instrument_map": {
                    "ASD_FS3": {"instrument": "ASD FieldSpec 3"},
                    "SpecEvo_PSM3500": {"instrument": "Spectral Evolution PSM-3500"},
                },
                "evidence": "test",
            },
            reference="spectralon",
        )
        _, records = _load(tmp_path, [policy])
        m = {r.original_id: r.measurement for r in records}
        assert m["lc"].geometry == Geometry.BICONICAL
        assert (m["lc"].instrument, m["lc"].light_source, m["lc"].foreoptic) == (
            "ASD FieldSpec 3",
            "lamp",
            "leaf clip",
        )
        assert m["is"].geometry == Geometry.DIRECTIONAL_HEMISPHERICAL
        assert m["is"].instrument == "Spectral Evolution PSM-3500"
        # The package lists two light sources and the IS setup names none:
        # never borrow another setup's value.
        assert m["is"].light_source is None
        assert m["odd"].geometry == Geometry.UNKNOWN  # documented field, unmapped value
        assert m["odd"].instrument == "Mystery"
        assert m["odd"].foreoptic is None
        assert m["none"].geometry == Geometry.HEMISPHERICAL_CONICAL
        # An unrecognised instrument name is kept verbatim but its type is unknown.
        assert m["odd"].instrument_type is None
        assert all(
            r.measurement.instrument_type == "field_spectrometer"
            for r in records
            if r.original_id != "odd"
        )
        assert all(r.measurement.reference == "spectralon" for r in records)

    def test_resource_setup(self, tmp_path):
        spectra = [
            _spec("leaf", _veg(0.3), filename="LeafLevel_ASD_Spectra.csv"),
            _spec("canopy", _veg(0.31), filename="CanopyLevel_Piccolo_Spectra.csv"),
            _spec("other", _veg(0.32), filename="misc.csv"),
        ]
        _write_bundle(tmp_path, "fu", spectra, **{"Instrument Model": ["field spec 4", "flame"]})
        policy = _policy(
            "fu",
            setup={
                "resources": {
                    "^LeafLevel_ASD_": {"geometry": "biconical", "instrument": "ASD FieldSpec 4"},
                    "^CanopyLevel_Piccolo_": {
                        "geometry": "hemispherical_conical",
                        "instrument": "Piccolo Doppio",
                    },
                },
                "evidence": "test",
            },
        )
        _, records = _load(tmp_path, [policy])
        m = {r.original_id: r.measurement for r in records}
        assert (m["leaf"].geometry, m["leaf"].instrument) == (Geometry.BICONICAL, "ASD FieldSpec 4")
        assert (m["canopy"].geometry, m["canopy"].instrument) == (
            Geometry.HEMISPHERICAL_CONICAL,
            "Piccolo Doppio",
        )
        assert (m["other"].geometry, m["other"].instrument) == (Geometry.UNKNOWN, None)

    def test_single_valued_package_fields_fall_back(self, tmp_path):
        _write_bundle(
            tmp_path,
            "one",
            [_spec("a", _veg(0.3))],
            **{
                "Light Source": ["lamp"],
                "Foreoptic Type": ["leaf clip"],
                "Instrument Manufacturer": ["asd"],
                "Instrument Model": ["fieldspec 3", "fieldspec 3"],
            },
        )
        _, records = _load(tmp_path, [_policy("one")])
        m = records[0].measurement
        assert (m.light_source, m.foreoptic, m.instrument) == (
            "lamp",
            "leaf clip",
            "asd fieldspec 3",
        )
        assert m.geometry == Geometry.UNKNOWN
        assert m.instrument_type is None

    def test_explicit_null_clears_package_value(self, tmp_path):
        _write_bundle(
            tmp_path,
            "emit",
            [_spec("w", _veg(0.02))],
            **{"Instrument Manufacturer": ["model"], "Instrument Model": ["model"]},
        )
        policy = _policy("emit", setup={"default": {"instrument": None}, "evidence": "test"})
        _, records = _load(tmp_path, [policy])
        assert records[0].measurement.instrument is None


class TestConflictKeepOne:
    def test_keep_one_names_and_links(self, tmp_path):
        values = _veg(0.4)
        spectra = [
            _spec("a", values, distinct=False, SampleID="S1", **{"Common Name": "water sedge"}),
            _spec("b", values, distinct=False, SampleID="S2", **{"Common Name": "stone bramble"}),
        ]
        _write_bundle(tmp_path, "ngee", spectra)
        policy = _policy(
            "ngee",
            conflicting_duplicates={
                "action": "keep_one",
                "material_name": "arctic tundra canopy",
                "link_field": "SampleID",
            },
        )
        loader, records = _load(tmp_path, [policy])
        assert len(records) == 1
        r = records[0]
        assert r.material.name == "arctic tundra canopy"
        assert {"water sedge", "stone bramble"} <= set(r.material.keywords)
        assert r.extra["linked_sample_ids"] == ["S1", "S2"]
        assert r.extra["linked_material_names"] == ["stone bramble", "water sedge"]
        stats = loader.ingest_stats[0]
        assert (stats.conflicting_duplicate, stats.duplicate, stats.conflict_groups_kept) == (
            0,
            1,
            1,
        )

    def test_keep_one_needs_a_name(self):
        raw = {
            "id": "ds",
            "name": "n",
            "inclusion": "full",
            "reason": "r",
            "conflicting_duplicates": {"action": "keep_one"},
        }
        with pytest.raises(ValueError, match="material_name"):
            _parse_policy(raw)


class TestTargetNamesAndMixtures:
    def test_target_names_override_crop_common_name(self, tmp_path):
        spectra = [
            _spec("soil", _veg(0.3), **{"Target Type": "Ground / soil", "Common Name": "lemon"}),
            _spec("leaf", _veg(0.31), **{"Target Type": "Vegetation", "Common Name": "lemon"}),
        ]
        _write_bundle(tmp_path, "hy", spectra)
        policy = _policy("hy", identity={"target_names": {"Ground / soil": "soil"}})
        _, records = _load(tmp_path, [policy])
        by_id = {r.original_id: r for r in records}
        assert by_id["soil"].material.name == "soil"
        assert by_id["soil"].material.category == MaterialCategory.SOIL
        assert by_id["leaf"].material.name == "lemon"

    def test_mixture_threshold(self, tmp_path):
        spectra = [
            _spec("pure", _veg(0.3), gv_fraction="1.0", npv_fraction="0", soil_fraction="0"),
            _spec("mix", _veg(0.31), gv_fraction="0.6", npv_fraction="0.3", soil_fraction="0.1"),
        ]
        _write_bundle(tmp_path, "fc", spectra)
        policy = _policy(
            "fc",
            identity={
                "category_from_max_field": {
                    "gv_fraction": "vegetation",
                    "npv_fraction": "npv",
                    "soil_fraction": "soil",
                },
                "mixture_threshold": 0.999,
            },
        )
        _, records = _load(tmp_path, [policy])
        cats = {r.original_id: r.material.category for r in records}
        assert cats == {"pure": MaterialCategory.VEGETATION, "mix": MaterialCategory.MIXTURE}

    def test_mixture_threshold_requires_fractions(self):
        raw = {
            "id": "ds",
            "name": "n",
            "inclusion": "full",
            "reason": "r",
            "identity": {"mixture_threshold": 0.9},
        }
        with pytest.raises(ValueError, match="mixture_threshold"):
            _parse_policy(raw)


class TestDarkPercentWarning:
    def test_percent_units_with_unit_like_level_warns(self, tmp_path, caplog):
        spectra = [_spec(f"s{i}", _veg(1.2 + 0.01 * i)) for i in range(3)]
        _write_bundle(tmp_path, "dark", spectra, **{"Measurement Units": ["%"]})
        with caplog.at_level("WARNING", logger="openspeclib.loaders.ecosis"):
            loader, _ = _load(tmp_path, [_policy("dark")])
        assert loader.ingest_stats[0].base_divisor == 1
        assert any("declares Measurement Units '%'" in m for m in caplog.messages)

    def test_no_warning_for_ordinary_unit_data(self, tmp_path, caplog):
        spectra = [_spec(f"s{i}", _veg(0.4 + 0.01 * i)) for i in range(3)]
        _write_bundle(tmp_path, "ok", spectra, **{"Measurement Units": ["%"]})
        with caplog.at_level("WARNING", logger="openspeclib.loaders.ecosis"):
            _load(tmp_path, [_policy("ok")])
        assert not any("Measurement Units" in m for m in caplog.messages)


class TestDownloaderPartCheck:
    def test_zero_spectra_count_rejected(self, tmp_path, monkeypatch):
        def fake_api_get(path, retries=3):
            if path.startswith("/package/"):
                return {"_id": "ds", "ecosis": {"spectra_count": 0}}
            raise AssertionError("must not page spectra for an empty package")

        monkeypatch.setattr(ecosis, "_api_get", fake_api_get)
        monkeypatch.setattr(ecosis.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="1 EcoSIS dataset"):
            EcosisLoader(policies=[_policy("ds")]).download(tmp_path)
        assert not (tmp_path / "ecosis" / "ds.json").exists()

    def test_overlapping_pages_not_installed(self, tmp_path, monkeypatch):
        # The API repeats a page: the item count reaches spectra_count but
        # only 2 of the declared 3 spectra are distinct.
        pages = iter(
            [
                {"items": [_spec("s0", _veg(0.3)), _spec("s1", _veg(0.31))]},
                {"items": [_spec("s1", _veg(0.31))]},
            ]
        )

        def fake_api_get(path, retries=3):
            if path.startswith("/package/"):
                return {"_id": "ds", "ecosis": {"spectra_count": 3}}
            return next(pages)

        monkeypatch.setattr(ecosis, "_api_get", fake_api_get)
        monkeypatch.setattr(ecosis.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="1 EcoSIS dataset"):
            EcosisLoader(policies=[_policy("ds")]).download(tmp_path)
        out = tmp_path / "ecosis"
        assert not (out / "ds.json").exists()
        assert not list(out.glob("*.part"))


class TestIdsUnitsAndExclusions:
    def test_spectrum_without_id_is_dropped_and_counted(self, tmp_path):
        good = _spec("g", _veg(0.4))
        missing = _spec("x", _veg(0.5))
        del missing["_id"]
        _write_bundle(tmp_path, "ids", [good, missing])
        loader, records = _load(tmp_path, [_policy("ids")])
        assert [r.original_id for r in records] == ["g"]
        stats = loader.ingest_stats[0]
        assert stats.missing_id == 1
        assert loader.fatal_error_count == 0
        ds = loader.datasets()["ids"]
        assert ds.inclusion.value == "subset"
        assert "1 without an EcoSIS _id" in ds.inclusion_notes

    def test_excluded_packages_named_in_collection_notes(self):
        info = EcosisLoader().collection_info()
        notes = info.inclusion_notes
        assert CEDAR_CREEK in notes
        assert "Cedar Creek" in notes and "25,414 spectra" in notes
        assert "no material identity" in notes

    def test_no_exclusions_keeps_base_notes(self):
        info = EcosisLoader(policies=[_policy("a")]).collection_info()
        assert info.inclusion_notes == ecosis.ECOSIS_COLLECTION.inclusion_notes

    def test_micrometre_package_kept_native(self, tmp_path):
        um = [0.4 + 0.1 * i for i in range(20)]
        spectra = [_spec(f"s{i}", _veg(0.4)) for i in range(2)]
        for s in spectra:
            s["datapoints"] = {f"{w:.1f}": v for w, v in zip(um, s["datapoints"].values())}
            s["datapoints"]["Sample"] = s["_id"]
        _write_bundle(tmp_path, "um", spectra, **{"Wavelength Units": ["um"]})
        _, records = _load(tmp_path, [_policy("um")])
        r = records[0]
        assert r.spectral_data.wavelength_unit.value == "um"
        assert r.spectral_data.wavelengths[0] == pytest.approx(0.4)
        assert "wavelength_unit_inferred" not in r.extra

    def test_contradicting_unit_inferred_from_axis(self, tmp_path):
        um = [0.4 + 0.1 * i for i in range(20)]
        spec = _spec("s", _veg(0.4))
        spec["datapoints"] = {f"{w:.1f}": v for w, v in zip(um, spec["datapoints"].values())}
        _write_bundle(tmp_path, "bad", [spec], **{"Wavelength Units": ["nm"]})
        _, records = _load(tmp_path, [_policy("bad")])
        r = records[0]
        assert r.spectral_data.wavelength_unit.value == "um"
        note = r.extra["wavelength_unit_inferred"]
        assert note["unit"] == "um" and note["declared"] == ["nm"]

    def test_missing_unit_inferred_and_recorded(self, tmp_path):
        _write_bundle(tmp_path, "nounit", [_spec("s", _veg(0.4))])
        _, records = _load(tmp_path, [_policy("nounit")])
        r = records[0]
        assert r.spectral_data.wavelength_unit.value == "nm"
        assert r.extra["wavelength_unit_inferred"]["declared"] is None

    def test_spectrum_level_unit_wins(self):
        unit, note = ecosis.resolve_wavelength_unit([400.0, 2500.0], ["nanometers"], ["um"])
        assert unit.value == "nm" and note is None

    def test_duplicate_band_keys_resolved(self, tmp_path):
        spec = _spec("s", _veg(0.4))
        spec["datapoints"]["400.0"] = "0.9"  # same wavelength as "400"
        _write_bundle(tmp_path, "dups", [spec], **{"Wavelength Units": ["nm"]})
        _, records = _load(tmp_path, [_policy("dups")])
        r = records[0]
        wl = r.spectral_data.wavelengths
        assert len(wl) == len(set(wl)) == len(WAVELENGTHS)
        assert r.extra["duplicate_wavelength_bands_dropped"][0][1] == 400.0


class TestSaturation:
    def test_dominant_value_fraction(self):
        assert dominant_value_fraction([1.0, 1.0, 0.5, float("nan")]) == pytest.approx(2 / 3)
        assert dominant_value_fraction([float("nan")]) == 0.0

    def test_saturated_spectrum_dropped_and_explained(self, tmp_path):
        # Atqasuk 2016 day-213 pattern: most bands pinned at exactly 1.0.
        saturated = [0.05 + 0.01 * i for i in range(6)] + [1.0] * 14
        spectra = [_spec(f"s{i}", _veg(0.3 + 0.01 * i)) for i in range(3)]
        spectra.append(_spec("sat", saturated))
        spectra.append(_spec("zero", _flat(0.0)))
        _write_bundle(tmp_path, "atq", spectra)
        loader, records = _load(tmp_path, [_policy("atq")])
        assert {r.original_id for r in records} == {"s0", "s1", "s2"}
        stats = loader.ingest_stats[0]
        assert (stats.saturated, stats.constant_fill) == (1, 1)
        notes = loader.datasets()["atq"].inclusion_notes
        assert "1 saturated or clipped (over half of the bands hold one value)" in notes

    def test_half_equal_bands_kept(self, tmp_path):
        # _veg holds its NIR plateau on exactly half the bands: not saturated.
        _write_bundle(tmp_path, "veg", [_spec("v", _veg(0.4))])
        loader, records = _load(tmp_path, [_policy("veg")])
        assert len(records) == 1
        assert loader.ingest_stats[0].saturated == 0


class TestDuplicateLabels:
    def test_copy_error_group_dropped(self, tmp_path):
        # Fusarium pattern: 190 rows (here 12) of different samples (consecutive
        # SampleNumbers) carrying one spectrum and filled-down Plant labels.
        values = _veg(0.4)
        spectra = [
            _spec(f"s{i}", values, distinct=False, Plant="238", SampleNumber=str(2895 + i))
            for i in range(12)
        ]
        spectra.append(_spec("ok", _veg(0.5), Plant="22", SampleNumber="4606"))
        _write_bundle(tmp_path, "fus", spectra)
        policy = _policy("fus", carry_fields=["Plant", "SampleNumber"])
        loader, records = _load(tmp_path, [policy])
        assert [r.original_id for r in records] == ["ok"]
        stats = loader.ingest_stats[0]
        assert (stats.copy_error, stats.duplicate) == (12, 0)
        assert "12 in fill-down copy-error groups" in loader.datasets()["fus"].inclusion_notes

    def test_large_group_differing_only_in_other_metadata_is_duplicates(self, tmp_path):
        # Same sample labels and identity, different measurement times: honest
        # repeats, collapsed to one record rather than dropped as a copy error.
        values = _veg(0.4)
        spectra = [
            _spec(f"s{i}", values, distinct=False, Plant="238", Time=f"10:{i:02d}")
            for i in range(12)
        ]
        _write_bundle(tmp_path, "rep", spectra)
        loader, records = _load(tmp_path, [_policy("rep", carry_fields=["Plant"])])
        assert len(records) == 1
        stats = loader.ingest_stats[0]
        assert (stats.copy_error, stats.duplicate) == (0, 11)
        assert len(records[0].extra["duplicate_ids"]) == 11

    @pytest.mark.parametrize(
        "field, values",
        [
            ("Sample ID", lambda i: f"S{i}"),
            ("Sample Description", lambda i: f"leaf {i}"),
            ("USDA Symbol", lambda i: f"AC{i:02d}"),
        ],
    )
    def test_copy_error_on_a_label_difference(self, tmp_path, field, values):
        spectra = [
            _spec(f"s{i}", _veg(0.4), distinct=False, **{field: values(i)}) for i in range(12)
        ]
        _write_bundle(tmp_path, "grp", spectra)
        loader, records = _load(tmp_path, [_policy("grp")])
        assert records == []
        assert loader.ingest_stats[0].copy_error == 12

    def test_curated_fusarium_policy_carries_the_sample_number(self):
        by_id = {p.id: p for p in load_dataset_policies()}
        fusarium = by_id["2a9d952a-5a28-47b3-9f85-8aa6e6808991"]
        # The fill-down rows differ only in SampleNumber; carrying it makes
        # that a label difference, so the group stays a copy error.
        assert "SampleNumber" in ecosis._identity_fields(fusarium)

    def test_small_group_conflicting_labels_removed_with_evidence(self, tmp_path):
        # Multiscale pattern: adjacent pixels with one spectrum but different
        # coordinates and sample ids; the kept pixel gets neither copy's value.
        values = _veg(0.4)
        spectra = [
            _spec("p1", values, distinct=False, Lat="31.1", Site="Dorot", Sample_ID="A"),
            _spec("p2", values, distinct=False, Lat="31.2", Site="Dorot", Sample_ID="B"),
        ]
        _write_bundle(tmp_path, "ms", spectra)
        _, records = _load(tmp_path, [_policy("ms", carry_fields=["Lat", "Site"])])
        assert len(records) == 1
        r = records[0]
        assert r.extra["ecosis_metadata"] == {"Site": "Dorot"}
        assert r.extra["duplicate_conflicting_fields"] == {
            "Sample ID": ["A", "B"],
            "Lat": ["31.1", "31.2"],
        }
        assert r.sample is None or r.sample.id is None

    def test_keep_one_has_no_sample_id_and_is_explained(self, tmp_path):
        values = _veg(0.4)
        spectra = [
            _spec("a", values, distinct=False, SampleID="S1", **{"Common Name": "water sedge"}),
            _spec("b", values, distinct=False, SampleID="S2", **{"Common Name": "stone bramble"}),
            _spec("c", _veg(0.5), SampleID="S3", **{"Common Name": "dwarf birch"}),
        ]
        _write_bundle(tmp_path, "ngee", spectra)
        policy = _policy(
            "ngee",
            conflicting_duplicates={
                "action": "keep_one",
                "material_name": "arctic tundra canopy",
                "link_field": "SampleID",
            },
        )
        loader, records = _load(tmp_path, [policy])
        by_id = {r.original_id: r for r in records}
        merged = by_id[min({"a", "b"}, key=lambda s: ecosis._sample_rank(s))]
        assert merged.sample is None or merged.sample.id is None
        assert merged.extra["linked_sample_ids"] == ["S1", "S2"]
        assert "duplicate_conflicting_fields" not in merged.extra
        assert by_id["c"].sample.id == "S3"
        notes = loader.datasets()["ngee"].inclusion_notes
        assert "1 duplicate groups of one measurement linked to several labelled samples" in notes


class TestMetadataKeys:
    def test_field_lookup_ignores_case_and_separators(self):
        spectrum = {"Latin Genus": "", "Latin genus": "Acacia", "latin_species": "koa"}
        assert ecosis._field_str(spectrum, "Latin Genus") == "Acacia"
        assert ecosis._field_str(spectrum, "Latin Species") == "koa"
        assert ecosis._field_str({"Latin Genus": "Pinus", "latin genus": "x"}, "Latin Genus") == (
            "Pinus"
        )
        assert ecosis._field_str({"Other": "x"}, "Latin Genus") is None

    def test_lower_case_latin_names_name_the_material(self, tmp_path):
        # Hawaii pattern: only the lower-case "Latin genus"/"Latin species" set.
        spectra = [
            _spec(
                "h",
                _veg(0.4),
                Spectra="h19fpamo1",
                **{"Latin genus": "Acacia", "Latin species": "koa"},
            )
        ]
        _write_bundle(tmp_path, "hi", spectra)
        _, records = _load(tmp_path, [_policy("hi")])
        assert records[0].material.name == "Acacia koa"
        assert {"acacia", "koa"} <= set(records[0].material.keywords)

    @pytest.mark.parametrize("key", ["Sample ID", "SampleID", "sample_id", "Sample_ID"])
    def test_sample_id_spellings_and_usda_symbol(self, tmp_path, key):
        spectra = [_spec("s", _veg(0.4), **{key: "X-17", "USDA Symbol": "ACRU"})]
        _write_bundle(tmp_path, "ids", spectra)
        _, records = _load(tmp_path, [_policy("ids")])
        assert records[0].sample.id == "X-17"
        assert records[0].extra["usda_symbol"] == "ACRU"

    @pytest.mark.parametrize(
        "value, key",
        [
            ("ACRU", "usda_symbol"),
            ("ACME80", "usda_symbol"),
            ("Grape", "source_plant_code"),
            ("MandarinOrange", "source_plant_code"),
            ("Salix sp.", "source_plant_code"),
            ("SPAL-SPCY", "source_plant_code"),
            ("A", "source_plant_code"),
            ("ABCDEFGHI", "source_plant_code"),
        ],
    )
    def test_only_symbol_shaped_values_are_usda_symbols(self, tmp_path, value, key):
        _write_bundle(tmp_path, "usda", [_spec("s", _veg(0.4), **{"USDA Symbol": value})])
        _, records = _load(tmp_path, [_policy("usda")])
        other = {"usda_symbol", "source_plant_code"} - {key}
        assert records[0].extra[key] == value
        assert not other & set(records[0].extra)

    def test_usda_symbol_is_not_a_sample_id(self, tmp_path):
        _write_bundle(tmp_path, "usda", [_spec("s", _veg(0.4), **{"USDA Symbol": "ACRU"})])
        _, records = _load(tmp_path, [_policy("usda")])
        assert records[0].sample is None or records[0].sample.id is None
        assert records[0].extra["usda_symbol"] == "ACRU"

    def test_name_keeps_the_whole_dataset_title(self, tmp_path):
        title = "Leaf and canopy level detection of Fusarium virguliforme in soybean"
        _write_bundle(
            tmp_path,
            "long",
            [_spec("s", _veg(0.4))],
            ecosis={"package_title": title, "spectra_count": 1},
        )
        _, records = _load(tmp_path, [_policy("long", identity={"material_name": "soybean"})])
        assert records[0].name == f"soybean ({title})"

    def test_fallback_fields_beat_the_spectra_label(self, tmp_path):
        # HyspIRI pattern: no Common Name, an opaque Spectra label, and a
        # common name in the USDA Symbol field.
        spectra = [
            _spec(
                "g", _veg(0.4), Spectra="CVARS_na_Grape_L1T1chem_LC_R", **{"USDA Symbol": "Grape"}
            ),
            _spec("x", _veg(0.4), Spectra="label_only"),
        ]
        _write_bundle(tmp_path, "hy", spectra)
        policy = _policy(
            "hy",
            identity={
                "fallback_name_fields": ["USDA Symbol"],
                "material_name_map": {"Grape": "grape"},
            },
        )
        _, records = _load(tmp_path, [policy])
        assert {r.original_id: r.material.name for r in records} == {
            "g": "grape",
            "x": "label_only",
        }

    def test_curated_names_for_unnamed_hawaii_and_hyspiri_rows(self):
        by_id = {p.id: p for p in load_dataset_policies()}
        hawaii = by_id["060d2822-f250-4869-b734-4a92450393f0"].identity
        assert hawaii.fallback_name_fields == ("Description",)
        assert set(hawaii.material_name_map.values()) == {"grass", "ginger"}
        hyspiri = by_id["dd94f09c-1794-44f4-82e9-a7ca707a1ec0"].identity
        assert hyspiri.fallback_name_fields == ("USDA Symbol",)
        assert hyspiri.material_name_map["Grape"] == "grape"
