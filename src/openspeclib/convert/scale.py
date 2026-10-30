"""Value-scale resolution for imports: turn percent and scaled integers into fractions.

Format v1 stores reflectance, transmittance and emissivity as fractions
(spec section 4.1), so an importer has to know the scale of its input. A
wrong guess silently corrupts every value by a factor of 100 or 10000, so
this module prefers a loud error with a hint over a guess.

Only reflectance, transmittance and emissivity
(:data:`~openspeclib.convert.common.FRACTION_QUANTITIES`) are ever divided.
Absorbance (decadic, spec section 4.1) and any other quantity always get
divisor 1, whatever the flag or the file says; when a stated scale is left
unapplied to such spectra, the report warns.

The divisor of each fraction spectrum comes from the first of:

1. ``--value-scale``;
2. a re-import of an ``openspeclib export`` (values are already fractions:
   divisor 1);
3. a scale stated by the file (ENVI ``reflectance scale factor``);
4. declared units per spectrum (``Measurement Units``: ``%``, ``0-1``, …);
5. detection from the data.

Detection uses a spectrum's *level*, the 90th percentile of its finite
values (:data:`LEVEL_PERCENTILE`):

* float data: level ≤ 1.5 → fractions; 15–150 → percent; 150–15000 →
  0–10000. Between 1.5 and 15 the data could be bright fractions (a
  reflectance factor of 2) or a dark percent spectrum, so the scale must be
  given. A float file holding *only* dark 0–10000 spectra (reflectance
  below about 1.5 %, level 15–150) is read as percent, 100 times too
  bright. This is accepted by design, and nothing else catches it: the
  scaled values (0.15–1.5) pass the plausibility check below, and only a
  brighter spectrum in the same file would make the scales disagree. So
  whenever percent is detected, the report warns and names
  ``--value-scale 10000`` as the fix.
* integer data: level 150–15000 → 0–10000; at or below 150 it could be
  percent or a dark 0–10000 spectrum, so the scale must be given.
* declared units lose to the data only where they contradict it: a level
  between 0.015 and 1.5 with declared percent or 0–10000 units looks like
  fractions, so the scale must be given.
* every detected spectrum of a file must agree; a mixed-scale file is an
  error naming the rows.

After scaling, every spectrum's median must lie inside the hard
quality-profile bounds of its quantity (``rules.json`` ``qa_bounds``, rule
Q1). A failure is an error when the scale was stated by the file, declared
or detected, and a warning when the user gave it or the file is a
re-import of an export. A failure of a quantity that is never rescaled
(absorbance) is an error even with ``--value-scale``, which does not apply
to it; only a re-import of an export downgrades it to a warning.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from openspeclib.convert.common import FRACTION_QUANTITIES, ConversionError, ImportReport

__all__ = ["ScaleItem", "declared_divisor", "integral", "resolve_scales", "spectrum_level"]

# Percentile used as a spectrum's robust level.
LEVEL_PERCENTILE = 90.0
# Highest level that is plainly fractions.
FRACTION_MAX = 1.5
# Float levels in (FRACTION_MAX, PERCENT_MIN) are ambiguous.
PERCENT_MIN = 15.0
# Highest level of percent data.
PERCENT_MAX = 150.0
# Highest level of 0-10000 data.
SCALED_MAX = 15000.0
# With declared percent/0-10000 units, levels in this range look like fractions.
DECLARED_CONFLICT = (0.015, 1.5)
# How many row labels an error lists.
_MAX_LABELS = 5


@dataclass
class ScaleItem:
    """What scale resolution needs to know about one spectrum.

    Attributes:
        label: Spectrum label for messages.
        quantity: Its quantity.
        values: Its values, sentinels already NaN.
        integer: Whether the data are integers (ENVI integer type, or every
            finite CSV value integral).
        declared: Divisor implied by declared units, if any.
    """

    label: str
    quantity: str
    values: np.ndarray
    integer: bool
    declared: float | None = None


def spectrum_level(values: np.ndarray) -> float | None:
    """Return a spectrum's level: the 90th percentile of its finite values.

    Args:
        values: The values.

    Returns:
        The level, or ``None`` when no value is finite.
    """
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return float(np.percentile(finite, LEVEL_PERCENTILE))


def declared_divisor(units: str) -> float | None:
    """Interpret declared value units (EcoSIS ``Measurement Units``).

    Args:
        units: E.g. ``"%"``, ``"percent"``, ``"0-1"``, ``"fraction"``,
            ``"0-10000"``.

    Returns:
        The divisor the units imply, or ``None`` when they are not recognised.
    """
    text = units.strip().lower().replace("–", "-").replace(" ", "")
    if "%" in text or "percent" in text or text in ("0-100", "x100"):
        return 100.0
    if text in ("0-10000", "10000", "x10000", "*10000", "scaled10000"):
        return 10000.0
    if text in ("0-1", "1", "fraction", "fractional", "unitless", "dimensionless", "ratio"):
        return 1.0
    return None


def _detect(level: float, integer: bool) -> float | None:
    """Detect the divisor of one spectrum from its level.

    Args:
        level: The spectrum's level.
        integer: Whether the data are integers.

    Returns:
        The divisor, or ``None`` when the level is ambiguous or fits no scale.
    """
    if integer:
        if PERCENT_MAX < level <= SCALED_MAX:
            return 10000.0
        return None
    if level <= FRACTION_MAX:
        return 1.0
    if PERCENT_MIN <= level <= PERCENT_MAX:
        return 100.0
    if PERCENT_MAX < level <= SCALED_MAX:
        return 10000.0
    return None


def _labels(labels: Sequence[str]) -> str:
    """Format a few row labels for an error message.

    Args:
        labels: The labels.

    Returns:
        Up to five labels and a count of the rest.
    """
    shown = ", ".join(labels[:_MAX_LABELS])
    more = len(labels) - _MAX_LABELS
    return shown + (f" and {more} more" if more > 0 else "")


def _plausibility(
    items: Sequence[ScaleItem], divisors: Sequence[float]
) -> list[tuple[str, float, float, float, str]]:
    """Find spectra whose scaled median is outside the hard bounds of their quantity.

    Args:
        items: The spectra.
        divisors: The divisor of each.

    Returns:
        ``(label, median, low, high, quantity)`` per failing spectrum.
    """
    from openspeclib.conformance import load_qa_bounds

    bounds = load_qa_bounds()
    failures = []
    for item, divisor in zip(items, divisors):
        bound = bounds.get(item.quantity)
        finite = item.values[np.isfinite(item.values)]
        if bound is None or finite.size == 0:
            continue
        median = float(np.median(finite)) / divisor
        if not bound.hard_min <= median <= bound.hard_max:
            failures.append((item.label, median, bound.hard_min, bound.hard_max, item.quantity))
    return failures


def resolve_scales(
    items: Sequence[ScaleItem],
    report: ImportReport,
    *,
    explicit: float | None = None,
    from_export: bool = False,
    header_divisor: float | None = None,
) -> list[float]:
    """Choose the divisor of every spectrum (see the module docstring).

    Args:
        items: The spectra.
        report: Receives the divisor summary and warnings.
        explicit: ``--value-scale``, if given.
        from_export: The input is an ``openspeclib export`` of Format v1 data.
        header_divisor: Scale factor stated by the file, if any.

    Returns:
        One divisor per spectrum.

    Raises:
        ConversionError: If the scale is ambiguous, mixed, or implausible
            for a scale the user did not give, or a never-rescaled spectrum
            is implausible (unless the input is a re-import of an export).
    """
    hint = "pass --value-scale with the divisor that makes the values fractions (1, 100, 10000)"
    fraction = [item.quantity in FRACTION_QUANTITIES for item in items]
    stated: tuple[float, str, str] | None = None
    if explicit is not None:
        if not explicit > 0:
            raise ConversionError("--value-scale must be a positive number")
        stated = (explicit, "flag", f"--value-scale {explicit:g}")
    elif header_divisor is not None and not from_export:
        stated = (header_divisor, "header", f"reflectance scale factor = {header_divisor:g}")
    if from_export and explicit is None:
        divisors, source = [1.0] * len(items), "openspeclib export"
    elif stated is not None:
        divisor, source, what = stated
        divisors = [divisor if f else 1.0 for f in fraction]
        unscaled = Counter(item.quantity for item, f in zip(items, fraction) if not f)
        if unscaled and divisor != 1.0:
            kinds = ", ".join(f"{n} {q}" for q, n in sorted(unscaled.items()))
            report.warn(
                f"{what} not applied to {sum(unscaled.values())} spectra ({kinds}): only "
                f"reflectance, transmittance and emissivity are rescaled to fractions"
            )
    else:
        divisors, source = _infer(items, report, hint)

    report.divisor_source = source
    chosen = sorted({d for d, f in zip(divisors, fraction) if f}) or [1.0]
    report.value_divisor = chosen[0] if len(chosen) == 1 else max(chosen)
    if len(chosen) > 1:
        report.warn(f"value divisors per spectrum: {', '.join(f'{d:g}' for d in chosen)}")
    elif chosen[0] != 1.0:
        report.warn(f"values divided by {chosen[0]:g} ({source})")

    failures = _plausibility(items, divisors)
    if not failures:
        return divisors
    if source == "openspeclib export":
        report.warn(_implausible_message(failures))
        return divisors
    # --value-scale never applies to these, so it cannot vouch for them.
    never_rescaled = [f for f in failures if f[4] not in FRACTION_QUANTITIES]
    if never_rescaled:
        raise ConversionError(
            f"{_implausible_message(never_rescaled)}; "
            f"{', '.join(sorted({f[4] for f in never_rescaled}))} values are stored as given "
            f"and never rescaled (spec section 4.1): check the quantity, or convert the "
            f"values before importing"
        )
    if source == "flag":
        report.warn(_implausible_message(failures))
        return divisors
    raise ConversionError(f"{_implausible_message(failures)}; {hint}")


def _implausible_message(failures: Sequence[tuple[str, float, float, float, str]]) -> str:
    """Describe spectra whose scaled median is outside the hard bounds.

    Args:
        failures: ``(label, median, low, high, quantity)`` per failing spectrum.

    Returns:
        The message, naming the first failure and up to five labels.
    """
    label, median, low, high, quantity = failures[0]
    return (
        f"{len(failures)} spectra are implausible after scaling (e.g. {label}: {quantity} "
        f"median {median:.4g}, outside [{low:g}, {high:g}]): "
        f"{_labels([f[0] for f in failures])}"
    )


def _infer(items: Sequence[ScaleItem], report: ImportReport, hint: str) -> tuple[list[float], str]:
    """Infer per-spectrum divisors from declared units and the data.

    Args:
        items: The spectra.
        report: Receives warnings.
        hint: How to resolve an error.

    Returns:
        ``(divisors, source)``.

    Raises:
        ConversionError: If a spectrum is ambiguous or detected scales are mixed.
    """
    divisors: list[float | None] = []
    detected: dict[float, list[str]] = {}
    ambiguous: list[str] = []
    sources: set[str] = set()
    for item in items:
        level = spectrum_level(item.values)
        if item.quantity not in FRACTION_QUANTITIES:
            divisors.append(1.0)
            continue
        if item.declared is not None:
            low, high = DECLARED_CONFLICT
            if item.declared != 1.0 and level is not None and low <= level <= high:
                ambiguous.append(f"{item.label} (declared ÷{item.declared:g}, level {level:.3g})")
                divisors.append(None)
            else:
                divisors.append(item.declared)
                sources.add("declared units")
            continue
        if level is None:
            divisors.append(None)  # all NaN: takes the file's scale
            continue
        divisor = _detect(level, item.integer)
        if divisor is None:
            kind = "integer" if item.integer else "float"
            fits = "fits no supported scale" if level > SCALED_MAX else "ambiguous"
            ambiguous.append(f"{item.label} ({kind}, level {level:.3g}, {fits})")
        else:
            detected.setdefault(divisor, []).append(item.label)
            sources.add("detected")
        divisors.append(divisor)
    if ambiguous:
        raise ConversionError(
            f"cannot tell the value scale of {len(ambiguous)} spectra: {_labels(ambiguous)}. "
            f"Levels between 1.5 and 15 (float) or up to 150 (integer) may be bright "
            f"fractions, percent or 0-10000 data, levels above {SCALED_MAX:g} fit no "
            f"supported scale, and declared units that the data contradict are not "
            f"trusted; {hint}"
        )
    if len(detected) > 1:
        parts = "; ".join(f"÷{d:g}: {_labels(labels)}" for d, labels in sorted(detected.items()))
        raise ConversionError(f"the file mixes value scales ({parts}); {hint}")
    if 100.0 in detected:
        report.warn(
            f"{len(detected[100.0])} spectra detected as percent from their level alone "
            f"(90th percentile {PERCENT_MIN:g}-{PERCENT_MAX:g}); dark 0-10000 data (reflectance "
            f"below about 1.5 %) look the same and pass the plausibility check, so if the "
            f"file holds only such spectra pass --value-scale 10000"
        )
    fallback = next(iter(detected), None) or next((d for d in divisors if d), 1.0)
    resolved = [d if d is not None else fallback for d in divisors]
    if not sources:
        return resolved, "none"
    return resolved, " and ".join(sorted(sources))


def integral(values: np.ndarray) -> bool:
    """Tell whether every finite value is an integer.

    Args:
        values: The values.

    Returns:
        ``True`` when there is a finite value and all finite values are integral.
    """
    finite = values[np.isfinite(values)]
    return bool(finite.size) and bool(np.all(finite == np.round(finite)))
