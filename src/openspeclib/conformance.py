"""Reference conformance checker for the OpenSpecLib Format v1.

Checks a collection file against the must-rules F1–F13 and the should-rules
R1–R5 of ``docs/spec/format-v1.md`` section 8, and an index file against rule
I1 together with every collection file it lists. Rule ids, levels and titles
come from ``schemas/format-v1/rules.json``; columns, vocabularies and the
footer schema come from the sibling JSON files, so this module holds no second
copy of the specification text.

Row-level checks are vectorised with :mod:`pyarrow.compute` over record
batches. List columns are flattened once per batch and per-row aggregates are
taken as differences of cumulative sums at the list offsets, so no Python
object is created per row or per element.

Interpretation notes:

* Types follow the Parquet logical type (spec section 4): Arrow-only hints
  (dictionary encoding, ``fixed_size_list``, large and view variants,
  ``date64``) are equivalent to the plain types, list child names and
  nullability are ignored, and an all-null nullable column may use the Arrow
  ``null`` type. Such columns are cast to the plain type before value checks.
* F10 compares the stored extent with the extent of the finite wavelengths;
  null, NaN and infinite wavelengths are F8's. An infinite value never equals
  a finite one.
* F11 does not check empty or whitespace-only licences (F2 reports them in the
  ``license`` column, the footer schema in the footer).
* R1 statistics are located by column path (``id``), never by position.
  Files without rows are exempt from R1, and row groups without rows from R3.
* A file pyarrow cannot open (including a schema with a column name that is
  not UTF-8), or whose data pages cannot be decoded, fails F1 (collection) or
  I1 (index); rules that need the unreadable data are skipped.
* ``format_version`` is a semantic version: minor and patch numbers with a
  leading zero (``1.01.0``) fail F1 / I1. The footer schema's pattern says
  the same; it is also checked here so a footer that fails the schema for
  another reason is never treated as a newer minor version.
* Section 11 (a newer minor version) is decided per footer: an index's own
  ``format_version`` governs its footer members and columns, and each
  embedded ``collections[].metadata`` object is judged by its own
  ``format_version``, as the collection file itself is. A column unknown to
  this version is a warning when the index, or a listed collection file that
  holds the column, declares a newer minor version. Unknown columns must still
  be flat (F3 / I1).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlparse

import jsonschema
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

__all__ = [
    "COLLECTION_FOOTER_KEY",
    "INDEX_FOOTER_KEY",
    "ColumnSpec",
    "ConformanceError",
    "ConformanceReport",
    "Finding",
    "QualityBounds",
    "RuleResult",
    "RuleSpec",
    "check_collection",
    "check_index",
    "check_path",
    "check_spdx_expression",
    "is_index_file",
    "is_newer_minor",
    "load_qa_bounds",
    "load_qa_wavelength_nm",
    "load_rules",
    "spec_arrow_type",
    "spec_dir",
    "type_matches",
]

COLLECTION_FOOTER_KEY = "openspeclib"
INDEX_FOOTER_KEY = "openspeclib_index"
SUPPORTED_MAJOR_VERSION = 1

MAX_EXAMPLES = 5
"""Default number of example rows or details kept per rule (``None`` keeps all)."""

RELATIVE_TOLERANCE = 1e-6
"""F10: relative tolerance for ``wavelength_min_nm`` / ``wavelength_max_nm``."""

SENTINEL_MAGNITUDE = 1e10
"""F9: finite values with a larger magnitude are treated as sentinels."""

MAX_ROW_GROUP_ROWS = 250
"""R2: row groups with more rows than this produce a warning."""

UNKNOWN_GEOMETRY_FRACTION = 0.5
"""R5: warn when more than this fraction of rows has ``geometry = 'unknown'``."""

BATCH_SIZE = 2048
"""Rows per record batch while scanning a collection file."""

PER_BAND_COLUMNS = ("wavelengths", "values", "bandpass_fwhm", "values_stddev")
"""Array columns that index files omit (spec section 7)."""

_LOCAL_ID_PATTERN = "[A-Za-z0-9._~-]{1,200}"
_SPDX_IDSTRING = re.compile(r"[A-Za-z0-9.\-]+")
_SPDX_OPERATORS = frozenset({"AND", "OR", "WITH"})
_LICENSE_REF_PREFIX = "LicenseRef-"
_DOCUMENT_REF_PREFIX = "DocumentRef-"
_NOASSERTION = "NOASSERTION"
_DICTIONARY_ENCODINGS = frozenset({"PLAIN_DICTIONARY", "RLE_DICTIONARY"})
_URL_SCHEMES = frozenset({"http", "https", "s3", "gs", "gcs", "az", "abfs", "hf"})

_PASSED = "passed"
_FAILED = "failed"
_WARNED = "warned"
_SKIPPED = "skipped"


class ConformanceError(Exception):
    """Raised when a file cannot be checked at all (e.g. a URL without fsspec)."""


class _NotParquetError(Exception):
    """Raised when pyarrow cannot open a file as Parquet."""


# ---------------------------------------------------------------------------
# Specification files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    """One column definition from ``columns.json``.

    Attributes:
        name: Column name.
        type: Arrow type name as written in the spec (e.g. ``list<float64>``).
        nullable: Whether the column may hold nulls.
        extension: ``None`` for core columns, ``"*"`` for columns allowed in
            any file, ``"index"`` for index-only columns, otherwise the
            extension that declares the column.
        non_empty: Whether the column must not hold empty strings (F2).
    """

    name: str
    type: str
    nullable: bool
    extension: str | None
    non_empty: bool = False

    @property
    def index_only(self) -> bool:
        """Whether the column belongs only to index files."""
        return self.extension == "index"

    @property
    def is_extension(self) -> bool:
        """Whether the column is an extension column or ``extra`` (rule F4)."""
        return self.extension is not None and not self.index_only


@dataclass(frozen=True)
class RuleSpec:
    """One rule definition from ``rules.json``.

    Attributes:
        id: Stable rule identifier, e.g. ``F5``.
        level: ``must``, ``should`` or ``qa``.
        applies_to: ``collection`` or ``index``.
        title: Short rule title.
        text: Normative rule text.
    """

    id: str
    level: str
    applies_to: str
    title: str
    text: str


@dataclass(frozen=True)
class QualityBounds:
    """Quality-profile value bounds for one quantity (``rules.json`` ``qa_bounds``).

    Attributes:
        hard_min: Lower hard bound (Q1, Q2).
        hard_max: Upper hard bound (Q1, Q2).
        warn_min: Lower warning bound (Q3).
        warn_max: Upper warning bound (Q3).
    """

    hard_min: float
    hard_max: float
    warn_min: float
    warn_max: float


@dataclass(frozen=True)
class _Spec:
    """All machine-readable parts of the specification, loaded once."""

    columns: dict[str, ColumnSpec]
    vocabularies: dict[str, tuple[str, ...]]
    footer_schema: dict[str, Any]
    rules: dict[str, RuleSpec]
    qa_bounds: dict[str, QualityBounds]
    qa_wavelength_nm: tuple[float, float] | None
    format_version: str

    def rules_for(self, applies_to: str) -> list[RuleSpec]:
        """Return the must- and should-rules that apply to a file kind.

        Args:
            applies_to: ``collection`` or ``index``.

        Returns:
            Rules in ``rules.json`` order, excluding quality-profile rules.
        """
        return [
            r
            for r in self.rules.values()
            if r.applies_to == applies_to and r.level in ("must", "should")
        ]

    @property
    def core_columns(self) -> list[ColumnSpec]:
        """Core columns every collection file must contain."""
        return [c for c in self.columns.values() if c.extension is None]

    @property
    def index_only_columns(self) -> list[ColumnSpec]:
        """Columns that only index files contain."""
        return [c for c in self.columns.values() if c.index_only]


def spec_dir() -> Path:
    """Locate the ``schemas/format-v1`` directory.

    Wheel installs carry the files inside the package at
    ``openspeclib/_schemas/format-v1``; editable installs and source checkouts
    use the repository's ``schemas/format-v1``.

    Returns:
        The directory holding ``columns.json``, ``rules.json`` and friends.

    Raises:
        FileNotFoundError: If neither location holds the specification files.
    """
    candidates = [
        Path(str(resources.files("openspeclib"))) / "_schemas" / "format-v1",
        Path(__file__).resolve().parents[2] / "schemas" / "format-v1",
    ]
    for candidate in candidates:
        if (candidate / "rules.json").is_file():
            return candidate
    raise FileNotFoundError(
        "OpenSpecLib Format v1 schema files not found; looked in "
        + ", ".join(str(c) for c in candidates)
    )


def _read_json(path: Path) -> Any:
    """Read a UTF-8 JSON file.

    Args:
        path: File to read.

    Returns:
        The decoded JSON value.
    """
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _load_spec() -> _Spec:
    """Load and cache the specification files.

    Returns:
        The parsed specification.
    """
    directory = spec_dir()
    columns_doc = _read_json(directory / "columns.json")
    vocab_doc = _read_json(directory / "vocabularies.json")
    rules_doc = _read_json(directory / "rules.json")
    columns = {
        c["name"]: ColumnSpec(
            name=c["name"],
            type=c["type"],
            nullable=bool(c["nullable"]),
            extension=c.get("extension"),
            non_empty=bool(c.get("non_empty", False)),
        )
        for c in columns_doc["columns"]
    }
    vocabularies = {
        key: tuple(value) for key, value in vocab_doc.items() if isinstance(value, dict)
    }
    rules = {
        r["id"]: RuleSpec(
            id=r["id"],
            level=r["level"],
            applies_to=r["applies_to"],
            title=r["title"],
            text=r["text"],
        )
        for r in rules_doc["rules"]
    }
    qa_bounds = {
        quantity: QualityBounds(
            hard_min=float(b["hard"][0]),
            hard_max=float(b["hard"][1]),
            warn_min=float(b["warn"][0]),
            warn_max=float(b["warn"][1]),
        )
        for quantity, b in rules_doc.get("qa_bounds", {}).items()
    }
    return _Spec(
        columns=columns,
        vocabularies=vocabularies,
        footer_schema=_read_json(directory / "footer.schema.json"),
        rules=rules,
        qa_bounds=qa_bounds,
        format_version=str(rules_doc.get("format_version", "1.0.0")),
        qa_wavelength_nm=(
            (float(extent[0]), float(extent[1]))
            if (extent := rules_doc.get("qa_wavelength_nm")) is not None
            else None
        ),
    )


def load_rules() -> dict[str, RuleSpec]:
    """Return every rule defined in ``rules.json``, keyed by id.

    Returns:
        Mapping of rule id to :class:`RuleSpec`.
    """
    return dict(_load_spec().rules)


def load_qa_wavelength_nm() -> tuple[float, float]:
    """Return the plausible wavelength extent of rule Q4 (``rules.json`` ``qa_wavelength_nm``).

    Returns:
        ``(minimum, maximum)`` in nanometres.

    Raises:
        KeyError: If ``rules.json`` does not define ``qa_wavelength_nm``.
    """
    extent = _load_spec().qa_wavelength_nm
    if extent is None:
        raise KeyError("qa_wavelength_nm")
    return extent


def load_qa_bounds() -> dict[str, QualityBounds]:
    """Return the quality-profile bounds from ``rules.json`` ``qa_bounds``, by quantity.

    Returns:
        Mapping of quantity (e.g. ``reflectance``) to :class:`QualityBounds`.
    """
    return dict(_load_spec().qa_bounds)


@lru_cache(maxsize=4)
def _footer_validator(definition: str) -> Any:
    """Build a JSON Schema validator for one ``$defs`` entry of the footer schema.

    Validating against the definition directly (instead of the top-level
    ``oneOf``) gives error messages that point at the offending field.

    Args:
        definition: ``collectionFooter`` or ``indexFooter``.

    Returns:
        A ``jsonschema`` Draft 2020-12 validator.
    """
    schema = {k: v for k, v in _load_spec().footer_schema.items() if k != "oneOf"}
    schema["$ref"] = f"#/$defs/{definition}"
    return jsonschema.Draft202012Validator(_ecma_anchors(schema))


# ECMA-262 ``\s``: WhiteSpace and LineTerminator. Python's ``\s`` differs (it
# includes U+001C-U+001F and U+0085, and excludes U+FEFF).
_ECMA_SPACE = (
    "\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"
)


_SCHEMA_PATTERNS: dict[str, str] = {}
"""Rewritten pattern -> the schema's own pattern, for error messages."""


def _ecma_pattern(pattern: str) -> str:
    r"""Rewrite an ECMA-262 pattern so Python's ``re`` matches it the same way.

    Replaces a final ``$`` with ``\Z`` (in ECMA ``$`` matches only at the end
    of the string; Python's also matches before a trailing newline, so
    ``"x\n"`` would pass ``^x$``) and ``\s``/``\S`` with ECMA's whitespace
    set.

    Args:
        pattern: The schema pattern.

    Returns:
        The equivalent Python pattern.

    Raises:
        ValueError: If the pattern uses ``\S`` inside a character class.
    """
    out: list[str] = []
    in_class = False
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\" and i + 1 < len(pattern):
            escape = pattern[i + 1]
            if escape == "s":
                out.append(_ECMA_SPACE if in_class else f"[{_ECMA_SPACE}]")
            elif escape == "S":
                if in_class:
                    raise ValueError(f"unsupported \\S in a character class: {pattern}")
                out.append(f"[^{_ECMA_SPACE}]")
            else:
                out.append(pattern[i : i + 2])
            i += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        out.append(char)
        i += 1
    rewritten = "".join(out)
    if rewritten.endswith("$") and not rewritten.endswith("\\$"):
        rewritten = rewritten[:-1] + "\\Z"
    _SCHEMA_PATTERNS[rewritten] = pattern
    return rewritten


def _ecma_anchors(node: Any) -> Any:
    """Copy a JSON Schema with every ``pattern`` rewritten by :func:`_ecma_pattern`.

    JSON Schema patterns use ECMA-262 semantics; ``jsonschema`` evaluates them
    with Python's ``re``.

    Args:
        node: A schema (sub)tree.

    Returns:
        The rewritten copy.
    """
    if isinstance(node, list):
        return [_ecma_anchors(item) for item in node]
    if not isinstance(node, dict):
        return node
    copy = {key: _ecma_anchors(value) for key, value in node.items()}
    pattern = node.get("pattern")
    if isinstance(pattern, str):
        copy["pattern"] = _ecma_pattern(pattern)
    return copy


_FORWARD_KEYWORDS = frozenset({"additionalProperties", "enum"})
"""Schema keywords a newer 1.x minor version can legitimately trip (section 11)."""


def _split_schema_errors(
    document: Any, definition: str, forward_at: Callable[[Sequence[Any]], bool]
) -> tuple[list[str], list[str]]:
    """Validate a footer document, separating errors from section 11 unknowns.

    An error about an unknown member (``additionalProperties``) or value
    (``enum``, e.g. a new extension) is an *unknown* rather than an error when
    ``forward_at`` says the object holding it declares a newer minor version.

    Args:
        document: Decoded footer JSON.
        definition: ``collectionFooter`` or ``indexFooter``.
        forward_at: Maps the JSON path of an error to whether the footer that
            governs that path declares a newer minor version.

    Returns:
        ``(errors, unknowns)``: sorted descriptions of each.
    """
    errors = []
    unknowns = []
    for error in _footer_validator(definition).iter_errors(document):
        location = "/".join(str(p) for p in error.absolute_path) or "(root)"
        if error.validator in _FORWARD_KEYWORDS and forward_at(list(error.absolute_path)):
            unknowns.append(f"{location}: {error.message} (unknown in {_spec_version()})")
            continue
        message = error.message
        if error.validator == "pattern":
            # Show the schema's own pattern, not the rewrite used to evaluate it.
            original = _SCHEMA_PATTERNS.get(str(error.validator_value), error.validator_value)
            message = f"{error.instance!r} does not match {original!r}"
        errors.append(f"{location}: {message}")
    return sorted(errors), sorted(unknowns)


def _schema_errors(document: Any, definition: str, forward: bool = False) -> list[str]:
    """Validate a footer document and describe every error.

    Args:
        document: Decoded footer JSON.
        definition: ``collectionFooter`` or ``indexFooter``.
        forward: Leave out errors about unknown members and values
            (``additionalProperties``, ``enum``); see :func:`_schema_unknowns`.

    Returns:
        Sorted error descriptions, empty when the document is valid.
    """
    return _split_schema_errors(document, definition, lambda _path: forward)[0]


def _schema_unknowns(document: Any, definition: str) -> list[str]:
    """Describe the footer members and values the schema does not know.

    Used for files that declare a newer minor version: unknown members
    (``additionalProperties``) and vocabulary values (``enum``, e.g. a new
    extension) are warnings for them, not errors.

    Args:
        document: Decoded footer JSON.
        definition: ``collectionFooter`` or ``indexFooter``.

    Returns:
        Sorted descriptions.
    """
    return _split_schema_errors(document, definition, lambda _path: True)[1]


def _spec_version() -> str:
    """Return the format version this checker implements, e.g. ``1.0.0``."""
    return _load_spec().format_version


_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
"""A semantic version core (``MAJOR.MINOR.PATCH``, no leading zeros), ASCII digits only."""


def is_newer_minor(document: Any) -> bool:
    """Tell whether a footer declares a newer 1.x minor version than the checker's.

    Args:
        document: Decoded footer JSON.

    Returns:
        True when ``format_version`` is a well-formed semantic version
        ``1.N.x`` with N above the implemented minor version (spec section
        11). A malformed version (e.g. ``1.01.0``) is never newer: F1 / I1
        reject it.
    """
    version = document.get("format_version") if isinstance(document, dict) else None
    match = _SEMVER.fullmatch(version) if isinstance(version, str) else None
    implemented = _SEMVER.fullmatch(_spec_version())
    if match is None or implemented is None:
        return False
    return int(match.group(1)) == SUPPORTED_MAJOR_VERSION and int(match.group(2)) > int(
        implemented.group(2)
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One violation recorded for a rule.

    Attributes:
        row_id: The ``id`` of the row concerned, or ``None`` for file-level
            findings (and rows without a usable id).
        detail: Description of the violation.
    """

    row_id: str | None
    detail: str

    @property
    def text(self) -> str:
        """One-line form: ``"{row_id}: {detail}"`` or just the detail."""
        return f"{self.row_id}: {self.detail}" if self.row_id is not None else self.detail


@dataclass
class RuleResult:
    """Outcome of one rule for one file.

    Attributes:
        rule_id: Rule identifier from ``rules.json``.
        level: ``must`` or ``should``.
        title: Rule title from ``rules.json``.
        status: ``passed``; ``failed`` (must-rule violated); ``warned``
            (should-rule not met, or a must-rule met but with
            forward-compatibility warnings, spec section 11); or ``skipped``
            (could not be evaluated because a prerequisite failed).
        count: Number of violations (rows for row-level checks, items
            otherwise). Not capped by the example limit.
        findings: The recorded violations, capped at the ``max_examples``
            the check was run with.
        message: Optional note, e.g. why the rule was skipped.
        warnings: Forward-compatibility warnings: things a file declaring a
            newer 1.x minor version may contain that this checker does not
            know (spec section 11). They never fail a rule.
    """

    rule_id: str
    level: str
    title: str
    status: str
    count: int = 0
    findings: list[Finding] = field(default_factory=list)
    message: str | None = None
    warnings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether the rule was evaluated and met (a must-rule may carry warnings)."""
        return self.status == _PASSED or (self.status == _WARNED and self.level == "must")

    @property
    def examples(self) -> list[str]:
        """The recorded findings as one-line strings."""
        return [f.text for f in self.findings]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation.

        Returns:
            Dictionary with the rule id, level, title, status, count,
            examples and message.
        """
        return {
            "rule": self.rule_id,
            "level": self.level,
            "title": self.title,
            "status": self.status,
            "passed": self.passed,
            "count": self.count,
            "examples": self.examples,
            "warnings": [w.text for w in self.warnings],
            "message": self.message,
        }


@dataclass
class ConformanceReport:
    """Conformance result for one collection or index file.

    Attributes:
        location: Path or URL that was checked.
        kind: ``collection`` or ``index``.
        results: One :class:`RuleResult` per applicable rule, in
            ``rules.json`` order.
        num_rows: Number of rows in the file, if it could be read.
        format_version: ``format_version`` from the footer, if readable.
        collection_id: Footer ``collection.id`` of a collection file, if
            readable.
        collections: For index files, the reports of every listed collection
            file that could be opened.
    """

    location: str
    kind: str
    results: list[RuleResult]
    num_rows: int | None = None
    format_version: str | None = None
    collection_id: str | None = None
    collections: list[ConformanceReport] = field(default_factory=list)

    @property
    def conformant(self) -> bool:
        """Whether every must-rule passed (and, for an index, every collection conforms)."""
        own = all(r.passed for r in self.results if r.level == "must")
        return own and all(c.conformant for c in self.collections)

    @property
    def failed(self) -> list[str]:
        """Ids of must-rules that failed in this file (not its collections)."""
        return [r.rule_id for r in self.results if r.status == _FAILED]

    @property
    def warned(self) -> list[str]:
        """Ids of should-rules not met and must-rules with forward-compatibility warnings."""
        return [r.rule_id for r in self.results if r.status == _WARNED]

    @property
    def skipped(self) -> list[str]:
        """Ids of rules that could not be evaluated."""
        return [r.rule_id for r in self.results if r.status == _SKIPPED]

    def result(self, rule_id: str) -> RuleResult:
        """Return the result for one rule.

        Args:
            rule_id: Rule identifier, e.g. ``F8``.

        Returns:
            The matching :class:`RuleResult`.

        Raises:
            KeyError: If the rule does not apply to this kind of file.
        """
        for item in self.results:
            if item.rule_id == rule_id:
                return item
        raise KeyError(rule_id)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation.

        Returns:
            Dictionary with the location, kind, conformance verdict, failed and
            warned rule ids, per-rule results and nested collection reports.
        """
        return {
            "location": self.location,
            "kind": self.kind,
            "format_version": self.format_version,
            "collection_id": self.collection_id,
            "num_rows": self.num_rows,
            "conformant": self.conformant,
            "failed": self.failed,
            "warned": self.warned,
            "skipped": self.skipped,
            "rules": [r.to_dict() for r in self.results],
            "collections": [c.to_dict() for c in self.collections],
        }


class _Tally:
    """Mutable accumulator for one rule while a file is scanned."""

    def __init__(self, max_examples: int | None) -> None:
        self.max_examples = max_examples
        self.count = 0
        self.findings: list[Finding] = []
        self.warnings: list[Finding] = []
        self.notes: list[str] = []
        self.skip_reason: str | None = None

    def _room(self) -> int | None:
        """Return how many more findings may be kept (``None`` = unlimited)."""
        if self.max_examples is None:
            return None
        return max(self.max_examples - len(self.findings), 0)

    def warn(self, detail: str, row_id: str | None = None) -> None:
        """Record a forward-compatibility warning (spec section 11).

        Args:
            detail: Human-readable description.
            row_id: Row the warning concerns, if any.
        """
        if self.max_examples is None or len(self.warnings) < max(self.max_examples, 1):
            self.warnings.append(Finding(row_id, detail))

    def add(self, detail: str, count: int = 1, row_id: str | None = None) -> None:
        """Record a violation that is not tied to a row mask.

        Args:
            detail: Human-readable description, kept as a finding.
            count: Number of violations it represents.
            row_id: Row the violation concerns, if any.
        """
        self.count += count
        room = self._room()
        if room is None or room > 0:
            self.findings.append(Finding(row_id, detail))

    def flag(
        self,
        mask: pa.Array,
        label: _Labeler,
        reason: str | Callable[[int], str],
        warning: bool = False,
    ) -> None:
        """Record every row where ``mask`` is true.

        Args:
            mask: Boolean array over the rows of the current batch; nulls
                count as false.
            label: Maps a batch row index to the row's id.
            reason: Description, or a function of the row index returning one.
            warning: Record forward-compatibility warnings instead of
                violations.
        """
        mask = pc.fill_null(mask, False)
        hits = _count_true(mask)
        if not hits:
            return
        if warning:
            indices = pc.indices_nonzero(mask)
            if self.max_examples is not None:
                indices = indices.slice(0, max(self.max_examples - len(self.warnings), 1))
            for index in indices.to_pylist():
                text = reason(index) if callable(reason) else reason
                self.warnings.append(Finding(label.row_id(index), text))
            return
        self.count += hits
        room = self._room()
        if room == 0:
            return
        indices = pc.indices_nonzero(mask)
        if room is not None:
            indices = indices.slice(0, room)
        for index in indices.to_pylist():
            text = reason(index) if callable(reason) else reason
            row_id = label.row_id(index)
            if row_id is None:
                text = f"{label(index)}: {text}"
            self.findings.append(Finding(row_id, text))

    def to_result(self, rule: RuleSpec) -> RuleResult:
        """Convert the tally into a :class:`RuleResult`.

        Args:
            rule: The rule this tally belongs to.

        Returns:
            The finished result.
        """
        if self.count:
            status = _FAILED if rule.level == "must" else _WARNED
        elif self.warnings:
            status = _WARNED
        elif self.skip_reason is not None:
            status = _SKIPPED
        else:
            status = _PASSED
        message_parts = ([self.skip_reason] if self.skip_reason else []) + list(
            dict.fromkeys(self.notes)
        )
        return RuleResult(
            rule_id=rule.id,
            level=rule.level,
            title=rule.title,
            status=status,
            count=self.count,
            findings=list(self.findings),
            message="; ".join(message_parts) or None,
            warnings=list(self.warnings),
        )


class _Labeler:
    """Maps batch row indices to row ids (or ``row N`` when a row has none)."""

    def __init__(self, ids: pa.Array | None, offset: int) -> None:
        self.ids = ids
        self.offset = offset

    def row_id(self, index: int) -> str | None:
        """Return the id of a row, or ``None`` when it has none."""
        if self.ids is None:
            return None
        value = self.ids[index].as_py()
        return None if value is None else str(value)

    def __call__(self, index: int) -> str:
        """Return the id of a row, or ``row N`` when it has none."""
        row_id = self.row_id(index)
        return row_id if row_id is not None else f"row {self.offset + index}"


# ---------------------------------------------------------------------------
# Vectorised helpers
# ---------------------------------------------------------------------------


def _count_true(mask: pa.Array) -> int:
    """Count true values in a boolean array (nulls count as false).

    Args:
        mask: Boolean array.

    Returns:
        Number of true values.
    """
    total = pc.sum(pc.cast(pc.fill_null(mask, False), pa.int64())).as_py()
    return int(total or 0)


def _flatten(array: pa.Array) -> tuple[pa.Array, pa.Array]:
    """Return a list array's child values and offsets, aligned with each other.

    Unlike ``ListArray.flatten`` this keeps any values behind null entries so
    the returned offsets index directly into the returned values.

    Args:
        array: A ``list`` or ``large_list`` array (possibly sliced).

    Returns:
        ``(values, offsets)`` where ``offsets`` has ``len(array) + 1`` entries
        starting at zero.
    """
    offsets = array.offsets
    start = int(offsets[0].as_py())
    end = int(offsets[len(offsets) - 1].as_py())
    values = array.values.slice(start, end - start)
    return values, pc.subtract(offsets, start)


def _count_in_ranges(mask: pa.Array, starts: pa.Array, ends: pa.Array) -> pa.Array:
    """Count true values of ``mask`` inside ``[starts[i], ends[i])`` for every row.

    Args:
        mask: Boolean array over flattened elements; nulls count as false.
        starts: Inclusive start index per row.
        ends: Exclusive end index per row.

    Returns:
        Int64 array of per-row counts.
    """
    size = len(mask)
    csum = pc.cumulative_sum(pc.cast(pc.fill_null(mask, False), pa.int64()))
    padded = pa.concat_arrays([pa.array([0], pa.int64()), csum])
    lo = pc.min_element_wise(starts, size)
    hi = pc.min_element_wise(ends, size)
    return pc.subtract(pc.take(padded, hi), pc.take(padded, lo))


def _to_nm(wavelengths: pa.Array, units: pa.Array) -> pa.Array:
    """Convert native wavelengths to nanometres, element by element.

    Args:
        wavelengths: Float64 values in their native unit.
        units: Unit of each value (``nm``, ``um`` or ``cm-1``).

    Returns:
        Float64 values in nm (``um`` × 1000, ``cm-1`` as 1e7 / ν).
    """
    is_um = pc.fill_null(pc.equal(units, "um"), False)
    is_wavenumber = pc.fill_null(pc.equal(units, "cm-1"), False)
    microns = pc.if_else(is_um, pc.multiply(wavelengths, 1000.0), wavelengths)
    return pc.if_else(is_wavenumber, pc.divide(1e7, wavelengths), microns)


def _close(actual: pa.Array, expected: pa.Array) -> pa.Array:
    """Compare two float arrays with :data:`RELATIVE_TOLERANCE`.

    Args:
        actual: Stored values.
        expected: Recomputed values.

    Returns:
        Boolean array, false where either side is null or non-finite or the
        relative difference exceeds the tolerance.
    """
    difference = pc.abs(pc.subtract(actual, expected))
    scale = pc.max_element_wise(pc.abs(actual), pc.abs(expected))
    within = pc.less_equal(difference, pc.multiply(scale, RELATIVE_TOLERANCE))
    # With an infinite side the formula reads inf <= inf, which IEEE calls true.
    finite = pc.and_(pc.is_finite(actual), pc.is_finite(expected))
    return pc.fill_null(pc.and_(within, finite), False)


def _differs(left: pa.Array, right: pa.Array) -> pa.Array | None:
    """Compare two arrays row by row, treating null == null and NaN == NaN.

    Args:
        left: First array.
        right: Second array of the same length and type.

    Returns:
        Boolean array, true where the rows differ, or ``None`` when the types
        differ or cannot be compared.
    """
    left, right = _to_plain(left), _to_plain(right)
    if left.type != right.type:
        return None
    left_null = pc.is_null(left)
    right_null = pc.is_null(right)
    one_null = pc.xor(left_null, right_null)
    both_null = pc.and_(left_null, right_null)
    data_type = left.type
    if pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
        both_valid = pc.invert(pc.or_(left_null, right_null))
        same_length = pc.fill_null(
            pc.equal(pc.list_value_length(left), pc.list_value_length(right)), False
        )
        result = pc.or_(one_null, pc.and_(both_valid, pc.invert(same_length)))
        selected = pc.and_(both_valid, same_length)
        if not _count_true(selected):
            return result
        left_values, offsets = _flatten(left.filter(selected))
        right_values, _ = _flatten(right.filter(selected))
        inner = _differs(left_values, right_values)
        if inner is None:
            return None
        rows = len(offsets) - 1
        counts = _count_in_ranges(inner, offsets.slice(0, rows), offsets.slice(1, rows))
        return pc.replace_with_mask(result, selected, pc.greater(counts, 0))
    try:
        equal = pc.fill_null(pc.equal(left, right), False)
    except (pa.ArrowNotImplementedError, pa.ArrowInvalid):
        return None
    if pa.types.is_floating(data_type):
        both_nan = pc.and_(
            pc.fill_null(pc.is_nan(left), False), pc.fill_null(pc.is_nan(right), False)
        )
        equal = pc.or_(equal, both_nan)
    return pc.invert(pc.or_(equal, both_null))


def _is_type(data_type: pa.DataType, *predicates: str) -> bool:
    """Return whether any of the named ``pyarrow.types`` predicates holds.

    Predicates missing from older pyarrow versions count as false.

    Args:
        data_type: The type to test.
        *predicates: Names such as ``is_string`` or ``is_list_view``.

    Returns:
        True if at least one predicate is true.
    """
    return any(bool(getattr(pa.types, name, lambda _t: False)(data_type)) for name in predicates)


_STRING_PREDICATES = ("is_string", "is_large_string", "is_string_view")
_LIST_PREDICATES = (
    "is_list",
    "is_large_list",
    "is_list_view",
    "is_large_list_view",
    "is_fixed_size_list",
)
_SIMPLE_TYPES = {
    "string": pa.string(),
    "float64": pa.float64(),
    "float32": pa.float32(),
    "int32": pa.int32(),
    "date32": pa.date32(),
}


def _plain_type(data_type: pa.DataType) -> pa.DataType:
    """Map an Arrow type to the type implied by its Parquet logical type (spec section 4).

    Arrow-only hints that writers store in ``ARROW:schema`` do not change the
    type: dictionary encoding, ``fixed_size_list``, large and view variants of
    string and list, and ``date64`` (stored as a Parquet ``DATE``).

    Args:
        data_type: Any Arrow type.

    Returns:
        The plain equivalent: ``string``, ``list<plain(child)>``, ``date32``,
        or the type unchanged.
    """
    if pa.types.is_dictionary(data_type):
        return _plain_type(data_type.value_type)
    if _is_type(data_type, *_STRING_PREDICATES):
        return pa.string()
    if _is_type(data_type, *_LIST_PREDICATES):
        return pa.list_(_plain_type(data_type.value_type))
    if pa.types.is_date64(data_type):
        return pa.date32()
    return data_type


def spec_arrow_type(spec_type: str) -> pa.DataType | None:
    """Return the plain Arrow type for a type name from ``columns.json``.

    Args:
        spec_type: Type name such as ``string`` or ``list<float32>``.

    Returns:
        The Arrow type, or ``None`` for an unknown name.
    """
    if spec_type.startswith("list<") and spec_type.endswith(">"):
        child = spec_arrow_type(spec_type[len("list<") : -1])
        return None if child is None else pa.list_(child)
    return _SIMPLE_TYPES.get(spec_type)


def _to_plain(array: pa.Array, target: pa.DataType | None = None) -> pa.Array:
    """Cast an array of an equivalent type to its plain form.

    Args:
        array: Any Arrow array.
        target: The plain type an Arrow ``null``-typed array stands for, if
            known (an all-null nullable column may use the ``null`` type).

    Returns:
        The array in :func:`_plain_type` of its type (or ``target`` for a
        ``null``-typed array).
    """
    if pa.types.is_null(array.type) and target is not None:
        return pa.nulls(len(array), target)
    plain = _plain_type(array.type)
    if plain == array.type:
        return array
    if pa.types.is_dictionary(array.type):
        return _to_plain(pc.cast(array, array.type.value_type), target)
    if _is_type(array.type, "is_list_view", "is_large_list_view"):
        # pyarrow 23's list_view -> list cast yields invalid offsets; rebuild
        # the list from the logical child values and the per-row lengths.
        lengths = pc.cast(pc.fill_null(pc.list_value_length(array), 0), pa.int32())
        offsets = pa.concat_arrays(
            [pa.array([0], pa.int32()), pc.cast(pc.cumulative_sum(lengths), pa.int32())]
        )
        values = _to_plain(array.flatten())
        return pa.ListArray.from_arrays(offsets, values, mask=pc.is_null(array))
    return pc.cast(array, plain)


def type_matches(actual: pa.DataType, spec_type: str, nullable: bool = False) -> bool:
    """Check an Arrow type against a type name from ``columns.json``.

    The comparison is on :func:`_plain_type`, so Arrow-only hints (dictionary,
    ``fixed_size_list``, large and view variants) do not matter, and list
    child names and nullability are ignored (spec section 4).

    Args:
        actual: The Arrow type found in the file.
        spec_type: Type name such as ``string`` or ``list<float32>``.
        nullable: Whether the column is nullable; an all-null nullable column
            may be stored with the Arrow ``null`` type.

    Returns:
        Whether the types match.
    """
    if nullable and pa.types.is_null(actual):
        return True
    expected = spec_arrow_type(spec_type)
    return expected is not None and bool(_plain_type(actual) == expected)


def _is_flat(data_type: pa.DataType) -> bool:
    """Return whether a type is a primitive or a list of primitives (rule F3).

    Args:
        data_type: Any Arrow type.

    Returns:
        False for structs, maps, unions and nested lists.
    """
    plain = _plain_type(data_type)
    if pa.types.is_list(plain):
        plain = plain.value_type
    return not pa.types.is_nested(plain)


# ---------------------------------------------------------------------------
# SPDX expressions (F11)
# ---------------------------------------------------------------------------

MAX_LICENCE_NESTING = 32
"""F11: deepest parenthesis nesting the expression parser accepts."""


def check_spdx_expression(expression: str) -> tuple[str | None, set[str]]:
    """Check the syntax of a licence expression (rule F11).

    Accepts SPDX license ids (optionally with a ``+`` suffix), ``LicenseRef-``
    ids, the operators ``AND``, ``OR`` and ``WITH`` (upper case only) and
    parentheses, or ``NOASSERTION`` on its own (upper case only, like the
    operators: SPDX special values are case-sensitive, so ``noassertion`` is
    not a licence id). Tokens are separated by single
    spaces (U+0020) or parentheses; no other whitespace and no leading or
    trailing whitespace is allowed. ``WITH`` must follow a simple expression
    and be followed by an exception id. ``DocumentRef-`` references are
    rejected, and so is nesting deeper than :data:`MAX_LICENCE_NESTING`.
    Identifiers are not checked against the SPDX licence list.

    Args:
        expression: The licence string.

    Returns:
        ``(error, refs)``: ``error`` is ``None`` when the syntax is valid and
        ``refs`` holds every ``LicenseRef-`` identifier used.
    """
    if expression == _NOASSERTION:
        return None, set()
    if not expression:
        return "empty licence", set()
    if expression != expression.strip():
        return "leading or trailing whitespace", set()
    tokens: list[str] = []
    word: list[str] = []
    previous_space = False
    for offset, char in enumerate(expression):
        if char == " " or char in "()":
            if word:
                tokens.append("".join(word))
                word = []
            if char == " ":
                if previous_space:
                    return f"more than one space at offset {offset}", set()
                previous_space = True
                continue
            tokens.append(char)
        elif char.isspace():
            return (
                f"whitespace {char!r} at offset {offset}: tokens are separated by single "
                f"spaces (U+0020)",
                set(),
            )
        else:
            word.append(char)
        previous_space = False
    if word:
        tokens.append("".join(word))
    parser = _SpdxParser(tokens)
    error = parser.parse()
    return error, parser.refs


class _SpdxParser:
    """Recursive-descent parser for the SPDX license expression grammar."""

    def __init__(self, tokens: Sequence[str]) -> None:
        self.tokens = list(tokens)
        self.position = 0
        self.depth = 0
        self.refs: set[str] = set()

    def _peek(self) -> str | None:
        """Return the next token without consuming it."""
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

    def _next(self) -> str | None:
        """Consume and return the next token."""
        token = self._peek()
        self.position += 1
        return token

    def parse(self) -> str | None:
        """Parse the whole token stream.

        Returns:
            An error description, or ``None`` when the expression is valid.
        """
        error = self._expression()
        if error is None and self._peek() is not None:
            error = f"unexpected {self._peek()!r}"
        return error

    def _expression(self) -> str | None:
        """Parse ``term (("AND" | "OR") term)*``; AND binds tighter but both are valid."""
        error = self._term()
        while error is None and self._peek() in ("AND", "OR"):
            self._next()
            error = self._term()
        return error

    def _term(self) -> str | None:
        """Parse a parenthesised expression or a simple expression with optional WITH."""
        token = self._next()
        if token is None:
            return "expression ends unexpectedly"
        if token == "(":
            self.depth += 1
            if self.depth > MAX_LICENCE_NESTING:
                return f"parentheses nested deeper than {MAX_LICENCE_NESTING}"
            error = self._expression()
            if error is not None:
                return error
            if self._next() != ")":
                return "missing ')'"
            self.depth -= 1
            return None
        error = self._simple(token)
        if error is not None:
            return error
        if self._peek() == "WITH":
            self._next()
            exception = self._next()
            if exception is None:
                return "WITH must be followed by an exception id"
            if (
                exception in _SPDX_OPERATORS
                or exception in ("(", ")")
                or exception.upper() == _NOASSERTION
                or exception.lower().startswith(_LICENSE_REF_PREFIX.lower())
                or exception.lower().startswith(_DOCUMENT_REF_PREFIX.lower())
            ):
                return f"WITH must be followed by an exception id, not {exception!r}"
            if not _SPDX_IDSTRING.fullmatch(exception):
                return f"invalid exception id {exception!r}"
        return None

    def _simple(self, token: str) -> str | None:
        """Validate a license id, ``id+`` or ``LicenseRef-`` reference."""
        if token in _SPDX_OPERATORS or token == ")":
            return f"expected a licence id, found {token!r}"
        if token.removesuffix("+").upper() == _NOASSERTION:
            if token != _NOASSERTION:
                return f"the special value NOASSERTION is case-sensitive ({token!r})"
            return "NOASSERTION must stand on its own"
        if token.lower().startswith(_DOCUMENT_REF_PREFIX.lower()):
            return f"DocumentRef- references are not allowed ({token!r})"
        if token.lower().startswith(_LICENSE_REF_PREFIX.lower()) and not token.startswith(
            _LICENSE_REF_PREFIX
        ):
            return f"the LicenseRef- prefix is case-sensitive ({token!r})"
        if token.startswith(_LICENSE_REF_PREFIX):
            if not _SPDX_IDSTRING.fullmatch(token[len(_LICENSE_REF_PREFIX) :]):
                return f"invalid LicenseRef {token!r}"
            self.refs.add(token)
            return None
        identifier = token[:-1] if token.endswith("+") else token
        if not _SPDX_IDSTRING.fullmatch(identifier):
            return f"invalid licence id {token!r}"
        return None


# ---------------------------------------------------------------------------
# File access
# ---------------------------------------------------------------------------


def _is_url(location: str) -> bool:
    """Return whether a location is a URL rather than a local path.

    Args:
        location: Path or URL.

    Returns:
        True for remote URL schemes such as ``https``.
    """
    return urlparse(location).scheme.lower() in _URL_SCHEMES


def _fsspec() -> Any:
    """Import fsspec, raising a clear error if it is missing.

    Returns:
        The ``fsspec`` module.

    Raises:
        ConformanceError: If fsspec is not installed.
    """
    try:
        return importlib.import_module("fsspec")
    except ImportError as exc:
        raise ConformanceError(
            "Checking a URL needs the optional dependency fsspec "
            "(for HTTP also aiohttp): pip install fsspec aiohttp"
        ) from exc


@contextmanager
def _open_binary(location: str) -> Iterator[BinaryIO]:
    """Open a local path or URL for binary reading.

    Args:
        location: Local path or URL.

    Yields:
        A readable binary file object.

    Raises:
        ConformanceError: If a URL cannot be opened for lack of fsspec support.
    """
    if not _is_url(location):
        with open(location, "rb") as handle:
            yield handle
        return
    fsspec = _fsspec()
    try:
        handle = fsspec.open(location, "rb").open()
    except ImportError as exc:
        raise ConformanceError(f"Cannot read {location}: {exc}") from exc
    try:
        yield handle
    finally:
        handle.close()


@contextmanager
def _open_parquet(location: str) -> Iterator[pq.ParquetFile]:
    """Open a Parquet file from a local path or URL.

    Args:
        location: Local path or URL.

    Yields:
        The open ``pyarrow.parquet.ParquetFile``.

    Raises:
        _NotParquetError: If the file is not readable Parquet, including a
            schema whose column names are not valid UTF-8 (Parquet strings
            are UTF-8, so such a file is not valid Parquet: F1 or I1).
    """
    with _open_binary(location) as handle:
        try:
            parquet_file = pq.ParquetFile(handle)
        except (pa.ArrowInvalid, pa.ArrowIOError) as exc:
            raise _NotParquetError(str(exc)) from exc
        except UnicodeDecodeError as exc:
            raise _NotParquetError(f"the schema holds a name that is not UTF-8 ({exc})") from exc
        try:
            yield parquet_file
        finally:
            parquet_file.close()


def _sha256(location: str) -> str:
    """Compute the SHA-256 digest of a file.

    Args:
        location: Local path or URL.

    Returns:
        Lower-case hexadecimal digest.
    """
    digest = hashlib.sha256()
    with _open_binary(location) as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_reference_error(file: str) -> str | None:
    """Check an index ``file`` value: a relative path or an http(s) URL (section 7).

    Args:
        file: The ``file`` value from the index footer.

    Returns:
        A description of the problem, or ``None`` when the value is allowed.
    """
    try:
        parsed = urlparse(file)
    except ValueError as exc:  # e.g. an unbalanced or invalid [IPv6] host
        return f"not a valid URL ({exc})"
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return None
    if PurePosixPath(file).is_absolute() or PureWindowsPath(file).is_absolute():
        return "absolute paths are not allowed (use a path relative to the index file)"
    if PureWindowsPath(file).drive or parsed.scheme:
        return "only relative paths and http(s) URLs are allowed"
    return None


def _resolve(index_location: str, file: str) -> str:
    """Resolve an index ``file`` entry against the index file's location.

    Args:
        index_location: Path or URL of the index file.
        file: The ``file`` value (relative path or URL).

    Returns:
        A path or URL that can be opened.
    """
    if _is_url(file):
        return file
    if _is_url(index_location):
        return urljoin(index_location, file)
    return str(Path(index_location).parent / file)


def _footer_value(parquet_file: pq.ParquetFile, key: str) -> bytes | None:
    """Return the raw value of a footer key-value entry.

    Args:
        parquet_file: Open Parquet file.
        key: Footer key.

    Returns:
        The raw bytes, or ``None`` if the key is absent.
    """
    metadata = parquet_file.metadata.metadata or {}
    value = metadata.get(key.encode("utf-8"))
    return None if value is None else bytes(value)


def _parse_footer(raw: bytes | None, key: str) -> tuple[Any, list[str]]:
    """Decode a footer JSON document.

    Args:
        raw: Raw footer bytes, or ``None`` when the key is missing.
        key: Footer key, used in error messages.

    Returns:
        ``(document, errors)``; ``document`` is ``None`` when it could not be
        decoded.
    """
    if raw is None:
        return None, [f"footer key {key!r} is missing"]
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant), []
    except UnicodeDecodeError as exc:
        return None, [f"footer key {key!r} is not UTF-8: {exc}"]
    except ValueError as exc:  # json.JSONDecodeError, or NaN / Infinity
        return None, [f"footer key {key!r} is not valid JSON: {exc}"]


def _footer_errors(
    document: Any, definition: str, forward_at: Callable[[Sequence[Any]], bool]
) -> tuple[list[str], list[str]]:
    """Validate a decoded footer document: schema, version and ``created``.

    A ``format_version`` with an unsupported major version, or with leading
    zeros, is reported once, with a clear message, instead of as a schema
    pattern mismatch. ``created`` must also name a real calendar date and time
    (spec section 3).

    Args:
        document: Decoded footer JSON.
        definition: ``collectionFooter`` or ``indexFooter``.
        forward_at: Maps the JSON path of a schema error to whether the
            footer governing it declares a newer minor version, where unknown
            members and values are warnings (see :func:`_split_schema_errors`).

    Returns:
        ``(errors, unknowns)``: error descriptions (empty when the document is
        valid) and section 11 forward-compatibility warnings.
    """
    errors, unknowns = _split_schema_errors(document, definition, forward_at)
    version_errors = _major_version_errors(document)
    if version_errors:
        errors = [e for e in errors if not e.startswith("format_version:")] + version_errors
    created = document.get("created") if isinstance(document, dict) else None
    if isinstance(created, str) and not any(e.startswith("created:") for e in errors):
        if not _is_calendar_datetime(created):
            errors.append(f"created: {created!r} is not a real calendar date and time")
    return errors, unknowns


_RFC3339 = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-](\d{2}):(\d{2}))"
)


def _is_calendar_datetime(text: str) -> bool:
    """Check that an RFC 3339 timestamp names a real date and time (spec section 3).

    Leap seconds (second 60) are accepted; offsets must have hours 00-23 and
    minutes 00-59.

    Args:
        text: The timestamp.

    Returns:
        Whether the timestamp is a real calendar date and time.
    """
    match = _RFC3339.fullmatch(text)
    if match is None:
        return False
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if match.group(9) is not None and (int(match.group(9)) > 23 or int(match.group(10)) > 59):
        return False
    try:
        datetime(year, month, day, hour, minute, min(second, 59))
    except ValueError:
        return False
    return second <= 60


def _major_version_errors(document: Any) -> list[str]:
    r"""Check that ``format_version`` has a supported major version and is a semantic version.

    Semantic versioning (spec sections 3 and 11) forbids leading zeros, so
    ``1.01.0`` is rejected here as well as by the footer schema's pattern.

    Args:
        document: Decoded footer JSON.

    Returns:
        A one-element list describing the problem, or an empty list.
    """
    if not isinstance(document, dict):
        return []
    version = document.get("format_version")
    if not isinstance(version, str):
        return []
    if version.split(".")[0] != str(SUPPORTED_MAJOR_VERSION):
        return [f"format_version {version!r} has unsupported major version"]
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) and not _SEMVER.fullmatch(version):
        return [
            f"format_version {version!r} is not a semantic version "
            f"(version numbers have no leading zeros)"
        ]
    return []


# ---------------------------------------------------------------------------
# Collection files
# ---------------------------------------------------------------------------


@dataclass
class _CollectionData:
    """Data an index check needs from a collection file it lists."""

    footer: Any
    num_rows: int
    ids: pa.Array | None
    num_points: pa.Array | None
    table: pa.Table


def _invalid_utf8_rows(array: pa.Array) -> list[int] | None:
    """Find rows of a string or list-of-string array holding invalid UTF-8.

    Arrow validates UTF-8 only on request; Parquet readers do not, so a file
    can hold bytes that are not text. The full validation runs per array; the
    rows are located (in Python) only when it fails.

    Args:
        array: Any Arrow array.

    Returns:
        The offending row indices, or ``None`` when the array is not a string
        array or is valid.
    """
    plain = _plain_type(array.type)
    is_list = pa.types.is_list(plain)
    if (plain.value_type if is_list else plain) != pa.string():
        return None
    try:
        array.validate(full=True)
        return None
    except pa.ArrowInvalid:
        pass
    binary = pc.cast(
        _to_plain(array) if not pa.types.is_dictionary(array.type) else array,
        pa.list_(pa.binary()) if is_list else pa.binary(),
    )
    rows = []
    for index, value in enumerate(binary.to_pylist()):
        items = value if is_list else [value]
        for item in items or []:
            if item is None:
                continue
            try:
                item.decode("utf-8")
            except UnicodeDecodeError:
                rows.append(index)
                break
    return rows


def _replace_invalid_utf8(array: pa.Array) -> pa.Array:
    """Decode a string array with U+FFFD for invalid bytes, so later checks can run.

    Only called for arrays :func:`_invalid_utf8_rows` found invalid (rare), so
    rebuilding them in Python is acceptable; valid rows decode unchanged.

    Args:
        array: A string or list-of-string array.

    Returns:
        A valid array of the plain type.
    """
    plain = _plain_type(array.type)
    is_list = pa.types.is_list(plain)
    source = _to_plain(array) if not pa.types.is_dictionary(array.type) else array
    binary = pc.cast(source, pa.list_(pa.binary()) if is_list else pa.binary())

    def decode(item: bytes | None) -> str | None:
        return None if item is None else item.decode("utf-8", errors="replace")

    values = [
        ([decode(v) for v in value] if value is not None else None) if is_list else decode(value)
        for value in binary.to_pylist()
    ]
    return pa.array(values, plain)


def _reject_constant(name: str) -> Any:
    """Reject the non-standard JSON constants NaN and Infinity.

    Args:
        name: The constant Python's JSON parser found.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"{name} is not valid JSON")


def _json_object_error(text: str) -> str | None:
    """Check that a string is a JSON object (rule F4 for ``extra``).

    Args:
        text: The ``extra`` value.

    Returns:
        A description of the problem, or ``None`` for a JSON object.
    """
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        return f"is not valid JSON ({exc})"
    if not isinstance(value, dict):
        kind = {list: "an array", str: "a string", bool: "a boolean", type(None): "null"}.get(
            type(value), "a number"
        )
        return f"is {kind}, not a JSON object"
    return None


def _value_reason(name: str, array: pa.Array, suffix: str = "") -> Callable[[int], str]:
    """Build a reason function that quotes a row's value of one column.

    Args:
        name: Column name.
        array: The column's values in the current batch.
        suffix: Text appended to the reason.

    Returns:
        A function mapping a row index to ``"{name} {value!r}{suffix}"``.
    """

    def reason(index: int) -> str:
        return f"{name} {array[index].as_py()!r}{suffix}"

    return reason


class _CollectionChecker:
    """Scans one collection file and evaluates F1–F13 and R1–R5."""

    def __init__(
        self, parquet_file: pq.ParquetFile, location: str, max_examples: int | None
    ) -> None:
        self.pf = parquet_file
        self.location = location
        self.max_examples = max_examples
        self.spec = _load_spec()
        self.rules = self.spec.rules_for("collection")
        self.tally = {rule.id: _Tally(max_examples) for rule in self.rules}
        self.schema = parquet_file.schema_arrow
        self.num_rows = int(parquet_file.metadata.num_rows)
        self.footer: Any = None
        self.collection_id: str | None = None
        self.license_refs: set[str] | None = None
        self.dataset_ids: list[str] | None = None
        self.datasets: dict[str, Any] = {}
        self.dataset_counts: dict[str, int] = {}
        self.declared_extensions: set[str] | None = None
        self.scan_error: str | None = None
        self.forward = False
        self.targets = {c.name: spec_arrow_type(c.type) for c in self.spec.columns.values()}
        self.usable: dict[str, bool] = {}
        self.id_chunks: list[pa.Array] = []
        self.num_point_chunks: list[pa.Array] = []
        self.unknown_geometry = 0
        self.previous_id: str | None = None
        self.license_errors: dict[str, str | None] = {}

    # -- orchestration -----------------------------------------------------

    def run(self) -> ConformanceReport:
        """Evaluate every rule.

        Returns:
            The finished report.
        """
        self._check_footer()
        self._check_columns()
        self._check_layout()
        try:
            for offset, batch in self._batches():
                self._check_batch(batch, offset)
        except (pa.ArrowException, OSError) as exc:
            self._scan_failed(exc)
        else:
            self._finish()
        version = self.footer.get("format_version") if isinstance(self.footer, dict) else None
        return ConformanceReport(
            location=self.location,
            kind="collection",
            results=[self.tally[rule.id].to_result(rule) for rule in self.rules],
            num_rows=self.num_rows,
            format_version=version if isinstance(version, str) else None,
            collection_id=self.collection_id,
        )

    def collection_data(self) -> _CollectionData:
        """Return what an index check needs; call after :meth:`run`.

        Returns:
            Footer, ids, per-row point counts and the non-array columns.
        """
        names = self.schema.names
        keep = [name for name in names if name not in PER_BAND_COLUMNS and names.count(name) == 1]
        ids = (
            pa.chunked_array(self.id_chunks, pa.string()).combine_chunks()
            if self.usable.get("id")
            else None
        )
        num_points = (
            pa.chunked_array(self.num_point_chunks, pa.int32()).combine_chunks()
            if self.usable.get("values")
            else None
        )
        return _CollectionData(
            footer=self.footer,
            num_rows=self.num_rows,
            ids=ids,
            num_points=num_points,
            table=self.pf.read(columns=keep),
        )

    def _string_list_columns(self) -> list[str]:
        """Return the defined ``list<string>`` columns (F2: no null elements)."""
        return [c.name for c in self.spec.columns.values() if c.type == "list<string>"]

    def _skip(self, rule_id: str, reason: str) -> None:
        """Mark a rule as not evaluable."""
        self.tally[rule_id].skip_reason = reason

    def _scan_failed(self, exc: Exception) -> None:
        """Record unreadable column data (F1) and skip the row-level rules.

        Args:
            exc: The exception raised while decoding the data.
        """
        self.scan_error = f"not a valid Parquet file: {exc}"
        self.tally["F1"].add(self.scan_error)
        row_rules = ("F2", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "F13")
        for rule_id in row_rules + ("R1", "R4", "R5"):
            if self.tally[rule_id].skip_reason is None:
                self._skip(rule_id, "row data unreadable (F1)")

    def _batches(self) -> Iterator[tuple[int, pa.RecordBatch]]:
        """Yield ``(row_offset, batch)`` over every column.

        Every column is decoded, not only those the rules read, so a corrupt
        page anywhere in the file is found (F1).
        """
        if not self.schema.names:
            return
        offset = 0
        for batch in self.pf.iter_batches(batch_size=BATCH_SIZE):
            yield offset, batch
            offset += batch.num_rows

    # -- F1 and footer-derived state ----------------------------------------

    def _check_footer(self) -> None:
        """F1, and extract the values other rules need from the footer."""
        tally = self.tally["F1"]
        raw = _footer_value(self.pf, COLLECTION_FOOTER_KEY)
        document, errors = _parse_footer(raw, COLLECTION_FOOTER_KEY)
        self.forward = document is not None and is_newer_minor(document)
        if document is not None:
            forward = self.forward
            errors, unknowns = _footer_errors(document, "collectionFooter", lambda _p: forward)
            for unknown in unknowns:
                tally.warn(unknown)
        if _footer_value(self.pf, INDEX_FOOTER_KEY) is not None:
            errors.append(f"file also carries the footer key {INDEX_FOOTER_KEY!r}")
        for error in errors:
            tally.add(error)
        self.footer = document

        if not isinstance(document, dict):
            for rule_id in ("F5", "F6", "F11", "F12"):
                self._skip(rule_id, "footer metadata unavailable (F1)")
            return
        extensions = document.get("extensions")
        if isinstance(extensions, list) and all(isinstance(e, str) for e in extensions):
            self.declared_extensions = set(extensions)
        collection = document.get("collection")
        collection = collection if isinstance(collection, dict) else {}
        cid = collection.get("id")
        if isinstance(cid, str):
            self.collection_id = cid
        else:
            self._skip("F5", "footer `collection.id` unavailable (F1)")
            self._skip("F6", "footer `collection.id` unavailable (F1)")
        refs = collection.get("license_refs", {})
        self.license_refs = set(refs) if isinstance(refs, dict) else set()
        datasets = document.get("datasets", {})
        if isinstance(datasets, dict):
            self.dataset_ids = sorted(datasets)
            self.datasets = datasets
        else:
            self._skip("F12", "footer `datasets` is not an object (F1)")
        self._check_footer_licences(collection, datasets if isinstance(datasets, dict) else {})

    def _check_footer_licences(
        self, collection: Mapping[str, Any], datasets: Mapping[str, Any]
    ) -> None:
        """F11 for licence fields in the footer."""
        fields: list[tuple[str, Any]] = [("collection.license", collection.get("license"))]
        for key in sorted(datasets):
            entry = datasets[key]
            if isinstance(entry, dict) and "license" in entry:
                fields.append((f"datasets.{key}.license", entry.get("license")))
        for name, value in fields:
            if not isinstance(value, str) or not value:
                continue  # an empty footer licence fails the schema (F1)
            error = self._licence_error(value)
            if error is not None:
                self.tally["F11"].add(f"{name} {value!r}: {error}")

    def _licence_error(self, value: str) -> str | None:
        """Check one licence string (syntax and LicenseRef definitions), with caching."""
        if value not in self.license_errors:
            error, refs = check_spdx_expression(value)
            if error is None:
                undefined = sorted(refs - (self.license_refs or set()))
                if undefined:
                    error = "undefined " + ", ".join(undefined)
            self.license_errors[value] = error
        return self.license_errors[value]

    # -- schema-level rules ------------------------------------------------

    def _check_columns(self) -> None:
        """F2 (presence and types), F3 and F4, and record which columns are usable."""
        names = list(self.schema.names)
        columns = self.spec.columns
        for spec in self.spec.core_columns:
            count = names.count(spec.name)
            if count == 0:
                self.tally["F2"].add(f"missing core column {spec.name!r}")
            elif count > 1:
                self.tally["F2"].add(f"core column {spec.name!r} appears {count} times")
            else:
                actual = self.schema.field(spec.name).type
                if type_matches(actual, spec.type, spec.nullable):
                    self.usable[spec.name] = True
                else:
                    self.tally["F2"].add(f"{spec.name}: type {actual}, expected {spec.type}")

        declared = self.declared_extensions
        if declared is None:
            self.tally["F3"].notes.append("extension declarations not checked: no footer (F1)")
        for name in dict.fromkeys(names):
            spec_or_none = columns.get(name)
            if spec_or_none is None or spec_or_none.index_only:
                # x_ columns, and in a newer minor version unknown columns, are
                # allowed but must be flat (spec sections 2 and 6).
                allowed = name.startswith("x_") or (spec_or_none is None and self.forward)
                if not allowed:
                    self.tally["F3"].add(f"column {name!r} is not defined by the specification")
                elif names.count(name) > 1:
                    self.tally["F3"].add(f"column {name!r} appears {names.count(name)} times")
                elif not all(_is_flat(f.type) for f in self.schema if f.name == name):
                    self.tally["F3"].add(
                        f"column {name!r} is not flat (only primitives or lists of primitives)"
                    )
                elif not name.startswith("x_"):
                    self.tally["F3"].warn(
                        f"column {name!r} is not defined in format {_spec_version()} "
                        f"(the file declares a newer minor version)"
                    )
                continue
            spec = spec_or_none
            if spec.extension is None:
                continue
            if spec.extension != "*" and declared is not None and spec.extension not in declared:
                self.tally["F3"].add(
                    f"column {name!r} needs extension {spec.extension!r}, not declared"
                )
            if names.count(name) > 1:
                self.tally["F4"].add(f"column {name!r} appears {names.count(name)} times")
                continue
            actual = self.schema.field(name).type
            if type_matches(actual, spec.type, spec.nullable):
                self.usable[name] = True
            else:
                self.tally["F4"].add(f"{name}: type {actual}, expected {spec.type}")

        self._skip_unusable()

    def _skip_unusable(self) -> None:
        """Mark rules whose input columns are missing or mistyped as skipped."""
        needs = {
            "F5": ["id"],
            "F6": ["collection"],
            "F8": ["wavelengths"],
            "F9": ["values"],
            "F10": ["wavelengths", "wavelength_unit", "wavelength_min_nm", "wavelength_max_nm"],
            "F11": ["license"],
            "F12": ["dataset_id"],
            "R1": ["id"],
            "R4": ["values"],
            "R5": ["geometry"],
        }
        for rule_id, columns in needs.items():
            missing = [c for c in columns if not self.usable.get(c)]
            if missing and self.tally[rule_id].skip_reason is None:
                self._skip(rule_id, "unusable column(s) " + ", ".join(missing) + " (F2)")
        vocab_columns = ["material_category", "quantity", "geometry", "wavelength_unit"]
        unusable = [c for c in vocab_columns if not self.usable.get(c)]
        if unusable:
            note = "not checked (F2): " + ", ".join(unusable)
            if len(unusable) == len(vocab_columns):
                self._skip("F7", note)
            else:
                self.tally["F7"].notes.append(note)
        for name in ("bandpass_fwhm", "values_stddev"):
            if name in self.schema.names and not self.usable.get(name):
                self.tally["F13"].notes.append(f"{name} not checked (F4)")
        if not self.usable.get("wavelengths"):
            self._skip("F13", "unusable column wavelengths (F2)")

    def _check_layout(self) -> None:
        """R1 statistics, R2 and R3 from the Parquet metadata."""
        metadata = self.pf.metadata
        for group_index in range(metadata.num_row_groups):
            group = metadata.row_group(group_index)
            if group.num_rows > MAX_ROW_GROUP_ROWS:
                self.tally["R2"].add(
                    f"row group {group_index} has {group.num_rows} rows "
                    f"(more than {MAX_ROW_GROUP_ROWS})"
                )
            for column_index in range(group.num_columns):
                chunk = group.column(column_index)
                top = chunk.path_in_schema.split(".")[0]
                if chunk.path_in_schema == "id" and self.usable.get("id") and self.num_rows:
                    stats = chunk.statistics if chunk.is_stats_set else None
                    if stats is None or not stats.has_min_max:
                        self.tally["R1"].add(f"row group {group_index}: no statistics for id")
                if top in PER_BAND_COLUMNS and group.num_rows:
                    self._check_encoding(group_index, top, chunk)

    def _check_encoding(self, group_index: int, name: str, chunk: Any) -> None:
        """R3 for one column chunk."""
        encodings = set(chunk.encodings)
        problems = []
        if "BYTE_STREAM_SPLIT" not in encodings:
            problems.append("not BYTE_STREAM_SPLIT")
        if encodings & _DICTIONARY_ENCODINGS:
            problems.append("dictionary encoded")
        if chunk.compression != "ZSTD":
            problems.append(f"compression {chunk.compression}")
        if problems:
            self.tally["R3"].add(f"row group {group_index} {name}: " + ", ".join(problems))

    # -- row-level rules -----------------------------------------------------

    def _check_batch(self, batch: pa.RecordBatch, offset: int) -> None:
        """Evaluate every row-level rule on one record batch."""
        raw = {name: batch.column(i) for i, name in enumerate(batch.schema.names)}
        bad_utf8 = {}
        for name, array in raw.items():
            rows = _invalid_utf8_rows(array)
            if rows is not None:
                raw[name] = _replace_invalid_utf8(array)
                bad_utf8[name] = rows
        columns = {
            name: _to_plain(array, self.targets.get(name)) if self.usable.get(name) else array
            for name, array in raw.items()
        }
        ids = columns["id"] if self.usable.get("id") else None
        label = _Labeler(ids, offset)
        for name, rows in bad_utf8.items():
            spec = self.spec.columns.get(name)
            rule = "F2" if spec is not None and spec.extension is None else "F4"
            if spec is None or spec.index_only:
                rule = "F3"
            mask = [False] * len(batch)
            for row in rows:
                mask[row] = True
            self.tally[rule].flag(pa.array(mask), label, f"invalid UTF-8 in {name}")

        for spec in self.spec.core_columns:
            array = columns.get(spec.name)
            if array is None:
                continue
            if not spec.nullable and array.null_count:
                self.tally["F2"].flag(pc.is_null(array), label, f"null {spec.name}")
            if spec.non_empty and self.usable.get(spec.name):
                blank = pc.equal(pc.utf8_trim_whitespace(array), "")
                self.tally["F2"].flag(blank, label, f"empty or whitespace-only {spec.name}")

        if ids is not None:
            self._check_ids(ids, label)
        if self.usable.get("collection") and self.collection_id is not None:
            mismatch = pc.not_equal(columns["collection"], self.collection_id)
            self.tally["F6"].flag(
                mismatch, label, lambda i: f"collection {columns['collection'][i].as_py()!r}"
            )
        self._check_vocabularies(columns, label)
        wavelength_ok = self._check_wavelengths(columns, label)
        self._check_values(columns, label)
        self._check_extent(columns, label, wavelength_ok)
        self._check_licences(columns, label)
        self._check_datasets(columns, label)
        for name in ("bandpass_fwhm", "values_stddev"):
            self._check_per_band(columns, name, label)
        self._check_string_lists(columns, label)
        self._check_extra(columns, label, bad_utf8.get("extra", []))
        if self.usable.get("geometry"):
            self.unknown_geometry += _count_true(pc.equal(columns["geometry"], "unknown"))

    def _check_string_lists(self, columns: Mapping[str, pa.Array], label: _Labeler) -> None:
        """F2: list columns of strings contain no null elements."""
        for name in self._string_list_columns():
            if name not in columns or not self.usable.get(name):
                continue
            array = columns[name]
            rows = len(array)
            values, offsets = _flatten(array)
            nulls = _count_in_ranges(
                pc.is_null(values), offsets.slice(0, rows), offsets.slice(1, rows)
            )
            self.tally["F2"].flag(
                pc.and_(pc.is_valid(array), pc.greater(nulls, 0)),
                label,
                f"null element in {name}",
            )

    def _check_extra(
        self, columns: Mapping[str, pa.Array], label: _Labeler, invalid_utf8: Sequence[int]
    ) -> None:
        """F4: each non-null ``extra`` value is a JSON object (distinct values parsed once).

        Args:
            columns: The batch's columns.
            label: Row labeller of the batch.
            invalid_utf8: Rows whose ``extra`` is not UTF-8; they already have
                their F4 finding, so they are not parsed again.
        """
        if "extra" not in columns or not self.usable.get("extra"):
            return
        extra = columns["extra"]
        if invalid_utf8:
            keep = [True] * len(extra)
            for row in invalid_utf8:
                keep[row] = False
            extra = pc.if_else(pa.array(keep), extra, pa.scalar(None, extra.type))
        distinct = [v for v in pc.unique(extra).to_pylist() if v is not None]
        problems = {v: error for v in distinct if (error := _json_object_error(v)) is not None}
        if not problems:
            return
        mask = pc.is_in(extra, value_set=pa.array(list(problems), pa.string()))
        self.tally["F4"].flag(mask, label, lambda i: f"extra {problems[extra[i].as_py()]}")

    def _check_ids(self, ids: pa.Array, label: _Labeler) -> None:
        """F5 pattern per row; collect ids for uniqueness and R1 ordering."""
        self.id_chunks.append(ids)
        if self.collection_id is not None:
            pattern = f"^{re.escape(self.collection_id)}:{_LOCAL_ID_PATTERN}$"
            matches = pc.match_substring_regex(ids, pattern)
            self.tally["F5"].flag(
                pc.invert(pc.fill_null(matches, True)), label, "does not match the id pattern"
            )
        if self.tally["R1"].skip_reason is not None or len(ids) == 0:
            return
        if len(ids) > 1:
            descending = pc.less(ids.slice(1), ids.slice(0, len(ids) - 1))
            self.tally["R1"].flag(
                pa.concat_arrays([pa.array([False]), pc.fill_null(descending, False)]),
                label,
                "sorts before the previous row",
            )
        first = ids[0].as_py()
        if self.previous_id is not None and first is not None and first < self.previous_id:
            self.tally["R1"].add(f"{first}: sorts before the previous row")
        # Like a comparison within the batch: a null id is not compared, and
        # the next row is compared with it (so not at all).
        self.previous_id = ids[len(ids) - 1].as_py()

    def _check_vocabularies(self, columns: Mapping[str, pa.Array], label: _Labeler) -> None:
        """F7 for the four vocabulary columns."""
        for name in ("material_category", "quantity", "geometry", "wavelength_unit"):
            if not self.usable.get(name):
                continue
            array = columns[name]
            allowed = pa.array(self.spec.vocabularies[name], pa.string())
            invalid = pc.and_(pc.invert(pc.is_in(array, value_set=allowed)), pc.is_valid(array))
            suffix = f" is not in the {_spec_version()} vocabulary" if self.forward else ""
            self.tally["F7"].flag(
                invalid, label, _value_reason(name, array, suffix), warning=self.forward
            )

    def _check_wavelengths(
        self, columns: Mapping[str, pa.Array], label: _Labeler
    ) -> pa.Array | None:
        """F8. Returns a per-row mask of axes that passed, for F10."""
        if not self.usable.get("wavelengths"):
            return None
        wavelengths = columns["wavelengths"]
        rows = len(wavelengths)
        valid = pc.is_valid(wavelengths)
        lengths = pc.list_value_length(wavelengths)
        values, offsets = _flatten(wavelengths)
        starts, ends = offsets.slice(0, rows), offsets.slice(1, rows)
        positive = pc.fill_null(pc.greater(values, 0.0), False)
        finite_positive = pc.and_(pc.fill_null(pc.is_finite(values), False), positive)
        bad = pc.greater(_count_in_ranges(pc.invert(finite_positive), starts, ends), 0)
        steps = pc.subtract(values.slice(1), values.slice(0, max(len(values) - 1, 0)))
        step_ends = pc.max_element_wise(pc.subtract(ends, 1), starts)
        falls = pc.greater(_count_in_ranges(pc.less_equal(steps, 0.0), starts, step_ends), 0)
        rises = pc.greater(_count_in_ranges(pc.greater_equal(steps, 0.0), starts, step_ends), 0)
        short = pc.fill_null(pc.less(lengths, 2), False)
        not_monotonic = pc.and_(falls, rises)
        failed = pc.and_(valid, pc.or_(pc.or_(short, bad), not_monotonic))

        def reason(i: int) -> str:
            if short[i].as_py():
                return f"{lengths[i].as_py()} element(s)"
            if bad[i].as_py():
                return "null, non-finite or non-positive wavelength"
            return "not strictly monotonic"

        self.tally["F8"].flag(failed, label, reason)
        return pc.and_(valid, pc.invert(failed))

    def _check_values(self, columns: Mapping[str, pa.Array], label: _Labeler) -> None:
        """F9 and R4; collects point counts for index checks."""
        if not self.usable.get("values"):
            return
        values_list = columns["values"]
        rows = len(values_list)
        self.num_point_chunks.append(pc.cast(pc.list_value_length(values_list), pa.int32()))
        valid = pc.is_valid(values_list)
        values, offsets = _flatten(values_list)
        starts, ends = offsets.slice(0, rows), offsets.slice(1, rows)
        finite = pc.fill_null(pc.is_finite(values), False)
        nan = pc.fill_null(pc.is_nan(values), False)
        sentinel = pc.and_(
            finite, pc.fill_null(pc.greater(pc.abs(values), SENTINEL_MAGNITUDE), False)
        )
        bad_element = pc.or_(pc.and_(pc.invert(finite), pc.invert(nan)), sentinel)
        bad = pc.greater(_count_in_ranges(bad_element, starts, ends), 0)
        if self.usable.get("wavelengths"):
            length_mismatch = pc.fill_null(
                pc.not_equal(
                    pc.list_value_length(values_list),
                    pc.list_value_length(columns["wavelengths"]),
                ),
                False,
            )
        else:
            length_mismatch = pa.repeat(False, rows)
        failed = pc.and_(valid, pc.or_(length_mismatch, bad))
        self.tally["F9"].flag(
            failed,
            label,
            lambda i: (
                "length differs from wavelengths"
                if length_mismatch[i].as_py()
                else "null, infinite or sentinel value"
            ),
        )
        present = pc.and_(pc.invert(nan), pc.is_valid(values))
        empty = pc.equal(_count_in_ranges(present, starts, ends), 0)
        self.tally["R4"].flag(pc.and_(valid, empty), label, "no non-NaN value")

    def _check_extent(
        self,
        columns: Mapping[str, pa.Array],
        label: _Labeler,
        monotonic: pa.Array | None,
    ) -> None:
        """F10: stored nm extent against the recomputed one."""
        if monotonic is None or self.tally["F10"].skip_reason is not None:
            return
        wavelengths = columns["wavelengths"]
        units = columns["wavelength_unit"]
        stored_min = columns["wavelength_min_nm"]
        stored_max = columns["wavelength_max_nm"]
        rows = len(wavelengths)
        values, offsets = _flatten(wavelengths)
        if len(values) == 0:
            return
        allowed_units = pa.array(self.spec.vocabularies["wavelength_unit"], pa.string())
        checkable = pc.and_(
            pc.and_(
                pc.fill_null(pc.greater(pc.list_value_length(wavelengths), 0), False),
                pc.is_in(units, value_set=allowed_units),
            ),
            pc.and_(pc.is_valid(stored_min), pc.is_valid(stored_max)),
        )
        last = len(values) - 1
        first_index = pc.min_element_wise(offsets.slice(0, rows), last)
        last_index = pc.max_element_wise(pc.subtract(offsets.slice(1, rows), 1), 0)
        first_nm = _to_nm(pc.take(values, first_index), units)
        last_nm = _to_nm(pc.take(values, last_index), units)
        expected_min = pc.min_element_wise(first_nm, last_nm)
        expected_max = pc.max_element_wise(first_nm, last_nm)

        # Endpoints give the extent for strictly monotonic axes, except a
        # wavenumber axis crossing zero; those and failed axes are aggregated.
        crosses_zero = pc.and_(
            pc.fill_null(pc.equal(units, "cm-1"), False),
            pc.fill_null(
                pc.less_equal(
                    pc.multiply(pc.take(values, first_index), pc.take(values, last_index)), 0.0
                ),
                False,
            ),
        )
        slow = pc.and_(checkable, pc.or_(pc.invert(monotonic), crosses_zero))
        if _count_true(slow):
            subset = wavelengths.filter(slow)
            parents = pc.list_parent_indices(subset)
            elements = subset.flatten()
            element_nm = _to_nm(elements, pc.take(units.filter(slow), parents))
            # Non-finite wavelengths are F8's; the extent covers the finite ones.
            element_nm = pc.if_else(
                pc.fill_null(pc.is_finite(elements), False),
                element_nm,
                pa.scalar(None, pa.float64()),
            )
            grouped = (
                pa.table({"row": parents, "nm": element_nm})
                .group_by("row", use_threads=False)
                .aggregate([("nm", "min"), ("nm", "max")])
                .sort_by("row")
            )
            expected_min = pc.replace_with_mask(
                expected_min, slow, grouped["nm_min"].combine_chunks()
            )
            expected_max = pc.replace_with_mask(
                expected_max, slow, grouped["nm_max"].combine_chunks()
            )

        min_bad = pc.invert(_close(stored_min, expected_min))
        max_bad = pc.invert(_close(stored_max, expected_max))
        failed = pc.and_(checkable, pc.or_(min_bad, max_bad))

        def reason(i: int) -> str:
            return (
                f"wavelength_min_nm={stored_min[i].as_py()} (expected {expected_min[i].as_py()}),"
                f" wavelength_max_nm={stored_max[i].as_py()} (expected {expected_max[i].as_py()})"
            )

        self.tally["F10"].flag(failed, label, reason)

    def _check_licences(self, columns: Mapping[str, pa.Array], label: _Labeler) -> None:
        """F11 for the ``license`` column (each distinct value parsed once)."""
        if self.tally["F11"].skip_reason is not None:
            return
        licences = columns["license"]
        # Empty and whitespace-only values are F2's (F11 covers non-empty values).
        distinct = [v for v in pc.unique(licences).to_pylist() if v is not None and v.strip()]
        invalid = [v for v in distinct if self._licence_error(v) is not None]
        if not invalid:
            return
        mask = pc.is_in(licences, value_set=pa.array(invalid, pa.string()))
        self.tally["F11"].flag(
            mask,
            label,
            lambda i: f"license {licences[i].as_py()!r}: "
            f"{self._licence_error(licences[i].as_py())}",
        )

    def _check_datasets(self, columns: Mapping[str, pa.Array], label: _Labeler) -> None:
        """F12: every non-null ``dataset_id`` is a footer ``datasets`` key."""
        if self.tally["F12"].skip_reason is not None or self.dataset_ids is None:
            return
        dataset_ids = columns["dataset_id"]
        counts = pc.value_counts(dataset_ids)
        for value, count in zip(
            counts.field("values").to_pylist(), counts.field("counts").to_pylist()
        ):
            if value is not None:
                self.dataset_counts[value] = self.dataset_counts.get(value, 0) + int(count)
        known = pa.array(self.dataset_ids, pa.string())
        unknown = pc.and_(pc.is_valid(dataset_ids), pc.invert(pc.is_in(dataset_ids, known)))
        self.tally["F12"].flag(
            unknown, label, lambda i: f"dataset_id {dataset_ids[i].as_py()!r} not in footer"
        )

    def _check_per_band(self, columns: Mapping[str, pa.Array], name: str, label: _Labeler) -> None:
        """F13 for ``bandpass_fwhm`` or ``values_stddev``."""
        if (
            name not in columns
            or not self.usable.get(name)
            or self.tally["F13"].skip_reason is not None
        ):
            return
        array = columns[name]
        rows = len(array)
        valid = pc.is_valid(array)
        values, offsets = _flatten(array)
        finite = pc.fill_null(pc.is_finite(values), False)
        nan = pc.fill_null(pc.is_nan(values), False)
        non_negative = pc.fill_null(pc.greater_equal(values, 0.0), False)
        good = pc.or_(nan, pc.and_(finite, non_negative))
        bad = pc.greater(
            _count_in_ranges(pc.invert(good), offsets.slice(0, rows), offsets.slice(1, rows)), 0
        )
        mismatch = pc.fill_null(
            pc.not_equal(pc.list_value_length(array), pc.list_value_length(columns["wavelengths"])),
            False,
        )
        failed = pc.and_(valid, pc.or_(mismatch, bad))
        self.tally["F13"].flag(
            failed,
            label,
            lambda i: (
                f"{name} length differs from wavelengths"
                if mismatch[i].as_py()
                else f"{name} has a null, infinite or negative element"
            ),
        )

    # -- whole-file rules ------------------------------------------------------

    def _finish(self) -> None:
        """F5 uniqueness, F12 dataset usage and R5 after all batches were scanned."""
        if self.dataset_ids is not None and self.tally["F12"].skip_reason is None:
            for key in self.dataset_ids:
                used = self.dataset_counts.get(key, 0)
                if not used:
                    self.tally["F12"].add(f"datasets key {key!r} is not used by any row")
                    continue
                entry = self.datasets.get(key)
                included = entry.get("spectra_included") if isinstance(entry, dict) else None
                # JSON Schema counts 99.0 as an integer, so compare floats too.
                if isinstance(included, (int, float)) and not isinstance(included, bool):
                    if included != used:
                        self.tally["F12"].add(
                            f"datasets.{key}.spectra_included is {included}, "
                            f"but {used} row(s) have dataset_id {key!r}"
                        )
        if self.usable.get("id") and self.id_chunks:
            ids = pa.chunked_array(self.id_chunks, pa.string())
            counts = pc.value_counts(ids)
            values = counts.field("values")
            occurrences = counts.field("counts")
            duplicated = pc.and_(pc.greater(occurrences, 1), pc.is_valid(values))
            if _count_true(duplicated):
                dup_values = values.filter(duplicated)
                dup_counts = occurrences.filter(duplicated)
                tally = self.tally["F5"]
                room = tally._room()
                if room is not None:
                    dup_values = dup_values.slice(0, room)
                    dup_counts = dup_counts.slice(0, room)
                for value, count in zip(dup_values.to_pylist(), dup_counts.to_pylist()):
                    tally.add(f"appears {count} times", count=0, row_id=value)
                tally.count += int(pc.sum(occurrences.filter(duplicated)).as_py())
        if self.usable.get("geometry") and self.num_rows:
            fraction = self.unknown_geometry / self.num_rows
            if fraction > UNKNOWN_GEOMETRY_FRACTION:
                self.tally["R5"].add(
                    f"{self.unknown_geometry} of {self.num_rows} rows ({fraction:.0%}) have "
                    f"geometry 'unknown' (at most {UNKNOWN_GEOMETRY_FRACTION:.0%} should be)",
                    count=self.unknown_geometry,
                )


def _unreadable_report(location: str, kind: str, error: Exception) -> ConformanceReport:
    """Build a report for a file pyarrow cannot open as Parquet.

    Args:
        location: Path or URL.
        kind: ``collection`` or ``index``.
        error: The exception raised while opening.

    Returns:
        A report with F1 (or I1) failed and every other rule skipped.
    """
    first_rule = "F1" if kind == "collection" else "I1"
    results = []
    for rule in _load_spec().rules_for(kind):
        if rule.id == first_rule:
            results.append(
                RuleResult(
                    rule.id,
                    rule.level,
                    rule.title,
                    _FAILED,
                    count=1,
                    findings=[Finding(None, f"not a readable Parquet file: {error}")],
                )
            )
        else:
            results.append(
                RuleResult(
                    rule.id, rule.level, rule.title, _SKIPPED, message="file is not readable"
                )
            )
    return ConformanceReport(location=location, kind=kind, results=results)


def _run_collection(
    location: str, max_examples: int | None
) -> tuple[ConformanceReport, _CollectionData | None]:
    """Check a collection file and keep the data an index check needs.

    Args:
        location: Path or URL.
        max_examples: Findings kept per rule (``None`` keeps all).

    Returns:
        ``(report, data)``; ``data`` is ``None`` if the file is not readable.
    """
    try:
        with _open_parquet(location) as parquet_file:
            checker = _CollectionChecker(parquet_file, location, max_examples)
            report = checker.run()
            if checker.scan_error is not None:
                return report, None
            try:
                return report, checker.collection_data()
            except (pa.ArrowException, OSError):
                return report, None
    except _NotParquetError as exc:
        return _unreadable_report(location, "collection", exc), None


def _require_local(location: str) -> None:
    """Raise if a local path does not exist (URLs are not checked).

    Args:
        location: Path or URL.

    Raises:
        FileNotFoundError: If ``location`` is a local path that does not exist.
    """
    if not _is_url(location) and not os.path.exists(location):
        raise FileNotFoundError(location)


def check_collection(
    path_or_url: str | os.PathLike[str], max_examples: int | None = MAX_EXAMPLES
) -> ConformanceReport:
    """Check a collection file against rules F1–F13 and R1–R5.

    Args:
        path_or_url: Local path, or an ``http(s)://`` URL (needs fsspec and
            aiohttp).
        max_examples: Findings kept per rule; ``None`` keeps every one.

    Returns:
        The conformance report.

    Raises:
        FileNotFoundError: If a local file does not exist.
        ConformanceError: If a URL cannot be read for lack of fsspec support.
    """
    location = os.fspath(path_or_url)
    _require_local(location)
    report, _ = _run_collection(location, max_examples)
    return report


# ---------------------------------------------------------------------------
# Index files
# ---------------------------------------------------------------------------


class _IndexChecker:
    """Evaluates rule I1 for one index file and checks its collection files."""

    def __init__(
        self, parquet_file: pq.ParquetFile, location: str, max_examples: int | None
    ) -> None:
        self.pf = parquet_file
        self.location = location
        self.max_examples = max_examples
        self.spec = _load_spec()
        self.tally = _Tally(max_examples)
        self.footer: Any = None
        self.forward = False
        self.collections: dict[str, _CollectionData] = {}
        self.reports: list[ConformanceReport] = []
        # Non-array columns of listed collection files that declare a newer
        # minor version: an index may carry those unknown to this version.
        self.forward_columns: set[str] = set()

    def run(self) -> ConformanceReport:
        """Evaluate I1 and check every listed collection file.

        Returns:
            The index report, with collection reports nested.
        """
        entries = self._check_footer()
        self._load_collections(entries)
        schema = self.pf.schema_arrow
        self._check_columns(schema)
        if schema.names.count("file") == 1 and type_matches(schema.field("file").type, "string"):
            try:
                self._check_rows(schema)
            except (pa.ArrowException, OSError) as exc:
                self.tally.add(f"not a valid Parquet file: {exc}")
        else:
            self.tally.notes.append("row matching not evaluated: no usable `file` column")
        version = self.footer.get("format_version") if isinstance(self.footer, dict) else None
        return ConformanceReport(
            location=self.location,
            kind="index",
            results=[self.tally.to_result(rule) for rule in self.spec.rules_for("index")],
            num_rows=int(self.pf.metadata.num_rows),
            format_version=version if isinstance(version, str) else None,
            collections=self.reports,
        )

    def _check_footer(self) -> list[dict[str, Any]]:
        """Validate the index footer and return its usable collection entries."""
        raw = _footer_value(self.pf, INDEX_FOOTER_KEY)
        document, errors = _parse_footer(raw, INDEX_FOOTER_KEY)
        self.forward = document is not None and is_newer_minor(document)
        if document is not None:
            errors, unknowns = _footer_errors(document, "indexFooter", self._forward_at(document))
            for unknown in unknowns:
                self.tally.warn(f"footer: {unknown}")
        if _footer_value(self.pf, COLLECTION_FOOTER_KEY) is not None:
            errors.append(f"file also carries the footer key {COLLECTION_FOOTER_KEY!r}")
        for error in errors:
            self.tally.add(f"footer: {error}")
        self.footer = document
        entries = document.get("collections") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and isinstance(e.get("file"), str)]

    def _forward_at(self, document: Any) -> Callable[[Sequence[Any]], bool]:
        """Return which footer governs a JSON path of the index footer (section 11).

        An embedded ``collections[N].metadata`` object is a collection footer
        and is judged by its own ``format_version``, as the collection file
        itself is; everything else follows the index's ``format_version``.

        Args:
            document: The decoded index footer.

        Returns:
            A function mapping an error's JSON path to whether the governing
            footer declares a newer minor version.
        """
        entries = document.get("collections") if isinstance(document, dict) else None

        def forward_at(path: Sequence[Any]) -> bool:
            if (
                len(path) >= 3
                and path[0] == "collections"
                and path[2] == "metadata"
                and isinstance(entries, list)
                and isinstance(path[1], int)
                and 0 <= path[1] < len(entries)
                and isinstance(entries[path[1]], dict)
            ):
                return is_newer_minor(entries[path[1]].get("metadata"))
            return self.forward

        return forward_at

    def _load_collections(self, entries: Sequence[dict[str, Any]]) -> None:
        """Check each listed collection file and its footer entry."""
        seen: set[str] = set()
        owners: dict[str, str] = {}
        for entry in entries:
            file = entry["file"]
            reference_error = _file_reference_error(file)
            if reference_error is not None:
                self.tally.add(f"footer: file {file!r}: {reference_error}")
            target = _resolve(self.location, file)
            resolved = target if _is_url(target) else os.path.realpath(target)
            if resolved in seen:
                self.tally.add(f"footer: file {file!r} is listed more than once")
                continue
            seen.add(resolved)
            if not _is_url(target) and not os.path.exists(target):
                self.tally.add(f"{file}: file not found")
                continue
            try:
                report, data = _run_collection(target, self.max_examples)
            except (OSError, ConformanceError) as exc:
                self.tally.add(f"{file}: cannot be read ({exc})")
                continue
            self.reports.append(report)
            if data is None:
                self.tally.add(f"{file}: not a readable Parquet file")
                continue
            self.collections[file] = data
            if is_newer_minor(data.footer):
                self.forward_columns.update(data.table.column_names)
            collection = data.footer.get("collection") if isinstance(data.footer, dict) else None
            cid = collection.get("id") if isinstance(collection, dict) else None
            if isinstance(cid, str):
                if cid in owners:
                    self.tally.add(
                        f"{file}: collection id {cid!r} is also used by {owners[cid]} "
                        f"(files in one index cannot share a collection id)"
                    )
                else:
                    owners[cid] = file
            if entry.get("num_spectra") != data.num_rows:
                self.tally.add(
                    f"{file}: num_spectra {entry.get('num_spectra')!r}, file has {data.num_rows}"
                )
            if entry.get("metadata") != data.footer:
                self.tally.add(f"{file}: metadata differs from the file's `openspeclib` footer")
            if isinstance(entry.get("sha256"), str) and entry["sha256"] != _sha256(target):
                self.tally.add(f"{file}: sha256 does not match")

    def _required_columns(self) -> dict[str, ColumnSpec]:
        """Return the columns section 7 requires."""
        required = {c.name: c for c in self.spec.core_columns if c.name not in PER_BAND_COLUMNS}
        for data in self.collections.values():
            for name in data.table.column_names:
                spec = self.spec.columns.get(name)
                if spec is not None and spec.is_extension and name not in PER_BAND_COLUMNS:
                    required[name] = spec
        required.update({c.name: c for c in self.spec.index_only_columns})
        return required

    def _check_columns(self, schema: pa.Schema) -> None:
        """Required index columns exist once with the right types; no other columns."""
        required = self._required_columns()
        for name, spec in required.items():
            count = schema.names.count(name)
            if count == 0:
                self.tally.add(f"missing column {name!r}")
            elif count > 1:
                self.tally.add(f"column {name!r} appears {count} times")
            elif not type_matches(schema.field(name).type, spec.type, spec.nullable):
                self.tally.add(f"{name}: type {schema.field(name).type}, expected {spec.type}")
        for name in dict.fromkeys(schema.names):
            if name in required:
                continue
            # Section 11: a column unknown to this version is a warning when
            # the index, or a listed collection file holding the column,
            # declares a newer minor version. Like x_ columns it must be flat.
            unknown = name not in self.spec.columns and not name.startswith("x_")
            if unknown and self.forward:
                why = "the index declares a newer minor version"
            elif unknown and name in self.forward_columns:
                why = "a listed collection file that declares a newer minor version has it"
            elif not name.startswith("x_"):
                self.tally.add(f"column {name!r} is not allowed in an index file")
                continue
            else:
                why = None
            if schema.names.count(name) > 1:
                self.tally.add(f"column {name!r} appears {schema.names.count(name)} times")
            elif not all(_is_flat(f.type) for f in schema if f.name == name):
                self.tally.add(
                    f"column {name!r} is not flat (only primitives or lists of primitives)"
                )
            elif why is not None:
                self.tally.warn(
                    f"column {name!r} is not defined in format {_spec_version()} ({why})"
                )
        # A column a newer minor version defines may be a per-band array, which
        # an index leaves out, so a missing one is only a warning; a scalar one
        # is almost certainly required by section 7.
        for file, data in self.collections.items():
            if not is_newer_minor(data.footer):
                continue
            for column in data.table.schema:
                name = column.name
                if (
                    name in self.spec.columns
                    or name.startswith("x_")
                    or name in schema.names
                    or not _is_flat(column.type)
                    or pa.types.is_list(column.type)
                    or pa.types.is_large_list(column.type)
                ):
                    continue
                self.tally.warn(
                    f"{file}: column {name!r} (format {data.footer.get('format_version')}) is "
                    f"not in the index; section 7 requires every non-array column"
                )

    def _check_rows(self, schema: pa.Schema) -> None:
        """Every row matches exactly one collection row; every collection row is indexed."""
        names = [n for n in schema.names if schema.names.count(n) == 1]
        names = [n for n in names if n not in PER_BAND_COLUMNS]
        table = self.pf.read(columns=names)
        targets = {c.name: spec_arrow_type(c.type) for c in self.spec.columns.values()}
        columns = {}
        for name in names:
            array = table[name].combine_chunks()
            bad_rows = _invalid_utf8_rows(array)
            if bad_rows is not None:
                self.tally.add(f"column {name!r} holds invalid UTF-8 in {len(bad_rows)} row(s)")
                array = _replace_invalid_utf8(array)
            columns[name] = _to_plain(array, targets.get(name))
        files = columns["file"]
        has_ids = "id" in columns and columns["id"].type == pa.string()
        ids = columns["id"] if has_ids else None
        label = _Labeler(ids, 0)

        self.tally.flag(pc.is_null(files), label, "null file")
        if ids is not None:
            counts = pc.value_counts(ids)
            repeated_ids = counts.field("values").filter(pc.greater(counts.field("counts"), 1))
            self.tally.flag(
                pc.fill_null(pc.is_in(ids, value_set=repeated_ids), False),
                label,
                "id appears more than once in the index",
            )
        listed = pa.array(sorted(self.collections), pa.string())
        unlisted = pc.and_(pc.is_valid(files), pc.invert(pc.is_in(files, value_set=listed)))
        self.tally.flag(
            unlisted, label, lambda i: f"file {files[i].as_py()!r} is not a readable listed file"
        )
        if ids is None:
            self.tally.notes.append("row matching not evaluated: no usable `id` column")
            return
        for file in sorted(self.collections):
            selected = pc.fill_null(pc.equal(files, file), False)
            rows = {name: array.filter(selected) for name, array in columns.items()}
            self._match_file(file, rows, self.collections[file])

    def _match_file(self, file: str, rows: Mapping[str, pa.Array], data: _CollectionData) -> None:
        """Compare the index rows of one collection file with that file."""
        index_ids = rows["id"]
        label = _Labeler(index_ids, 0)
        if data.ids is None:
            self.tally.add(f"{file}: collection has no usable `id` column")
            return
        source_ids = _to_plain(data.ids)

        def duplicates(ids: pa.Array) -> pa.Array:
            counts = pc.value_counts(ids)
            return counts.field("values").filter(pc.greater(counts.field("counts"), 1))

        position = pc.index_in(index_ids, value_set=source_ids)
        missing = pc.is_null(position)
        ambiguous = pc.fill_null(pc.is_in(index_ids, value_set=duplicates(source_ids)), False)
        self.tally.flag(missing, label, f"no row with this id in {file}")
        self.tally.flag(ambiguous, label, f"matches more than one row of {file}")
        not_indexed = pc.invert(pc.is_in(source_ids, value_set=index_ids))
        self.tally.flag(
            not_indexed, _Labeler(source_ids, 0), f"row of {file} is missing from the index"
        )

        matched = pc.and_(pc.invert(missing), pc.invert(ambiguous))
        source_names = set(data.table.column_names)
        for name, index_values in rows.items():
            if name in ("file", "num_points") or name.startswith("x_"):
                continue
            if name not in source_names:
                spec = self.spec.columns.get(name)
                if spec is not None and spec.is_extension:
                    self.tally.flag(
                        pc.is_valid(index_values),
                        label,
                        f"{name} is not null, but {file} has no {name} column",
                    )
                continue
            spec = self.spec.columns.get(name)
            target = spec_arrow_type(spec.type) if spec is not None else None
            source = data.table[name].combine_chunks()
            source_values = pc.take(_to_plain(source, target), position)
            differs = _differs(index_values, source_values)
            if differs is None:
                self.tally.add(
                    f"{file}: column {name!r} cannot be compared "
                    f"(index {index_values.type}, collection {source_values.type})"
                )
                continue
            self.tally.flag(pc.and_(matched, differs), label, f"{name} differs from {file}")
        if "num_points" in rows and data.num_points is not None:
            index_points = pc.cast(rows["num_points"], pa.int64())
            source_points = pc.cast(pc.take(data.num_points, position), pa.int64())
            differs = _differs(index_points, source_points)
            if differs is not None:
                self.tally.flag(pc.and_(matched, differs), label, f"num_points differs from {file}")


def check_index(
    path: str | os.PathLike[str], max_examples: int | None = MAX_EXAMPLES
) -> ConformanceReport:
    """Check an index file against rule I1 and every collection file it lists.

    The index conforms when I1 passes and every listed collection file
    conforms; the collection reports are nested in
    :attr:`ConformanceReport.collections`.

    Args:
        path: Local path (or URL, with fsspec) of the index file.
        max_examples: Findings kept per rule; ``None`` keeps every one.

    Returns:
        The conformance report.

    Raises:
        FileNotFoundError: If a local file does not exist.
        ConformanceError: If a URL cannot be read for lack of fsspec support.
    """
    location = os.fspath(path)
    _require_local(location)
    try:
        with _open_parquet(location) as parquet_file:
            return _IndexChecker(parquet_file, location, max_examples).run()
    except _NotParquetError as exc:
        return _unreadable_report(location, "index", exc)


def is_index_file(path_or_url: str | os.PathLike[str]) -> bool | None:
    """Tell whether a file is checked as an index file.

    A file is an index when its footer has ``openspeclib_index`` and not
    ``openspeclib``; a file with both keys is checked as a collection, where
    F1 fails it.

    Args:
        path_or_url: Local path or URL.

    Returns:
        ``True`` for an index, ``False`` for a collection, ``None`` when the
        file is not readable Parquet.

    Raises:
        FileNotFoundError: If a local file does not exist.
    """
    location = os.fspath(path_or_url)
    _require_local(location)
    try:
        with _open_parquet(location) as parquet_file:
            return (
                _footer_value(parquet_file, INDEX_FOOTER_KEY) is not None
                and _footer_value(parquet_file, COLLECTION_FOOTER_KEY) is None
            )
    except _NotParquetError:
        return None


def check_path(
    path_or_url: str | os.PathLike[str], max_examples: int | None = MAX_EXAMPLES
) -> ConformanceReport:
    """Check a file as an index or a collection, depending on its footer.

    See :func:`is_index_file` for the dispatch rule; unreadable files are
    reported as collection files failing F1.

    Args:
        path_or_url: Local path or URL.
        max_examples: Findings kept per rule; ``None`` keeps every one.

    Returns:
        The conformance report.

    Raises:
        FileNotFoundError: If a local file does not exist.
        ConformanceError: If a URL cannot be read for lack of fsspec support.
    """
    location = os.fspath(path_or_url)
    if is_index_file(location):
        return check_index(location, max_examples)
    return check_collection(location, max_examples)
