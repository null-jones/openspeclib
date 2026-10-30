"""Validation of OpenSpecLib Format v1 files: conformance plus the quality profile.

:func:`validate` checks a collection file, an index file, or a directory of
them. Two profiles exist:

* ``format`` (the default, as spec section 10 says): conformance only (rules
  F1–F13, I1 and the R1–R5 warnings), using :mod:`openspeclib.conformance`.
* ``openspeclib``: conformance plus the OpenSpecLib quality profile Q1–Q4
  (``docs/spec/format-v1.md`` section 12), with bounds from ``rules.json``
  (``qa_bounds``, ``qa_wavelength_nm``) and per-spectrum waivers from an
  allowlist.

Quality checks keep the names and semantics of the v0 validator:

* ``value_scale`` (Q1, error): the median of a spectrum's valid values lies
  outside the hard bounds of its quantity, i.e. the spectrum as a whole is
  off-scale (high: a missed scale conversion; low: corrupt or non-spectral
  data). Waivable by exact ``id`` only.
* ``value_out_of_bounds`` (Q2, error): the median is in range but some valid
  values lie outside the hard bounds (spikes, clipped or corrupt bands). An
  allowlist entry waives it or, with ``bounds``, replaces the hard bounds.
* ``value_range_suspect`` (Q3, warning): values within the hard bounds but
  outside the warning bounds. An allowlist entry for Q2 does not waive Q3.
* ``wavelength_extent`` (Q4, error): ``wavelength_min_nm`` or
  ``wavelength_max_nm`` lies outside the plausible extent (100 nm to 1 mm),
  almost always a mislabelled ``wavelength_unit``. Waivable by exact ``id``
  only.
* ``value_bounds_share`` (warning): a collection or dataset has at least 10
  spectra of one quantity and at least 10 % of them fail Q1, Q2 or Q4, which
  points at a set-wide scale or unit problem.

"Valid" values are finite and not sentinels (magnitude up to 1e10). Values
are float32, so every bound is rounded to float32 before comparison and a
value exactly at a bound passes. The median is exact: the middle valid value,
or the mean of the two middle ones.

Checks that Format v1 makes structural are conformance rules now and cannot be
waived: sentinels and non-finite values (F9), NaN accounting (gone: NaN is the
only missing-value marker), the wavelength extent (F10), and the catalog and
grid-registry checks, which the index cross-checks (I1) replace. Spectra with
no valid value are reported by R4.

Every quality computation is vectorised with :mod:`pyarrow.compute`; Python
objects are created only for the spectra that produce an issue.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import struct
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

from openspeclib import conformance
from openspeclib.conformance import ConformanceReport, QualityBounds

# Allowlist shipped with the package and loaded by default.
DEFAULT_ALLOWLIST_PATH = Path(__file__).resolve().parent / "validation_allowlist.json"

Profile = Literal["format", "openspeclib"]
PROFILES: tuple[str, ...] = ("format", "openspeclib")
DEFAULT_PROFILE: Profile = "format"

SENTINEL_ABS_THRESHOLD = conformance.SENTINEL_MAGNITUDE
"""Finite values with a larger magnitude are sentinels, not valid values."""

# A collection (or contributing dataset) with at least _SHARE_MIN_SPECTRA
# spectra of one quantity, of which at least _SHARE_THRESHOLD fail Q1, Q2 or Q4
# (value_scale, value_out_of_bounds, wavelength_extent), gets one
# value_bounds_share warning.
_SHARE_THRESHOLD = 0.10
_SHARE_MIN_SPECTRA = 10

_BATCH_SIZE = 2048

# The quality check names and the quality-profile rules they implement.
QUALITY_RULES: dict[str, str] = {
    "value_scale": "Q1",
    "value_out_of_bounds": "Q2",
    "value_range_suspect": "Q3",
    "value_bounds_share": "Q2",
    "wavelength_extent": "Q4",
}


# ---------------------------------------------------------------------------
# Value bounds
# ---------------------------------------------------------------------------


def _f32(value: float) -> float:
    """Round a Python float to the nearest float32, as stored values are.

    Args:
        value: The value to round.

    Returns:
        The float32-rounded value (infinities pass through).
    """
    if not math.isfinite(value):
        return value
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


def quality_bounds() -> dict[str, QualityBounds]:
    """Return the quality-profile bounds by quantity, rounded to float32.

    Returns:
        Mapping of quantity to its float32-rounded :class:`QualityBounds`.
    """
    return {
        quantity: QualityBounds(
            hard_min=_f32(b.hard_min),
            hard_max=_f32(b.hard_max),
            warn_min=_f32(b.warn_min),
            warn_max=_f32(b.warn_max),
        )
        for quantity, b in conformance.load_qa_bounds().items()
    }


def _g(value: float) -> str:
    """Format a float32-derived value with enough digits to tell it from a bound.

    Args:
        value: The value to format.

    Returns:
        The value with 7 significant digits.
    """
    return f"{value:.7g}"


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

_QUALITY_DESCRIPTIONS: dict[str, str] = {
    "value_scale": (
        "the spectrum as a whole (its median) is outside the quantity's hard bounds — "
        "a missed scale conversion (median too high) or corrupt/non-spectral data "
        "(median too low)"
    ),
    "value_out_of_bounds": (
        "some values outside the quantity's hard bounds while the median is in range — "
        "spikes, clipped or corrupt bands"
    ),
    "value_bounds_share": (
        "at least 10 spectra of one quantity and at least 10% of them fail Q1, Q2 or Q4 — "
        "suspect a set-wide scale or units problem"
    ),
    "value_range_suspect": "values outside the quantity's expected range (within hard bounds)",
    "wavelength_extent": (
        "wavelength extent outside the plausible range — almost always a mislabelled "
        "wavelength_unit"
    ),
    "quality_skipped": "quality profile not evaluated for a file",
    "unindexed_file": "collection file in the directory is not listed by any index file",
    "no_files": "no Parquet files to validate",
    "unreadable_path": (
        "a path that cannot be read or checked (a *.parquet directory, a broken link, "
        "an unreadable directory, or a file the checker could not process)"
    ),
    "stale_allowlist_entry": "allowlist entry did not match any issue — remove or update it",
    "other": "uncategorised issue",
}


def _check_descriptions() -> dict[str, str]:
    """Return the description of every check, including the conformance rules.

    Returns:
        Mapping of check name (``value_scale``, ``F9``, ``I1``, ...) to a
        short description; rule descriptions come from ``rules.json``.
    """
    descriptions = {
        rule.id: f"{rule.title} ({rule.level} rule)" for rule in conformance.load_rules().values()
    }
    descriptions.update(_QUALITY_DESCRIPTIONS)
    return descriptions


CHECK_DESCRIPTIONS: dict[str, str] = _check_descriptions()

# Quality checks an allowlist entry may waive with any selector (exact id, id
# prefix or dataset).
WAIVABLE_CHECKS: frozenset[str] = frozenset({"value_out_of_bounds", "value_range_suspect"})
# Waivable only for one exact spectrum id: a whole-spectrum scale error is a
# loader bug for any set of spectra, so it must never be waived wholesale.
ID_ONLY_WAIVABLE_CHECKS: frozenset[str] = frozenset({"value_scale", "wavelength_extent"})
# Conformance rules are never waivable: a file either follows the format or
# it does not.


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    """A single validation finding.

    Attributes:
        severity: ``"error"`` (fails validation) or ``"warning"``.
        check: Check name: a quality check (``value_scale``, ...), a
            conformance rule id (``F9``, ``I1``, ``R3``, ...) or a
            directory-level check.
        message: Full, self-contained human-readable message.
        source: Collection id the issue belongs to (or the file name when the
            collection id is unknown).
        spectrum_id: Spectrum the issue concerns, when per-spectrum.
        detail: Short detail used in aggregated example lines.
        rule: Specification rule the check implements (``F9``, ``Q2``, ...).
        file: File the issue was found in, relative to the validated path.
    """

    severity: Severity
    check: str
    message: str
    source: str | None = None
    spectrum_id: str | None = None
    detail: str | None = None
    rule: str | None = None
    file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the issue as a JSON-serialisable dict.

        Returns:
            A dict with every attribute of the issue.
        """
        return {
            "severity": self.severity,
            "check": self.check,
            "rule": self.rule,
            "file": self.file,
            "source": self.source,
            "spectrum_id": self.spectrum_id,
            "detail": self.detail,
            "message": self.message,
        }


@dataclass
class IssueGroup:
    """Issues sharing a severity, check and source, for aggregated output.

    Attributes:
        severity: Shared severity.
        check: Shared check name.
        source: Shared source (``None`` for run-level issues).
        issues: The grouped issues, in discovery order.
    """

    severity: Severity
    check: str
    source: str | None
    issues: list[Issue]

    @property
    def description(self) -> str:
        """Return the check's description (or the message of a lone issue)."""
        if len(self.issues) == 1 and self.issues[0].spectrum_id is None:
            return self.issues[0].message
        return CHECK_DESCRIPTIONS.get(self.check, self.check)

    def to_dict(self, max_examples: int) -> dict[str, Any]:
        """Return the group as a JSON-serialisable dict.

        Args:
            max_examples: Maximum number of example ids/messages to include.

        Returns:
            A dict with the group key, count, description and examples.
        """
        return {
            "severity": self.severity,
            "check": self.check,
            "source": self.source,
            "count": len(self.issues),
            "description": self.description,
            "examples": [_example_text(issue) for issue in self.issues[:max_examples]],
        }


def _example_text(issue: Issue) -> str:
    """Return the one-line example form of an issue.

    Args:
        issue: The issue to render.

    Returns:
        ``"<spectrum id>: <detail>"`` for per-spectrum issues with a detail,
        otherwise the full message.
    """
    if issue.spectrum_id is not None and issue.detail:
        return f"{issue.spectrum_id}: {issue.detail}"
    return issue.message


def _source_of(spectrum_id: str | None) -> str | None:
    """Return the collection prefix of a spectrum id.

    Args:
        spectrum_id: A ``{collection}:{local_id}`` id, or ``None``.

    Returns:
        The prefix before the first colon, or ``None``.
    """
    if spectrum_id is None or ":" not in spectrum_id:
        return None
    return spectrum_id.split(":", 1)[0]


@dataclass
class ValidationResult:
    """Result of a validation run.

    ``errors`` and ``warnings`` hold the full message of every issue;
    ``issues`` holds the same findings with their check name, source and
    spectrum id for aggregation and machine-readable reports. ``waived``
    holds issues suppressed by the allowlist. ``reports`` holds the
    conformance report of every top-level file checked.
    """

    profile: str = DEFAULT_PROFILE
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    waived: list[Issue] = field(default_factory=list)
    reports: list[ConformanceReport] = field(default_factory=list)
    files_checked: int = 0
    spectra_checked: int = 0
    elapsed_seconds: float | None = None
    allowlist_files: list[str] = field(default_factory=list)
    allowlist_entry_count: int = 0

    @property
    def is_valid(self) -> bool:
        """Return ``True`` if no errors were recorded."""
        return len(self.errors) == 0

    def add(
        self,
        severity: Severity,
        check: str,
        message: str,
        *,
        spectrum_id: str | None = None,
        source: str | None = None,
        detail: str | None = None,
        rule: str | None = None,
        file: str | None = None,
    ) -> Issue:
        """Record an issue.

        Args:
            severity: ``"error"`` or ``"warning"``.
            check: Check name (a key of ``CHECK_DESCRIPTIONS``).
            message: Full human-readable message.
            spectrum_id: Spectrum concerned, if any.
            source: Collection id; inferred from ``spectrum_id`` if omitted.
            detail: Short detail for aggregated example lines.
            rule: Specification rule the check implements.
            file: File the issue was found in.

        Returns:
            The recorded ``Issue``.
        """
        issue = Issue(
            severity=severity,
            check=check,
            message=message,
            source=source if source is not None else _source_of(spectrum_id),
            spectrum_id=spectrum_id,
            detail=detail,
            rule=rule if rule is not None else QUALITY_RULES.get(check),
            file=file,
        )
        self.issues.append(issue)
        (self.errors if severity == "error" else self.warnings).append(message)
        return issue

    def add_error(self, check: str, message: str, **kwargs: Any) -> Issue:
        """Record an error (see :meth:`add` for keyword arguments).

        Args:
            check: Check name.
            message: Full human-readable message.
            **kwargs: Keyword arguments of :meth:`add`.

        Returns:
            The recorded ``Issue``.
        """
        return self.add("error", check, message, **kwargs)

    def add_warning(self, check: str, message: str, **kwargs: Any) -> Issue:
        """Record a warning (see :meth:`add` for keyword arguments).

        Args:
            check: Check name.
            message: Full human-readable message.
            **kwargs: Keyword arguments of :meth:`add`.

        Returns:
            The recorded ``Issue``.
        """
        return self.add("warning", check, message, **kwargs)

    def all_issues(self) -> list[Issue]:
        """Return every issue, including plain strings added to errors/warnings.

        Messages appended directly to ``errors``/``warnings`` (rather than via
        :meth:`add`) are wrapped as issues with check ``"other"``.

        Returns:
            All issues, structured ones first.
        """
        issues = list(self.issues)
        plain: tuple[tuple[Severity, list[str]], ...] = (
            ("error", self.errors),
            ("warning", self.warnings),
        )
        for severity, messages in plain:
            structured = Counter(i.message for i in self.issues if i.severity == severity)
            for message in messages:
                if structured[message] > 0:
                    structured[message] -= 1
                else:
                    issues.append(Issue(severity=severity, check="other", message=message))
        return issues

    def groups(self) -> list[IssueGroup]:
        """Aggregate issues by (severity, check, source).

        Returns:
            Groups ordered errors first, then by descending size.
        """
        by_key: dict[tuple[str, str, str], IssueGroup] = {}
        for issue in self.all_issues():
            key = (issue.severity, issue.check, issue.source or "")
            group = by_key.get(key)
            if group is None:
                group = IssueGroup(issue.severity, issue.check, issue.source, [])
                by_key[key] = group
            group.issues.append(issue)
        return sorted(
            by_key.values(),
            key=lambda g: (g.severity != "error", -len(g.issues), g.check, g.source or ""),
        )

    def summary(self, max_examples: int = 5) -> str:
        """Return an aggregated human-readable summary of validation results.

        Repeated issues are collapsed by check and source with a count and up
        to ``max_examples`` example lines; use :meth:`detailed_summary` or
        :meth:`to_dict` for every individual issue.

        Args:
            max_examples: Example lines shown per group.

        Returns:
            The summary text.
        """
        n_errors, n_warnings = len(self.errors), len(self.warnings)
        lines: list[str] = []
        parts = []
        if self.files_checked:
            parts.append(f"{self.files_checked:,} file(s)")
        if self.spectra_checked:
            parts.append(f"{self.spectra_checked:,} spectra")
        scope = ", ".join(parts)
        if self.elapsed_seconds is not None:
            scope = f"{scope or 'checked'} in {self.elapsed_seconds:.1f}s"
        scope = f" (profile {self.profile}; {scope})" if scope else f" (profile {self.profile})"
        if not self.is_valid:
            lines.append(
                f"Validation FAILED: {n_errors:,} error(s), {n_warnings:,} warning(s){scope}"
            )
        elif n_warnings:
            lines.append(f"Validation passed with {n_warnings:,} warning(s){scope}")
        else:
            lines.append(f"Validation passed with no issues.{scope}")

        groups = self.groups()
        for severity, label in (("error", "Errors"), ("warning", "Warnings")):
            selected = [g for g in groups if g.severity == severity]
            if not selected:
                continue
            total = sum(len(g.issues) for g in selected)
            lines.append("")
            lines.append(f"{label} ({total:,} in {len(selected)} group(s)):")
            for group in selected:
                lines.extend(_format_group(group, max_examples))

        if self.allowlist_files or self.waived:
            lines.append("")
            lines.append(
                f"Allowlist: {self.allowlist_entry_count} entr"
                f"{'y' if self.allowlist_entry_count == 1 else 'ies'} from "
                f"{', '.join(self.allowlist_files) or 'no files'}; "
                f"{len(self.waived):,} issue(s) waived"
            )
            waived_counts = Counter((i.check, i.source or "-") for i in self.waived)
            for (check, source), count in sorted(waived_counts.items()):
                lines.append(f"  [{check}] {source}: {count:,}")
        return "\n".join(lines)

    def detailed_summary(self) -> str:
        """Return every issue on its own line (no aggregation).

        Returns:
            The full listing of errors, warnings and waived issues.
        """
        lines = [self.summary(max_examples=0).splitlines()[0]]
        issues = self.all_issues()
        for severity, label in (("error", "ERROR"), ("warning", "WARN")):
            for issue in issues:
                if issue.severity == severity:
                    lines.append(f"  {label} [{issue.check}]: {issue.message}")
        for issue in self.waived:
            lines.append(f"  WAIVED [{issue.check}]: {issue.message}")
        return "\n".join(lines)

    def to_dict(self, max_examples: int = 5) -> dict[str, Any]:
        """Return a machine-readable report of the run.

        Args:
            max_examples: Example lines included per group.

        Returns:
            A JSON-serialisable dict with counts, groups, every issue, every
            waived issue and the conformance report of every file.
        """
        return {
            "valid": self.is_valid,
            "profile": self.profile,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "files_checked": self.files_checked,
            "spectra_checked": self.spectra_checked,
            "elapsed_seconds": self.elapsed_seconds,
            "allowlist": {
                "files": self.allowlist_files,
                "entry_count": self.allowlist_entry_count,
                "waived_count": len(self.waived),
            },
            "groups": [g.to_dict(max_examples) for g in self.groups()],
            "issues": [i.to_dict() for i in self.all_issues()],
            "waived": [i.to_dict() for i in self.waived],
            "conformance": [_summarise_report(r) for r in self.reports],
        }


def _summarise_report(report: ConformanceReport) -> dict[str, Any]:
    """Return the verdict of a conformance report without its findings.

    Args:
        report: A conformance report.

    Returns:
        Location, kind, verdict, failed/warned/skipped rules and nested
        collection verdicts.
    """
    return {
        "location": report.location,
        "kind": report.kind,
        "collection_id": report.collection_id,
        "num_rows": report.num_rows,
        "conformant": report.conformant,
        "failed": report.failed,
        "warned": report.warned,
        "skipped": report.skipped,
        "collections": [_summarise_report(c) for c in report.collections],
    }


def _format_group(group: IssueGroup, max_examples: int) -> list[str]:
    """Render one aggregated issue group.

    Args:
        group: The group to render.
        max_examples: Maximum number of example lines.

    Returns:
        Output lines for the group.
    """
    source = f" {group.source}" if group.source else ""
    count = len(group.issues)
    if count == 1 and group.issues[0].spectrum_id is None:
        return [f"  [{group.check}]{source}: {group.issues[0].message}"]
    lines = [f"  [{group.check}]{source}: {count:,} — {group.description}"]
    for issue in group.issues[:max_examples]:
        lines.append(f"      {_example_text(issue)}")
    if count > max_examples > 0:
        lines.append(f"      … and {count - max_examples:,} more")
    return lines


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

_COLLECTION_ID = re.compile(r"[a-z0-9][a-z0-9_]{1,62}")


class AllowlistBounds(BaseModel):
    """Replacement hard bounds for the ``value_out_of_bounds`` check."""

    model_config = ConfigDict(extra="forbid")

    min: Optional[float] = Field(
        default=None, description="Hard lower bound replacing the quantity default."
    )
    max: Optional[float] = Field(
        default=None, description="Hard upper bound replacing the quantity default."
    )


class AllowlistEntry(BaseModel):
    """One allowlist entry: which spectra, which checks, and why.

    Exactly one selector (``id``, ``id_prefix`` or ``dataset``) must be set,
    and each starts with a collection id and a colon: ``id`` is a Format v1
    spectrum id (``{collection}:{local_id}``), ``id_prefix`` a prefix of one,
    and ``dataset`` is ``{collection}:{dataset_id}``. ``quantity`` (formerly
    ``technique``, still accepted) optionally narrows the selector. With
    ``bounds``, the ``value_out_of_bounds`` check is re-run against the
    replacement bounds instead of being waived outright, so a later rescale
    still fails. ``value_scale`` may only be waived with an exact ``id``;
    conformance rules are never waivable.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: Optional[str] = Field(default=None, description="Exact spectrum id.")
    id_prefix: Optional[str] = Field(default=None, description="Spectrum id prefix.")
    dataset: Optional[str] = Field(
        default=None, description="Dataset key ``{collection}:{dataset_id}``."
    )
    quantity: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("quantity", "technique"),
        description="Only match spectra with this `quantity`.",
    )
    checks: list[str] = Field(min_length=1, description="Check names waived by this entry.")
    bounds: Optional[AllowlistBounds] = Field(
        default=None, description="Replacement hard bounds for the value_out_of_bounds check."
    )
    reason: str = Field(description="Human-readable justification (required).")

    @model_validator(mode="after")
    def _validate_entry(self) -> AllowlistEntry:
        """Check selector, quantity, check names, bounds and reason are coherent.

        Returns:
            The validated entry.

        Raises:
            ValueError: If the entry is malformed.
        """
        selectors = [s for s in (self.id, self.id_prefix, self.dataset) if s]
        if len(selectors) != 1:
            raise ValueError("exactly one of 'id', 'id_prefix' or 'dataset' must be set")
        collection, colon, rest = selectors[0].partition(":")
        if not colon or not _COLLECTION_ID.fullmatch(collection):
            raise ValueError(
                f"selector '{selectors[0]}' must start with a collection id and a colon "
                f"(collection ids match {_COLLECTION_ID.pattern})"
            )
        if not rest:
            raise ValueError(
                f"selector '{selectors[0]}' has nothing after the collection id (an entry "
                f"can never waive a whole collection)"
            )
        quantities = conformance.load_qa_bounds()
        if self.quantity is not None and self.quantity not in quantities:
            raise ValueError(
                f"unknown quantity '{self.quantity}' (one of {', '.join(sorted(quantities))})"
            )
        allowed = WAIVABLE_CHECKS | (ID_ONLY_WAIVABLE_CHECKS if self.id else frozenset())
        unknown = sorted(set(self.checks) - allowed)
        if unknown:
            id_only = sorted(set(unknown) & ID_ONLY_WAIVABLE_CHECKS)
            hint = f" ({id_only} may only be waived with an exact 'id')" if id_only else ""
            raise ValueError(
                f"unknown or non-waivable check(s) {unknown}{hint}; waivable checks are "
                f"{sorted(WAIVABLE_CHECKS)} plus {sorted(ID_ONLY_WAIVABLE_CHECKS)} by exact id"
            )
        if self.bounds is not None:
            if "value_out_of_bounds" not in self.checks:
                raise ValueError("'bounds' is only meaningful with the 'value_out_of_bounds' check")
            lo, hi = self.bounds.min, self.bounds.max
            if lo is None and hi is None:
                raise ValueError("'bounds' must set 'min' and/or 'max'")
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"'bounds' min ({lo}) is greater than max ({hi})")
        if not self.reason.strip():
            raise ValueError("'reason' must be a non-empty explanation")
        return self

    @property
    def collection(self) -> str:
        """Return the collection the selector targets (text before the first colon)."""
        selector = self.id or self.id_prefix or self.dataset or ""
        return selector.split(":", 1)[0]

    @property
    def selector(self) -> str:
        """Return a short human-readable form of the entry's selector."""
        if self.id:
            base = f"id={self.id}"
        elif self.id_prefix:
            base = f"id_prefix={self.id_prefix}"
        else:
            base = f"dataset={self.dataset}"
        return f"{base}, quantity={self.quantity}" if self.quantity else base


class AllowlistFile(BaseModel):
    """Top-level structure of an allowlist JSON file."""

    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = Field(description="Allowlist file format version.")
    description: Optional[str] = Field(default=None, description="Free-text description.")
    entries: list[AllowlistEntry] = Field(default_factory=list, description="Allowlist entries.")


@dataclass
class _LoadedEntry:
    """An allowlist entry with its origin file and usage flag."""

    entry: AllowlistEntry
    origin: str
    order: int
    used: bool = False


class Allowlist:
    """In-memory allowlist with lookup by spectrum and stale-entry tracking."""

    def __init__(self) -> None:
        """Initialise an empty allowlist."""
        self.files: list[str] = []
        self._entries: list[_LoadedEntry] = []
        self._by_id: dict[str, list[_LoadedEntry]] = {}
        self._by_dataset: dict[str, list[_LoadedEntry]] = {}
        self._prefixed: list[_LoadedEntry] = []

    def __len__(self) -> int:
        """Return the number of entries."""
        return len(self._entries)

    @classmethod
    def load(cls, paths: Iterable[Path] = (), include_default: bool = True) -> Allowlist:
        """Load the packaged allowlist and/or additional allowlist files.

        Args:
            paths: Extra allowlist files, applied after the default. On equal
                specificity a later file's entry wins.
            include_default: Whether to load ``DEFAULT_ALLOWLIST_PATH``.

        Returns:
            The combined allowlist.

        Raises:
            ValueError: If a file cannot be read or is malformed.
        """
        allowlist = cls()
        all_paths = ([DEFAULT_ALLOWLIST_PATH] if include_default else []) + list(paths)
        for path in all_paths:
            allowlist.add_file(path)
        return allowlist

    def add_file(self, path: Path) -> None:
        """Parse an allowlist file and add its entries.

        Args:
            path: Path to an allowlist JSON file.

        Raises:
            ValueError: If the file cannot be read or is malformed.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            parsed = AllowlistFile.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as e:
            raise ValueError(f"Invalid allowlist file {path}: {e}") from e
        origin = Path(path).name
        self.files.append(str(path))
        for entry in parsed.entries:
            self.add_entry(entry, origin=origin)

    def add_entry(self, entry: AllowlistEntry, origin: str = "<inline>") -> None:
        """Add a single entry.

        Args:
            entry: The entry to add.
            origin: Where the entry came from (for stale-entry messages).
        """
        loaded = _LoadedEntry(entry=entry, origin=origin, order=len(self._entries))
        self._entries.append(loaded)
        if entry.id:
            self._by_id.setdefault(entry.id, []).append(loaded)
        elif entry.dataset:
            self._by_dataset.setdefault(entry.dataset, []).append(loaded)
        else:
            self._prefixed.append(loaded)

    def match(
        self,
        spectrum_id: str,
        dataset_key: str | None,
        quantity: str,
        check: str,
    ) -> AllowlistEntry | None:
        """Find the most specific entry waiving ``check`` for a spectrum.

        Specificity is exact id, then the longest matching id prefix, then
        dataset; ties go to the most recently loaded entry. A match marks the
        entry as used (so it is not reported stale).

        Args:
            spectrum_id: The spectrum id.
            dataset_key: The spectrum's ``{collection}:{dataset_id}``, if any.
            quantity: The spectrum's ``quantity``.
            check: The check about to be reported.

        Returns:
            The matching entry, or ``None``.
        """
        if not self._entries:
            return None

        def _applies(loaded: _LoadedEntry) -> bool:
            e = loaded.entry
            return check in e.checks and (e.quantity is None or e.quantity == quantity)

        candidates = [e for e in self._by_id.get(spectrum_id, []) if _applies(e)]
        if not candidates:
            prefixed = [
                e
                for e in self._prefixed
                if spectrum_id.startswith(e.entry.id_prefix or "\0") and _applies(e)
            ]
            if prefixed:
                longest = max(len(e.entry.id_prefix or "") for e in prefixed)
                candidates = [e for e in prefixed if len(e.entry.id_prefix or "") == longest]
        if not candidates and dataset_key is not None:
            candidates = [e for e in self._by_dataset.get(dataset_key, []) if _applies(e)]
        if not candidates:
            return None
        best = max(candidates, key=lambda e: e.order)
        best.used = True
        return best.entry

    def stale_entries(self) -> list[tuple[AllowlistEntry, str]]:
        """Return entries that never matched an issue.

        Returns:
            ``(entry, origin)`` pairs, in load order.
        """
        return [(e.entry, e.origin) for e in self._entries if not e.used]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate(
    path: str | os.PathLike[str],
    profile: str = DEFAULT_PROFILE,
    allowlist: Allowlist | None = None,
) -> ValidationResult:
    """Validate a Format v1 collection file, index file, or directory of them.

    A directory is searched recursively for ``*.parquet`` files. Index files
    are checked first, together with the collection files they list; the
    remaining collection files are then checked on their own. When a
    directory holds an index, collection files no index lists are reported
    (``unindexed_file`` warning). Directories that cannot be listed, and files
    that cannot be read or checked, are ``unreadable_path`` errors; the rest
    of the directory is still checked.

    Args:
        path: File or directory to validate.
        profile: ``format`` (conformance only, the default) or ``openspeclib``
            (conformance plus the quality profile Q1–Q4).
        allowlist: Allowlist for the quality profile. ``None`` loads the
            allowlist shipped with the package. Ignored by ``format``.

    Returns:
        ValidationResult with every error and warning found.

    Raises:
        ValueError: If ``profile`` is unknown.
        FileNotFoundError: If ``path`` does not exist.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile '{profile}' (one of {', '.join(PROFILES)})")
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(str(root))
    started = time.perf_counter()
    result = ValidationResult(profile=profile)
    quality = profile == "openspeclib"
    if quality and allowlist is None:
        allowlist = Allowlist.load()
    if quality and allowlist is not None:
        result.allowlist_files = [Path(f).name for f in allowlist.files]
        result.allowlist_entry_count = len(allowlist)

    collections = _run_conformance(root, result)
    if quality and allowlist is not None:
        checker = _QualityChecker(allowlist, result)
        for location, report in collections:
            checker.check_file(location, report, _display(root, location))
        checker.finish()
        _report_stale_entries(allowlist, {r.collection_id for _, r in collections}, result)
    result.elapsed_seconds = time.perf_counter() - started
    return result


def _display(root: Path, location: str) -> str:
    """Return a file location relative to the validated path, for messages.

    Args:
        root: The path given to :func:`validate`.
        location: A file path.

    Returns:
        The path relative to ``root`` (or its name when ``root`` is a file).
    """
    base = root if root.is_dir() else root.parent
    try:
        return str(Path(location).resolve().relative_to(base.resolve()))
    except ValueError:
        return location


def _find_parquet(root: Path, result: ValidationResult) -> list[Path]:
    """Return every ``*.parquet`` path under a directory, reporting unreadable directories.

    Symbolic links to directories are not followed (as with
    :meth:`pathlib.Path.rglob`). Each directory that cannot be listed is an
    ``unreadable_path`` error: it may hold files that would otherwise go
    unchecked without a trace.

    Args:
        root: The directory to search.
        result: Validation result to append to.

    Returns:
        The matching paths (files, links and directories), sorted.
    """
    found: list[Path] = []

    def unreadable(error: OSError) -> None:
        where = Path(error.filename) if error.filename else root
        try:
            shown = str(where.relative_to(root))
        except ValueError:
            shown = str(where)
        reason = error.strerror or str(error)
        result.add_error(
            "unreadable_path", f"{shown}: directory cannot be read: {reason}", file=shown
        )

    for directory, dirnames, filenames in os.walk(root, onerror=unreadable):
        for name in dirnames + filenames:
            if fnmatch.fnmatchcase(name, "*.parquet"):
                found.append(Path(directory) / name)
    return sorted(found)


def _unexpected(result: ValidationResult, shown: str, error: Exception) -> None:
    """Record a file the checker could not process as an ``unreadable_path`` error.

    Args:
        result: Validation result to append to.
        shown: The file's display name.
        error: The exception raised while checking it.
    """
    result.add_error(
        "unreadable_path",
        f"{shown}: cannot be checked: {type(error).__name__}: {error}",
        file=shown,
    )


def _run_conformance(root: Path, result: ValidationResult) -> list[tuple[str, ConformanceReport]]:
    """Check every file under ``root`` for conformance and record the issues.

    Args:
        root: File or directory to validate.
        result: Validation result to append to.

    Returns:
        ``(location, report)`` for every readable collection file checked,
        directly or through an index, without duplicates.
    """
    candidates = _find_parquet(root, result) if root.is_dir() else [root]
    files: list[Path] = []
    kinds: dict[Path, bool | None] = {}
    shown_names: dict[Path, str] = {}
    for candidate in candidates:
        # As walked, not resolved: a broken link is named by its own path.
        shown = str(candidate.relative_to(root)) if root.is_dir() else candidate.name
        shown_names[candidate] = shown
        if candidate.is_dir():
            if root.is_dir():  # its contents are part of the walk anyway
                result.add_error(
                    "unreadable_path", f"{shown}: a directory, not a Parquet file", file=shown
                )
            continue
        try:
            kinds[candidate] = conformance.is_index_file(candidate)
        except OSError as exc:
            reason = "a broken symbolic link" if candidate.is_symlink() else str(exc)
            result.add_error("unreadable_path", f"{shown}: cannot be read: {reason}", file=shown)
            continue
        except Exception as exc:  # any failure is this file's error; the walk goes on
            _unexpected(result, shown, exc)
            continue
        files.append(candidate)
    if not files and not result.errors:
        result.add_error("no_files", f"No .parquet files found under {root}")
    if not files:
        return []
    collections: dict[str, tuple[str, ConformanceReport]] = {}
    listed: set[str] = set()
    recorded: set[str] = set()
    for file in [f for f in files if kinds[f]]:
        try:
            report = conformance.check_index(file, max_examples=None)
        except Exception as exc:  # any failure is this file's error; the walk goes on
            _unexpected(result, shown_names[file], exc)
            continue
        result.reports.append(report)
        _record_report(report, root, result, recorded)
        for sub in report.collections:
            key = str(Path(sub.location).resolve())
            listed.add(key)
            collections.setdefault(key, (sub.location, sub))
    for file in [f for f in files if not kinds[f]]:
        key = str(file.resolve())
        if key in collections:
            continue
        try:
            report = conformance.check_collection(file, max_examples=None)
        except Exception as exc:  # any failure is this file's error; the walk goes on
            _unexpected(result, shown_names[file], exc)
            continue
        result.reports.append(report)
        _record_report(report, root, result, recorded)
        collections[key] = (str(file), report)
        if listed and kinds[file] is False:
            result.add_warning(
                "unindexed_file",
                f"{_display(root, str(file))}: collection file is not listed by any index "
                f"file in {root}",
                source=report.collection_id,
                file=_display(root, str(file)),
            )
    result.files_checked += len(recorded)
    readable = [
        (location, report)
        for location, report in collections.values()
        if report.num_rows is not None
    ]
    result.spectra_checked += sum(r.num_rows or 0 for _, r in readable)
    return readable


def _record_report(
    report: ConformanceReport, root: Path, result: ValidationResult, recorded: set[str]
) -> None:
    """Turn a conformance report (and its nested reports) into issues.

    Must-rule findings become errors and should-rule findings warnings; each
    finding is one issue. Nested collection reports of an index are recorded
    too, each file once even when several indexes list it.

    Args:
        report: The conformance report.
        root: The validated path (for relative file names).
        result: Validation result to append to.
        recorded: Resolved paths of the files recorded so far; updated.
    """
    key = str(Path(report.location).resolve())
    if key in recorded:
        return
    recorded.add(key)
    file = _display(root, report.location)
    # Issues in an index file belong to the index, even for rows of a collection.
    source = file if report.kind == "index" else report.collection_id or file
    for rule in report.results:
        for warning in rule.warnings:
            # Forward compatibility (spec section 11): unknown to this version.
            where = f"spectrum '{warning.row_id}': " if warning.row_id else ""
            result.add(
                "warning",
                rule.rule_id,
                f"{file}: [{rule.rule_id} {rule.title}] {where}{warning.detail}",
                spectrum_id=warning.row_id,
                source=source,
                detail=warning.detail if warning.row_id else f"{file}: {warning.detail}",
                rule=rule.rule_id,
                file=file,
            )
        if rule.status not in ("failed", "warned"):
            continue
        severity: Severity = "error" if rule.status == "failed" else "warning"
        for finding in rule.findings:
            where = f"spectrum '{finding.row_id}': " if finding.row_id else ""
            result.add(
                severity,
                rule.rule_id,
                f"{file}: [{rule.rule_id} {rule.title}] {where}{finding.detail}",
                spectrum_id=finding.row_id,
                source=source,
                detail=finding.detail if finding.row_id else f"{file}: {finding.detail}",
                rule=rule.rule_id,
                file=file,
            )
    for sub in report.collections:
        _record_report(sub, root, result, recorded)


def _report_stale_entries(
    allowlist: Allowlist, collections: set[str | None], result: ValidationResult
) -> None:
    """Warn about allowlist entries that matched nothing in the validated collections.

    Entries for collections that were not validated could never have matched,
    so they are not reported.

    Args:
        allowlist: The allowlist used.
        collections: Collection ids of the files checked.
        result: Validation result to append to.
    """
    for entry, origin in allowlist.stale_entries():
        if entry.collection not in collections:
            continue
        result.add_warning(
            "stale_allowlist_entry",
            f"Allowlist entry ({entry.selector}; checks={entry.checks}) from {origin} "
            f"did not match any issue — remove it or update it if the data was fixed",
            source=entry.collection,
            detail=f"{origin}: {entry.selector}",
        )


# ---------------------------------------------------------------------------
# Quality profile (Q1–Q3)
# ---------------------------------------------------------------------------

_QUALITY_COLUMNS = {
    "id": "string",
    "quantity": "string",
    "values": "list<float32>",
    "dataset_id": "string",
}
_EXTENT_COLUMNS = {"wavelength_min_nm": "float64", "wavelength_max_nm": "float64"}


@dataclass(frozen=True)
class _SpectrumContext:
    """Identity of a spectrum for allowlist lookups and messages."""

    spectrum_id: str
    dataset_key: str | None
    quantity: str
    collection: str
    file: str


def _exact_medians(rows: pa.Array, values: pa.Array, group_rows: pa.Array) -> pa.Array:
    """Return the exact median of the valid values of each group.

    Args:
        rows: Group (row) index of each value.
        values: The values; null marks a value that is not valid.
        group_rows: The groups to return medians for, in order.

    Returns:
        One float64 median per entry of ``group_rows``: the middle value, or
        the mean of the two middle values; null where a group has no valid
        value.
    """
    row = np.asarray(rows.to_numpy(zero_copy_only=False), dtype=np.int64)
    value = np.asarray(
        pc.fill_null(values, np.nan).to_numpy(zero_copy_only=False), dtype=np.float64
    )
    keep = ~np.isnan(value)
    row, value = row[keep], value[keep]
    order = np.lexsort((value, row))
    row, value = row[order], value[order]
    groups, starts, counts = np.unique(row, return_index=True, return_counts=True)
    middle = (value[starts + (counts - 1) // 2] + value[starts + counts // 2]) / 2
    wanted = np.asarray(group_rows.to_numpy(zero_copy_only=False), dtype=np.int64)
    if len(groups) == 0:
        return pa.nulls(len(wanted), pa.float64())
    position = np.clip(np.searchsorted(groups, wanted), 0, len(groups) - 1)
    found = groups[position] == wanted
    return pa.array(np.where(found, middle[position], np.nan), pa.float64(), mask=~found)


class _QualityChecker:
    """Applies Q1–Q4 and the bounds-share warning to collection files."""

    def __init__(self, allowlist: Allowlist, result: ValidationResult) -> None:
        self.allowlist = allowlist
        self.result = result
        self.bounds = quality_bounds()
        self.extent = conformance.load_qa_wavelength_nm()
        quantities = sorted(self.bounds)
        self.quantities = pa.array(quantities, pa.string())
        self.hard_min = pa.array([self.bounds[q].hard_min for q in quantities], pa.float32())
        self.hard_max = pa.array([self.bounds[q].hard_max for q in quantities], pa.float32())
        self.select_min = pa.array(
            [max(self.bounds[q].warn_min, self.bounds[q].hard_min) for q in quantities],
            pa.float32(),
        )
        self.select_max = pa.array(
            [min(self.bounds[q].warn_max, self.bounds[q].hard_max) for q in quantities],
            pa.float32(),
        )
        # [spectra, spectra failing Q1, Q2 or Q4] per (collection, dataset key
        # or None for the whole collection, quantity).
        self.tally: dict[tuple[str, str | None, str], list[int]] = {}

    def check_file(self, location: str, report: ConformanceReport, file: str) -> None:
        """Run the quality checks on one collection file.

        Args:
            location: Path of the collection file.
            report: Its conformance report.
            file: Display name of the file.
        """
        collection = report.collection_id or file
        try:
            with pq.ParquetFile(location) as parquet_file:
                self._check_parquet(parquet_file, collection, file)
        except (pa.ArrowException, OSError, UnicodeDecodeError) as exc:
            self.result.add_warning(
                "quality_skipped",
                f"{file}: quality profile not evaluated: data not readable ({exc}; see F1/F2)",
                source=collection,
                file=file,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            _unexpected(self.result, file, exc)

    def _check_parquet(self, parquet_file: pq.ParquetFile, collection: str, file: str) -> None:
        """Run the quality checks on an open collection file.

        Args:
            parquet_file: The open file.
            collection: Collection id of the file.
            file: Display name of the file.
        """
        schema = parquet_file.schema_arrow
        names = schema.names

        def usable(name: str, spec_type: str) -> bool:
            return names.count(name) == 1 and conformance.type_matches(
                schema.field(name).type, spec_type, nullable=name == "dataset_id"
            )

        problems = [n for n, t in _QUALITY_COLUMNS.items() if not usable(n, t)]
        if problems:
            self.result.add_warning(
                "quality_skipped",
                f"{file}: quality profile not evaluated: column(s) "
                f"{', '.join(problems)} missing or mistyped (see F2)",
                source=collection,
                file=file,
            )
            return
        columns = list(_QUALITY_COLUMNS)
        extent = all(usable(n, t) for n, t in _EXTENT_COLUMNS.items())
        if extent:
            columns += list(_EXTENT_COLUMNS)
        else:
            self.result.add_warning(
                "quality_skipped",
                f"{file}: Q4 not evaluated: wavelength_min_nm/wavelength_max_nm missing or "
                f"mistyped (see F2)",
                source=collection,
                file=file,
            )
        for batch in parquet_file.iter_batches(batch_size=_BATCH_SIZE, columns=columns):
            self._check_batch(batch, collection, file)

    def _check_batch(self, batch: pa.RecordBatch, collection: str, file: str) -> None:
        """Evaluate one record batch.

        Args:
            batch: Columns ``id``, ``quantity``, ``values``, ``dataset_id`` and,
                when usable, ``wavelength_min_nm`` and ``wavelength_max_nm``.
            collection: Collection id of the file.
            file: Display name of the file.
        """
        ids = pc.cast(batch.column("id"), pa.string())
        quantity = pc.cast(batch.column("quantity"), pa.string())
        dataset_ids = pc.cast(batch.column("dataset_id"), pa.string())
        self._count_spectra(quantity, dataset_ids, collection)

        def context(row: int) -> _SpectrumContext:
            spectrum_id = ids[row].as_py()
            dataset_id = dataset_ids[row].as_py()
            return _SpectrumContext(
                spectrum_id=spectrum_id if spectrum_id is not None else f"row {row}",
                dataset_key=f"{collection}:{dataset_id}" if dataset_id else None,
                quantity=quantity[row].as_py() or "",
                collection=collection,
                file=file,
            )

        failed: set[int] = set()
        if "wavelength_min_nm" in batch.schema.names:
            failed |= self._check_extent(batch, context)
        failed |= self._check_values(batch, quantity, context)
        for row in sorted(failed):
            ctx = context(row)
            if ctx.quantity in self.bounds:
                for key in self._tally_keys(ctx):
                    self.tally[key][1] += 1

    def _check_extent(
        self, batch: pa.RecordBatch, context: Callable[[int], _SpectrumContext]
    ) -> set[int]:
        """Q4: the wavelength extent lies within the plausible range.

        Args:
            batch: The record batch.
            context: Builds the context of a row.

        Returns:
            Rows with an unwaived Q4 error.
        """
        low, high = self.extent
        lo_nm = pc.cast(batch.column("wavelength_min_nm"), pa.float64())
        hi_nm = pc.cast(batch.column("wavelength_max_nm"), pa.float64())
        outside = pc.or_(
            pc.fill_null(pc.less(lo_nm, low), False), pc.fill_null(pc.greater(hi_nm, high), False)
        )
        failed: set[int] = set()
        for row in pc.indices_nonzero(outside).to_pylist():
            ctx = context(row)
            lo_value, hi_value = lo_nm[row].as_py(), hi_nm[row].as_py()
            message = (
                f"Spectrum '{ctx.spectrum_id}': wavelength extent [{lo_value:g}, {hi_value:g}] "
                f"nm is outside the plausible range [{low:g}, {high:g}] nm — almost always a "
                f"mislabelled wavelength_unit (e.g. micrometres labelled nm)"
            )
            detail = f"extent [{lo_value:g}, {hi_value:g}] nm; plausible [{low:g}, {high:g}]"
            entry = self.allowlist.match(ctx.spectrum_id, None, ctx.quantity, "wavelength_extent")
            if entry is not None:
                self._waive(ctx, "error", "wavelength_extent", message, detail, entry.reason)
                continue
            self.result.add_error(
                "wavelength_extent",
                message,
                spectrum_id=ctx.spectrum_id,
                source=ctx.collection,
                detail=detail,
                file=ctx.file,
            )
            failed.add(row)
        return failed

    def _check_values(
        self,
        batch: pa.RecordBatch,
        quantity: pa.Array,
        context: Callable[[int], _SpectrumContext],
    ) -> set[int]:
        """Q1–Q3 on one record batch.

        Args:
            batch: The record batch.
            quantity: The batch's ``quantity`` column as strings.
            context: Builds the context of a row.

        Returns:
            Rows with an unwaived Q1 or Q2 error.
        """
        values = batch.column("values")
        if values.type != pa.list_(pa.float32()):
            values = pc.cast(values, pa.list_(pa.float32()))
        kind = pc.index_in(quantity, value_set=self.quantities)
        flat = values.flatten()
        if len(flat) == 0:
            return set()
        parents = pc.list_parent_indices(values)
        element_kind = pc.take(kind, parents)
        valid = pc.and_(
            pc.fill_null(pc.is_finite(flat), False),
            pc.fill_null(pc.less_equal(pc.abs(flat), SENTINEL_ABS_THRESHOLD), False),
        )

        # Candidate spectra: a valid value outside the warning bounds (or the
        # hard bounds, were a warning band ever wider). A median outside the
        # hard bounds implies such values, so no spectrum that can produce an
        # issue is missed; only candidates go through the (costly) grouped
        # min/max/median below. Per-row counts come from cumulative sums.
        outside = pc.or_(
            pc.less(flat, pc.take(self.select_min, element_kind)),
            pc.greater(flat, pc.take(self.select_max, element_kind)),
        )
        outside = pc.cast(pc.and_(valid, pc.fill_null(outside, False)), pa.int64())
        lengths = pc.cast(pc.fill_null(pc.list_value_length(values), 0), pa.int64())
        ends = pc.cumulative_sum(lengths)
        padded = pa.concat_arrays([pa.array([0], pa.int64()), pc.cumulative_sum(outside)])
        counts = pc.subtract(pc.take(padded, ends), pc.take(padded, pc.subtract(ends, lengths)))
        candidate = pc.greater(counts, 0)
        if not pc.any(candidate).as_py():
            return set()
        keep = pc.take(candidate, parents)
        flat, parents, valid = flat.filter(keep), parents.filter(keep), valid.filter(keep)
        element_kind = element_kind.filter(keep)

        masked = pc.if_else(valid, flat, pa.scalar(None, pa.float32()))
        below = pc.fill_null(pc.less(masked, pc.take(self.hard_min, element_kind)), False)
        above = pc.fill_null(pc.greater(masked, pc.take(self.hard_max, element_kind)), False)
        grouped = (
            pa.table(
                {
                    "row": parents,
                    "v": masked,
                    "below": pc.cast(below, pa.int64()),
                    "above": pc.cast(above, pa.int64()),
                    "valid": pc.cast(valid, pa.int64()),
                }
            )
            .group_by("row", use_threads=False)
            .aggregate(
                [
                    ("v", "min"),
                    ("v", "max"),
                    ("below", "sum"),
                    ("above", "sum"),
                    ("valid", "sum"),
                ]
            )
        )
        group_rows = grouped["row"].combine_chunks()
        lo = grouped["v_min"].combine_chunks()
        hi = grouped["v_max"].combine_chunks()
        median = _exact_medians(parents, masked, group_rows)
        group_kind = pc.take(kind, group_rows)

        def bound(name: str) -> pa.Array:
            table = pa.array([getattr(self.bounds[q], name) for q in self.quantities.to_pylist()])
            return pc.take(table, group_kind)

        # Rows needing a closer look: median outside the hard bounds, or any
        # valid value outside the warning bounds. Comparisons are float32 vs
        # float32-rounded bounds, so a value exactly at a bound passes.
        attention = pc.or_(
            pc.or_(
                pc.less(median, bound("hard_min")),
                pc.greater(median, bound("hard_max")),
            ),
            pc.or_(pc.less(lo, bound("warn_min")), pc.greater(hi, bound("warn_max"))),
        )
        attention = pc.and_(pc.fill_null(attention, False), pc.is_valid(group_kind))
        selected = pc.indices_nonzero(attention)
        picked = {
            "row": pc.take(group_rows, selected).to_pylist(),
            "lo": pc.take(lo, selected).to_pylist(),
            "hi": pc.take(hi, selected).to_pylist(),
            "median": pc.take(median, selected).to_pylist(),
            "below": pc.take(grouped["below_sum"].combine_chunks(), selected).to_pylist(),
            "above": pc.take(grouped["above_sum"].combine_chunks(), selected).to_pylist(),
            "valid": pc.take(grouped["valid_sum"].combine_chunks(), selected).to_pylist(),
        }
        failed: set[int] = set()
        for i, row in enumerate(picked["row"]):
            if self._check_spectrum(
                context(row),
                lo=picked["lo"][i],
                hi=picked["hi"][i],
                median=picked["median"][i],
                n_out=int(picked["below"][i]) + int(picked["above"][i]),
                n_valid=int(picked["valid"][i]),
            ):
                failed.add(row)
        return failed

    def _count_spectra(self, quantity: pa.Array, dataset_ids: pa.Array, collection: str) -> None:
        """Count spectra per quantity and dataset for the bounds-share warning."""
        counts = (
            pa.table({"quantity": quantity, "dataset_id": dataset_ids})
            .group_by(["quantity", "dataset_id"], use_threads=False)
            .aggregate([([], "count_all")])
            .to_pylist()
        )
        for row in counts:
            q = row["quantity"]
            if q not in self.bounds:
                continue
            n = int(row["count_all"])
            self.tally.setdefault((collection, None, q), [0, 0])[0] += n
            if row["dataset_id"]:
                key = (collection, f"{collection}:{row['dataset_id']}", q)
                self.tally.setdefault(key, [0, 0])[0] += n

    @staticmethod
    def _tally_keys(ctx: _SpectrumContext) -> list[tuple[str, str | None, str]]:
        """Return the bounds-share tally keys a spectrum counts towards."""
        keys: list[tuple[str, str | None, str]] = [(ctx.collection, None, ctx.quantity)]
        if ctx.dataset_key is not None:
            keys.append((ctx.collection, ctx.dataset_key, ctx.quantity))
        return keys

    def _waive(
        self,
        ctx: _SpectrumContext,
        severity: Severity,
        check: str,
        message: str,
        detail: str,
        reason: str,
    ) -> None:
        """Record a waived issue."""
        self.result.waived.append(
            Issue(
                severity=severity,
                check=check,
                message=f"{message} [waived: {reason}]",
                source=ctx.collection,
                spectrum_id=ctx.spectrum_id,
                detail=detail,
                rule=QUALITY_RULES[check],
                file=ctx.file,
            )
        )

    def _check_spectrum(
        self,
        ctx: _SpectrumContext,
        *,
        lo: float,
        hi: float,
        median: float | None,
        n_out: int,
        n_valid: int,
    ) -> bool:
        """Apply Q1–Q3 to one spectrum (at most one of them is reported).

        Args:
            ctx: The spectrum concerned.
            lo: Minimum valid value.
            hi: Maximum valid value.
            median: Median of the valid values.
            n_out: Valid values outside the hard bounds.
            n_valid: Number of valid values.

        Returns:
            ``True`` if an unwaived ``value_scale`` or ``value_out_of_bounds``
            error was recorded.
        """
        b = self.bounds[ctx.quantity]
        sid = ctx.spectrum_id
        quantity = ctx.quantity
        hard = f"[{_g(b.hard_min)}, {_g(b.hard_max)}]"
        span = f"values span [{_g(lo)}, {_g(hi)}]"

        # Q1: the spectrum as a whole is off-scale.
        if median is not None and (median > b.hard_max or median < b.hard_min):
            if median > b.hard_max:
                cause = "likely a missed scale conversion (e.g. percent stored as a fraction)"
            else:
                cause = (
                    f"most values are below the hard minimum — not a valid {quantity} spectrum "
                    f"(corrupt or non-spectral data, or a different quantity)"
                )
            message = (
                f"Spectrum '{sid}': median {quantity} value {_g(median)} is outside the hard "
                f"bounds {hard} ({span}) — {cause}"
            )
            detail = f"{quantity} median={median:.4g} (min={lo:.4g} max={hi:.4g}); hard {hard}"
            entry = self.allowlist.match(sid, ctx.dataset_key, quantity, "value_scale")
            if entry is not None:
                self._waive(ctx, "error", "value_scale", message, detail, entry.reason)
                return False
            self.result.add_error(
                "value_scale",
                message,
                spectrum_id=sid,
                source=ctx.collection,
                detail=detail,
                file=ctx.file,
            )
            return True

        # Q2: individual values beyond the hard bounds; Q3 only when Q2 holds.
        is_error = lo < b.hard_min or hi > b.hard_max
        is_warning = lo < b.warn_min or hi > b.warn_max
        if not is_error and not is_warning:
            return False
        detail = f"{quantity} min={lo:.4g} max={hi:.4g} median={median or 0:.3g}"
        if is_error:
            entry = self.allowlist.match(sid, ctx.dataset_key, quantity, "value_out_of_bounds")
            if entry is not None and entry.bounds is not None:
                emin = _f32(entry.bounds.min) if entry.bounds.min is not None else b.hard_min
                emax = _f32(entry.bounds.max) if entry.bounds.max is not None else b.hard_max
                if lo < emin or hi > emax:
                    allowed = f"[{_g(emin)}, {_g(emax)}]"
                    self.result.add_error(
                        "value_out_of_bounds",
                        f"Spectrum '{sid}': {quantity} {span}, outside even the allowlisted "
                        f"bounds {allowed} ({entry.selector})",
                        spectrum_id=sid,
                        source=ctx.collection,
                        detail=f"{detail}; allowlisted bounds {allowed}",
                        file=ctx.file,
                    )
                    return True
            if entry is not None:
                self._waive(
                    ctx,
                    "error",
                    "value_out_of_bounds",
                    f"Spectrum '{sid}': {quantity} {span}",
                    detail,
                    entry.reason,
                )
                return False
            self.result.add_error(
                "value_out_of_bounds",
                f"Spectrum '{sid}': {n_out} of {n_valid} valid {quantity} value(s) lie outside "
                f"the hard bounds {hard} ({span}) while the median ({_g(median or 0.0)}) is in "
                f"range — typically spikes, clipped or corrupt bands (a dark spectrum stored "
                f"in percent can also look like this; see value_bounds_share)",
                spectrum_id=sid,
                source=ctx.collection,
                detail=f"{n_out}/{n_valid} values outside {hard}; {detail}",
                file=ctx.file,
            )
            return True

        soft = f"[{_g(b.warn_min)}, {_g(b.warn_max)}]"
        message = (
            f"Spectrum '{sid}': {quantity} {span}, outside the expected range {soft} "
            f"(within hard bounds {hard})"
        )
        entry = self.allowlist.match(sid, ctx.dataset_key, quantity, "value_range_suspect")
        if entry is not None:
            self._waive(
                ctx,
                "warning",
                "value_range_suspect",
                message,
                f"{detail}; expected {soft}",
                entry.reason,
            )
            return False
        self.result.add_warning(
            "value_range_suspect",
            message,
            spectrum_id=sid,
            source=ctx.collection,
            detail=f"{detail}; expected {soft}",
            file=ctx.file,
        )
        return False

    def finish(self) -> None:
        """Emit the bounds-share warnings once every file has been checked."""
        for (collection, dataset_key, quantity), (total, failed) in sorted(
            self.tally.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2])
        ):
            if total < _SHARE_MIN_SPECTRA or failed / total < _SHARE_THRESHOLD:
                continue
            where = f"dataset '{dataset_key}'" if dataset_key else f"collection '{collection}'"
            scope = f"{quantity} spectra of {where}"
            self.result.add_warning(
                "value_bounds_share",
                f"{failed:,} of {total:,} {scope} ({failed / total:.0%}) fail Q1, Q2 or Q4 "
                f"(reported when at least {_SHARE_MIN_SPECTRA} spectra of one quantity and at "
                f"least {_SHARE_THRESHOLD:.0%} of them fail) — suspect a set-wide scale or "
                f"units problem (dark spectra of a mis-scaled set can still pass "
                f"per-spectrum bounds)",
                source=collection,
                detail=f"{scope}: {failed:,}/{total:,} ({failed / total:.0%})",
            )
