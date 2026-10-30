"""Build the OpenSpecLib Format v1 conformance corpus.

Usage::

    python tests/conformance/build_fixtures.py

Writes ``tests/conformance/fixtures/*.parquet`` and
``tests/conformance/expected.json``. The files are written with pyarrow
directly from a reading of ``docs/spec/format-v1.md``; nothing here goes
through ``openspeclib.storage`` or ``openspeclib.conformance``, so the corpus
is an independent check of the checker.

Each fixture declares its expected outcome next to the code that builds it:
``conformant``, the must-rules it fails and the should-rules it warns about.
Invalid collection fixtures start from a valid file and change exactly one
thing, so that they violate exactly one rule where that is possible.

Determinism: every input is fixed (including the footer ``created``
timestamp), so re-running the script reproduces byte-identical files *for the
same pyarrow version*. Parquet files record the writer in ``created_by``
(e.g. ``parquet-cpp-arrow version 23.0.1``) and encodings can differ between
versions, so a different pyarrow produces different bytes, and the ``sha256``
values in the index fixtures change with them. The expected outcomes do not.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
CREATED = "2026-10-29T12:00:00Z"
GENERATOR = "openspeclib conformance corpus 1"
NAN = float("nan")

# Column types as section 4 and section 5 of the spec state them.
CORE_FIELDS: list[tuple[str, pa.DataType]] = [
    ("id", pa.string()),
    ("collection", pa.string()),
    ("original_id", pa.string()),
    ("name", pa.string()),
    ("material_name", pa.string()),
    ("material_category", pa.string()),
    ("quantity", pa.string()),
    ("geometry", pa.string()),
    ("wavelength_unit", pa.string()),
    ("wavelengths", pa.list_(pa.float64())),
    ("values", pa.list_(pa.float32())),
    ("wavelength_min_nm", pa.float64()),
    ("wavelength_max_nm", pa.float64()),
    ("license", pa.string()),
    ("dataset_id", pa.string()),
]
EXTENSION_FIELDS: dict[str, list[tuple[str, pa.DataType]]] = {
    "material": [
        ("material_subcategory", pa.string()),
        ("material_formula", pa.string()),
        ("material_keywords", pa.list_(pa.string())),
    ],
    "sample": [
        ("sample_id", pa.string()),
        ("sample_description", pa.string()),
        ("sample_particle_size", pa.string()),
        ("sample_origin", pa.string()),
        ("sample_owner", pa.string()),
        ("sample_collection_date", pa.date32()),
        ("sample_preparation", pa.string()),
    ],
    "measurement": [
        ("measurement_instrument", pa.string()),
        ("measurement_instrument_type", pa.string()),
        ("measurement_laboratory", pa.string()),
        ("measurement_date", pa.date32()),
        ("measurement_processing", pa.list_(pa.string())),
        ("measurement_light_source", pa.string()),
        ("measurement_venue", pa.string()),
        ("measurement_acquisition_method", pa.string()),
        ("measurement_foreoptic", pa.string()),
        ("measurement_reference", pa.string()),
    ],
    "bandpass": [("bandpass_fwhm", pa.list_(pa.float32()))],
    "uncertainty": [("values_stddev", pa.list_(pa.float32()))],
    "quality": [("quality_notes", pa.string())],
}
ALL_EXTENSIONS = list(EXTENSION_FIELDS)
PER_BAND = ("wavelengths", "values", "bandpass_fwhm", "values_stddev")

EXPECTED: dict[str, dict[str, Any]] = {}
FOOTERS: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def to_nm(value: float, unit: str) -> float:
    if unit == "um":
        return value * 1000.0
    if unit == "cm-1":
        return 1e7 / value
    return value


def spectrum(cid: str, local: str, unit: str, wavelengths: list[float], values: list[float], **kw):
    finite = [to_nm(w, unit) for w in wavelengths if w is not None and math.isfinite(w)]
    row = {
        "id": f"{cid}:{local}",
        "collection": cid,
        "original_id": f"SRC-{local}",
        "name": f"Sample {local}",
        "material_name": "Olivine",
        "material_category": "mineral",
        "quantity": "reflectance",
        "geometry": "directional_hemispherical",
        "wavelength_unit": unit,
        "wavelengths": wavelengths,
        "values": values,
        "wavelength_min_nm": min(finite) if finite else None,
        "wavelength_max_nm": max(finite) if finite else None,
        "license": "CC-BY-4.0",
        "dataset_id": None,
    }
    row.update(kw)
    return row


def footer(cid: str, *, extensions: list[str] | None = None, datasets=None, **collection):
    meta: dict[str, Any] = {
        "format_version": "1.0.0",
        "collection": {
            "id": cid,
            "title": f"Conformance corpus collection {cid}",
            "version": "1",
            "license": "CC-BY-4.0",
            "citation": "OpenSpecLib conformance corpus.",
            "inclusion": "complete",
        },
        "extensions": list(extensions or []),
        "created": CREATED,
        "generator": GENERATOR,
    }
    meta["collection"].update(collection)
    if datasets is not None:
        meta["datasets"] = datasets
    return meta


def fields_for(extensions: list[str], extra: list[tuple[str, pa.DataType]] = ()):
    fields = list(CORE_FIELDS)
    for ext in extensions:
        fields.extend(EXTENSION_FIELDS[ext])
    fields.extend(extra)
    return fields


def write_collection(
    name: str,
    rows: list[dict[str, Any]],
    meta: dict[str, Any] | None,
    *,
    fields: list[tuple[str, pa.DataType]] | None = None,
    raw_footer: bytes | None = None,
    sort: bool = True,
    row_group_size: int = 250,
    recommended_encoding: bool = True,
    id_statistics: bool = True,
    extra_metadata: dict[bytes, bytes] | None = None,
    bss: list[str] | None = None,
    compression: str = "zstd",
    store_schema: bool = True,
    patch: Callable[[Path], None] | None = None,
    expect: dict[str, Any],
    out: Path,
) -> None:
    """Write one collection fixture and record its expected outcome.

    ``bss`` lists the per-band columns written with BYTE_STREAM_SPLIT (default:
    all present); ``patch`` post-processes the written file.
    """
    if fields is None:
        fields = fields_for(meta["extensions"] if meta else [])
    if sort:
        rows = sorted(rows, key=lambda r: r["id"])
    schema = pa.schema([pa.field(n, t) for n, t in fields])
    table = pa.Table.from_pylist([{n: r.get(n) for n, _ in fields} for r in rows], schema=schema)
    metadata = {}
    if raw_footer is not None:
        metadata[b"openspeclib"] = raw_footer
    elif meta is not None:
        metadata[b"openspeclib"] = json.dumps(meta).encode("utf-8")
    metadata.update(extra_metadata or {})
    table = table.replace_schema_metadata(metadata)
    names = [n for n, _ in fields]
    options: dict[str, Any] = {"row_group_size": row_group_size}
    if recommended_encoding:
        split = [n for n in PER_BAND if n in names] if bss is None else bss
        options.update(
            compression=compression,
            use_dictionary=[n for n, t in fields if n not in PER_BAND and pa.types.is_string(t)],
            use_byte_stream_split=[f"{n}.list.element" for n in split],
        )
    if not id_statistics:
        options["write_statistics"] = [n for n in names if n != "id"]
    if store_schema:
        pq.write_table(table, out / name, **options)
    else:
        # Like a non-Arrow writer: no ARROW:schema entry, footer key written directly.
        row_group_size = options.pop("row_group_size")
        with pq.ParquetWriter(
            out / name, table.schema.remove_metadata(), store_schema=False, **options
        ) as writer:
            writer.write_table(table, row_group_size=row_group_size)
            writer.add_key_value_metadata({k.decode(): v.decode() for k, v in metadata.items()})
    if patch is not None:
        patch(out / name)
    if meta is not None:
        FOOTERS[name] = meta
    EXPECTED[name] = expect


def ok(description: str, warned: list[str] | None = None) -> dict[str, Any]:
    return {"description": description, "conformant": True, "failed": [], "warned": warned or []}


def bad(description: str, failed: list[str], warned: list[str] | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "conformant": False,
        "failed": failed,
        "warned": warned or [],
    }


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

MIN = "conformance_minimal"


def minimal_rows() -> list[dict[str, Any]]:
    return [
        spectrum(MIN, "a1", "nm", [400.0, 500.0, 600.0, 700.0], [0.10, 0.20, 0.30, 0.40]),
        spectrum(
            MIN,
            "a2",
            "um",
            [0.4, 0.8, 1.6, 2.5],
            [0.31, 0.35, 0.40, 0.42],
            material_name="Quartz",
            geometry="biconical",
        ),
        spectrum(
            MIN,
            "a3",
            "um",
            [8.0, 9.0, 10.0, 12.0],
            [0.95, 0.91, 0.97, 0.98],
            quantity="emissivity",
            geometry="directional",
            material_category="rock",
            material_name="Basalt",
        ),
    ]


def minimal_footer(**kw) -> dict[str, Any]:
    return footer(MIN, **kw)


FULL = "conformance_full"


def full_footer() -> dict[str, Any]:
    return footer(
        FULL,
        extensions=ALL_EXTENSIONS,
        title="Conformance corpus aggregated collection",
        license="NOASSERTION",
        license_note="Each row's `license` column is authoritative.",
        license_refs={
            "LicenseRef-Lab-B-Terms": {
                "name": "Laboratory B data terms",
                "url": "https://example.org/lab-b-terms",
            }
        },
        doi="10.5555/conformance.full",
        keywords=["conformance", "test"],
        datasets={
            "lab_a": {
                "title": "Laboratory A minerals",
                "license": "CC-BY-4.0",
                "citation": "Lab A (2026).",
                "inclusion": "complete",
                "spectra_available": 3,
                "spectra_included": 3,
            },
            "lab_b": {
                "title": "Laboratory B leaves",
                "license": "LicenseRef-Lab-B-Terms",
                "inclusion": "subset",
                "inclusion_notes": "Only leaf spectra with complete metadata.",
                "spectra_available": 10,
                "spectra_included": 2,
            },
        },
    )


def full_rows() -> list[dict[str, Any]]:
    import datetime as dt

    rows = [
        spectrum(
            FULL,
            "b01",
            "nm",
            [400.0, 500.0, 600.0, 700.0],
            [0.12, NAN, 0.25, 0.31],
            dataset_id="lab_a",
            material_subcategory="olivine group",
            material_formula="(Mg,Fe)2SiO4",
            material_keywords=["olivine", "forsterite"],
            sample_id="LA-001",
            sample_description="Powdered olivine",
            sample_particle_size="<45 um",
            sample_origin="San Carlos, Arizona",
            sample_owner="Laboratory A",
            sample_collection_date=dt.date(2019, 5, 17),
            sample_preparation="Ground and sieved",
            measurement_instrument="ASD FieldSpec 4",
            measurement_instrument_type="field_spectroradiometer",
            measurement_laboratory="Laboratory A",
            measurement_date=dt.date(2020, 1, 2),
            measurement_processing=["averaged"],
            measurement_light_source="lamp",
            measurement_venue="laboratory",
            measurement_acquisition_method="integrating_sphere",
            measurement_foreoptic="bare fibre",
            measurement_reference="spectralon",
            bandpass_fwhm=[3.0, 3.0, 10.0, 10.0],
            values_stddev=[0.001, NAN, 0.002, 0.0],
            quality_notes="Band 2 masked: detector saturation.",
            extra=json.dumps({"masked_bands": {"500": 1.7}}),
            x_custom=7,
        ),
        spectrum(
            FULL,
            "b02",
            "cm-1",
            [4000.0, 3000.0, 2000.0, 1000.0],
            [0.40, 0.42, 0.45, 0.50],
            dataset_id="lab_b",
            license="LicenseRef-Lab-B-Terms",
            material_category="vegetation",
            material_name="Quercus alba",
            geometry="biconical",
            measurement_processing=["resampled", "smoothed"],
            bandpass_fwhm=[4.0, 4.0, 4.0, 4.0],
            extra=json.dumps({}),
            x_custom=8,
        ),
        spectrum(
            FULL,
            "b03",
            "um",
            [0.35, 1.0, 2.5],
            [0.05, 0.08, 0.06],
            dataset_id="lab_b",
            license="(CC-BY-4.0 AND LicenseRef-Lab-B-Terms) OR CC0-1.0",
            material_category="npv",
            material_name="Oak bark",
            geometry="unknown",
        ),
        spectrum(
            FULL,
            "b04",
            "nm",
            [1000.0, 1500.0, 2000.0],
            [0.3, 0.6, 0.9],
            dataset_id="lab_a",
            license="GPL-2.0-or-later WITH Classpath-exception-2.0 OR MIT+",
            quantity="absorbance",
            measurement_processing=["absorbance_from_reflectance"],
            geometry="hemispherical_conical",
        ),
        spectrum(
            FULL,
            "b05",
            "nm",
            [400.0, 450.0, 500.0],
            [0.2, 0.2, 0.2],
            dataset_id="lab_a",
            quantity="transmittance",
            material_category="man_made",
            material_name="Glass",
            # 2.5e-7 relative error: inside the F10 tolerance of 1e-6.
            wavelength_min_nm=400.0001,
        ),
        spectrum(
            FULL,
            "b06",
            "nm",
            [400.0, 500.0, 600.0],
            [0.5, 0.6, 0.7],
            material_category="water",
            material_name="Lake water",
            geometry="bihemispherical",
        ),
    ]
    return rows


def full_fields() -> list[tuple[str, pa.DataType]]:
    return fields_for(ALL_EXTENSIONS, [("extra", pa.string()), ("x_custom", pa.int64())])


def build_collections(out: Path) -> None:
    w = write_collection

    # -- valid -------------------------------------------------------------
    w(
        "valid_minimal.parquet",
        minimal_rows(),
        minimal_footer(),
        expect=ok("Core columns only; nm and um axes; no extensions, no datasets."),
        out=out,
    )
    w(
        "valid_full.parquet",
        full_rows(),
        full_footer(),
        fields=full_fields(),
        expect=ok(
            "Every extension column; aggregated collection with two datasets; NOASSERTION "
            "collection licence; LicenseRef row and dataset licences; compound SPDX "
            "expressions (AND/OR/WITH/+); descending cm-1 axis; NaN in values and stddev; "
            "`extra` and `x_custom` columns; wavelength_min_nm off by 2.5e-7 (within F10)."
        ),
        out=out,
    )

    plain_to_large = {
        "name": pa.large_string(),
        "license": pa.string_view(),
        "values": pa.large_list(pa.float32()),
        "wavelengths": pa.large_list(pa.float64()),
    }
    w(
        "valid_large_types.parquet",
        minimal_rows(),
        minimal_footer(),
        fields=[(n, plain_to_large.get(n, t)) for n, t in CORE_FIELDS],
        expect=ok(
            "large_string, string_view and large_list columns: equivalent to the plain "
            "types (spec section 4)."
        ),
        out=out,
    )

    # -- F1 ----------------------------------------------------------------
    w(
        "f1_missing_footer.parquet",
        minimal_rows(),
        None,
        fields=fields_for([]),
        expect=bad("No `openspeclib` footer key (F3, F5, F6, F11, F12 are skipped).", ["F1"]),
        out=out,
    )
    w(
        "f1_invalid_json.parquet",
        minimal_rows(),
        None,
        fields=fields_for([]),
        raw_footer=b'{"format_version": "1.0.0",',
        expect=bad("Footer value is not valid JSON.", ["F1"]),
        out=out,
    )
    w(
        "f1_unsupported_major.parquet",
        minimal_rows(),
        minimal_footer() | {"format_version": "2.0.0"},
        expect=bad("format_version 2.0.0.", ["F1"]),
        out=out,
    )
    w(
        "f1_unknown_footer_field.parquet",
        minimal_rows(),
        minimal_footer() | {"producer": "someone"},
        expect=bad("Footer has a property the schema does not allow.", ["F1"]),
        out=out,
    )
    w(
        "f1_subset_without_notes.parquet",
        minimal_rows(),
        minimal_footer(inclusion="subset"),
        expect=bad("collection.inclusion is subset without inclusion_notes.", ["F1"]),
        out=out,
    )
    (out / "f1_not_parquet.parquet").write_bytes(b"id,name\nx:1,not parquet\n")
    EXPECTED["f1_not_parquet.parquet"] = bad(
        "Not a Parquet file (attributed to F1; other rules skipped).", ["F1"]
    )

    w(
        "f1_both_footer_keys.parquet",
        minimal_rows(),
        minimal_footer(),
        extra_metadata={b"openspeclib_index": b"{}"},
        expect=bad("File carries both `openspeclib` and `openspeclib_index`.", ["F1"]),
        out=out,
    )
    w(
        "f1_bad_created.parquet",
        minimal_rows(),
        minimal_footer() | {"created": "2026-10-29 12:00"},
        expect=bad("`created` is not an RFC 3339 timestamp with a time zone.", ["F1"]),
        out=out,
    )

    # -- F2 ----------------------------------------------------------------
    rows = minimal_rows()
    rows[2]["name"] = ""
    w(
        "f2_empty_name.parquet",
        rows,
        minimal_footer(),
        expect=bad("`name` is an empty string on one row.", ["F2"]),
        out=out,
    )
    w(
        "f2_missing_column.parquet",
        minimal_rows(),
        minimal_footer(),
        fields=[f for f in CORE_FIELDS if f[0] != "original_id"],
        expect=bad("Core column original_id is missing.", ["F2"]),
        out=out,
    )
    rows = minimal_rows()
    rows[1]["name"] = None
    w(
        "f2_null_value.parquet",
        rows,
        minimal_footer(),
        expect=bad("Non-nullable core column `name` holds a null.", ["F2"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["values"] = None
    w(
        "f2_null_values_list.parquet",
        rows,
        minimal_footer(),
        expect=bad("`values` is null on one row.", ["F2"]),
        out=out,
    )
    rows = minimal_rows()
    for i, r in enumerate(rows):
        r["original_id"] = 1000 + i
    w(
        "f2_wrong_type.parquet",
        rows,
        minimal_footer(),
        fields=[(n, pa.int64() if n == "original_id" else t) for n, t in CORE_FIELDS],
        expect=bad("original_id is int64 instead of string.", ["F2"]),
        out=out,
    )

    # -- F3 ----------------------------------------------------------------
    rows = minimal_rows()
    for r in rows:
        r["notes"] = "free text"
    w(
        "f3_unknown_column.parquet",
        rows,
        minimal_footer(),
        fields=fields_for([], [("notes", pa.string())]),
        expect=bad("Column `notes` is neither defined, `extra` nor `x_`-prefixed.", ["F3"]),
        out=out,
    )
    rows = minimal_rows()
    for r in rows:
        r["material_formula"] = "SiO2"
    w(
        "f3_undeclared_extension.parquet",
        rows,
        minimal_footer(),
        fields=fields_for([], [("material_formula", pa.string())]),
        expect=bad("material_formula present but `material` not declared.", ["F3"]),
        out=out,
    )
    rows = minimal_rows()
    for r in rows:
        r["num_points"] = len(r["values"])
    w(
        "f3_index_column.parquet",
        rows,
        minimal_footer(),
        fields=fields_for([], [("num_points", pa.int32())]),
        expect=bad("Index-only column num_points in a collection file.", ["F3"]),
        out=out,
    )

    # -- F4 ----------------------------------------------------------------
    rows = minimal_rows()
    for r in rows:
        r["sample_collection_date"] = "2020-01-02"
    w(
        "f4_extension_type.parquet",
        rows,
        minimal_footer(extensions=["sample"]),
        fields=fields_for([], [("sample_collection_date", pa.string())]),
        expect=bad("sample_collection_date is string instead of date32.", ["F4"]),
        out=out,
    )

    # -- F5 ----------------------------------------------------------------
    rows = minimal_rows()
    rows[2]["id"] = rows[1]["id"]
    w(
        "f5_duplicate_id.parquet",
        rows,
        minimal_footer(),
        expect=bad("Two rows share an id.", ["F5"]),
        out=out,
    )
    rows = minimal_rows()
    rows[1]["id"] = f"{MIN}:a 2"
    w(
        "f5_bad_local_id.parquet",
        rows,
        minimal_footer(),
        expect=bad("Local id contains a space.", ["F5"]),
        out=out,
    )
    rows = minimal_rows()
    rows[2]["id"] = "other_collection:a3"
    w(
        "f5_wrong_prefix.parquet",
        rows,
        minimal_footer(),
        expect=bad("Id prefix is not the footer collection.id.", ["F5"]),
        out=out,
    )

    # -- F6 ----------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["collection"] = "other_collection"
    w(
        "f6_collection_mismatch.parquet",
        rows,
        minimal_footer(),
        expect=bad("`collection` differs from the footer collection.id on one row.", ["F6"]),
        out=out,
    )

    # -- F7 ----------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["quantity"] = "radiance"
    w(
        "f7_bad_quantity.parquet",
        rows,
        minimal_footer(),
        expect=bad("quantity 'radiance' is not in the vocabulary.", ["F7"]),
        out=out,
    )
    rows = minimal_rows()
    rows[1]["geometry"] = "Biconical"
    w(
        "f7_vocabulary_case.parquet",
        rows,
        minimal_footer(),
        expect=bad("geometry 'Biconical': vocabularies are case-sensitive.", ["F7"]),
        out=out,
    )

    # -- F8 ----------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["wavelengths"] = [400.0, 600.0, 500.0, 700.0]
    w(
        "f8_not_monotonic.parquet",
        rows,
        minimal_footer(),
        expect=bad("Axis goes up, down and up again (extent still correct).", ["F8"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["wavelengths"] = [400.0, 500.0, 500.0, 700.0]
    w(
        "f8_repeated_wavelength.parquet",
        rows,
        minimal_footer(),
        expect=bad("Axis repeats a value: not strictly monotonic.", ["F8"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0] = spectrum(MIN, "a1", "nm", [500.0], [0.2])
    w(
        "f8_single_point.parquet",
        rows,
        minimal_footer(),
        expect=bad("Axis with one element.", ["F8"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["wavelengths"] = [400.0, NAN, 600.0, 700.0]
    w(
        "f8_nan_wavelength.parquet",
        rows,
        minimal_footer(),
        expect=bad("NaN in the axis (extent of the finite values is correct).", ["F8"]),
        out=out,
    )

    rows = minimal_rows()
    rows[0] = spectrum(MIN, "a1", "nm", [0.0, 100.0, 200.0, 300.0], [0.1, 0.2, 0.3, 0.4])
    w(
        "f8_zero_wavelength.parquet",
        rows,
        minimal_footer(),
        expect=bad("Axis starts at 0 nm: wavelengths must be > 0.", ["F8"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0] = spectrum(MIN, "a1", "um", [-0.5, 0.4, 0.8, 1.6], [0.1, 0.2, 0.3, 0.4])
    w(
        "f8_negative_wavelength.parquet",
        rows,
        minimal_footer(),
        expect=bad("Negative wavelength (extent still matches).", ["F8"]),
        out=out,
    )

    # -- F9 ----------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["values"] = [0.1, 0.2, 0.3]
    w(
        "f9_length_mismatch.parquet",
        rows,
        minimal_footer(),
        expect=bad("`values` shorter than `wavelengths`.", ["F9"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["values"] = [0.1, -1.23e34, 0.3, 0.4]
    w(
        "f9_sentinel.parquet",
        rows,
        minimal_footer(),
        expect=bad("USGS-style sentinel -1.23e34 instead of NaN.", ["F9"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["values"] = [0.1, float("inf"), 0.3, 0.4]
    w(
        "f9_infinity.parquet",
        rows,
        minimal_footer(),
        expect=bad("Infinite value.", ["F9"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["values"] = [0.1, None, 0.3, 0.4]
    w(
        "f9_null_element.parquet",
        rows,
        minimal_footer(),
        expect=bad("Null element inside `values`.", ["F9"]),
        out=out,
    )

    # -- F10 ---------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["wavelength_max_nm"] = 701.0
    w(
        "f10_extent_mismatch.parquet",
        rows,
        minimal_footer(),
        expect=bad("wavelength_max_nm 701 for an axis ending at 700 nm.", ["F10"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0] = spectrum(
        MIN,
        "a1",
        "cm-1",
        [4000.0, 3000.0, 2000.0],
        [0.1, 0.2, 0.3],
        wavelength_min_nm=2000.0,
        wavelength_max_nm=4000.0,
    )
    w(
        "f10_wavenumber_not_converted.parquet",
        rows,
        minimal_footer(),
        expect=bad("cm-1 axis with its extent stored in cm-1 instead of nm.", ["F10"]),
        out=out,
    )
    rows = minimal_rows()
    rows[1]["wavelength_min_nm"] = 0.4
    rows[1]["wavelength_max_nm"] = 2.5
    w(
        "f10_microns_not_converted.parquet",
        rows,
        minimal_footer(),
        expect=bad("um axis with its extent stored in um instead of nm.", ["F10"]),
        out=out,
    )

    # -- F11 ---------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["license"] = "CC-BY-4.0 AND"
    w(
        "f11_bad_syntax.parquet",
        rows,
        minimal_footer(),
        expect=bad("Licence expression ends with an operator.", ["F11"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["license"] = "MIT or Apache-2.0"
    w(
        "f11_lowercase_operator.parquet",
        rows,
        minimal_footer(),
        expect=bad("Lower-case `or` is not an SPDX operator.", ["F11"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["license"] = "LicenseRef-Nowhere"
    w(
        "f11_undefined_ref.parquet",
        rows,
        minimal_footer(),
        expect=bad("Row uses a LicenseRef not defined in collection.license_refs.", ["F11"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["license"] = ""
    w(
        "f11_empty.parquet",
        rows,
        minimal_footer(),
        expect=bad(
            "Empty licence string: F2 (non_empty) only; F11 covers non-empty values.",
            ["F2"],
        ),
        out=out,
    )
    w(
        "f11_footer_licence.parquet",
        minimal_rows(),
        minimal_footer(license="CC BY 4.0"),
        expect=bad("collection.license 'CC BY 4.0' is not an SPDX expression.", ["F11"]),
        out=out,
    )

    rows = minimal_rows()
    rows[0]["license"] = "MIT AND NOASSERTION"
    w(
        "f11_noassertion_compound.parquet",
        rows,
        minimal_footer(),
        expect=bad("NOASSERTION inside a compound expression.", ["F11"]),
        out=out,
    )

    # -- F12 ---------------------------------------------------------------
    rows = minimal_rows()
    for r in rows:
        r["dataset_id"] = "lab_a"
    rows[0]["dataset_id"] = "lab_z"
    w(
        "f12_unknown_dataset.parquet",
        rows,
        minimal_footer(datasets={"lab_a": {"title": "Laboratory A"}}),
        expect=bad("dataset_id 'lab_z' is not a footer datasets key.", ["F12"]),
        out=out,
    )

    # -- F13 ---------------------------------------------------------------
    rows = minimal_rows()
    for r in rows:
        r["bandpass_fwhm"] = [5.0] * len(r["wavelengths"])
    rows[0]["bandpass_fwhm"] = [5.0, 5.0, 5.0]
    w(
        "f13_bandpass_length.parquet",
        rows,
        minimal_footer(extensions=["bandpass"]),
        expect=bad("bandpass_fwhm shorter than wavelengths.", ["F13"]),
        out=out,
    )
    rows = minimal_rows()
    rows[2]["values_stddev"] = [0.01, -0.02, 0.01, 0.01]
    w(
        "f13_negative_stddev.parquet",
        rows,
        minimal_footer(extensions=["uncertainty"]),
        expect=bad("Negative standard deviation.", ["F13"]),
        out=out,
    )

    # -- should-rules --------------------------------------------------------
    w(
        "r1_unsorted.parquet",
        list(reversed(minimal_rows())),
        minimal_footer(),
        sort=False,
        expect=ok("Rows not sorted by id.", ["R1"]),
        out=out,
    )
    w(
        "r1_no_id_statistics.parquet",
        minimal_rows(),
        minimal_footer(),
        id_statistics=False,
        expect=ok("No Parquet statistics for `id`.", ["R1"]),
        out=out,
    )
    many = [
        spectrum(MIN, f"s{i:03d}", "nm", [400.0, 500.0], [0.1, 0.2], original_id=f"S{i}")
        for i in range(260)
    ]
    w(
        "r2_large_row_group.parquet",
        many,
        minimal_footer(),
        row_group_size=1000,
        expect=ok("One row group of 260 spectra.", ["R2"]),
        out=out,
    )
    w(
        "r3_default_encoding.parquet",
        minimal_rows(),
        minimal_footer(),
        recommended_encoding=False,
        expect=ok("pyarrow defaults: dictionary encoding and snappy.", ["R3"]),
        out=out,
    )
    rows = minimal_rows()
    rows[1]["values"] = [NAN, NAN, NAN, NAN]
    w(
        "r4_all_nan.parquet",
        rows,
        minimal_footer(),
        expect=ok("One spectrum is entirely NaN.", ["R4"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["geometry"] = "unknown"
    rows[1]["geometry"] = "unknown"
    w(
        "r5_unknown_geometry.parquet",
        rows,
        minimal_footer(),
        expect=ok("2 of 3 rows have geometry 'unknown' (R5 approximation: > 50 %).", ["R5"]),
        out=out,
    )


# ---------------------------------------------------------------------------
# Index files
# ---------------------------------------------------------------------------


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def index_rows(out: Path, files: list[str]) -> pa.Table:
    """Derive index rows from collection files, as section 7 describes."""
    tables = []
    for file in files:
        table = pq.read_table(out / file)
        num_points = pc.cast(pc.list_value_length(table["values"]), pa.int32())
        table = table.drop_columns([c for c in PER_BAND if c in table.column_names])
        table = table.append_column("num_points", num_points)
        table = table.append_column("file", pa.array([file] * table.num_rows, pa.string()))
        tables.append(table.replace_schema_metadata(None))
    return pa.concat_tables(tables, promote_options="default")


def index_footer(out: Path, files: list[str], with_sha: tuple[str, ...] = ()) -> dict[str, Any]:
    entries = []
    for file in files:
        entry: dict[str, Any] = {
            "file": file,
            "num_spectra": pq.ParquetFile(out / file).metadata.num_rows,
        }
        if file in with_sha:
            entry["sha256"] = sha256(out / file)
        entry["metadata"] = copy.deepcopy(FOOTERS[file])
        entries.append(entry)
    return {
        "format_version": "1.0.0",
        "release": "conformance-1",
        "created": CREATED,
        "generator": GENERATOR,
        "collections": entries,
    }


def write_index(
    name: str, table: pa.Table, meta: dict[str, Any], expect: dict[str, Any], out: Path
) -> None:
    table = table.replace_schema_metadata({b"openspeclib_index": json.dumps(meta).encode()})
    pq.write_table(table, out / name, compression="zstd")
    EXPECTED[name] = expect


def replace_column(table: pa.Table, name: str, values: list[Any], type_=None) -> pa.Table:
    i = table.column_names.index(name)
    return table.set_column(i, name, pa.array(values, type_ or table.schema.field(name).type))


def build_indexes(out: Path) -> None:
    files = ["valid_minimal.parquet", "valid_full.parquet"]
    base = index_rows(out, files)
    meta = index_footer(out, files, with_sha=("valid_minimal.parquet",))

    write_index(
        "index_valid.parquet",
        base,
        meta,
        ok(
            "Index over valid_minimal and valid_full: every non-array column, extension "
            "columns only valid_full has, `extra`, `x_custom`, num_points and file; "
            "sha256 for one entry."
        ),
        out,
    )

    broken = copy.deepcopy(meta)
    del broken["generator"]
    write_index("index_bad_footer.parquet", base, broken, bad("No `generator`.", ["I1"]), out)

    write_index(
        "index_missing_footer.parquet",
        base,
        {},
        bad("Footer `openspeclib_index` is an empty object.", ["I1"]),
        out,
    )

    write_index(
        "index_missing_column.parquet",
        base.drop_columns(["num_points"]),
        meta,
        bad("No num_points column.", ["I1"]),
        out,
    )
    write_index(
        "index_missing_extension_column.parquet",
        base.drop_columns(["material_formula"]),
        meta,
        bad("material_formula (present in valid_full) is not indexed.", ["I1"]),
        out,
    )
    write_index(
        "index_wrong_type.parquet",
        replace_column(base, "num_points", base["num_points"].to_pylist(), pa.int64()),
        meta,
        bad("num_points is int64 instead of int32.", ["I1"]),
        out,
    )

    names = base["material_name"].to_pylist()
    names[0] = names[0] + " (edited)"
    write_index(
        "index_value_mismatch.parquet",
        replace_column(base, "material_name", names),
        meta,
        bad("material_name differs from the collection file on one row.", ["I1"]),
        out,
    )

    keywords = base["material_keywords"].to_pylist()
    first_full = files.index("valid_full.parquet")
    row = base["file"].to_pylist().index(files[first_full])
    keywords[row] = list(reversed(keywords[row]))
    write_index(
        "index_list_mismatch.parquet",
        replace_column(base, "material_keywords", keywords),
        meta,
        bad("material_keywords (a list column) differs on one row.", ["I1"]),
        out,
    )

    points = base["num_points"].to_pylist()
    points[0] += 1
    write_index(
        "index_wrong_num_points.parquet",
        replace_column(base, "num_points", points),
        meta,
        bad("num_points does not equal len(values).", ["I1"]),
        out,
    )

    write_index(
        "index_missing_row.parquet",
        base.slice(1),
        meta,
        bad("A collection row is not in the index.", ["I1"]),
        out,
    )

    extra = base.slice(0, 1)
    extra = replace_column(extra, "id", [f"{MIN}:zz_not_there"])
    write_index(
        "index_extra_row.parquet",
        pa.concat_tables([base, extra]),
        meta,
        bad("An index row has no matching collection row.", ["I1"]),
        out,
    )

    edited = copy.deepcopy(meta)
    edited["collections"][1]["metadata"]["collection"]["title"] = "Edited title"
    write_index(
        "index_metadata_mismatch.parquet",
        base,
        edited,
        bad("Footer metadata differs from the collection file's footer.", ["I1"]),
        out,
    )

    edited = copy.deepcopy(meta)
    edited["collections"][0]["num_spectra"] = 99
    write_index(
        "index_num_spectra_mismatch.parquet",
        base,
        edited,
        bad("num_spectra differs from the file's row count.", ["I1"]),
        out,
    )

    edited = copy.deepcopy(meta)
    edited["collections"][0]["sha256"] = "0" * 64
    write_index(
        "index_sha256_mismatch.parquet",
        base,
        edited,
        bad("sha256 does not match the file.", ["I1"]),
        out,
    )

    edited = copy.deepcopy(meta)
    edited["collections"].append(copy.deepcopy(edited["collections"][0]))
    edited["collections"][-1]["file"] = "does_not_exist.parquet"
    write_index(
        "index_missing_file.parquet",
        base,
        edited,
        bad("Footer lists a file that does not exist.", ["I1"]),
        out,
    )

    write_index(
        "index_unknown_column.parquet",
        base.append_column("notes", pa.array(["x"] * base.num_rows, pa.string())),
        meta,
        bad("Index has a column `notes` that is neither required nor `x_`.", ["I1"]),
        out,
    )
    write_index(
        "index_duplicate_row.parquet",
        pa.concat_tables([base, base.slice(0, 1)]),
        meta,
        bad("One collection row appears twice in the index.", ["I1"]),
        out,
    )
    formulas = base["material_formula"].to_pylist()
    minimal_row = base["file"].to_pylist().index("valid_minimal.parquet")
    formulas[minimal_row] = "SiO2"
    write_index(
        "index_extension_value_for_lacking_file.parquet",
        replace_column(base, "material_formula", formulas),
        meta,
        bad(
            "material_formula is set on a valid_minimal row, but valid_minimal has no "
            "material_formula column (must be null).",
            ["I1"],
        ),
        out,
    )

    nonconformant = ["f7_bad_quantity.parquet"]
    write_index(
        "index_of_nonconformant.parquet",
        index_rows(out, nonconformant),
        index_footer(out, nonconformant),
        {
            "description": "I1 holds, but the indexed collection file fails F7, so the "
            "index does not conform.",
            "conformant": False,
            "failed": [],
            "warned": [],
        },
        out,
    )


# ---------------------------------------------------------------------------
# Round 3: boundaries, new rules and type equivalence
# ---------------------------------------------------------------------------


def footer_bounds(path: Path) -> tuple[int, int]:
    """Return the byte range of the Parquet footer (thrift FileMetaData)."""
    data = path.read_bytes()
    length = struct.unpack("<I", data[-8:-4])[0]
    return len(data) - 8 - length, len(data) - 8


def mark_dictionary(path: Path) -> None:
    """Rewrite one BSS column chunk's encodings [RLE, BSS] as [RLE_DICTIONARY, BSS].

    pyarrow cannot write a chunk that is both dictionary- and BSS-encoded (its
    dictionary fallback is PLAIN), so this edits the thrift-compact list in
    place (same length). The data pages are unchanged and stay readable.
    """
    data = bytearray(path.read_bytes())
    start, end = footer_bounds(path)
    pattern = bytes([0x25, 0x06, 0x12])  # list<i32>[2]: RLE (3), BYTE_STREAM_SPLIT (9)
    offset = bytes(data[start:end]).index(pattern)
    data[start + offset + 1] = 0x10  # RLE_DICTIONARY (8)
    path.write_bytes(bytes(data))


def corrupt_values_page(path: Path) -> None:
    """Overwrite the page header of the first `values` data page with 0xFF bytes."""
    meta = pq.ParquetFile(path).metadata
    group = meta.row_group(0)
    chunk = next(
        group.column(i)
        for i in range(group.num_columns)
        if group.column(i).path_in_schema.startswith("values.")
    )
    data = bytearray(path.read_bytes())
    data[chunk.data_page_offset : chunk.data_page_offset + 8] = b"\xff" * 8
    path.write_bytes(bytes(data))


def build_round3(out: Path) -> None:
    w = write_collection

    # -- F1 ------------------------------------------------------------------
    w(
        "f1_empty_footer_licence.parquet",
        minimal_rows(),
        minimal_footer(license=""),
        expect=bad("collection.license is empty: the footer schema (F1), not F11.", ["F1"]),
        out=out,
    )
    w(
        "f1_impossible_created.parquet",
        minimal_rows(),
        minimal_footer() | {"created": "2026-13-01T00:00:00Z"},
        expect=bad("`created` has the right shape but month 13.", ["F1"]),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["dataset_id"] = "lab a"
    w(
        "f1_bad_dataset_key.parquet",
        rows,
        minimal_footer(datasets={"lab a": {"title": "Lab A"}}),
        expect=bad("datasets key 'lab a' does not match ^[A-Za-z0-9._~-]{1,200}$.", ["F1"]),
        out=out,
    )
    w(
        "f1_corrupt_page.parquet",
        minimal_rows(),
        minimal_footer(),
        patch=corrupt_values_page,
        expect=bad(
            "The `values` data page header is overwritten: F1 (not valid Parquet); the "
            "row-level rules are skipped.",
            ["F1"],
        ),
        out=out,
    )
    rows = minimal_rows()
    for r in rows:
        r["notes"] = "x"
    w(
        "f1_f3_no_footer_unknown_column.parquet",
        rows,
        None,
        fields=fields_for([], [("notes", pa.string())]),
        expect=bad("No footer (F1); F3 still checks column names.", ["F1", "F3"]),
        out=out,
    )

    # -- F2 and type equivalence ---------------------------------------------
    rows = minimal_rows()
    rows[1]["name"] = " \t "
    w(
        "f2_whitespace_name.parquet",
        rows,
        minimal_footer(),
        expect=bad("`name` is whitespace-only on one row.", ["F2"]),
        out=out,
    )
    w(
        "valid_dictionary_string.parquet",
        minimal_rows(),
        minimal_footer(),
        fields=[
            (n, pa.dictionary(pa.int32(), pa.string()) if n == "material_name" else t)
            for n, t in CORE_FIELDS
        ],
        expect=ok("material_name stored as dictionary<int32, string>: still a string."),
        out=out,
    )
    w(
        "valid_fixed_size_list.parquet",
        minimal_rows(),
        minimal_footer(),
        fields=[
            (
                n,
                {
                    "values": pa.list_(pa.float32(), 4),
                    "wavelengths": pa.list_(pa.float64(), 4),
                }.get(n, t),
            )
            for n, t in CORE_FIELDS
        ],
        expect=ok("values and wavelengths stored as fixed_size_list[4]: still lists."),
        out=out,
    )
    w(
        "valid_null_dataset_id.parquet",
        minimal_rows(),
        minimal_footer(),
        fields=[(n, pa.null() if n == "dataset_id" else t) for n, t in CORE_FIELDS],
        expect=ok("All-null dataset_id stored with the Arrow null type."),
        out=out,
    )
    w(
        "valid_no_arrow_schema.parquet",
        full_rows(),
        full_footer(),
        fields=full_fields(),
        store_schema=False,
        expect=ok(
            "valid_full written without an ARROW:schema entry, as non-Arrow writers do: "
            "types come from the Parquet logical types alone."
        ),
        out=out,
    )
    w(
        "valid_empty.parquet",
        [],
        minimal_footer(),
        expect=ok("Zero rows: conformant, and exempt from R1."),
        out=out,
    )
    w(
        "valid_minimal_subset.parquet",
        minimal_rows()[:1],
        minimal_footer(inclusion="subset", inclusion_notes="Only the first spectrum."),
        expect=ok("A deliberate subset (one row) of valid_minimal, with inclusion_notes."),
        out=out,
    )

    # -- F3 ------------------------------------------------------------------
    rows = minimal_rows()
    for i, r in enumerate(rows):
        r["x_meta"] = {"source": "lab", "run": i}
    w(
        "f3_nested_x_column.parquet",
        rows,
        minimal_footer(),
        fields=fields_for(
            [], [("x_meta", pa.struct([("source", pa.string()), ("run", pa.int64())]))]
        ),
        expect=bad("x_meta is a struct: x_ columns must be flat.", ["F3"]),
        out=out,
    )

    # -- F5 ------------------------------------------------------------------
    for name, local, description in (
        ("f5_empty_local_id.parquet", "", "Empty local id (`conformance_minimal:`)."),
        ("f5_id_too_long.parquet", "a" * 201, "Local id of 201 characters (limit 200)."),
    ):
        rows = minimal_rows()
        rows[0]["id"] = f"{MIN}:{local}"
        w(name, rows, minimal_footer(), expect=bad(description, ["F5"]), out=out)
    rows = minimal_rows()
    rows[0]["id"] = f"zz{MIN}:a1"
    w(
        "f5_unanchored_prefix.parquet",
        rows,
        minimal_footer(),
        expect=bad("Id `zzconformance_minimal:a1` merely contains the prefix.", ["F5"]),
        out=out,
    )

    # -- F8 ------------------------------------------------------------------
    for name, axis, description in (
        ("f8_infinite_wavelength.parquet", [400.0, 500.0, 600.0, math.inf], "+inf in axis."),
        ("f8_null_wavelength.parquet", [400.0, None, 600.0, 700.0], "Null element in axis."),
    ):
        rows = minimal_rows()
        rows[0] = spectrum(MIN, "a1", "nm", axis, [0.1, 0.2, 0.3, 0.4])
        w(name, rows, minimal_footer(), expect=bad(description, ["F8"]), out=out)
    rows = minimal_rows()
    rows[0] = spectrum(MIN, "a1", "nm", [700.0, 600.0, 500.0, 400.0], [0.4, 0.3, 0.2, 0.1])
    w(
        "valid_descending_nm.parquet",
        rows,
        minimal_footer(),
        expect=ok("A descending nm axis: min is the last element."),
        out=out,
    )

    # -- F9 ------------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["values"] = [1e10, -1e10, 0.3, 0.4]
    w(
        "valid_sentinel_limit.parquet",
        rows,
        minimal_footer(),
        expect=ok("Values of exactly +-1e10 are allowed (the limit is > 1e10)."),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["values"] = [0.1, 1.0001e10, 0.3, 0.4]
    w(
        "f9_just_above_sentinel.parquet",
        rows,
        minimal_footer(),
        expect=bad("A value of 1.0001e10 is a sentinel.", ["F9"]),
        out=out,
    )

    # -- F10 -----------------------------------------------------------------
    rows = minimal_rows()
    rows[0]["wavelength_min_nm"] = 401.0
    w(
        "f10_min_mismatch.parquet",
        rows,
        minimal_footer(),
        expect=bad("Only wavelength_min_nm is wrong.", ["F10"]),
        out=out,
    )

    rows = minimal_rows()
    rows[0]["wavelength_max_nm"] = math.inf
    w(
        "f10_infinite_stored.parquet",
        rows,
        minimal_footer(),
        expect=bad("wavelength_max_nm is +inf: never equal to a finite extent.", ["F10"]),
        out=out,
    )

    # -- F11 -----------------------------------------------------------------
    rows = minimal_rows()
    for r in rows:
        r["dataset_id"] = "lab_a"
    w(
        "f11_dataset_licence.parquet",
        rows,
        minimal_footer(datasets={"lab_a": {"title": "Lab A", "license": "CC BY 4.0"}}),
        expect=bad("datasets.lab_a.license is not an expression.", ["F11"]),
        out=out,
    )
    for name, value, description in (
        ("f11_invalid_character.parquet", "MIT/Apache-2.0", "A `/` inside an identifier."),
        ("f11_double_space.parquet", "MIT  OR Apache-2.0", "Two spaces between tokens."),
        ("f11_trailing_space.parquet", "MIT ", "Trailing space."),
        ("f11_tab.parquet", "MIT\tOR Apache-2.0", "A tab between tokens."),
        ("f11_nbsp.parquet", "MIT OR Apache-2.0", "A non-breaking space."),
        ("f11_deep_nesting.parquet", "(" * 200 + "MIT" + ")" * 200, "200 nested parentheses."),
        (
            "f11_documentref.parquet",
            "DocumentRef-spdx-tool-1.2:LicenseRef-x",
            "DocumentRef- references are not allowed.",
        ),
    ):
        rows = minimal_rows()
        rows[0]["license"] = value
        w(name, rows, minimal_footer(), expect=bad(description, ["F11"]), out=out)

    # -- F12 -----------------------------------------------------------------
    rows = minimal_rows()
    for r in rows:
        r["dataset_id"] = "lab_a"
    w(
        "f12_unused_dataset.parquet",
        rows,
        minimal_footer(datasets={"lab_a": {"title": "Lab A"}, "lab_b": {"title": "Lab B"}}),
        expect=bad("datasets key lab_b has no rows.", ["F12"]),
        out=out,
    )
    w(
        "f12_spectra_included_mismatch.parquet",
        rows,
        minimal_footer(datasets={"lab_a": {"title": "Lab A", "spectra_included": 5}}),
        expect=bad("lab_a says spectra_included 5, but 3 rows use it.", ["F12"]),
        out=out,
    )

    # -- F13 -----------------------------------------------------------------
    for name, column, extension, band, description in (
        (
            "f13_infinite_element.parquet",
            "bandpass_fwhm",
            "bandpass",
            [5.0, math.inf, 5.0, 5.0],
            "Infinite bandpass.",
        ),
        (
            "f13_null_element.parquet",
            "values_stddev",
            "uncertainty",
            [0.01, None, 0.01, 0.01],
            "Null stddev element.",
        ),
    ):
        rows = minimal_rows()
        rows[0][column] = band
        w(
            name,
            rows,
            minimal_footer(extensions=[extension]),
            expect=bad(description, ["F13"]),
            out=out,
        )

    # -- R2, R3 --------------------------------------------------------------
    many = [
        spectrum(MIN, f"s{i:03d}", "nm", [400.0, 500.0], [0.1, 0.2], original_id=f"S{i}")
        for i in range(250)
    ]
    w(
        "valid_250_rows.parquet",
        many,
        minimal_footer(),
        expect=ok("One row group of exactly 250 spectra: no R2 warning."),
        out=out,
    )
    w(
        "r3_no_bss.parquet",
        minimal_rows(),
        minimal_footer(),
        bss=[],
        expect=ok("zstd without dictionary, but no BYTE_STREAM_SPLIT.", ["R3"]),
        out=out,
    )
    w(
        "r3_dictionary.parquet",
        minimal_rows(),
        minimal_footer(),
        patch=mark_dictionary,
        expect=ok("BYTE_STREAM_SPLIT and zstd, but one chunk records dictionary encoding.", ["R3"]),
        out=out,
    )
    w(
        "r3_snappy.parquet",
        minimal_rows(),
        minimal_footer(),
        compression="snappy",
        expect=ok("BYTE_STREAM_SPLIT without dictionary, but snappy.", ["R3"]),
        out=out,
    )
    rows = minimal_rows()
    for r in rows:
        r["bandpass_fwhm"] = [5.0] * len(r["wavelengths"])
    w(
        "r3_bandpass_no_bss.parquet",
        rows,
        minimal_footer(extensions=["bandpass"]),
        bss=["wavelengths", "values"],
        expect=ok("bandpass_fwhm without BYTE_STREAM_SPLIT.", ["R3"]),
        out=out,
    )


def build_round3_indexes(out: Path) -> None:
    minimal = ["valid_minimal.parquet"]
    base = index_rows(out, minimal)
    meta = index_footer(out, minimal)

    stray = replace_column(base.slice(0, 1), "id", [f"{MIN}:zz_elsewhere"])
    stray = replace_column(stray, "file", ["elsewhere.parquet"])
    write_index(
        "index_unlisted_file.parquet",
        pa.concat_tables([base, stray]),
        meta,
        bad("One row points at elsewhere.parquet, which the footer does not list.", ["I1"]),
        out,
    )

    twice = copy.deepcopy(meta)
    twice["collections"].append(copy.deepcopy(twice["collections"][0]))
    twice["collections"][-1]["file"] = "./valid_minimal.parquet"
    write_index(
        "index_file_listed_twice.parquet",
        base,
        twice,
        bad("valid_minimal is listed as both `valid_minimal.parquet` and `./…`.", ["I1"]),
        out,
    )

    both = ["valid_minimal.parquet", "valid_minimal_subset.parquet"]
    write_index(
        "index_duplicate_id_across_files.parquet",
        index_rows(out, both),
        index_footer(out, both),
        bad("conformance_minimal:a1 is in two listed files.", ["I1"]),
        out,
    )

    for name, reference, description in (
        (
            "index_absolute_path.parquet",
            "/srv/openspeclib/valid_minimal.parquet",
            "Absolute local path in `file`.",
        ),
        ("index_file_url.parquet", "file:///srv/valid_minimal.parquet", "A file: URL."),
    ):
        edited = copy.deepcopy(meta)
        edited["collections"][0]["file"] = reference
        rows = replace_column(base, "file", [reference] * base.num_rows)
        write_index(name, rows, edited, bad(description, ["I1"]), out)

    full = index_rows(out, ["valid_minimal.parquet", "valid_full.parquet"])
    custom = full["x_custom"].to_pylist()
    custom = [None if v is None else v + 100 for v in custom]
    write_index(
        "index_x_column_differs.parquet",
        replace_column(full, "x_custom", custom),
        index_footer(out, ["valid_minimal.parquet", "valid_full.parquet"]),
        ok("x_custom differs from the collection file: x_ columns are not compared."),
        out,
    )
    position = base.column_names.index("dataset_id")
    write_index(
        "index_null_typed_column.parquet",
        base.set_column(position, "dataset_id", pa.nulls(base.num_rows, pa.null())),
        meta,
        ok("All-null dataset_id stored with the Arrow null type in the index."),
        out,
    )

    table = index_rows(out, ["valid_minimal.parquet", "valid_full.parquet"])
    table = table.replace_schema_metadata(
        {
            b"openspeclib_index": json.dumps(
                index_footer(out, ["valid_minimal.parquet", "valid_full.parquet"])
            ).encode(),
            b"openspeclib": json.dumps(FOOTERS["valid_minimal.parquet"]).encode(),
        }
    )
    pq.write_table(table, out / "index_both_keys.parquet", compression="zstd")
    EXPECTED["index_both_keys.parquet"] = {
        **bad("Index footer plus an `openspeclib` key; checked with check_index.", ["I1"]),
        "check": "index",
    }


# ---------------------------------------------------------------------------
# Round 4: extra objects, string-list nulls, forward compatibility
# ---------------------------------------------------------------------------


def build_round4(out: Path) -> None:
    w = write_collection

    # -- F4: extra must be a JSON object ----------------------------------------
    for name, value, expect in (
        ("valid_extra_object.parquet", '{"masked_bands": [3, 4]}', ok("extra is a JSON object.")),
        ("f4_extra_invalid_json.parquet", "{masked: 3", bad("extra is not valid JSON.", ["F4"])),
        ("f4_extra_array.parquet", "[1, 2]", bad("extra is a JSON array.", ["F4"])),
        ("f4_extra_string.parquet", '"masked"', bad("extra is a JSON string.", ["F4"])),
    ):
        rows = minimal_rows()
        rows[0]["extra"] = value
        w(
            name,
            rows,
            minimal_footer(),
            fields=fields_for([], [("extra", pa.string())]),
            expect=expect,
            out=out,
        )

    # -- F2: no null elements in list<string>; date64 equals date32 -------------
    rows = minimal_rows()
    for r in rows:
        r["material_keywords"] = ["olivine"]
    rows[1]["material_keywords"] = ["quartz", None]
    w(
        "f2_null_keyword_element.parquet",
        rows,
        minimal_footer(extensions=["material"]),
        fields=fields_for([], [("material_keywords", pa.list_(pa.string()))]),
        expect=bad("material_keywords holds a null element.", ["F2"]),
        out=out,
    )
    import datetime as dt

    rows = minimal_rows()
    for r in rows:
        r["sample_collection_date"] = dt.date(2019, 5, 17)
    w(
        "valid_date64.parquet",
        rows,
        minimal_footer(extensions=["sample"]),
        fields=fields_for([], [("sample_collection_date", pa.date64())]),
        expect=ok("sample_collection_date stored with the date64 hint: equals date32."),
        out=out,
    )

    # -- F1: dataset subset without inclusion_notes ---------------------------
    rows = minimal_rows()
    for r in rows:
        r["dataset_id"] = "lab_a"
    w(
        "f1_dataset_subset_without_notes.parquet",
        rows,
        minimal_footer(datasets={"lab_a": {"title": "Lab A", "inclusion": "subset"}}),
        expect=bad("datasets.lab_a.inclusion is subset without inclusion_notes.", ["F1"]),
        out=out,
    )

    # -- Section 11: a newer minor version --------------------------------------
    def future(version: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = minimal_rows()
        rows[0]["geometry"] = "directional_conical"
        for i, r in enumerate(rows):
            r["spectral_index"] = 0.5 + i
        meta = minimal_footer(extensions=["spectral_indices"]) | {
            "format_version": version,
            "provenance": {"pipeline": "corpus"},
        }
        return rows, meta

    rows, meta = future("1.1.0")
    w(
        "valid_future_minor.parquet",
        rows,
        meta,
        fields=fields_for([], [("spectral_index", pa.float64())]),
        expect=ok(
            "format_version 1.1.0 with a new geometry value, a new column, a new extension "
            "and a new footer member: unknown to a 1.0 checker, so warnings, not errors.",
            ["F1", "F3", "F7"],
        ),
        out=out,
    )
    rows, meta = future("1.0.0")
    w(
        "f1_f3_f7_unknowns_in_1_0.parquet",
        rows,
        meta,
        fields=fields_for([], [("spectral_index", pa.float64())]),
        expect=bad(
            "The same file declaring 1.0.0: the unknown member, extension, column and "
            "geometry are errors.",
            ["F1", "F3", "F7"],
        ),
        out=out,
    )


# ---------------------------------------------------------------------------
# Round 5: final review
# ---------------------------------------------------------------------------


def corrupt_page(column: str) -> Callable[[Path], None]:
    """Return a patch that overwrites the first data page header of a column."""

    def patch(path: Path) -> None:
        group = pq.ParquetFile(path).metadata.row_group(0)
        chunk = next(
            group.column(i)
            for i in range(group.num_columns)
            if group.column(i).path_in_schema.split(".")[0] == column
        )
        data = bytearray(path.read_bytes())
        data[chunk.data_page_offset : chunk.data_page_offset + 8] = b"\xff" * 8
        path.write_bytes(bytes(data))

    return patch


def write_invalid_utf8(
    name: str, column: str, out: Path, expect: dict[str, Any], default: str = "ok"
) -> None:
    """Write valid_minimal with invalid UTF-8 bytes in one string column of row 0.

    ``x_`` columns and ``extra`` are added, holding ``default`` on the other rows.
    """
    added = column.startswith("x_") or column == "extra"
    fields = fields_for([], [(column, pa.string())] if added else [])
    rows = minimal_rows()
    for r in rows:
        r.setdefault(column, default)
    table = pa.Table.from_pylist(
        [{n: r.get(n) for n, _ in fields} for r in sorted(rows, key=lambda r: r["id"])],
        schema=pa.schema([pa.field(n, t) for n, t in fields]),
    )
    raw = [v.encode() for v in table[column].to_pylist()]
    raw[0] = b"Sample \xff\xfe"
    position = table.column_names.index(column)
    table = table.set_column(
        position, column, pa.array(raw, pa.binary()).view(pa.string())
    ).replace_schema_metadata({b"openspeclib": json.dumps(minimal_footer()).encode()})
    pq.write_table(
        table,
        out / name,
        compression="zstd",
        use_dictionary=False,
        use_byte_stream_split=["wavelengths.list.element", "values.list.element"],
    )
    FOOTERS[name] = minimal_footer()
    EXPECTED[name] = expect


REST = ["a2", "a3"]


def build_round5(out: Path) -> None:
    w = write_collection

    w(
        "valid_minimal_rest.parquet",
        [r for r in minimal_rows() if r["id"].split(":")[1] in REST],
        minimal_footer(inclusion="subset", inclusion_notes="All but the first spectrum."),
        expect=ok("The other two rows of valid_minimal (same collection id as the subset)."),
        out=out,
    )
    write_invalid_utf8(
        "f2_invalid_utf8_name.parquet",
        "name",
        out,
        bad("`name` holds bytes that are not UTF-8 (F2 for a core column).", ["F2"]),
    )
    write_invalid_utf8(
        "f3_invalid_utf8_x_column.parquet",
        "x_label",
        out,
        bad("An x_ string column holds bytes that are not UTF-8 (F3 for x_ columns).", ["F3"]),
    )
    rows = minimal_rows()
    for r in rows:
        r["quality_notes"] = "checked"
    w(
        "f1_corrupt_extension_page.parquet",
        rows,
        minimal_footer(extensions=["quality"]),
        patch=corrupt_page("quality_notes"),
        expect=bad("A corrupt page in an extension column: every column is decoded (F1).", ["F1"]),
        out=out,
    )
    w(
        "f1_doi_trailing_newline.parquet",
        minimal_rows(),
        minimal_footer(doi="10.5555/conformance\n"),
        expect=bad("collection.doi ends with a newline: patterns match the whole value.", ["F1"]),
        out=out,
    )
    w(
        "f1_created_bad_offset.parquet",
        minimal_rows(),
        minimal_footer() | {"created": "2026-10-29T12:00:00+12:60"},
        expect=bad("`created` has the offset +12:60.", ["F1"]),
        out=out,
    )
    w(
        "valid_leap_second.parquet",
        minimal_rows(),
        minimal_footer() | {"created": "2016-12-31T23:59:60Z"},
        expect=ok("`created` is the leap second 2016-12-31T23:59:60Z."),
        out=out,
    )
    rows = minimal_rows()
    for r in rows:
        r["dataset_id"] = "lab_a"
    w(
        "f12_spectra_included_float.parquet",
        rows,
        minimal_footer(datasets={"lab_a": {"title": "Lab A", "spectra_included": 99.0}}),
        expect=bad("spectra_included 99.0 (a JSON Schema integer) for 3 rows.", ["F12"]),
        out=out,
    )
    for name, value, description in (
        ("f11_with_noassertion.parquet", "MIT WITH NOASSERTION", "WITH NOASSERTION."),
        ("f11_lowercase_licenseref.parquet", "licenseref-lab", "Lower-case licenseref- prefix."),
    ):
        rows = minimal_rows()
        rows[0]["license"] = value
        w(name, rows, minimal_footer(), expect=bad(description, ["F11"]), out=out)


def build_round5_indexes(out: Path) -> None:
    files = ["valid_null_dataset_id.parquet"]
    table = index_rows(out, files)
    position = table.column_names.index("dataset_id")
    table = table.set_column(
        position, "dataset_id", pa.nulls(table.num_rows, pa.null()).cast(pa.string())
    )
    write_index(
        "index_over_null_typed_collection.parquet",
        table,
        index_footer(out, files),
        ok("The collection stores dataset_id with the null type; the index as string."),
        out,
    )
    files = ["valid_minimal_subset.parquet", "valid_minimal_rest.parquet"]
    write_index(
        "index_shared_collection_id.parquet",
        index_rows(out, files),
        index_footer(out, files),
        bad("Two listed files share the collection id conformance_minimal.", ["I1"]),
        out,
    )
    files = ["valid_minimal.parquet"]
    table = index_rows(out, files)
    table = table.append_column("x_meta", pa.array([{"a": 1}] * table.num_rows))
    write_index(
        "index_nested_x_column.parquet",
        table,
        index_footer(out, files),
        bad("An x_ struct column in the index: x_ columns must be flat.", ["I1"]),
        out,
    )


# ---------------------------------------------------------------------------
# Round 6: release edge cases
# ---------------------------------------------------------------------------


def corrupt_column_name(old: bytes, new: bytes) -> Callable[[Path], None]:
    """Return a patch that overwrites a column name in the Parquet footer (same length)."""

    def patch(path: Path) -> None:
        assert len(old) == len(new)
        data = bytearray(path.read_bytes())
        start, end = footer_bounds(path)
        data[start:end] = bytes(data[start:end]).replace(old, new)
        path.write_bytes(bytes(data))

    return patch


def build_round6(out: Path) -> None:
    w = write_collection

    rows = minimal_rows()
    for r in rows:
        r["x_zz"] = 1
    w(
        "f1_invalid_utf8_column_name.parquet",
        rows,
        minimal_footer(),
        fields=fields_for([], [("x_zz", pa.int64())]),
        store_schema=False,
        patch=corrupt_column_name(b"x_zz", b"x_\xff\xfe"),
        expect=bad(
            "A column name is not UTF-8: the schema cannot be read, so F1 (not valid "
            "Parquet); every other rule is skipped.",
            ["F1"],
        ),
        out=out,
    )
    write_invalid_utf8(
        "f4_invalid_utf8_extra.parquet",
        "extra",
        out,
        bad("`extra` holds bytes that are not UTF-8: one F4 finding, not two.", ["F4"]),
        default="{}",
    )
    w(
        "f1_format_version_leading_zero.parquet",
        minimal_rows(),
        minimal_footer() | {"format_version": "1.01.0"},
        expect=bad(
            "format_version 1.01.0: semantic versions have no leading zeros (stricter "
            "than the footer schema pattern).",
            ["F1"],
        ),
        out=out,
    )
    rows = minimal_rows()
    rows[0]["license"] = "noassertion"
    w(
        "f11_lowercase_noassertion.parquet",
        rows,
        minimal_footer(),
        expect=bad("`noassertion`: the SPDX special value is case-sensitive.", ["F11"]),
        out=out,
    )
    rows = minimal_rows()
    for i, r in enumerate(rows):
        r["spectral_meta"] = {"band": i}
    w(
        "f3_future_minor_nested_column.parquet",
        rows,
        minimal_footer() | {"format_version": "1.1.0"},
        fields=fields_for([], [("spectral_meta", pa.struct([("band", pa.int64())]))]),
        expect=bad(
            "format_version 1.1.0 with an unknown struct column: unknown columns are "
            "warnings, but every column must be flat (F3).",
            ["F3"],
        ),
        out=out,
    )


def build_round6_indexes(out: Path) -> None:
    future = ["valid_future_minor.parquet"]
    rows = index_rows(out, future)
    write_index(
        "index_over_future_minor.parquet",
        rows.drop_columns(["spectral_index"]),
        index_footer(out, future),
        ok(
            "A 1.0.0 index listing a 1.1.0 collection: the embedded metadata is judged by "
            "its own format_version, so its unknown extension and member are warnings.",
            ["I1"],
        ),
        out,
    )
    write_index(
        "index_over_future_minor_with_column.parquet",
        rows,
        index_footer(out, future),
        ok(
            "As index_over_future_minor, also carrying the collection's spectral_index "
            "column (unknown in 1.0.0): a warning, and its values are compared.",
            ["I1"],
        ),
        out,
    )
    both = index_rows(out, ["valid_full.parquet", "valid_future_minor.parquet"])
    write_index(
        "index_unknown_column_not_from_future_file.parquet",
        both.append_column("notes", pa.array(["x"] * both.num_rows, pa.string())),
        index_footer(out, ["valid_full.parquet", "valid_future_minor.parquet"]),
        bad(
            "A 1.0.0 index lists a 1.1.0 collection but has a column `notes` that no "
            "listed file holds: an error (the 1.1.0 file excuses only its own columns).",
            ["I1"],
        ),
        out,
    )

    minimal = ["valid_minimal.parquet"]
    base = index_rows(out, minimal)
    meta = index_footer(out, minimal) | {"format_version": "1.1.0", "provenance": "corpus"}
    write_index(
        "index_future_minor.parquet",
        base.append_column("spectral_summary", pa.array([0.5] * base.num_rows)),
        meta,
        ok(
            "An index declaring 1.1.0 with its own unknown column and footer member: "
            "warnings (section 11).",
            ["I1"],
        ),
        out,
    )
    write_index(
        "index_future_minor_nested_column.parquet",
        base.append_column("spectral_summary", pa.array([{"band": 1}] * base.num_rows)),
        meta,
        bad(
            "An index declaring 1.1.0 with an unknown struct column: every column must " "be flat.",
            ["I1"],
        ),
        out,
    )


def duplicate_column(name: str) -> Callable[[Path], None]:
    """Return a patch that appends a second copy of column ``name``."""

    def patch(path: Path) -> None:
        table = pq.read_table(path)
        position = table.column_names.index(name)
        table = table.append_column(table.field(position), table.column(position))
        split = [f"{n}.list.element" for n in PER_BAND if n in table.column_names]
        strings = [f.name for f in table.schema if pa.types.is_string(f.type)]
        pq.write_table(
            table,
            path,
            compression="zstd",
            use_dictionary=strings,
            use_byte_stream_split=split,
        )

    return patch


def build_round7(out: Path) -> None:
    rows = minimal_rows()
    for r in rows:
        r["x_note"] = 1
    write_collection(
        "f3_duplicate_x_column.parquet",
        rows,
        minimal_footer(),
        fields=fields_for([], [("x_note", pa.int32())]),
        patch=duplicate_column("x_note"),
        expect=bad("The x_ column `x_note` appears twice.", ["F3"]),
        out=out,
    )

    meta = minimal_footer() | {"format_version": "1.1.0"}
    write_collection(
        "f1_footer_nan.parquet",
        minimal_rows(),
        meta,
        raw_footer=(json.dumps(meta)[:-1] + ',"x_future":NaN}').encode(),
        expect=bad(
            "A 1.1.0 footer with an unknown member whose value is NaN: NaN is not valid "
            "JSON, so the footer fails F1 even where unknown members are only warnings.",
            ["F1"],
        ),
        out=out,
    )


def build_round7_indexes(out: Path) -> None:
    future = ["valid_future_minor.parquet"]
    rows = index_rows(out, future)
    position = rows.column_names.index("spectral_index")
    write_index(
        "index_duplicate_future_column.parquet",
        rows.append_column(rows.field(position), rows.column(position)),
        index_footer(out, future),
        bad(
            "A 1.0.0 index over a 1.1.0 collection carrying its spectral_index column "
            "twice: a column may appear only once.",
            ["I1"],
        ),
        out,
    )


def build(fixtures_dir: Path, expected_path: Path) -> dict[str, dict[str, Any]]:
    """Write every fixture and expected.json; return the expectations."""
    EXPECTED.clear()
    FOOTERS.clear()
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    for stale in fixtures_dir.glob("*.parquet"):
        stale.unlink()
    build_collections(fixtures_dir)
    build_round3(fixtures_dir)
    build_round4(fixtures_dir)
    build_round5(fixtures_dir)
    build_round6(fixtures_dir)
    build_round7(fixtures_dir)
    build_indexes(fixtures_dir)
    build_round3_indexes(fixtures_dir)
    build_round5_indexes(fixtures_dir)
    build_round6_indexes(fixtures_dir)
    build_round7_indexes(fixtures_dir)
    expected = {name: EXPECTED[name] for name in sorted(EXPECTED)}
    expected_path.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "fixtures")
    parser.add_argument("--expected", type=Path, default=HERE / "expected.json")
    args = parser.parse_args()
    expected = build(args.out, args.expected)
    print(f"Wrote {len(expected)} fixtures to {args.out}")


if __name__ == "__main__":
    main()
