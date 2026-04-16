#!/usr/bin/env python3
"""Generate schema files from Pydantic models and the canonical Arrow schema.

Catalog and spectrum-record schemas are emitted as JSON Schema (Draft 2020-12)
derived from the Pydantic models. Library chunk files are stored as Parquet,
so the chunk-level schema is emitted as a JSON dump of the canonical Arrow
schema (column names, types, nullability, and footer metadata keys) — useful
as documentation for downstream consumers without forcing them to open a
Parquet file.

Run from the repository root:
    python scripts/generate_schemas.py
"""

import json
from pathlib import Path
from typing import Any

import pyarrow as pa

from openspeclib.models import CatalogFile, LicensesFile, SpectrumRecord
from openspeclib.storage import (
    _META_SOURCE,
    _META_VERSION,
    ARROW_SCHEMA,
)

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def write_json_schema(model_cls: type, filename: str) -> None:
    schema = model_cls.model_json_schema()
    path = SCHEMAS_DIR / filename
    path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"  {path.relative_to(SCHEMAS_DIR.parent)}")


def _arrow_type_to_dict(arrow_type: pa.DataType) -> dict[str, Any]:
    """Render a pyarrow DataType as a JSON-serialisable dict."""
    if pa.types.is_list(arrow_type):
        return {
            "kind": "list",
            "value_type": _arrow_type_to_dict(arrow_type.value_type),
        }
    return {"kind": "primitive", "type": str(arrow_type)}


def write_arrow_schema(filename: str) -> None:
    """Dump ARROW_SCHEMA as JSON for chunk-file documentation."""
    columns = [
        {
            "name": field.name,
            "type": _arrow_type_to_dict(field.type),
            "nullable": field.nullable,
        }
        for field in ARROW_SCHEMA
    ]
    payload = {
        "format": "parquet",
        "compression": "zstd",
        "footer_metadata_keys": [
            _META_VERSION.decode("utf-8"),
            _META_SOURCE.decode("utf-8"),
        ],
        "columns": columns,
    }
    path = SCHEMAS_DIR / filename
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  {path.relative_to(SCHEMAS_DIR.parent)}")


def main() -> None:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    print("Generating schemas:")
    write_json_schema(SpectrumRecord, "spectrum.schema.json")
    write_json_schema(CatalogFile, "catalog.schema.json")
    write_json_schema(LicensesFile, "licenses.schema.json")
    write_arrow_schema("library.arrow.schema.json")
    print("Done.")


if __name__ == "__main__":
    main()
