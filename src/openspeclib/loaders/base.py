"""Abstract base class and shared helpers for spectral library loaders.

Missing-value contract (applies to every loader):

* A missing or invalid band is stored as ``float("nan")`` in
  ``spectral_data.values`` — never as a source sentinel such as
  ``-1.23e34`` or ``-9999``.
* Wavelengths are never NaN; rows with a non-finite wavelength are dropped.

:func:`mask_invalid_values` and :func:`drop_invalid_wavelengths` implement
the contract so loaders do not each re-derive it.

Identifier contract (OpenSpecLib Format v1, rule F5): a spectrum id is
``{collection}:{local_id}`` where ``local_id`` uses only
``[A-Za-z0-9._~-]``. :func:`spectrum_id` derives it from the upstream
identifier by replacing every other character with ``_``; the combiner
resolves the (rare) collisions this can cause deterministically.
"""

import hashlib
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from openspeclib.models import (
    LOCAL_ID_ALLOWED,
    LOCAL_ID_MAX_LENGTH,
    CollectionInfo,
    DatasetInfo,
    SpectrumRecord,
)

# Any finite value whose magnitude exceeds this is a fill value, never a
# physical reflectance / emissivity / transmittance / absorbance. Catches the
# USGS ``-1.23e34`` deleted-channel marker and IEEE-style fills such as
# ``1e36``. Matches rule F9 (finite ``|v| > 1e10`` is a sentinel), so loaders
# mask everything the format would reject.
FILL_VALUE_MAGNITUDE = 1e10

# Conventional "no data" markers used by ad-hoc two-column ASCII spectra
# (RELAB, ASU TES, Bishop, OSSL). None is a physically possible value for
# any spectral quantity these loaders ingest.
GENERIC_SENTINELS: tuple[float, ...] = (-9999.0, -999.0)

_DISALLOWED_LOCAL_ID_CHARS = re.compile(rf"[^{LOCAL_ID_ALLOWED}]")
# Length of the hash suffix appended when an original id is too long.
_HASH_SUFFIX_CHARS = 8


def make_local_id(original: str) -> str:
    """Derive a Format v1 local id from an upstream identifier.

    Every character outside ``[A-Za-z0-9._~-]`` is replaced with ``_``. An
    id longer than the 200-character limit is truncated and suffixed with
    ``~`` and the first 8 hex digits of the SHA-256 of the original, so
    distinct long ids stay distinct.

    Args:
        original: The upstream identifier.

    Returns:
        A non-empty local id matching rule F5.
    """
    local = _DISALLOWED_LOCAL_ID_CHARS.sub("_", original) or "_"
    if len(local) > LOCAL_ID_MAX_LENGTH:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:_HASH_SUFFIX_CHARS]
        local = f"{local[: LOCAL_ID_MAX_LENGTH - _HASH_SUFFIX_CHARS - 1]}~{digest}"
    return local


def spectrum_id(collection: str, original: str) -> str:
    """Build a spectrum id ``{collection}:{local_id}`` from an upstream identifier.

    Args:
        collection: Collection id.
        original: The upstream identifier the local id is derived from.

    Returns:
        The spectrum id.
    """
    return f"{collection}:{make_local_id(original)}"


def mask_invalid_values(
    values: Iterable[float],
    sentinels: Iterable[float] = (),
    *,
    rel_tol: float = 1e-6,
) -> list[float]:
    """Replace sentinel, fill and non-finite values with NaN.

    Args:
        values: Raw spectral values as parsed from the source file.
        sentinels: Source-specific "missing data" markers. A value matches a
            sentinel when it is equal within ``rel_tol`` (sources write the
            same marker with differing precision, e.g. ``-1.23e34`` vs
            ``-1.2300000e+034``).
        rel_tol: Relative tolerance used when matching ``sentinels``.

    Returns:
        A new list in which every missing/invalid value is ``float("nan")``.
        Values of magnitude ``> FILL_VALUE_MAGNITUDE`` and ``±inf`` are
        always treated as missing.
    """
    markers = tuple(sentinels)
    out: list[float] = []
    for v in values:
        if (
            not math.isfinite(v)
            or abs(v) > FILL_VALUE_MAGNITUDE
            or any(math.isclose(v, s, rel_tol=rel_tol) for s in markers)
        ):
            out.append(float("nan"))
        else:
            out.append(v)
    return out


def drop_invalid_wavelengths(
    wavelengths: Sequence[float], values: Sequence[float]
) -> tuple[list[float], list[float]]:
    """Drop (wavelength, value) pairs whose wavelength is not finite.

    Args:
        wavelengths: Parsed wavelength (or wavenumber) axis.
        values: Spectral values aligned with ``wavelengths``.

    Returns:
        ``(wavelengths, values)`` with every pair whose wavelength is NaN or
        infinite removed, so the stored axis never contains NaN.
    """
    kept = [(w, v) for w, v in zip(wavelengths, values) if math.isfinite(w)]
    return [w for w, _ in kept], [v for _, v in kept]


@dataclass(frozen=True)
class AxisOrder:
    """Result of :func:`sort_by_wavelength`.

    Attributes:
        wavelengths: The strictly monotonic axis.
        arrays: The aligned per-band arrays, in the same order as passed
            (``None`` entries kept).
        reordered: Whether bands were moved (the source axis was not
            monotonic).
        source_order: For each output band, its index in the source axis.
        dropped: Source indices of bands dropped because their wavelength
            exactly equals that of another (kept) band.
    """

    wavelengths: list[float]
    arrays: list[list[float] | None]
    reordered: bool
    source_order: list[int]
    dropped: list[int] = field(default_factory=list)

    def extra(self, source_wavelengths: Sequence[float]) -> dict[str, Any]:
        """Describe the reordering for a record's ``extra`` (spec section 4.3).

        Args:
            source_wavelengths: The axis as the source published it.

        Returns:
            ``{}`` when nothing changed. Otherwise
            ``bands_reordered_by_wavelength`` and ``source_band_order`` (the
            source index of every stored band) when bands were moved, and
            ``duplicate_wavelength_bands_dropped`` (``[source_index,
            wavelength]`` pairs) and ``duplicate_wavelength_band_count`` (how
            many) when equal-wavelength bands were dropped.
        """
        out: dict[str, Any] = {}
        if self.reordered:
            out["bands_reordered_by_wavelength"] = True
            out["source_band_order"] = list(self.source_order)
        if self.dropped:
            out["duplicate_wavelength_bands_dropped"] = [
                [i, source_wavelengths[i]] for i in self.dropped
            ]
            out["duplicate_wavelength_band_count"] = len(self.dropped)
        return out


def sort_by_wavelength(wavelengths: Sequence[float], *arrays: Sequence[float] | None) -> AxisOrder:
    """Make an axis strictly monotonic, moving aligned arrays with it.

    Format v1 requires a strictly monotonic axis (rule F8, spec section 4.3).
    Some sources publish axes whose detectors overlap (e.g. USGS AVIRIS, where
    each of the four spectrometers starts below the previous one's last band).

    Rules, applied deterministically:

    * An axis that is already non-decreasing or non-increasing keeps its
      direction and order (``reordered`` is ``False``).
    * Any other axis is sorted ascending with a stable sort (``reordered``
      is ``True``); no value is changed.
    * Of several bands with exactly the same wavelength, one is kept and the
      others are dropped: the first (in source order) whose value in the first
      array is finite, or the first of them when none is. A missing (NaN) band
      never displaces a measured one.

    Args:
        wavelengths: The axis.
        *arrays: Per-band arrays aligned with ``wavelengths`` (``None`` kept).
            The first array holds the values used to prefer finite bands.

    Returns:
        The resulting axis, arrays and provenance (see :class:`AxisOrder`).
    """
    n = len(wavelengths)
    pairs = list(zip(wavelengths[:-1], wavelengths[1:]))
    if all(a <= b for a, b in pairs) or all(a >= b for a, b in pairs):
        order = list(range(n))
        reordered = False
    else:
        order = sorted(range(n), key=lambda i: wavelengths[i])
        reordered = True
    values = arrays[0] if arrays else None
    chosen: dict[float, int] = {}
    for i in order:
        current = chosen.get(wavelengths[i])
        if current is None or (
            values is not None and not _finite(values[current]) and _finite(values[i])
        ):
            chosen[wavelengths[i]] = i
    keep = set(chosen.values())
    kept = [i for i in order if i in keep]
    dropped = sorted(set(range(n)) - keep)
    return AxisOrder(
        wavelengths=[wavelengths[i] for i in kept],
        arrays=[None if a is None else [a[i] for i in kept] for a in arrays],
        reordered=reordered,
        source_order=kept,
        dropped=dropped,
    )


def _finite(value: float | None) -> bool:
    """Tell whether a band value is a finite number.

    Args:
        value: A band value, possibly ``None`` or NaN.

    Returns:
        ``True`` for a finite float.
    """
    return value is not None and math.isfinite(value)


def monotonic_axis(
    wavelengths: Sequence[float], values: Sequence[float]
) -> tuple[list[float], list[float], dict[str, Any]]:
    """Apply :func:`sort_by_wavelength` to an axis and its values.

    Args:
        wavelengths: The axis as parsed.
        values: The aligned values.

    Returns:
        ``(wavelengths, values, extra)`` where ``extra`` records any
        reordering or dropped equal-wavelength bands (empty when the axis was
        already strictly monotonic).
    """
    axis = sort_by_wavelength(wavelengths, values)
    return axis.wavelengths, axis.arrays[0] or [], axis.extra(wavelengths)


class BaseLoader(ABC):
    """Abstract base for all spectral library source loaders.

    Each loader is responsible for:

    1. Downloading the source archive (:meth:`download`).
    2. Parsing native files into ``SpectrumRecord`` objects (:meth:`load`).
    3. Describing its collection for the file footer (:meth:`collection_info`
       and, for aggregated collections, :meth:`datasets`).

    Loaders yield records via generators to support large datasets
    without holding everything in memory.
    """

    @property
    def supports_auto_download(self) -> bool:
        """Whether this loader can download data automatically.

        Returns ``True`` by default. Loaders that require manual data
        placement should override this to return ``False``.
        """
        return True

    @property
    def fatal_error_count(self) -> int:
        """Number of input files rejected for reasons that must fail the run.

        Loaders may skip individual files and keep going, but some skips
        (e.g. a spectrum whose value scale cannot be determined) mean the
        output is incomplete in a way a release must not silently absorb.
        Valid after :meth:`load` has been fully consumed; ``ingest`` exits
        non-zero when it is positive. The default is ``0``.
        """
        return 0

    @abstractmethod
    def source_name(self) -> str:
        """Return the collection id (e.g. ``'usgs_splib07'``)."""
        ...

    @abstractmethod
    def collection_info(self) -> CollectionInfo:
        """Return the footer ``collection`` object for this loader's collection.

        Returns:
            The collection's identity, provenance, licence and inclusion.
        """
        ...

    def datasets(self) -> dict[str, DatasetInfo]:
        """Return the contributing datasets of an aggregated collection.

        Valid after :meth:`load` has been fully consumed, since inclusion
        counts come from the ingest. Monolithic collections have none.

        Returns:
            Dataset metadata keyed by dataset id (empty by default).
        """
        return {}

    @abstractmethod
    def download(self, target_dir: Path) -> Path:
        """Download and extract the source library.

        Args:
            target_dir: Directory to download/extract into.

        Returns:
            Path to the extracted data directory.
        """
        ...

    @abstractmethod
    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse native source files and yield unified SpectrumRecord objects.

        Args:
            source_dir: Root directory of the extracted source data.

        Yields:
            SpectrumRecord for each valid spectrum found.
        """
        ...
