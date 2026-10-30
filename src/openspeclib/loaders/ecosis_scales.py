"""ECOSIS reflectance scale inference and per-band validity masking.

ECOSIS aggregates user-submitted spectral datasets, each curated by a
different research group; some upload reflectance in the unit interval
(0–1), others as percent (0–100), and others as a scaled integer range
(typically 0–10000 — the convention used by some satellite-derived
products that store reflectance as int16). This mismatch is silent in
the source data — ECOSIS does not enforce a scale convention at upload
and even when a ``Measurement Units`` metadata field is provided it is
often missing or wrong (several unit-scale datasets declare ``%``) — so we
infer the source scale at ingest from the data itself and divide by the
appropriate factor so every record in the combined library lives on the
unit interval.

Assumption: the source scale is always a power-of-10 multiplier of the
unit interval — i.e. one of ``{0–1, 0–100, 0–10000}`` with divisor in
``{1, 100, 10000}``.

Scale is decided in two steps:

1. **Dataset base divisor** — the smallest divisor that brings the median
   per-spectrum *level* under ``UNIT_CEILING``. A spectrum's level is the
   90th percentile of its finite values rather than its maximum: field
   spectra routinely carry noise spikes of several hundred percent in the
   atmospheric water-absorption bands (~1400/1900 nm) and at the detector
   edges (<400 nm, >2400 nm), and a maximum-based level mistakes those
   spikes for a different scale (NGEE Arctic 2017 is the canonical case).
   Using the dataset median makes the decision robust to a few odd spectra
   and keeps dark targets (water, shade) on their dataset's scale. A
   dataset may also pin its divisor explicitly in ``ecosis_datasets.json``.
2. **Per-spectrum escalation** — a spectrum whose level does not fit the
   base divisor (mixed-scale uploads) is moved to the next larger divisor
   that brings its level into ``[ESCALATION_MIN_LEVEL, UNIT_CEILING]``.
   Escalation is upward only: a too-dark spectrum is indistinguishable
   from a dark target, so it is never re-scaled downward. A spectrum that
   fits no supported scale is dropped by the loader.

A spectrum whose scaled level falls more than ``DARK_LEVEL_RATIO`` times below
the dataset's median scaled level is dropped by the loader, whatever its
divisor. In a percent-scale dataset that is the signature of a unit-scale
spectrum that would otherwise be divided and kept 100x too dark; in a
unit-scale dataset, of a spectrum the uploader already divided by 100 (the
Madison PSR copies). A dataset of dark targets (water) is unaffected, because
the ratio is taken against its own median.

A spectrum in which more than ``DOMINANT_VALUE_FRACTION`` of the finite bands
hold one and the same value is dropped as well: that is a saturated or
clipped detector (e.g. bands pinned at exactly 1.0) or a partial fill, not a
measured spectrum. Measured spectra, even quantised dark water spectra, stay
well below that fraction.

After scaling, individual values outside ``[VALID_MIN, VALID_MAX]`` — the
validator's warning band — are masked to NaN as bad bands under the
project-wide missing-value contract. That window removes detector-edge and
water-band junk while leaving near-zero noise and genuine slightly-above-1
values (bright targets, BRDF) untouched, so the kept values are not biased by
one-sided clipping. Spectra left with less than ``MIN_COVERAGE`` valid bands
are dropped.
"""

import logging
import math
from typing import Sequence

logger = logging.getLogger(__name__)

# Candidate source scale divisors, in increasing order.
SUPPORTED_DIVISORS: tuple[int, ...] = (1, 100, 10000)

# Upper bound on a correctly-scaled spectrum's level (90th percentile). A
# bit above 1.0 to tolerate bright targets and noisy bands without flipping
# the classification.
UNIT_CEILING = 1.5

# Percentile used as a spectrum's robust level.
LEVEL_PERCENTILE = 90.0

# An escalated (re-scaled) spectrum must land at or above this level;
# otherwise the escalation would turn junk into a plausible-looking dark
# spectrum, and the spectrum is dropped instead.
ESCALATION_MIN_LEVEL = 0.02

# Scaled values outside this window are masked as bad bands. It matches the
# validator's warning band for reflectance/transmittance.
VALID_MIN = -0.05
VALID_MAX = 1.2

# A divided spectrum whose scaled level is this many times below the
# dataset's median scaled level is treated as mis-scaled and dropped.
DARK_LEVEL_RATIO = 30.0

# Minimum fraction of valid (finite, in-range) bands for a spectrum to be
# kept at all.
MIN_COVERAGE = 0.5

# A spectrum in which more than this fraction of the finite bands share one
# value (saturated, clipped or partly filled) is dropped.
DOMINANT_VALUE_FRACTION = 0.5


def spectrum_level(values: Sequence[float]) -> float | None:
    """Return a spectrum's robust level: the 90th percentile of finite values.

    Uses the nearest-rank definition so the result is always an observed
    value.

    Args:
        values: Spectral values, possibly containing NaN/Inf.

    Returns:
        The level, or ``None`` when the spectrum has no finite values.
    """
    finite = sorted(v for v in values if math.isfinite(v))
    if not finite:
        return None
    rank = math.ceil(LEVEL_PERCENTILE / 100.0 * len(finite))
    return finite[max(rank, 1) - 1]


def dominant_value_fraction(values: Sequence[float]) -> float:
    """Return the share of finite values taken by the most frequent value.

    Args:
        values: Spectral values, possibly containing NaN/Inf.

    Returns:
        The count of the most common finite value divided by the number of
        finite values; 0 when there are none.
    """
    counts: dict[float, int] = {}
    for v in values:
        if math.isfinite(v):
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return 0.0
    return max(counts.values()) / sum(counts.values())


def divisor_from_levels(levels: Sequence[float]) -> int:
    """Infer a dataset's base divisor from its per-spectrum levels.

    Args:
        levels: One robust level per spectrum (see ``spectrum_level``).

    Returns:
        The smallest supported divisor bringing the median level under
        ``UNIT_CEILING``; 1 for empty input; the largest divisor (with a
        warning) when no supported scale fits.
    """
    if not levels:
        return 1
    ordered = sorted(levels)
    median_level = ordered[len(ordered) // 2]
    for divisor in SUPPORTED_DIVISORS:
        if median_level / divisor <= UNIT_CEILING:
            return divisor
    largest = SUPPORTED_DIVISORS[-1]
    logger.warning(
        "ECOSIS dataset median spectrum level %.3g exceeds all supported "
        "power-of-10 scales %s; falling back to divisor %d",
        median_level,
        SUPPORTED_DIVISORS,
        largest,
    )
    return largest


def infer_dataset_divisor(spectra_values: Sequence[Sequence[float]]) -> int:
    """Infer the source scale divisor for an ECOSIS dataset.

    Args:
        spectra_values: Values per spectrum; NaN/Inf are ignored.

    Returns:
        The divisor (1 for unit-scale, 100 for percent-scale, 10000 for the
        scaled-integer convention). Defaults to 1 for empty input.
    """
    levels = [lvl for lvl in (spectrum_level(v) for v in spectra_values) if lvl is not None]
    return divisor_from_levels(levels)


def resolve_spectrum_divisor(level: float, base_divisor: int) -> int | None:
    """Choose the divisor for one spectrum given its dataset's base divisor.

    Args:
        level: The spectrum's robust level (see ``spectrum_level``).
        base_divisor: The dataset's base divisor.

    Returns:
        ``base_divisor`` when the level fits it; otherwise the smallest
        larger supported divisor that brings the level into
        ``[ESCALATION_MIN_LEVEL, UNIT_CEILING]``; ``None`` when no supported
        scale is plausible.
    """
    if level / base_divisor <= UNIT_CEILING:
        return base_divisor
    for divisor in SUPPORTED_DIVISORS:
        if divisor <= base_divisor:
            continue
        scaled = level / divisor
        if scaled <= UNIT_CEILING:
            return divisor if scaled >= ESCALATION_MIN_LEVEL else None
    return None


def count_out_of_range(values: Sequence[float], divisor: int) -> int:
    """Count finite values outside ``[VALID_MIN, VALID_MAX]`` after scaling.

    Args:
        values: Raw (unscaled) spectral values.
        divisor: Divisor that will be applied.

    Returns:
        Number of finite values that would be masked as out of range.
    """
    return sum(
        1 for v in values if math.isfinite(v) and not (VALID_MIN <= _scale(v, divisor) <= VALID_MAX)
    )


def _scale(value: float, divisor: int) -> float:
    """Apply a divisor exactly as ``scale_and_mask`` does.

    Args:
        value: Raw value.
        divisor: Source scale divisor.

    Returns:
        The scaled value.
    """
    return value / divisor if divisor != 1 else value


def scale_and_mask(values: Sequence[float], divisor: int) -> tuple[list[float], int, list[int]]:
    """Divide values by ``divisor`` and mask invalid ones to NaN.

    Args:
        values: Raw spectral values (NaN marks missing bands).
        divisor: Source scale divisor.

    Returns:
        Tuple ``(scaled, n_missing, masked_indices)`` where ``scaled`` has
        NaN at every missing or masked band and ``masked_indices`` lists the
        positions masked for falling outside ``[VALID_MIN, VALID_MAX]``.
    """
    scaled: list[float] = []
    missing = 0
    masked: list[int] = []
    for i, v in enumerate(values):
        if not math.isfinite(v):
            scaled.append(math.nan)
            missing += 1
            continue
        s = _scale(v, divisor)
        if not (VALID_MIN <= s <= VALID_MAX):
            scaled.append(math.nan)
            masked.append(i)
        else:
            scaled.append(s)
    return scaled, missing, masked
