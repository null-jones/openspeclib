"""Converters between the OpenSpecLib Format v1 and common spectral-library formats.

Import (``openspeclib convert``): ENVI spectral libraries (``.sli`` +
``.hdr``) and CSV tables (wide, EcoSIS-style, or long) become a conformant
Format v1 collection file. Export (``openspeclib export``): spectra of a
collection file become an ENVI spectral library or a CSV table, with their
licences and citations alongside.

>>> from pathlib import Path
>>> from openspeclib.convert import ImportDefaults, convert_file
>>> result = convert_file(
...     Path("library.hdr"),
...     Path("my_lab.parquet"),
...     collection={"id": "my_lab", "title": "My lab", "license": "CC-BY-4.0",
...                 "citation": "My Lab (2026). Spectral library."},
...     defaults=ImportDefaults(quantity="reflectance"),
... )  # doctest: +SKIP
>>> result.conformance.conformant  # doctest: +SKIP
True
"""

from openspeclib.convert.common import (
    DEFAULT_ALIASES,
    ConversionError,
    FieldMapper,
    ImportDefaults,
    ImportReport,
    SourceSpectrum,
)
from openspeclib.convert.envi import (
    EnviLibrary,
    parse_envi_header,
    read_envi_library,
    write_envi_library,
)
from openspeclib.convert.exporter import (
    EXPORT_FORMATS,
    ExportResult,
    export_collection,
    parse_resample_spec,
    parse_search_terms,
)
from openspeclib.convert.importer import (
    SOURCE_FORMATS,
    ConvertResult,
    NonConformantError,
    convert_file,
)

__all__ = [
    "DEFAULT_ALIASES",
    "EXPORT_FORMATS",
    "SOURCE_FORMATS",
    "ConversionError",
    "ConvertResult",
    "EnviLibrary",
    "ExportResult",
    "FieldMapper",
    "NonConformantError",
    "ImportDefaults",
    "ImportReport",
    "SourceSpectrum",
    "convert_file",
    "export_collection",
    "parse_envi_header",
    "parse_resample_spec",
    "parse_search_terms",
    "read_envi_library",
    "write_envi_library",
]
