#!/usr/bin/env python3
"""Check that the code agrees with the normative OpenSpecLib Format v1 schema files.

The Format v1 machine-readable specification in ``schemas/format-v1/``
(``columns.json``, ``vocabularies.json``, ``footer.schema.json``,
``rules.json``) is maintained by hand and is normative, so nothing is
generated from the code any more: the v0 outputs (``catalog.schema.json``,
``spectrum.schema.json``, ``licenses.schema.json``,
``library.arrow.schema.json``) are obsolete. This script instead verifies that
the writer's column definitions (``openspeclib.storage``) and the model
vocabularies (``openspeclib.models``) match those files, and exits non-zero on
any drift. It never writes to ``schemas/format-v1/``.

Run from the repository root:
    python scripts/generate_schemas.py
"""

import json
import sys
from pathlib import Path

from openspeclib import models, storage

SPEC_DIR = Path(__file__).resolve().parent.parent / "schemas" / "format-v1"

_TYPE_NAMES = {
    "string": "string",
    "double": "float64",
    "float": "float32",
    "int32": "int32",
    "date32[day]": "date32",
}


def _type_name(arrow_type) -> str:
    """Render an Arrow type the way columns.json names it."""
    text = str(arrow_type)
    if text.startswith("list<"):
        return f"list<{_type_name(arrow_type.value_type)}>"
    return _TYPE_NAMES.get(text, text)


def check_columns() -> list[str]:
    spec = json.loads((SPEC_DIR / "columns.json").read_text())["columns"]
    errors = []
    spec_by_name = {c["name"]: c for c in spec}
    code_by_name = storage.COLUMNS
    for name in sorted(set(spec_by_name) ^ set(code_by_name)):
        where = "columns.json" if name in spec_by_name else "openspeclib.storage"
        errors.append(f"column {name!r} only in {where}")
    for name, col in spec_by_name.items():
        code = code_by_name.get(name)
        if code is None:
            continue
        if _type_name(code.type) != col["type"]:
            errors.append(f"{name}: type {_type_name(code.type)} != {col['type']}")
        if code.nullable != col["nullable"]:
            errors.append(f"{name}: nullable {code.nullable} != {col['nullable']}")
        if code.extension != col["extension"]:
            errors.append(f"{name}: extension {code.extension!r} != {col['extension']!r}")
    spec_order = [c["name"] for c in spec if c["extension"] not in ("index",)]
    code_order = [c.name for c in storage.COLLECTION_COLUMNS]
    if spec_order != code_order:
        errors.append("collection column order differs from columns.json")
    return errors


def check_vocabularies() -> list[str]:
    vocab = json.loads((SPEC_DIR / "vocabularies.json").read_text())
    pairs = {
        "quantity": models.Quantity,
        "geometry": models.Geometry,
        "wavelength_unit": models.WavelengthUnit,
        "material_category": models.MaterialCategory,
        "inclusion": models.Inclusion,
    }
    errors = []
    for key, enum in pairs.items():
        spec_values = set(vocab[key])
        code_values = {m.value for m in enum}
        if spec_values != code_values:
            errors.append(
                f"vocabulary {key}: spec {sorted(spec_values)} != code {sorted(code_values)}"
            )
    if list(vocab["extension"]) != list(storage.EXTENSIONS):
        errors.append(f"extensions: spec {list(vocab['extension'])} != code {storage.EXTENSIONS}")
    return errors


def main() -> int:
    errors = check_columns() + check_vocabularies()
    for error in errors:
        print(f"DRIFT: {error}")
    if errors:
        return 1
    print(f"Code matches {SPEC_DIR.relative_to(SPEC_DIR.parent.parent)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
