#!/usr/bin/env python3
"""Generate JSON Schema files from Pydantic models.

Run from the repository root:
    python scripts/generate_schemas.py
"""

import json
from pathlib import Path

from openspeclib.models import CatalogFile, LibraryChunkFile, SpectrumRecord

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def write_schema(model_cls: type, filename: str) -> None:
    schema = model_cls.model_json_schema()
    path = SCHEMAS_DIR / filename
    path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"  {path.relative_to(SCHEMAS_DIR.parent)}")


def main() -> None:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    print("Generating JSON schemas from Pydantic models:")
    write_schema(SpectrumRecord, "spectrum.schema.json")
    write_schema(CatalogFile, "catalog.schema.json")
    write_schema(LibraryChunkFile, "library.schema.json")
    print("Done.")


if __name__ == "__main__":
    main()
