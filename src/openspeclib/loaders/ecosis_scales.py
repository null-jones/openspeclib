"""ECOSIS reflectance scale inference.

ECOSIS aggregates user-submitted spectral datasets, each curated by a
different research group; some upload reflectance in the unit interval
(0–1), others as percent (0–100), and others as a scaled integer range
(typically 0–10000 — the convention used by some satellite-derived
products that store reflectance as int16). This mismatch is silent in
the source data — ECOSIS does not enforce a scale convention at upload
and even when a ``Measurement Units`` metadata field is provided it is
often missing or wrong — so we infer the source scale at ingest from
the data itself and divide by the appropriate factor so every record in
the combined library lives on the unit interval.

Assumption: the source scale is always a power-of-10 multiplier of the
unit interval — i.e. one of ``{0–1, 0–100, 0–10000}`` with divisor in
``{1, 100, 10000}``. This holds for every ECOSIS dataset we have
encountered, and matches the conventions used across the broader remote
sensing literature. Other multipliers (0–10, 0–1000) have not been
observed in practice.

The two distributions for adjacent scales are well separated — unit
medians sit near ~0.3, percent medians near ~30, and the 0–10000
scale's medians near ~3000 — so the decision is made by comparing each
candidate divisor's post-normalisation median against a small
``UNIT_CEILING`` and picking the smallest divisor whose result lands in
the unit interval. Using the *median* of per-spectrum maxima resists a
few noisy outlier spectra flipping the classification.
"""

import logging

logger = logging.getLogger(__name__)

# Candidate source scale divisors, in increasing order. A dataset is
# classified by the smallest divisor whose post-normalisation median
# spectrum max sits below ``UNIT_CEILING``.
SUPPORTED_DIVISORS: tuple[int, ...] = (1, 100, 10000)

# Soft upper bound on a unit-scale spectrum's max value. A bit above 1.0
# to allow for emissivity / absorbance bands that legitimately exceed 1
# without flipping the classification.
UNIT_CEILING = 1.5


def infer_dataset_divisor(spectra_values: list[list[float]]) -> int:
    """Infer the source scale divisor for an ECOSIS dataset.

    The returned divisor is one of ``SUPPORTED_DIVISORS`` (1, 100, or
    10000) and represents the factor the loader should divide each
    value by to land on the unit interval.

    The decision uses the median of per-spectrum maxima: a single noisy
    spectrum with one out-of-range value can't flip the classification
    for the whole dataset.

    Args:
        spectra_values: Reflectance values per spectrum, with NaN/Inf
            already filtered out by the loader.

    Returns:
        The divisor (1 for unit-scale, 100 for percent-scale, 10000 for
        the scaled-integer convention). Defaults to 1 for empty input.
    """
    maxes: list[float] = []
    for vals in spectra_values:
        if vals:
            maxes.append(max(vals))
    if not maxes:
        return 1

    maxes.sort()
    median_max = maxes[len(maxes) // 2]

    for divisor in SUPPORTED_DIVISORS:
        if median_max / divisor <= UNIT_CEILING:
            return divisor

    # The dataset's median max exceeds every supported scale — most
    # likely a non-power-of-10 scaling we don't model. Fall back to the
    # largest divisor and warn so the dataset can be inspected.
    largest = SUPPORTED_DIVISORS[-1]
    logger.warning(
        "ECOSIS dataset median spectrum max %.3g exceeds all supported "
        "power-of-10 scales %s; falling back to divisor %d",
        median_max,
        SUPPORTED_DIVISORS,
        largest,
    )
    return largest
