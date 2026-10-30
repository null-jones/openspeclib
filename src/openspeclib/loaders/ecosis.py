"""Loader for the EcoSIS (Ecological Spectral Information System) spectral library.

EcoSIS is a NASA-supported collection of ecological spectral datasets hosted
at https://ecosis.org. Unlike monolithic spectral libraries, EcoSIS aggregates
individual researcher-contributed datasets, each with its own citation and
metadata. This loader ingests a curated subset of datasets defined in
``ecosis_datasets.json``.

Data is fetched via the EcoSIS REST API:
    - Dataset metadata: ``GET /api/package/{id}``
    - Spectra: ``GET /api/spectra/search/{id}?start=N&stop=M``

Inclusion policy
----------------
Most of the bulk of the curated packages is replicate or experimental data
with no per-spectrum material identity, so each entry of
``ecosis_datasets.json`` carries a machine-readable policy:

- ``inclusion``: ``"full"`` (every eligible spectrum), ``"sample"`` (a
  deterministic stratified sample, see ``sampling``) or ``"exclude"``
  (never downloaded or ingested). ``reason`` records why.
- ``sampling``: ``stratify_by`` (per-spectrum field), ``max_total`` and/or
  ``max_per_stratum`` caps, ``min_per_stratum``, and optionally ``one_per``
  (a per-spectrum field such as a leaf id: at most one replicate per value is
  sampled). Strata and sampling units are ranked by SHA-256 of their key, so
  the sample is independent of API or file order. Spectra without a stratum
  value have no identity and are not eligible.
- ``identity``: material naming and category rules where the default naming
  (Common Name / Latin name / Spectra label) and the Target Type category
  mapping do not apply.
- ``exclude_resources``: regexes on a spectrum's source resource filename
  for uploads that are not measured spectra (e.g. model coefficients).
- ``resource_techniques``: regex on the resource filename -> measurement
  quantity, for packages that upload reflectance and transmittance as
  separate files without a per-spectrum ``Measurement Quantity``.
- ``units_field`` / ``units_divisors``: a per-spectrum units field whose
  values are authoritative for the source scale.
- ``dedupe_key``: ``"datapoints"`` (default: identical datapoints from the
  same resource file) or ``"content"`` (datapoints plus all non-id
  metadata) — the key under which repeated uploads of the same spectrum are
  collapsed to one record, before scale inference and sampling. The kept copy
  is the lowest SHA-256-ranked id; the others are listed in
  ``extra["duplicate_ids"]``. Copies carrying different material names or
  strata are dropped as a group (or kept once under ``conflicting_duplicates``
  ``keep_one``, see below). Label fields the copies
  disagree on (sample id, sample description, USDA symbol, carried fields)
  are left out of the kept record and listed with every value in
  ``extra["duplicate_conflicting_fields"]``. A group of
  ``_LARGE_DUPLICATE_GROUP`` or more rows of different samples (rows that
  disagree on a label or identity field, see ``_identity_fields``) is a
  fill-down copy error and is dropped whole.
- ``drop_spectra``: individual spectra dropped with a recorded reason.
- ``scale_divisor``: explicit source scale override (see ``ecosis_scales``).
- ``simulated``: data are simulated; records get a ``"simulated"``
  processing flag.
- ``setup``: the measurement setup per spectrum — ``geometry`` (Format v1
  vocabulary), ``instrument``, ``instrument_type``, ``light_source``,
  ``foreoptic``, ``acquisition_method``, ``venue`` — as a dataset ``default``,
  per-resource ``resources`` patterns, a per-spectrum ``field``/``field_map``
  and an ``instrument_field``/``instrument_map``, with the metadata
  ``evidence`` it rests on (required). Without a block geometry is
  ``unknown``; other attributes fall back to the package-level value only
  when the package lists exactly one.
- ``conflicting_duplicates``: ``{"action": "drop"}`` (default) or
  ``{"action": "keep_one", "material_name": …, "link_field": …}`` for
  duplicate groups whose copies carry different labels.
- ``reference``: the reference standard (e.g. ``spectralon``) where the
  package states it.
- ``carry_fields``: per-spectrum fields copied into
  ``extra["ecosis_metadata"]``.

The downloader mirrors every non-excluded package completely; the policy is
enforced at ingest so policy changes never need a re-download.

Missing values follow the project-wide contract: a missing or invalid band
is NaN in ``values``. Spectra with NaN bands record where the NaNs came
from in ``extra``: ``source_missing_band_count`` (missing or
non-numeric in the source), ``masked_band_count`` and ``masked_bands`` (a
list of ``[wavelength, source_value]`` pairs masked for falling outside the
valid window after scaling; ``source_value`` is the unscaled source value).

Collection layout (OpenSpecLib Format v1)
-----------------------------------------
The collection id is ``ecosis``; a spectrum's local id is its EcoSIS ``_id``
and each package is a dataset of the collection, referenced by
``dataset_id``. Package licence labels are mapped to SPDX identifiers (or
``LicenseRef-EcoSIS-*`` definitions) by ``ECOSIS_LICENSE_MAP``; a label
the map does not know becomes ``NOASSERTION`` with the original text in the
dataset's ``license_note``.

Reference: https://ecosis.org
"""

import email.utils
import hashlib
import http.client
import json
import logging
import math
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterator, Mapping, Sequence

from tqdm import tqdm

from openspeclib.loaders.base import BaseLoader, monotonic_axis
from openspeclib.loaders.base import spectrum_id as format_spectrum_id
from openspeclib.loaders.ecosis_scales import (
    DARK_LEVEL_RATIO,
    DOMINANT_VALUE_FRACTION,
    MIN_COVERAGE,
    SUPPORTED_DIVISORS,
    UNIT_CEILING,
    count_out_of_range,
    divisor_from_levels,
    dominant_value_fraction,
    resolve_spectrum_divisor,
    scale_and_mask,
    spectrum_level,
)
from openspeclib.models import (
    CollectionInfo,
    DatasetInfo,
    Geometry,
    Inclusion,
    LicenseRef,
    Material,
    MaterialCategory,
    Measurement,
    Quantity,
    Sample,
    SpectralData,
    SpectrumRecord,
    WavelengthUnit,
)

logger = logging.getLogger(__name__)

ECOSIS_API_BASE = "https://ecosis.org/api"
DATASETS_FILE = Path(__file__).parent / "ecosis_datasets.json"
# Page size to *request*. The API serves at most 100 items per response
# regardless, so pagination always advances by the number of items actually
# returned rather than by this constant.
SPECTRA_PAGE_SIZE = 500
# A 200 response with an empty items list while more spectra remain is a
# soft failure (rate limiting), not end-of-data — retry with backoff this
# many times before declaring the dataset unfetchable.
_EMPTY_PAGE_RETRIES = 5
# Politeness delay between spectra page requests. Full mirrors of the
# curated datasets take >1,000 requests.
_REQUEST_THROTTLE_S = 0.2
# Attempts per API request, and the cap on any single exponential backoff
# wait. With backoff (1, 2, 4, … 60 s) ten attempts ride out several minutes
# of 502/503 bursts before a dataset is declared unfetchable.
_API_RETRIES = 10
_MAX_BACKOFF_S = 60.0
# Cap on an explicit server ``Retry-After`` request.
_MAX_RETRY_AFTER_S = 300.0
# Characters read per chunk by the streaming bundle reader. The largest
# bundles are several GB, far too big to ``json.loads`` in one go.
_READ_CHUNK_CHARS = 1 << 22
# Largest single JSON value (one spectrum, or the package metadata) the
# streaming reader will buffer before declaring the bundle corrupt.
_MAX_VALUE_CHARS = 1 << 26

INCLUSION_MODES: tuple[str, ...] = ("full", "sample", "exclude")
DEDUPE_KEYS: tuple[str, ...] = ("content", "datapoints")

# Per-spectrum field values that mean "no value" in EcoSIS uploads.
_MISSING_TOKENS = frozenset({"", "-9999", "na", "n/a", "nan", "null", "none"})

ECOSIS_CATEGORY_MAP: dict[str, MaterialCategory] = {
    "leaf": MaterialCategory.VEGETATION,
    "canopy": MaterialCategory.VEGETATION,
    "vegetation": MaterialCategory.VEGETATION,
    "flower": MaterialCategory.VEGETATION,
    "branch": MaterialCategory.NPV,
    "bark": MaterialCategory.NPV,
    "litter": MaterialCategory.NPV,
    "npv": MaterialCategory.NPV,
    "npv / litter": MaterialCategory.NPV,
    "npv / soil": MaterialCategory.SOIL,
    "soil": MaterialCategory.SOIL,
    "ground / soil": MaterialCategory.SOIL,
    "ground": MaterialCategory.SOIL,
    "water": MaterialCategory.WATER,
    "snow": MaterialCategory.SNOW_ICE,
    "mineral": MaterialCategory.MINERAL,
    "rock": MaterialCategory.ROCK,
    "gravel / rock": MaterialCategory.ROCK,
    "asphalt": MaterialCategory.MAN_MADE,
    "concrete": MaterialCategory.MAN_MADE,
    "brick/paver": MaterialCategory.MAN_MADE,
    "paint": MaterialCategory.MAN_MADE,
    "road/gravel": MaterialCategory.MAN_MADE,
    "other": MaterialCategory.OTHER,
    "reference": MaterialCategory.OTHER,
}

ECOSIS_QUANTITY_MAP: dict[str, Quantity] = {
    "reflectance": Quantity.REFLECTANCE,
    "transmittance": Quantity.TRANSMITTANCE,
    "absorbance": Quantity.ABSORBANCE,
}

# Quantities EcoSIS packages upload that the schema cannot represent.
# Absorptance (1 - R - T, a fraction) is not log10 absorbance; such spectra
# are dropped with a counted reason rather than mislabelled.
UNSUPPORTED_QUANTITIES: frozenset[str] = frozenset({"absorptance"})

COLLECTION_ID = "ecosis"

# Geometry of a record whose dataset policy maps none (spec section 4.2:
# ``unknown`` rather than a guess). Each curated package's geometry is decided
# from its metadata in ``ecosis_datasets.json`` (``geometry`` blocks, with the
# evidence recorded alongside).
ECOSIS_GEOMETRY = Geometry.UNKNOWN

# A duplicate group this large is almost certainly a fill-down or copy error
# in the upload rather than an honest repeat; it is logged as a warning.
_LARGE_DUPLICATE_GROUP = 10

# EcoSIS licence labels (``metadata.ecosis.license``, CKAN licence titles)
# that name no licence version are defined as ``LicenseRef-EcoSIS-*`` rather
# than mapped to a versioned SPDX id, which would claim a version the
# package never stated.
ECOSIS_LICENSE_REFS: dict[str, LicenseRef] = {
    "LicenseRef-EcoSIS-CC-BY": LicenseRef(
        name="Creative Commons Attribution (version not stated)",
        text="EcoSIS licence label 'Creative Commons Attribution'; no version is given.",
    ),
    "LicenseRef-EcoSIS-CC-BY-SA": LicenseRef(
        name="Creative Commons Attribution Share-Alike (version not stated)",
        text=(
            "EcoSIS licence label 'Creative Commons Attribution Share-Alike'; no version "
            "is given."
        ),
    ),
    "LicenseRef-EcoSIS-CC-NC": LicenseRef(
        name="Creative Commons Non-Commercial (any version)",
        text="EcoSIS licence label 'Creative Commons Non-Commercial (Any)'.",
    ),
    "LicenseRef-EcoSIS-Other-Open": LicenseRef(
        name="Other open licence (unspecified)",
        text=(
            "EcoSIS licence label 'Other (Open)': the package declares an open licence "
            "without naming it. Check the package page for terms."
        ),
    ),
}

# Exact EcoSIS licence label -> SPDX identifier or LicenseRef. Built from the
# labels of the curated packages; anything else maps to NOASSERTION.
ECOSIS_LICENSE_MAP: dict[str, str] = {
    "Open Data Commons Open Database License (ODbL)": "ODbL-1.0",
    "Open Data Commons Attribution License": "ODC-By-1.0",
    "Creative Commons Attribution": "LicenseRef-EcoSIS-CC-BY",
    "Creative Commons Attribution Share-Alike": "LicenseRef-EcoSIS-CC-BY-SA",
    "Creative Commons Non-Commercial (Any)": "LicenseRef-EcoSIS-CC-NC",
    "Other (Open)": "LicenseRef-EcoSIS-Other-Open",
}

ECOSIS_COLLECTION = CollectionInfo(
    id=COLLECTION_ID,
    title="EcoSIS (OpenSpecLib collection)",
    description=(
        "A curated selection of packages from the Ecological Spectral Information System "
        "(EcoSIS): leaf, canopy, litter, soil, water and urban-material spectra contributed "
        "by individual research groups. Each package is a dataset with its own licence and "
        "citation."
    ),
    url="https://ecosis.org",
    publisher="Ecological Spectral Information System (EcoSIS)",
    license="NOASSERTION",
    license_note=(
        "Licences vary by dataset. The per-row `license` column and each dataset's `license` "
        "in the footer `datasets` are authoritative."
    ),
    license_refs=ECOSIS_LICENSE_REFS,
    citation=(
        "Ecological Spectral Information System (EcoSIS), https://ecosis.org. "
        "Cite each contributing dataset; per-dataset citations are listed in the footer "
        "`datasets`."
    ),
    inclusion=Inclusion.SUBSET,
    inclusion_notes=(
        "A curated selection of EcoSIS packages. Each listed package is included in full, as "
        "a deterministic stratified sample, or excluded, under a recorded per-package policy "
        "(src/openspeclib/loaders/ecosis_datasets.json; see docs/ecosis-curation.md). Within "
        "included packages, repeated uploads of the same spectrum are collapsed, duplicate "
        "groups whose copies are labelled as different materials are dropped (or, where one "
        "measurement is documented as linked to several samples, kept once), and spectra "
        "of an unsupported quantity (absorptance), on an implausible scale, with too few valid "
        "bands or that are constant no-data fills are dropped. Modelled spectra (the EMIT water "
        "library) are kept with the 'simulated' processing flag. Each dataset's inclusion, "
        "inclusion_notes, spectra_available and spectra_included give the details."
    ),
    keywords=["vegetation", "leaf", "canopy", "field spectroscopy"],
)


def map_ecosis_license(label: str | None) -> tuple[str, str | None]:
    """Map an EcoSIS package licence label to a Format v1 licence value.

    Args:
        label: The package's ``metadata.ecosis.license`` value.

    Returns:
        ``(license, license_note)``: an SPDX id or ``LicenseRef-`` from
        ``ECOSIS_LICENSE_MAP`` with no note, or ``NOASSERTION`` with a note
        quoting the original label.
    """
    text = (label or "").strip()
    mapped = ECOSIS_LICENSE_MAP.get(text)
    if mapped is not None:
        return mapped, None
    if not text or text.casefold() == "license not specified":
        shown = f" (licence field: {text!r})" if text else ""
        return "NOASSERTION", (
            f"The EcoSIS package states no licence{shown}. The EcoSIS data use policy at "
            "https://ecosis.org may apply; check the package page before reuse."
        )
    return "NOASSERTION", f"EcoSIS licence label not mapped to SPDX: {text!r}."


# ---------------------------------------------------------------------------
# Inclusion policy
# ---------------------------------------------------------------------------


def _norm_key(value: str) -> str:
    """Normalise a stratum/unit/lookup key: collapse whitespace, casefold.

    Args:
        value: The raw key.

    Returns:
        The normalised key.
    """
    return " ".join(value.split()).casefold()


@dataclass(frozen=True)
class SamplingSpec:
    """Deterministic stratified sampling parameters for one dataset.

    Attributes:
        stratify_by: Per-spectrum field whose value defines the stratum.
        max_total: Cap on the total number of spectra kept, or ``None``.
        max_per_stratum: Cap on spectra kept per stratum, or ``None``.
        min_per_stratum: Minimum kept per stratum (when available) before the
            remainder of ``max_total`` is shared proportionally.
        one_per: Optional per-spectrum field (e.g. a leaf id) identifying
            replicate groups; at most one spectrum per group is sampled.
    """

    stratify_by: str
    max_total: int | None = None
    max_per_stratum: int | None = None
    min_per_stratum: int = 1
    one_per: str | None = None


@dataclass(frozen=True)
class IdentitySpec:
    """How to derive material name, category and keywords for a dataset.

    Attributes:
        fixed_material_name: Name used for every spectrum, ahead of any
            per-spectrum naming.
        material_name_field: Per-spectrum field that, when present, names the
            material ahead of the default naming chain.
        material_name_template: ``str.format`` template applied to that
            field's (mapped) value, e.g. ``"{} leaf"``.
        material_name_map: Mapping of raw field values to names, applied to
            ``material_name_field``, ``fallback_name_fields`` and
            ``keyword_fields`` values (also used to merge sampling strata
            when ``stratify_by`` is the name field).
        fallback_name_fields: Per-spectrum fields tried, in order, when
            Common Name and Latin name are empty, ahead of the (often opaque)
            Spectra label.
        material_name: Constant name used when nothing else applies, in place
            of the dataset title.
        keywords: Constant keywords added to every record.
        keyword_fields: Per-spectrum fields whose values become keywords.
        category: Constant category, used when ``category_map`` does not
            match.
        category_map: Normalised Target Type value -> category, consulted
            before the built-in mapping.
        category_from_max_field: ``(field, category)`` pairs; the category of
            the field with the largest numeric value wins (e.g. dominant
            cover fraction).
        target_type_field: Per-spectrum field holding the target type
            (subcategory and category input); ``"Target Type"`` by default.
        target_names: Normalised target type -> material name, applied ahead
            of per-spectrum names (e.g. soil spectra in a crop dataset carry
            the crop's Common Name but measured soil).
        mixture_threshold: With ``category_from_max_field``, a spectrum whose
            largest fraction is below this value is a ``mixture``.
    """

    fixed_material_name: str | None = None
    material_name_field: str | None = None
    material_name_template: str = "{}"
    material_name_map: Mapping[str, str] = field(default_factory=dict)
    fallback_name_fields: tuple[str, ...] = ()
    material_name: str | None = None
    keywords: tuple[str, ...] = ()
    keyword_fields: tuple[str, ...] = ()
    category: MaterialCategory | None = None
    category_map: Mapping[str, MaterialCategory] = field(default_factory=dict)
    category_from_max_field: tuple[tuple[str, MaterialCategory], ...] = ()
    target_type_field: str = "Target Type"
    target_names: Mapping[str, str] = field(default_factory=dict)
    mixture_threshold: float | None = None


# Measurement-setup attributes a policy ``setup`` block may assign per spectrum.
SETUP_ATTRS: tuple[str, ...] = (
    "geometry",
    "instrument",
    "instrument_type",
    "light_source",
    "foreoptic",
    "acquisition_method",
    "venue",
)


@dataclass(frozen=True)
class SetupSpec:
    """How to assign each spectrum's measurement setup (geometry, instrument …).

    A setup is a partial mapping of ``SETUP_ATTRS`` to values; a ``None``
    value states explicitly that the attribute is unknown. Per spectrum the
    ``default`` setup is updated by the matching ``resources`` setup, then by
    the ``field_map`` setup for its ``field_name`` value, then by the
    ``instrument_map`` setup for its ``instrument_field`` value. An attribute
    no setup assigns falls back to the package-level value only when the
    package lists exactly one, so a multi-setup package never lends one
    setup's light source, foreoptic or instrument to another's spectra.

    Attributes:
        default: Setup applying to every spectrum.
        field_name: Optional per-spectrum field naming the setup (e.g.
            HyspIRI's ``Acquisition_Method``); ``field`` in the policy file.
        field_map: Normalised ``field_name`` value -> setup. A value missing
            from the map leaves geometry, light source, foreoptic and
            acquisition method unknown rather than guessed.
        resources: Compiled regexes on the resource filename -> setup.
        instrument_field: Optional per-spectrum field naming the instrument.
        instrument_map: Normalised ``instrument_field`` value -> setup
            (usually ``instrument`` and ``instrument_type``); an unmapped
            value is used verbatim as the instrument.
        evidence: The metadata the mapping rests on (required).
    """

    default: Mapping[str, Any] = field(default_factory=dict)
    field_name: str | None = None
    field_map: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    resources: tuple[tuple["re.Pattern[str]", Mapping[str, Any]], ...] = ()
    instrument_field: str | None = None
    instrument_map: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evidence: str = ""


@dataclass(frozen=True)
class ConflictSpec:
    """What to do with duplicate groups whose copies carry different labels.

    Attributes:
        action: ``"drop"`` (default: no copy's label can be trusted) or
            ``"keep_one"`` (one measurement legitimately linked to several
            labelled samples, e.g. a canopy spectrum whose field of view
            holds several harvested species).
        material_name: Name for a kept group (``keep_one``).
        link_field: Per-spectrum field whose values from every copy are
            recorded in ``extra["linked_sample_ids"]`` (``keep_one``).
    """

    action: str = "drop"
    material_name: str | None = None
    link_field: str | None = None


@dataclass(frozen=True)
class DatasetPolicy:
    """Machine-readable inclusion policy for one curated EcoSIS dataset.

    Attributes:
        id: EcoSIS package id.
        name: Human-readable dataset name.
        inclusion: One of ``INCLUSION_MODES``.
        reason: Why the dataset has this inclusion mode.
        available_spectra: Spectra count declared by EcoSIS at curation time.
        sampling: Sampling parameters (required iff ``inclusion == "sample"``).
        identity: Material naming and category rules.
        simulated: Whether the spectra are simulated rather than measured.
        scale_divisor: Explicit source scale divisor, or ``None`` to infer.
        exclude_resources: Compiled regexes matched against each spectrum's
            source resource filename; matches are not ingested.
        drop_spectra: Spectrum ids dropped at ingest, with reasons.
        carry_fields: Per-spectrum fields copied into additional properties.
        resource_techniques: Compiled regexes on the resource filename mapped
            to a (lower-case) measurement quantity.
        units_field: Per-spectrum field naming the value units.
        units_divisors: Normalised ``units_field`` value -> divisor; a mapped
            value is authoritative for that spectrum's scale.
        dedupe_key: ``"datapoints"`` or ``"content"`` (see module docs).
        setup: Measurement-setup rules (default: geometry ``unknown``).
        conflicting_duplicates: Handling of duplicate groups with
            conflicting labels.
        reference: Reference standard the values are relative to, where the
            package states it (e.g. ``spectralon``).
    """

    id: str
    name: str
    inclusion: str
    reason: str
    available_spectra: int | None = None
    sampling: SamplingSpec | None = None
    identity: IdentitySpec = field(default_factory=IdentitySpec)
    simulated: bool = False
    scale_divisor: int | None = None
    exclude_resources: tuple["re.Pattern[str]", ...] = ()
    drop_spectra: Mapping[str, str] = field(default_factory=dict)
    carry_fields: tuple[str, ...] = ()
    resource_techniques: tuple[tuple["re.Pattern[str]", str], ...] = ()
    units_field: str | None = None
    units_divisors: Mapping[str, int] = field(default_factory=dict)
    dedupe_key: str = "datapoints"
    setup: SetupSpec = field(default_factory=SetupSpec)
    conflicting_duplicates: ConflictSpec = field(default_factory=ConflictSpec)
    reference: str | None = None


_POLICY_KEYS = frozenset(
    {
        "id",
        "name",
        "inclusion",
        "reason",
        "available_spectra",
        "sampling",
        "identity",
        "simulated",
        "scale_divisor",
        "exclude_resources",
        "drop_spectra",
        "carry_fields",
        "resource_techniques",
        "units_field",
        "units_divisors",
        "dedupe_key",
        "setup",
        "reference",
        "conflicting_duplicates",
    }
)
_SETUP_KEYS = frozenset(
    {"default", "field", "field_map", "resources", "instrument_field", "instrument_map", "evidence"}
)
_CONFLICT_KEYS = frozenset({"action", "material_name", "link_field"})
_SAMPLING_KEYS = frozenset(
    {"stratify_by", "max_total", "max_per_stratum", "min_per_stratum", "one_per"}
)
_IDENTITY_KEYS = frozenset(
    {
        "fixed_material_name",
        "material_name_field",
        "material_name_template",
        "material_name_map",
        "fallback_name_fields",
        "material_name",
        "keywords",
        "keyword_fields",
        "category",
        "category_map",
        "category_from_max_field",
        "target_type_field",
        "target_names",
        "mixture_threshold",
    }
)


def _require_str(value: Any, what: str) -> str:
    """Validate a non-empty string policy value.

    Args:
        value: The raw value.
        what: Description used in the error message.

    Returns:
        The stripped string.

    Raises:
        ValueError: If ``value`` is not a non-empty string.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string, got {value!r}")
    return value.strip()


def _optional_str(value: Any, what: str) -> str | None:
    """Validate an optional non-empty string policy value.

    Args:
        value: The raw value (``None`` allowed).
        what: Description used in the error message.

    Returns:
        The stripped string, or ``None``.
    """
    return None if value is None else _require_str(value, what)


def _optional_int(value: Any, what: str, minimum: int) -> int | None:
    """Validate an optional integer policy value.

    Args:
        value: The raw value (``None`` allowed).
        what: Description used in the error message.
        minimum: Smallest allowed value.

    Returns:
        The integer, or ``None``.

    Raises:
        ValueError: If ``value`` is not an integer ``>= minimum``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{what} must be an integer >= {minimum}, got {value!r}")
    return int(value)


def _str_tuple(value: Any, what: str) -> tuple[str, ...]:
    """Validate a list of non-empty strings.

    Args:
        value: The raw value (``None`` allowed, meaning empty).
        what: Description used in the error message.

    Returns:
        The strings as a tuple.

    Raises:
        ValueError: If ``value`` is not a list of non-empty strings.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list of strings, got {value!r}")
    return tuple(_require_str(v, what) for v in value)


def _str_map(value: Any, what: str) -> dict[str, str]:
    """Validate a mapping of strings to non-empty strings.

    Args:
        value: The raw value (``None`` allowed, meaning empty).
        what: Description used in the error message.

    Returns:
        The mapping with stripped keys and values.

    Raises:
        ValueError: If ``value`` is not such a mapping.
    """
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in value.items()
    ):
        raise ValueError(f"{what} must map strings to non-empty strings")
    return {k.strip(): v.strip() for k, v in value.items()}


def _category(value: Any, what: str) -> MaterialCategory:
    """Validate a material category name.

    Args:
        value: The raw value.
        what: Description used in the error message.

    Returns:
        The category.

    Raises:
        ValueError: If ``value`` is not a ``MaterialCategory`` value.
    """
    try:
        return MaterialCategory(value)
    except ValueError as exc:
        allowed = sorted(c.value for c in MaterialCategory)
        raise ValueError(f"{what} must be one of {allowed}, got {value!r}") from exc


def _check_keys(entry: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    """Reject unknown keys so typos in the policy file fail loudly.

    Args:
        entry: The mapping to check.
        allowed: Allowed key names.
        what: Description used in the error message.

    Raises:
        ValueError: If ``entry`` has keys outside ``allowed``.
    """
    unknown = set(entry) - allowed
    if unknown:
        raise ValueError(f"{what} has unknown key(s): {', '.join(sorted(unknown))}")


def _parse_sampling(raw: Any, where: str) -> SamplingSpec:
    """Parse and validate a ``sampling`` block.

    Args:
        raw: The raw JSON value.
        where: Dataset context for error messages.

    Returns:
        The validated sampling spec.

    Raises:
        ValueError: If the block is malformed.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: sampling must be an object")
    _check_keys(raw, _SAMPLING_KEYS, f"{where}: sampling")
    stratify_by = _require_str(raw.get("stratify_by"), f"{where}: sampling.stratify_by")
    max_total = _optional_int(raw.get("max_total"), f"{where}: sampling.max_total", 1)
    max_per = _optional_int(raw.get("max_per_stratum"), f"{where}: sampling.max_per_stratum", 1)
    min_per = _optional_int(raw.get("min_per_stratum"), f"{where}: sampling.min_per_stratum", 0)
    if max_total is None and max_per is None:
        raise ValueError(f"{where}: sampling needs max_total and/or max_per_stratum")
    min_per = 1 if min_per is None else min_per
    if max_per is not None and min_per > max_per:
        raise ValueError(f"{where}: sampling.min_per_stratum exceeds max_per_stratum")
    return SamplingSpec(
        stratify_by=stratify_by,
        max_total=max_total,
        max_per_stratum=max_per,
        min_per_stratum=min_per,
        one_per=_optional_str(raw.get("one_per"), f"{where}: sampling.one_per"),
    )


def _parse_identity(raw: Any, where: str) -> IdentitySpec:
    """Parse and validate an ``identity`` block.

    Args:
        raw: The raw JSON value (``None`` allowed).
        where: Dataset context for error messages.

    Returns:
        The validated identity spec.

    Raises:
        ValueError: If the block is malformed.
    """
    if raw is None:
        return IdentitySpec()
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: identity must be an object")
    _check_keys(raw, _IDENTITY_KEYS, f"{where}: identity")
    template = raw.get("material_name_template", "{}")
    if not isinstance(template, str) or template.count("{}") != 1:
        raise ValueError(f"{where}: identity.material_name_template must contain one '{{}}'")
    category_map = {
        _norm_key(k): _category(v, f"{where}: identity.category_map[{k!r}]")
        for k, v in _str_map(raw.get("category_map"), f"{where}: identity.category_map").items()
    }
    from_max = tuple(
        (k, _category(v, f"{where}: identity.category_from_max_field[{k!r}]"))
        for k, v in _str_map(
            raw.get("category_from_max_field"), f"{where}: identity.category_from_max_field"
        ).items()
    )
    category_raw = raw.get("category")
    return IdentitySpec(
        fixed_material_name=_optional_str(
            raw.get("fixed_material_name"), f"{where}: identity.fixed_material_name"
        ),
        material_name_field=_optional_str(
            raw.get("material_name_field"), f"{where}: identity.material_name_field"
        ),
        material_name_template=template,
        material_name_map=_str_map(
            raw.get("material_name_map"), f"{where}: identity.material_name_map"
        ),
        fallback_name_fields=_str_tuple(
            raw.get("fallback_name_fields"), f"{where}: identity.fallback_name_fields"
        ),
        material_name=_optional_str(raw.get("material_name"), f"{where}: identity.material_name"),
        keywords=tuple(
            k.lower() for k in _str_tuple(raw.get("keywords"), f"{where}: identity.keywords")
        ),
        keyword_fields=_str_tuple(raw.get("keyword_fields"), f"{where}: identity.keyword_fields"),
        category=(
            _category(category_raw, f"{where}: identity.category")
            if category_raw is not None
            else None
        ),
        category_map=category_map,
        category_from_max_field=from_max,
        target_type_field=_optional_str(
            raw.get("target_type_field"), f"{where}: identity.target_type_field"
        )
        or "Target Type",
        target_names={
            _norm_key(k): v
            for k, v in _str_map(raw.get("target_names"), f"{where}: identity.target_names").items()
        },
        mixture_threshold=_mixture_threshold(raw.get("mixture_threshold"), from_max, where),
    )


def _mixture_threshold(
    value: Any, from_max: tuple[tuple[str, MaterialCategory], ...], where: str
) -> float | None:
    """Validate ``identity.mixture_threshold``.

    Args:
        value: The raw value (``None`` allowed).
        from_max: The parsed ``category_from_max_field`` pairs.
        where: Dataset context for error messages.

    Returns:
        The threshold, or ``None``.

    Raises:
        ValueError: If it is not a number in (0, 1] or is given without
            ``category_from_max_field``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise ValueError(f"{where}: identity.mixture_threshold must be a number in (0, 1]")
    if not from_max:
        raise ValueError(f"{where}: identity.mixture_threshold needs category_from_max_field")
    return float(value)


def _geometry_value(value: Any, what: str) -> Geometry:
    """Validate a geometry vocabulary value.

    Args:
        value: The raw value.
        what: Description used in the error message.

    Returns:
        The geometry.

    Raises:
        ValueError: If ``value`` is not a ``Geometry`` value.
    """
    try:
        return Geometry(value)
    except ValueError as exc:
        allowed = sorted(g.value for g in Geometry)
        raise ValueError(f"{what} must be one of {allowed}, got {value!r}") from exc


def _parse_setup_values(raw: Any, what: str) -> dict[str, Any]:
    """Validate one setup object (a partial mapping of ``SETUP_ATTRS``).

    Args:
        raw: The raw JSON object.
        what: Description used in error messages.

    Returns:
        The setup, with ``geometry`` as a ``Geometry`` and other attributes as
        non-empty strings or ``None`` (explicitly unknown).

    Raises:
        ValueError: If it has unknown keys or invalid values.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be an object")
    _check_keys(raw, frozenset(SETUP_ATTRS), what)
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "geometry":
            out[key] = _geometry_value(value, f"{what}.geometry")
        elif value is None:
            out[key] = None
        else:
            out[key] = _require_str(value, f"{what}.{key}")
    return out


def _parse_setup(raw: Any, where: str) -> SetupSpec:
    """Parse and validate a ``setup`` block.

    Args:
        raw: The raw JSON value (``None`` allowed: geometry ``unknown`` and
            package-level setup values where unambiguous).
        where: Dataset context for error messages.

    Returns:
        The validated setup spec.

    Raises:
        ValueError: If the block is malformed, names a geometry outside the
            vocabulary, or omits its evidence.
    """
    if raw is None:
        return SetupSpec()
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: setup must be an object")
    _check_keys(raw, _SETUP_KEYS, f"{where}: setup")

    def _setup_map(key: str) -> dict[str, dict[str, Any]]:
        value = raw.get(key) or {}
        if not isinstance(value, dict):
            raise ValueError(f"{where}: setup.{key} must be an object")
        return {k: _parse_setup_values(v, f"{where}: setup.{key}[{k!r}]") for k, v in value.items()}

    field_name = _optional_str(raw.get("field"), f"{where}: setup.field")
    field_map = {_norm_key(k): v for k, v in _setup_map("field_map").items()}
    if bool(field_name) != bool(field_map):
        raise ValueError(f"{where}: setup.field and setup.field_map go together")
    instrument_field = _optional_str(
        raw.get("instrument_field"), f"{where}: setup.instrument_field"
    )
    instrument_map = {_norm_key(k): v for k, v in _setup_map("instrument_map").items()}
    if instrument_map and not instrument_field:
        raise ValueError(f"{where}: setup.instrument_map needs setup.instrument_field")
    resources = tuple(
        (_compile(pattern, f"{where}: setup.resources"), value)
        for pattern, value in _setup_map("resources").items()
    )
    return SetupSpec(
        default=_parse_setup_values(raw.get("default", {}), f"{where}: setup.default"),
        field_name=field_name,
        field_map=field_map,
        resources=resources,
        instrument_field=instrument_field,
        instrument_map=instrument_map,
        evidence=_require_str(raw.get("evidence"), f"{where}: setup.evidence"),
    )


def _parse_conflicts(raw: Any, where: str) -> ConflictSpec:
    """Parse and validate a ``conflicting_duplicates`` block.

    Args:
        raw: The raw JSON value (``None`` allowed: drop).
        where: Dataset context for error messages.

    Returns:
        The validated spec.

    Raises:
        ValueError: If the block is malformed.
    """
    if raw is None:
        return ConflictSpec()
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: conflicting_duplicates must be an object")
    _check_keys(raw, _CONFLICT_KEYS, f"{where}: conflicting_duplicates")
    action = raw.get("action", "drop")
    if action not in ("drop", "keep_one"):
        raise ValueError(f"{where}: conflicting_duplicates.action must be 'drop' or 'keep_one'")
    material_name = _optional_str(
        raw.get("material_name"), f"{where}: conflicting_duplicates.material_name"
    )
    if action == "keep_one" and not material_name:
        raise ValueError(f"{where}: conflicting_duplicates keep_one needs a material_name")
    return ConflictSpec(
        action=action,
        material_name=material_name,
        link_field=_optional_str(
            raw.get("link_field"), f"{where}: conflicting_duplicates.link_field"
        ),
    )


def _compile(pattern: str, what: str) -> "re.Pattern[str]":
    """Compile a policy regex.

    Args:
        pattern: The regex source.
        what: Description used in the error message.

    Returns:
        The compiled pattern.

    Raises:
        ValueError: If the regex is invalid.
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{what}: bad regex {pattern!r}: {exc}") from exc


def _parse_policy(entry: Any) -> DatasetPolicy:
    """Parse and validate one ``ecosis_datasets.json`` entry.

    Args:
        entry: The raw JSON object for one dataset.

    Returns:
        The validated policy.

    Raises:
        ValueError: If the entry is malformed or internally inconsistent.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"dataset entry must be an object, got {entry!r}")
    dataset_id = _require_str(entry.get("id"), "dataset id")
    where = f"dataset {dataset_id}"
    _check_keys(entry, _POLICY_KEYS, where)
    inclusion = entry.get("inclusion")
    if inclusion not in INCLUSION_MODES:
        raise ValueError(f"{where}: inclusion must be one of {INCLUSION_MODES}, got {inclusion!r}")
    sampling_raw = entry.get("sampling")
    if inclusion == "sample" and sampling_raw is None:
        raise ValueError(f"{where}: inclusion 'sample' requires a sampling block")
    if inclusion != "sample" and sampling_raw is not None:
        raise ValueError(f"{where}: sampling is only valid with inclusion 'sample'")
    scale_divisor = entry.get("scale_divisor")
    if scale_divisor is not None and scale_divisor not in SUPPORTED_DIVISORS:
        raise ValueError(f"{where}: scale_divisor must be one of {SUPPORTED_DIVISORS}")
    simulated = entry.get("simulated", False)
    if not isinstance(simulated, bool):
        raise ValueError(f"{where}: simulated must be a boolean")
    dedupe_key = entry.get("dedupe_key", "datapoints")
    if dedupe_key not in DEDUPE_KEYS:
        raise ValueError(f"{where}: dedupe_key must be one of {DEDUPE_KEYS}")

    patterns = tuple(
        _compile(p, f"{where}: exclude_resources")
        for p in _str_tuple(entry.get("exclude_resources"), f"{where}: exclude_resources")
    )
    known_quantities = set(ECOSIS_QUANTITY_MAP) | set(UNSUPPORTED_QUANTITIES)
    resource_techniques: list[tuple[re.Pattern[str], str]] = []
    for pattern, quantity in _str_map(
        entry.get("resource_techniques"), f"{where}: resource_techniques"
    ).items():
        if quantity.lower() not in known_quantities:
            raise ValueError(
                f"{where}: resource_techniques value {quantity!r} must be one of "
                f"{sorted(known_quantities)}"
            )
        resource_techniques.append(
            (_compile(pattern, f"{where}: resource_techniques"), quantity.lower())
        )

    units_field = _optional_str(entry.get("units_field"), f"{where}: units_field")
    units_raw = entry.get("units_divisors", {})
    if not isinstance(units_raw, dict) or not all(
        isinstance(k, str) and v in SUPPORTED_DIVISORS for k, v in units_raw.items()
    ):
        raise ValueError(
            f"{where}: units_divisors must map unit strings to one of {SUPPORTED_DIVISORS}"
        )
    if bool(units_field) != bool(units_raw):
        raise ValueError(f"{where}: units_field and units_divisors must be given together")

    drop = _str_map(entry.get("drop_spectra"), f"{where}: drop_spectra")
    return DatasetPolicy(
        id=dataset_id,
        name=_require_str(entry.get("name"), f"{where}: name"),
        inclusion=inclusion,
        reason=_require_str(entry.get("reason"), f"{where}: reason"),
        available_spectra=_optional_int(
            entry.get("available_spectra"), f"{where}: available_spectra", 0
        ),
        sampling=_parse_sampling(sampling_raw, where) if sampling_raw is not None else None,
        identity=_parse_identity(entry.get("identity"), where),
        simulated=simulated,
        scale_divisor=scale_divisor,
        exclude_resources=patterns,
        drop_spectra=drop,
        carry_fields=_str_tuple(entry.get("carry_fields"), f"{where}: carry_fields"),
        resource_techniques=tuple(resource_techniques),
        units_field=units_field,
        units_divisors={_norm_key(k): int(v) for k, v in units_raw.items()},
        dedupe_key=dedupe_key,
        setup=_parse_setup(entry.get("setup"), where),
        conflicting_duplicates=_parse_conflicts(entry.get("conflicting_duplicates"), where),
        reference=_optional_str(entry.get("reference"), f"{where}: reference"),
    )


def load_dataset_policies(path: Path = DATASETS_FILE) -> list[DatasetPolicy]:
    """Load and validate the curated dataset policy file.

    Args:
        path: Path to ``ecosis_datasets.json``.

    Returns:
        Policies in file order.

    Raises:
        ValueError: If the file is not a list of valid, uniquely-identified
            dataset entries.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON list of dataset entries")
    policies = [_parse_policy(entry) for entry in raw]
    seen: set[str] = set()
    for policy in policies:
        if policy.id in seen:
            raise ValueError(f"{path}: duplicate dataset id {policy.id}")
        seen.add(policy.id)
    return policies


def _load_curated_datasets() -> list[DatasetPolicy]:
    """Load the curated dataset policies from the JSON config file.

    Returns:
        The validated policies, in file order.
    """
    return load_dataset_policies(DATASETS_FILE)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _is_retryable_status(status: int) -> bool:
    """Whether an HTTP status code denotes a transient, retryable failure.

    Args:
        status: HTTP status code of a failed response.

    Returns:
        ``True`` for 429 (rate limited) and any 5xx server error.
    """
    return status == 429 or 500 <= status <= 599


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP date).

    Args:
        exc: The HTTP error whose headers to inspect.

    Returns:
        Seconds to wait (clamped to ``[0, _MAX_RETRY_AFTER_S]``), or ``None``
        when the header is absent or unparseable.
    """
    raw = exc.headers.get("Retry-After") if exc.headers is not None else None
    if not raw:
        return None
    raw = raw.strip()
    seconds: float
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at ``_MAX_BACKOFF_S``.

    Args:
        attempt: Zero-based retry attempt number.

    Returns:
        Seconds to wait: uniformly drawn from ``[base/2, base]`` where
        ``base = min(2**attempt, _MAX_BACKOFF_S)``.
    """
    base = min(float(2**attempt), _MAX_BACKOFF_S)
    return base / 2 + random.uniform(0.0, base / 2)


def _api_get(path: str, retries: int = _API_RETRIES) -> Any:
    """Fetch JSON from the EcoSIS API, patiently retrying transient failures.

    EcoSIS rate-limits bulk clients and its proxy intermittently returns
    502/503 bursts lasting tens of seconds. 429 and 5xx responses, network
    errors, timeouts and unparseable bodies (proxy error pages) are retried
    with exponential backoff plus jitter (up to ~1 minute per wait), honouring
    ``Retry-After`` (up to 5 minutes) when present. Other 4xx responses are permanent and
    raised immediately.

    Args:
        path: API path (appended to ``ECOSIS_API_BASE``).
        retries: Total number of attempts before giving up.

    Returns:
        Parsed JSON response.

    Raises:
        urllib.error.HTTPError: On a non-retryable status, or when retries are
            exhausted on a retryable one.
        OSError: When retries are exhausted on a network error or timeout.
        ValueError: When retries are exhausted on an unparseable body.
    """
    url = f"{ECOSIS_API_BASE}{path}"
    for attempt in range(retries):
        retry_after: float | None = None
        error: Exception
        try:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if not _is_retryable_status(exc.code) or attempt >= retries - 1:
                raise
            retry_after = _retry_after_seconds(exc)
            error = exc
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # URLError, timeouts, connection resets, truncated reads, and
            # non-JSON bodies (proxy error pages served with 200).
            if attempt >= retries - 1:
                raise
            error = exc
        wait = retry_after if retry_after is not None else _backoff_delay(attempt)
        logger.warning(
            "EcoSIS API request %s failed (%s); retry %d/%d in %.1fs",
            path,
            error,
            attempt + 1,
            retries - 1,
            wait,
        )
        time.sleep(wait)
    raise RuntimeError("unreachable: retry loop exited without returning")  # pragma: no cover


def _iter_spectra_pages(dataset_id: str, expected_total: int) -> Iterator[list[dict[str, Any]]]:
    """Yield every page of a dataset's spectra, or fail — never stop short.

    Historical context: releases v0.0.6/v0.0.7 shipped 21–27k EcoSIS spectra
    out of ~131k actually present in the curated datasets, because the old
    pagination loop treated any transient failure or rate-limited empty page
    as end-of-data and saved the truncated result as if complete. This
    function instead retries empty pages with backoff and raises when the
    dataset cannot be fetched in full.

    Args:
        dataset_id: EcoSIS package identifier.
        expected_total: ``ecosis.spectra_count`` from the package metadata.

    Yields:
        Lists of spectrum items, in API order.

    Raises:
        RuntimeError: If the complete set could not be fetched.
    """
    fetched = 0
    empty_retries = 0
    while fetched < expected_total:
        stop = fetched + SPECTRA_PAGE_SIZE
        page = _api_get(f"/spectra/search/{dataset_id}?start={fetched}&stop={stop}")
        items = page.get("items", [])
        if not items:
            empty_retries += 1
            if empty_retries > _EMPTY_PAGE_RETRIES:
                raise RuntimeError(
                    f"EcoSIS returned {_EMPTY_PAGE_RETRIES + 1} consecutive empty pages at "
                    f"offset {fetched}/{expected_total} for dataset {dataset_id}"
                )
            wait = 2**empty_retries
            logger.warning(
                "Empty spectra page at offset %d/%d for %s; retrying in %ds",
                fetched,
                expected_total,
                dataset_id,
                wait,
            )
            time.sleep(wait)
            continue
        empty_retries = 0
        fetched += len(items)
        yield items
        time.sleep(_REQUEST_THROTTLE_S)

    if fetched > expected_total:
        logger.warning(
            "Fetched %d spectra but metadata declares %d for %s — dataset may have "
            "been updated mid-download",
            fetched,
            expected_total,
            dataset_id,
        )


def _declared_count(metadata: Mapping[str, Any]) -> int:
    """Return the package's declared spectra count, or fail loudly.

    Args:
        metadata: EcoSIS package metadata.

    Returns:
        ``ecosis.spectra_count`` as an integer.

    Raises:
        RuntimeError: If the count is missing, zero or not an integer —
            downloading against it would save an empty or truncated bundle.
    """
    raw = (metadata.get("ecosis") or {}).get("spectra_count")
    count = -1
    if raw is not None and not isinstance(raw, bool):
        try:
            count = int(raw)
        except (TypeError, ValueError):
            count = -1
    if count <= 0:
        raise RuntimeError(
            f"EcoSIS package {metadata.get('_id', '?')} metadata has no valid "
            f"(positive) ecosis.spectra_count ({raw!r})"
        )
    return count


def _fetch_all_spectra(dataset_id: str, expected_total: int) -> list[dict[str, Any]]:
    """Fetch every spectrum of a dataset into memory, or fail.

    Args:
        dataset_id: EcoSIS package identifier.
        expected_total: ``ecosis.spectra_count`` from the package metadata.

    Returns:
        All spectra items for the dataset.

    Raises:
        RuntimeError: If the complete set could not be fetched.
    """
    spectra: list[dict[str, Any]] = []
    for page in _iter_spectra_pages(dataset_id, expected_total):
        spectra.extend(page)
    return spectra


def _write_bundle(
    path: Path, metadata: dict[str, Any], pages: Iterator[list[dict[str, Any]]]
) -> int:
    """Stream a ``{"metadata": ..., "spectra": [...]}`` bundle to disk.

    Pages are written as they arrive, so memory stays bounded even for the
    multi-GB datasets. ``metadata`` is always written first, which the
    streaming reader relies on.

    Args:
        path: Destination file (callers write to a temporary name and rename
            on success).
        metadata: Package metadata.
        pages: Iterator of spectrum pages.

    Returns:
        Number of spectra written.
    """
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        fh.write('{"metadata": ')
        json.dump(metadata, fh)
        fh.write(', "spectra": [')
        for page in pages:
            for item in page:
                if count:
                    fh.write(", ")
                json.dump(item, fh)
                count += 1
        fh.write("]}")
    return count


# ---------------------------------------------------------------------------
# Streaming bundle reader
# ---------------------------------------------------------------------------

_DECODER = json.JSONDecoder()
_WHITESPACE = " \t\n\r"


class _JsonStream:
    """Incremental reader over a JSON text file using ``raw_decode``."""

    def __init__(self, fh: IO[str]) -> None:
        """Wrap an open text file.

        Args:
            fh: File handle positioned at the start of the JSON document.
        """
        self._fh = fh
        self._buf = ""
        self._pos = 0
        self._eof = False

    def _fill(self) -> bool:
        """Append the next chunk to the buffer.

        Returns:
            ``False`` at end of file.
        """
        if self._eof:
            return False
        chunk = self._fh.read(_READ_CHUNK_CHARS)
        if not chunk:
            self._eof = True
            return False
        self._buf = self._buf[self._pos :] + chunk
        self._pos = 0
        return True

    def peek(self) -> str:
        """Skip whitespace and return the next character without consuming it.

        Returns:
            The next non-whitespace character, or ``""`` at end of file.
        """
        while True:
            while self._pos < len(self._buf) and self._buf[self._pos] in _WHITESPACE:
                self._pos += 1
            if self._pos < len(self._buf):
                return self._buf[self._pos]
            if not self._fill():
                return ""

    def expect(self, chars: str) -> str:
        """Consume the next non-whitespace character, which must be in ``chars``.

        Args:
            chars: Allowed characters.

        Returns:
            The consumed character.

        Raises:
            ValueError: If the next character is not allowed.
        """
        ch = self.peek()
        if not ch or ch not in chars:
            raise ValueError(f"malformed bundle: expected one of {chars!r}, got {ch!r}")
        self._pos += 1
        return ch

    def value(self) -> Any:
        """Decode and consume the next JSON value.

        Returns:
            The decoded value.

        Raises:
            ValueError: If the value is malformed or truncated.
        """
        self.peek()
        while True:
            try:
                obj, end = _DECODER.raw_decode(self._buf, self._pos)
            except json.JSONDecodeError:
                # Corrupt input would otherwise pull the rest of a multi-GB
                # file into memory one chunk at a time.
                if len(self._buf) - self._pos > _MAX_VALUE_CHARS or not self._fill():
                    raise
                continue
            # A number ending exactly at the buffer edge may continue in the
            # next chunk; make sure the token is complete.
            if end >= len(self._buf) and self._fill():
                continue
            self._pos = end
            return obj


def _iter_bundle(path: Path) -> Iterator[tuple[str, Any]]:
    """Stream a bundle's top-level members without loading it whole.

    Args:
        path: Path to a ``{dataset_id}.json`` bundle.

    Yields:
        ``(key, value)`` for each top-level member except ``spectra``, and
        ``("spectrum", item)`` for each element of the ``spectra`` array, in
        file order.

    Raises:
        ValueError: If the file is not a well-formed bundle object.
    """
    with path.open(encoding="utf-8") as fh:
        stream = _JsonStream(fh)
        stream.expect("{")
        if stream.peek() == "}":
            return
        while True:
            key = stream.value()
            if not isinstance(key, str):
                raise ValueError("malformed bundle: non-string key")
            stream.expect(":")
            if key == "spectra":
                stream.expect("[")
                if stream.peek() == "]":
                    stream.expect("]")
                else:
                    while True:
                        yield "spectrum", stream.value()
                        if stream.expect(",]") == "]":
                            break
            else:
                yield key, stream.value()
            if stream.expect(",}") == "}":
                return


def _iter_bundle_spectra(path: Path) -> Iterator[dict[str, Any]]:
    """Stream only the spectra of a bundle.

    Args:
        path: Path to a ``{dataset_id}.json`` bundle.

    Yields:
        Spectrum items in file order.
    """
    for key, value in _iter_bundle(path):
        if key == "spectrum":
            yield value


def _read_bundle_metadata(path: Path) -> dict[str, Any]:
    """Read a bundle's ``metadata`` member (written first by the downloader).

    Args:
        path: Path to a ``{dataset_id}.json`` bundle.

    Returns:
        The metadata dict.

    Raises:
        ValueError: If the bundle has no metadata object.
    """
    for key, value in _iter_bundle(path):
        if key == "metadata":
            if not isinstance(value, dict):
                raise ValueError(f"{path.name}: metadata is not an object")
            return value
    raise ValueError(f"{path.name}: bundle has no metadata")


def _bundle_is_complete(path: Path) -> bool:
    """Check whether a previously downloaded dataset bundle is complete.

    Old versions of the downloader saved truncated bundles and then skipped
    them forever as "already downloaded". Compare the saved spectra count
    against the metadata's declared count so incomplete files get refetched.
    Spectra are counted by unique ``_id`` so a bundle padded with repeated
    pages is not mistaken for complete. The bundle is streamed, so this is
    safe for multi-GB files.

    Args:
        path: Path to a ``{dataset_id}.json`` bundle.

    Returns:
        ``True`` if the bundle parses, declares a valid spectra count, and
        holds at least that many distinct spectrum ids.
    """
    try:
        declared: int | None = None
        ids: set[str] = set()
        for key, value in _iter_bundle(path):
            if key == "spectrum":
                ids.add(str(value["_id"]))
            elif key == "metadata":
                declared = _declared_count(value)
        return declared is not None and len(ids) >= declared
    except Exception:  # noqa: BLE001 — any unreadable/odd bundle just gets refetched
        return False


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _classify_target_type(target_types: list[str]) -> MaterialCategory:
    """Map EcoSIS Target Type values to a canonical material category.

    Args:
        target_types: List of target type strings from the dataset/spectrum.

    Returns:
        The best-matching material category.
    """
    for raw in target_types:
        key = raw.strip().lower()
        if key in ECOSIS_CATEGORY_MAP:
            return ECOSIS_CATEGORY_MAP[key]
    return MaterialCategory.OTHER


def _first_nonempty(values: list[str]) -> str | None:
    """Return the first non-empty trimmed string in ``values``, or ``None``.

    ECOSIS exposes single-valued metadata fields as one-element lists; this
    helper unwraps them while tolerating empty strings and missing entries.

    Args:
        values: List of candidate values from an ECOSIS metadata field.

    Returns:
        The first non-empty stripped string, or ``None`` if none qualify.
    """
    for raw in values or []:
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


def _field_key(name: str) -> str:
    """Normalise a metadata field name for matching.

    EcoSIS uploads spell the same field differently (``Latin Genus``,
    ``Latin genus``, ``latin_genus``; ``Sample ID``, ``SampleID``,
    ``sample_id``), so names are compared case-insensitively and without
    whitespace, underscores or hyphens.

    Args:
        name: Field name.

    Returns:
        The name casefolded, with whitespace, ``_`` and ``-`` removed.
    """
    return "".join(name.split()).replace("_", "").replace("-", "").casefold()


# Single-entry cache of the last spectrum's normalised key index. It holds a
# reference to the spectrum, so an identity match cannot be a recycled id;
# the key count guards against a dict changed in place between lookups.
_KEY_INDEX: list[Any] = [None, 0, {}]


def _key_index(spectrum: Mapping[str, Any]) -> dict[str, list[str]]:
    """Map normalised field names to a spectrum's actual keys.

    Args:
        spectrum: A spectrum item.

    Returns:
        ``_field_key(name)`` -> the spectrum's keys with that normalised name,
        in sorted order (so the choice among variants is deterministic).
    """
    if _KEY_INDEX[0] is spectrum and _KEY_INDEX[1] == len(spectrum):
        cached: dict[str, list[str]] = _KEY_INDEX[2]
        return cached
    index: dict[str, list[str]] = {}
    for key in sorted(k for k in spectrum if isinstance(k, str)):
        index.setdefault(_field_key(key), []).append(key)
    _KEY_INDEX[:] = [spectrum, len(spectrum), index]
    return index


def _clean_value(raw: Any) -> str | None:
    """Turn a raw metadata value into a trimmed string, or ``None``.

    Args:
        raw: The raw value.

    Returns:
        The stripped text, or ``None`` when absent, structured, empty or a
        placeholder (``-9999``, ``NA``, ``N/A``, ``null`` …).
    """
    if raw is None or isinstance(raw, (dict, list)):
        return None
    text = str(raw).strip()
    if text.casefold() in _MISSING_TOKENS:
        return None
    return text


def _field_str(spectrum: Mapping[str, Any], name: str) -> str | None:
    """Return a per-spectrum metadata field as a trimmed string.

    The field is looked up by its exact name first, then under any spelling
    that differs only in case, whitespace, underscores or hyphens (see
    ``_field_key``); the first variant with a value wins. Placeholder values
    EcoSIS uploads use for "no value" (``-9999``, ``NA``, ``N/A``, ``null`` …)
    are treated as absent.

    Args:
        spectrum: A spectrum item from the EcoSIS API.
        name: Field name.

    Returns:
        The stripped value, or ``None`` when absent, empty or a placeholder.
    """
    value = _clean_value(spectrum.get(name))
    if value is not None:
        return value
    for key in _key_index(spectrum).get(_field_key(name), ()):
        if key != name:
            value = _clean_value(spectrum.get(key))
            if value is not None:
                return value
    return None


def _processing_flags(metadata: dict[str, Any]) -> list[str]:
    """Translate ECOSIS Processing * fields into a flat list of flags.

    ECOSIS records processing as separate ``Processing Averaged``,
    ``Processing Interpolated``, and ``Processing Resampled`` lists, each
    holding ``["yes"]`` or ``["no"]``. We project the affirmative answers
    onto canonical lower-case flag names that fit ``Measurement.processing``.

    Args:
        metadata: The full ECOSIS package metadata dict.

    Returns:
        Ordered list of processing flags applied to the data.
    """
    flags: list[str] = []
    mapping = (
        ("Processing Averaged", "averaged"),
        ("Processing Interpolated", "interpolated"),
        ("Processing Resampled", "resampled"),
    )
    for field_name, flag in mapping:
        for raw in metadata.get(field_name, []) or []:
            if str(raw).strip().lower() == "yes":
                flags.append(flag)
                break
    return flags


def _first_quantity(measurement_quantities: Sequence[str]) -> str | None:
    """Return the first recognised (lower-case) measurement quantity.

    Args:
        measurement_quantities: EcoSIS Measurement Quantity values.

    Returns:
        A key of ``ECOSIS_QUANTITY_MAP`` or a member of
        ``UNSUPPORTED_QUANTITIES``, or ``None`` when none is recognised.
    """
    for raw in measurement_quantities:
        key = str(raw).strip().lower()
        if key in ECOSIS_QUANTITY_MAP or key in UNSUPPORTED_QUANTITIES:
            return key
    return None


def _detect_quantity(
    measurement_quantities: list[str],
) -> Quantity:
    """Map EcoSIS Measurement Quantity to a supported quantity.

    Unsupported quantities (absorptance) are skipped here; the loader drops
    spectra whose own quantity is unsupported.

    Args:
        measurement_quantities: Values from the dataset metadata.

    Returns:
        The first supported quantity, defaulting to reflectance.
    """
    for raw in measurement_quantities:
        key = raw.strip().lower()
        if key in ECOSIS_QUANTITY_MAP:
            return ECOSIS_QUANTITY_MAP[key]
    return Quantity.REFLECTANCE


def _parse_datapoints(
    datapoints: dict[str, Any],
) -> tuple[list[float], list[float]]:
    """Extract wavelength and value arrays from an EcoSIS datapoints dict.

    EcoSIS returns datapoints as ``{"wavelength_nm": "value_str", ...}``.
    Keys that are not finite numbers are not bands and are ignored. A band
    whose value is missing, non-numeric or non-finite is kept on the
    wavelength grid with a NaN value (the project-wide missing-value
    contract), so every spectrum of a dataset shares its band grid.

    Args:
        datapoints: Raw datapoints dict from the API.

    Returns:
        Tuple of ``(wavelengths, values)`` sorted by wavelength.

    Raises:
        ValueError: If there is no band with a finite value.
    """
    pairs: list[tuple[float, float]] = []
    any_finite = False
    for key, val in datapoints.items():
        try:
            wl = float(key)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(wl):
            continue
        try:
            v = float(val)
        except (ValueError, TypeError):
            v = math.nan
        if math.isfinite(v):
            any_finite = True
        else:
            v = math.nan
        pairs.append((wl, v))

    if not any_finite:
        raise ValueError("No valid spectral data points found in datapoints")

    pairs.sort(key=lambda p: p[0])
    wavelengths = [p[0] for p in pairs]
    values = [p[1] for p in pairs]
    return wavelengths, values


def _sample_rank(key: str) -> tuple[str, str]:
    """Order-independent rank of a spectrum, sampling unit or duplicate.

    Args:
        key: The spectrum id (or sampling-unit key).

    Returns:
        ``(sha256 hex digest, key)`` — the key breaks (practically impossible)
        hash ties deterministically.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest(), key


def _content_key(spectrum: Mapping[str, Any], dedupe_key: str) -> str:
    """Hash identifying repeated uploads of the same spectrum.

    Args:
        spectrum: The spectrum item.
        dedupe_key: ``"content"`` (datapoints plus all non-id metadata) or
            ``"datapoints"`` (datapoints only). Both include the resource
            filename, so e.g. reflectance and transmittance never collide.

    Returns:
        A SHA-256 hex digest.
    """
    filename = (spectrum.get("ecosis") or {}).get("filename")
    if dedupe_key == "datapoints":
        payload: Any = [spectrum.get("datapoints"), filename]
    else:
        payload = [
            {k: v for k, v in spectrum.items() if k not in ("_id", "ecosis")},
            filename,
        ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity_fields(policy: "DatasetPolicy") -> tuple[str, ...]:
    """Per-spectrum fields that say which sample and material a row is.

    These are the label fields a record carries (``_label_fields``) and the
    fields the identity rules read: the naming chain (``material_name_field``,
    Common Name, Latin genus and species, ``fallback_name_fields``, the
    Spectra label), the target type, ``keyword_fields``,
    ``category_from_max_field``, the sampling stratum and sampling unit
    (``one_per``). Measurement
    conditions (date, time, instrument settings) are not among them.

    Args:
        policy: The dataset policy.

    Returns:
        The field names, without repeats.
    """
    identity = policy.identity
    names = [
        *_label_fields(policy),
        *([identity.material_name_field] if identity.material_name_field else []),
        "Common Name",
        "Latin Genus",
        "Latin Species",
        *identity.fallback_name_fields,
        "Spectra",
        identity.target_type_field,
        *identity.keyword_fields,
        *(name for name, _ in identity.category_from_max_field),
        *([policy.sampling.stratify_by] if policy.sampling is not None else []),
        *([policy.sampling.one_per] if policy.sampling and policy.sampling.one_per else []),
    ]
    return tuple(dict.fromkeys(names))


def _identity_key(spectrum: Mapping[str, Any], fields: Sequence[str]) -> str:
    """Hash of a spectrum's label and identity field values.

    Args:
        spectrum: The spectrum item.
        fields: The fields to read (see ``_identity_fields``).

    Returns:
        A SHA-256 hex digest; equal for rows that present themselves as the
        same sample.
    """
    payload = [_field_str(spectrum, name) for name in fields]
    encoded = json.dumps(payload, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Per-spectrum fields that end up on a record as sample labels.
SAMPLE_ID_FIELD = "Sample ID"
SAMPLE_DESCRIPTION_FIELD = "Sample Description"
USDA_SYMBOL_FIELD = "USDA Symbol"
# A USDA PLANTS symbol: an upper-case letter then upper-case letters and
# digits, 2 to 8 characters in all (``PIPO``, ``ACME80``, ``ARCTO3``).
USDA_SYMBOL_SHAPE = re.compile(r"[A-Z][A-Z0-9]{1,7}")


def _label_fields(policy: DatasetPolicy) -> tuple[str, ...]:
    """Per-spectrum label fields whose values a record carries.

    Duplicate-group members that disagree on one of these have their value
    removed from the kept record (see ``_conflicting_labels``).

    Args:
        policy: The dataset policy.

    Returns:
        The sample id, sample description and USDA symbol fields and the
        policy's ``carry_fields``, without repeats.
    """
    names = (SAMPLE_ID_FIELD, SAMPLE_DESCRIPTION_FIELD, USDA_SYMBOL_FIELD, *policy.carry_fields)
    return tuple(dict.fromkeys(names))


def _conflicting_labels(cand: "_Candidate", policy: DatasetPolicy) -> dict[str, list[str | None]]:
    """Label fields on which a duplicate group's members disagree.

    Args:
        cand: The kept candidate, with the label values of its whole group.
        policy: The dataset policy (its ``keep_one`` link field is reported
            separately in ``linked_sample_ids`` and left out here).

    Returns:
        Field name -> every value the group's members carry (``None`` for a
        member without one), for fields with more than one value.
    """
    link_field = policy.conflicting_duplicates.link_field
    out: dict[str, list[str | None]] = {}
    for name, seen in cand.label_values.items():
        is_link = link_field is not None and _field_key(name) == _field_key(link_field)
        if len(seen) > 1 and not (cand.conflict_merged and is_link):
            out[name] = sorted(seen, key=lambda v: (v is None, v or ""))
    return out


def _allocate_strata(sizes: Mapping[str, int], spec: SamplingSpec) -> dict[str, int]:
    """Decide how many sampling units to keep from each stratum.

    Each stratum is first capped at ``max_per_stratum``. If the capped total
    still exceeds ``max_total``, every stratum gets ``min_per_stratum``
    (smallest strata first, should the minimums alone exceed the budget, so
    rare strata are represented) and the remaining budget is shared in
    proportion to each stratum's remaining capacity using the
    largest-remainder method, ties broken by stratum name.

    Args:
        sizes: Number of eligible units per stratum.
        spec: Sampling parameters.

    Returns:
        Number of units to keep per stratum; never exceeds the stratum size,
        ``max_per_stratum`` or (in total) ``max_total``.
    """
    caps = {
        k: min(n, spec.max_per_stratum) if spec.max_per_stratum is not None else n
        for k, n in sizes.items()
    }
    if spec.max_total is None or sum(caps.values()) <= spec.max_total:
        return caps

    budget = spec.max_total
    alloc = {k: 0 for k in caps}
    for k in sorted(caps, key=lambda s: (caps[s], s)):
        give = min(spec.min_per_stratum, caps[k], budget)
        alloc[k] = give
        budget -= give

    residual = {k: caps[k] - alloc[k] for k in caps}
    total_residual = sum(residual.values())
    if budget > 0 and total_residual > 0:
        shares = {k: budget * residual[k] / total_residual for k in caps}
        floors = {k: int(math.floor(shares[k])) for k in caps}
        for k in caps:
            alloc[k] += floors[k]
        left = budget - sum(floors.values())
        by_remainder = sorted(caps, key=lambda s: (-(shares[s] - floors[s]), s))
        for k in by_remainder[:left]:
            alloc[k] += 1
    return alloc


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    """Pass-1 summary of one eligible spectrum (no spectral arrays kept)."""

    index: int
    spectrum_id: str
    quantity: str
    stratum: str | None
    unit: str | None
    level: float
    num_points: int
    missing: int
    out_of_range: dict[int, int]
    material_name: str = ""
    units_divisor: int | None = None
    divisor: int = 1
    duplicate_ids: list[str] = field(default_factory=list)
    duplicate_names: set[str] = field(default_factory=set)
    duplicate_strata: set[str] = field(default_factory=set)
    group_size: int = 1
    link_values: set[str] = field(default_factory=set)
    conflict_merged: bool = False
    identity_keys: set[str] = field(default_factory=set)
    label_values: dict[str, set[str | None]] = field(default_factory=dict)


@dataclass
class DatasetIngestStats:
    """Per-dataset ingest accounting, logged and kept on the loader.

    Attributes:
        dataset_id: EcoSIS package id.
        title: Dataset title.
        inclusion: Policy inclusion mode (or ``"uncurated"``).
        declared: Spectra count declared by the package metadata.
        available: Spectra present in the bundle.
        excluded_resource: Spectra from excluded resources (e.g. model output).
        unsupported_quantity: Spectra of a quantity the schema cannot
            represent (absorptance).
        dropped_by_policy: Spectra listed in ``drop_spectra``.
        parse_failed: Spectra with no usable datapoints.
        missing_id: Spectra without an EcoSIS ``_id`` (no upstream identifier
            to build a stable spectrum id from).
        missing_stratum: Sampled-dataset spectra with no stratum value.
        duplicate: Repeated uploads of an already-seen spectrum.
        conflicting_duplicate: Rows of duplicate groups whose copies carry
            different material names or strata (the whole group is dropped).
        conflict_groups_kept: Conflicting groups kept once under the policy's
            ``keep_one`` rule (their extra copies count as ``duplicate``).
        copy_error: Rows of large duplicate groups (``_LARGE_DUPLICATE_GROUP``
            or more rows) whose rows present themselves as different samples
            (they disagree on a label or identity field, see
            ``_identity_fields``; identical datapoints): a fill-down or copy
            error in the upload whose labels cannot be trusted, so the whole
            group is dropped.
        units_conflict: Spectra whose declared units contradict their values.
        no_plausible_scale: Spectra fitting no supported source scale.
        implausibly_dark: Spectra far darker than the dataset median after
            scaling (unit-scale spectra inside a percent-scale dataset, or
            spectra already divided by 100 inside a unit-scale one).
        low_coverage: Spectra with < ``MIN_COVERAGE`` valid bands.
        constant_fill: Spectra whose values are all identical (no-data fill,
            e.g. all-zero pixels).
        saturated: Spectra in which more than ``DOMINANT_VALUE_FRACTION`` of
            the finite bands share one value (a saturated or clipped detector,
            or a partial fill).
        not_sampled: Eligible spectra left out by sampling.
        build_failed: Spectra whose record failed model validation.
        escalated: Included spectra re-scaled with a larger divisor than the
            dataset base.
        masked_out_of_range: Band values masked to NaN for falling outside
            the valid window after scaling.
        missing_values: Band values that were missing or non-numeric.
        included: Records yielded.
        base_divisor: The dataset's base scale divisor.
        unreadable: Whether the bundle could not be parsed.
    """

    dataset_id: str
    title: str
    inclusion: str
    declared: int = 0
    available: int = 0
    excluded_resource: int = 0
    unsupported_quantity: int = 0
    dropped_by_policy: int = 0
    parse_failed: int = 0
    missing_id: int = 0
    missing_stratum: int = 0
    duplicate: int = 0
    conflicting_duplicate: int = 0
    conflict_groups_kept: int = 0
    copy_error: int = 0
    units_conflict: int = 0
    no_plausible_scale: int = 0
    implausibly_dark: int = 0
    low_coverage: int = 0
    constant_fill: int = 0
    saturated: int = 0
    not_sampled: int = 0
    build_failed: int = 0
    escalated: int = 0
    masked_out_of_range: int = 0
    missing_values: int = 0
    included: int = 0
    base_divisor: int = 1
    unreadable: bool = False


_STAT_FIELDS: tuple[str, ...] = (
    "available",
    "included",
    "excluded_resource",
    "unsupported_quantity",
    "dropped_by_policy",
    "parse_failed",
    "missing_id",
    "missing_stratum",
    "duplicate",
    "conflicting_duplicate",
    "conflict_groups_kept",
    "copy_error",
    "units_conflict",
    "no_plausible_scale",
    "implausibly_dark",
    "low_coverage",
    "constant_fill",
    "saturated",
    "not_sampled",
    "build_failed",
    "escalated",
    "masked_out_of_range",
    "missing_values",
    "base_divisor",
)


@dataclass(frozen=True)
class _DatasetContext:
    """Dataset-level fields shared by every record of one bundle."""

    dataset_id: str
    title: str
    dataset: DatasetInfo
    target_types: list[str]
    quantity: str
    package_setup: Mapping[str, str | None]
    organization: str
    ecosystem: str
    processing: list[str]
    declared_units: tuple[str, ...] = ()
    wavelength_units: tuple[str, ...] = ()


# Normalised EcoSIS "Wavelength Units" spellings -> unit.
_WAVELENGTH_UNIT_NAMES: dict[str, WavelengthUnit] = {
    "nm": WavelengthUnit.NANOMETERS,
    "nanometer": WavelengthUnit.NANOMETERS,
    "nanometers": WavelengthUnit.NANOMETERS,
    "nanometre": WavelengthUnit.NANOMETERS,
    "nanometres": WavelengthUnit.NANOMETERS,
    "um": WavelengthUnit.MICROMETERS,
    "µm": WavelengthUnit.MICROMETERS,
    "μm": WavelengthUnit.MICROMETERS,
    "micron": WavelengthUnit.MICROMETERS,
    "microns": WavelengthUnit.MICROMETERS,
    "micrometer": WavelengthUnit.MICROMETERS,
    "micrometers": WavelengthUnit.MICROMETERS,
    "micrometre": WavelengthUnit.MICROMETERS,
    "micrometres": WavelengthUnit.MICROMETERS,
    "cm-1": WavelengthUnit.WAVENUMBERS,
    "1/cm": WavelengthUnit.WAVENUMBERS,
    "wavenumber": WavelengthUnit.WAVENUMBERS,
    "wavenumbers": WavelengthUnit.WAVENUMBERS,
}
# Per-spectrum fields EcoSIS uploads use for the wavelength unit.
_SPECTRUM_WAVELENGTH_UNIT_FIELDS: tuple[str, ...] = ("Wavelength Units", "Wavelength_Units")
# A wavelength axis in nanometres for the optical range ends far above this; an
# axis ending below it is in micrometres (EcoSIS spectra span ~0.35-2.5 um).
_UM_AXIS_CEILING = 100.0


def _declared_wavelength_unit(raw: Sequence[str]) -> WavelengthUnit | None:
    """Interpret declared "Wavelength Units" values.

    Args:
        raw: Declared values (package- or spectrum-level).

    Returns:
        The unit when every recognised value agrees, else ``None``.
    """
    units = {_WAVELENGTH_UNIT_NAMES.get(" ".join(str(v).split()).casefold()) for v in raw}
    units.discard(None)
    return units.pop() if len(units) == 1 else None


def resolve_wavelength_unit(
    wavelengths: Sequence[float],
    spectrum_units: Sequence[str],
    dataset_units: Sequence[str],
) -> tuple[WavelengthUnit, dict[str, Any] | None]:
    """Decide the native unit of an EcoSIS spectrum's axis, without converting it.

    The spectrum's own "Wavelength Units" field wins over the package's.
    A declared unit is accepted when the axis agrees with it (a nanometre
    axis ends at or above 100, a micrometre axis below 100; wavenumbers are
    accepted as declared). When nothing is declared, or the declaration
    contradicts the axis, the unit is inferred from the axis range and the
    inference is returned for ``extra``.

    Args:
        wavelengths: The axis values.
        spectrum_units: Per-spectrum declared values.
        dataset_units: Package-level declared values.

    Returns:
        ``(unit, note)`` where ``note`` is ``None`` when the declaration was
        used, else a dict describing the inference.
    """
    raw = list(spectrum_units) or list(dataset_units)
    declared = _declared_wavelength_unit(raw)
    from_axis = (
        WavelengthUnit.MICROMETERS
        if max(wavelengths) < _UM_AXIS_CEILING
        else WavelengthUnit.NANOMETERS
    )
    if declared is WavelengthUnit.WAVENUMBERS or declared is from_axis:
        return declared, None
    reason = "no wavelength unit declared" if declared is None else "declared unit contradicts axis"
    return from_axis, {
        "unit": from_axis.value,
        "declared": raw or None,
        "reason": f"{reason}; axis {min(wavelengths):g}-{max(wavelengths):g}",
    }


def _dataset_context(
    metadata: dict[str, Any], policy: DatasetPolicy, fallback_id: str
) -> _DatasetContext:
    """Build the dataset-level record context from package metadata.

    Args:
        metadata: EcoSIS package metadata.
        policy: The dataset's inclusion policy.
        fallback_id: Id to use when the metadata lacks ``_id``.

    Returns:
        The shared context for this dataset's records.
    """
    dataset_id = metadata.get("_id", fallback_id)
    ecosis_meta = metadata.get("ecosis", {})
    dataset_title = ecosis_meta.get("package_title", fallback_id)

    citation_list = metadata.get("Citation", [])
    citation = citation_list[0] if citation_list else ""
    organization = ecosis_meta.get("organization", "") or ""
    author_list = metadata.get("Author", metadata.get("Author Email", []))
    authors = "; ".join(author_list) if author_list else ""

    # ECOSIS packages each carry their own licence (ODbL, CC-BY, etc.); the
    # label is mapped to SPDX, and an unmapped or missing label becomes
    # NOASSERTION with the original text in the note.
    dataset_license, license_note = map_ecosis_license(ecosis_meta.get("license"))
    dataset_doi_raw = ecosis_meta.get("doi") or ""
    dataset_doi = (dataset_doi_raw.split("doi:", 1)[-1] if dataset_doi_raw else None) or None
    dataset_description = (
        ecosis_meta.get("description") or ecosis_meta.get("sort_description") or None
    )
    dataset_url = f"https://ecosis.org/package/{dataset_id}"

    processing = _processing_flags(metadata)
    if policy.simulated:
        processing.append("simulated")

    dataset = DatasetInfo(
        id=dataset_id,
        title=dataset_title,
        description=dataset_description,
        url=dataset_url,
        doi=dataset_doi,
        license=dataset_license,
        license_note=license_note,
        citation=citation or None,
        authors=authors or None,
        organization=organization or None,
    )
    return _DatasetContext(
        dataset_id=dataset_id,
        title=dataset_title,
        dataset=dataset,
        target_types=list(metadata.get("Target Type", []) or []),
        quantity=_first_quantity(metadata.get("Measurement Quantity", []) or []) or "reflectance",
        package_setup=_package_setup(metadata),
        organization=organization,
        ecosystem=(metadata.get("Ecosystem Type") or [""])[0],
        processing=processing,
        declared_units=tuple(
            str(u).strip() for u in (metadata.get("Measurement Units") or []) if u is not None
        ),
        wavelength_units=tuple(
            str(u).strip() for u in (metadata.get("Wavelength Units") or []) if u is not None
        ),
    )


def _spectrum_quantity(
    spectrum: Mapping[str, Any], policy: DatasetPolicy, dataset_quantity: str
) -> str:
    """Decide a spectrum's measurement quantity.

    Order: the spectrum's own Measurement Quantity; the policy's
    ``resource_techniques`` for its resource file; the dataset default.

    Args:
        spectrum: The spectrum item.
        policy: The dataset policy.
        dataset_quantity: The dataset-level quantity.

    Returns:
        A lower-case quantity (possibly unsupported, e.g. ``"absorptance"``).
    """
    own = _field_str(spectrum, "Measurement Quantity")
    if own:
        quantity = _first_quantity([own])
        if quantity:
            return quantity
    filename = str((spectrum.get("ecosis") or {}).get("filename") or "")
    for pattern, quantity in policy.resource_techniques:
        if pattern.search(filename):
            return quantity
    return dataset_quantity


def _stratum_key(spectrum: Mapping[str, Any], policy: DatasetPolicy) -> str | None:
    """Normalised sampling stratum of a spectrum.

    When the stratum field is also the identity name field, the policy's
    ``material_name_map`` is applied first so mapped synonyms share a stratum.

    Args:
        spectrum: The spectrum item.
        policy: The (sampled) dataset policy.

    Returns:
        The normalised stratum key, or ``None`` when absent.
    """
    assert policy.sampling is not None
    value = _field_str(spectrum, policy.sampling.stratify_by)
    if value is None:
        return None
    if policy.sampling.stratify_by == policy.identity.material_name_field:
        value = policy.identity.material_name_map.get(value, value)
    return _norm_key(value)


def _material_name(
    spectrum: Mapping[str, Any], identity: IdentitySpec, spec_target: str, dataset_title: str
) -> str:
    """Choose a spectrum's material name.

    Order: the policy's ``fixed_material_name``; its ``target_names`` entry
    for the spectrum's target type; its ``material_name_field``;
    Common Name; Latin genus and species; the policy's
    ``fallback_name_fields``; the Spectra label; its constant
    ``material_name``; the target type; the dataset title. Field names match
    case-insensitively (see ``_field_str``).

    Args:
        spectrum: The spectrum item.
        identity: The dataset's identity rules.
        spec_target: The spectrum's Target Type (may be empty).
        dataset_title: The dataset title (last resort).

    Returns:
        The material name.
    """
    if identity.fixed_material_name:
        return identity.fixed_material_name
    if spec_target and identity.target_names:
        target_name = identity.target_names.get(_norm_key(spec_target))
        if target_name:
            return target_name
    if identity.material_name_field:
        value = _field_str(spectrum, identity.material_name_field)
        if value:
            value = identity.material_name_map.get(value, value)
            return identity.material_name_template.format(value)

    common_name = _field_str(spectrum, "Common Name")
    if common_name:
        return common_name
    latin_genus = _field_str(spectrum, "Latin Genus")
    if latin_genus:
        return f"{latin_genus} {_field_str(spectrum, 'Latin Species') or ''}".strip()
    # Policy-chosen fields name the material; the Spectra label is only an
    # upload's own (often opaque) spectrum label, so it comes after them.
    for name in identity.fallback_name_fields:
        value = _field_str(spectrum, name)
        if value:
            return identity.material_name_map.get(value, value)
    spectra_label = _field_str(spectrum, "Spectra")
    if spectra_label:
        return spectra_label
    if identity.material_name:
        return identity.material_name
    return spec_target or dataset_title


def _material_category(
    spectrum: Mapping[str, Any], identity: IdentitySpec, spec_target: str, ctx: _DatasetContext
) -> MaterialCategory:
    """Choose a spectrum's material category.

    Order: the dominant ``category_from_max_field`` (``mixture`` when no
    fraction reaches ``mixture_threshold``); the spectrum's own target
    type through the policy's ``category_map``, then the built-in mapping; the
    policy's constant ``category``; the dataset-level target types when they
    all map to one category; otherwise ``other``.

    Args:
        spectrum: The spectrum item.
        identity: The dataset's identity rules.
        spec_target: The spectrum's Target Type (may be empty).
        ctx: Dataset context (dataset-level target types).

    Returns:
        The material category.
    """
    best: tuple[float, MaterialCategory] | None = None
    for name, category in identity.category_from_max_field:
        try:
            value = float(_field_str(spectrum, name) or "nan")
        except ValueError:
            continue
        if math.isfinite(value) and (best is None or value > best[0]):
            best = (value, category)
    if best is not None:
        if identity.mixture_threshold is not None and best[0] < identity.mixture_threshold:
            return MaterialCategory.MIXTURE
        return best[1]
    if spec_target:
        mapped = identity.category_map.get(_norm_key(spec_target))
        if mapped is not None:
            return mapped
        key = spec_target.strip().lower()
        if key in ECOSIS_CATEGORY_MAP:
            return ECOSIS_CATEGORY_MAP[key]
    if identity.category is not None:
        return identity.category
    # Dataset-level target types describe the whole package; they only say
    # something about an individual spectrum when they all agree.
    dataset_categories = {
        identity.category_map.get(_norm_key(t)) or _classify_target_type([t])
        for t in ctx.target_types
    }
    if len(dataset_categories) == 1:
        return dataset_categories.pop()
    return MaterialCategory.OTHER


def _keywords(spectrum: Mapping[str, Any], identity: IdentitySpec, ecosystem: str) -> list[str]:
    """Build a spectrum's de-duplicated, lower-case keyword list.

    Args:
        spectrum: The spectrum item.
        identity: The dataset's identity rules.
        ecosystem: The dataset's Ecosystem Type (may be empty).

    Returns:
        Keywords in first-seen order.
    """
    raw: list[str | None] = [
        _field_str(spectrum, "Common Name"),
        _field_str(spectrum, "Latin Genus"),
        _field_str(spectrum, "Latin Species"),
        ecosystem,
    ]
    raw.extend(identity.keywords)
    for name in identity.keyword_fields:
        value = _field_str(spectrum, name)
        raw.append(identity.material_name_map.get(value, value) if value else None)
    keywords: list[str] = []
    for item in raw:
        if item and item.strip():
            word = item.strip().lower()
            if word not in keywords:
                keywords.append(word)
    return keywords


def _spectrum_setup(spectrum: Mapping[str, Any], spec: SetupSpec) -> dict[str, Any]:
    """Resolve a spectrum's measurement setup from its dataset's rules.

    Args:
        spectrum: The spectrum item.
        spec: The dataset's setup rules.

    Returns:
        The attributes the rules assign (see ``SetupSpec``); attributes not
        present are left to the package-level fallback.
    """
    setup: dict[str, Any] = dict(spec.default)
    filename = str((spectrum.get("ecosis") or {}).get("filename") or "")
    for pattern, values in spec.resources:
        if pattern.search(filename):
            setup.update(values)
            break
    if spec.field_name:
        value = _field_str(spectrum, spec.field_name)
        if value:
            mapped = spec.field_map.get(_norm_key(value))
            if mapped is not None:
                setup.update(mapped)
            else:
                setup.update(
                    {
                        "geometry": Geometry.UNKNOWN,
                        "light_source": None,
                        "foreoptic": None,
                        "acquisition_method": None,
                    }
                )
    if spec.instrument_field:
        value = _field_str(spectrum, spec.instrument_field)
        if value:
            mapped = spec.instrument_map.get(_norm_key(value))
            if mapped is not None:
                setup.update(mapped)
            else:
                setup.update({"instrument": value, "instrument_type": None})
    return setup


def _package_setup(metadata: Mapping[str, Any]) -> dict[str, str | None]:
    """Package-level setup values, only where the package lists exactly one.

    Args:
        metadata: EcoSIS package metadata.

    Returns:
        ``instrument``, ``light_source``, ``foreoptic``, ``acquisition_method``
        and ``venue``; each is ``None`` when the package lists none or
        several distinct values (a multi-setup package).
    """

    def unique(name: str) -> str | None:
        values = {
            str(v).strip() for v in (metadata.get(name) or []) if v is not None and str(v).strip()
        }
        return values.pop() if len(values) == 1 else None

    manufacturer = unique("Instrument Manufacturer")
    model = unique("Instrument Model")
    instrument = " ".join(filter(None, [manufacturer, model])) if manufacturer and model else None
    return {
        "instrument": instrument,
        "light_source": unique("Light Source"),
        "foreoptic": unique("Foreoptic Type"),
        "acquisition_method": unique("Acquisition Method"),
        "venue": unique("Measurement Venue"),
    }


class EcosisLoader(BaseLoader):
    """Loader for the EcoSIS Ecological Spectral Information System."""

    def __init__(self, policies: Sequence[DatasetPolicy] | None = None) -> None:
        """Create a loader.

        Args:
            policies: Dataset policies to enforce; defaults to the curated
                ``ecosis_datasets.json``.
        """
        self._policies = list(policies) if policies is not None else None
        self.ingest_stats: list[DatasetIngestStats] = []
        self.missing_datasets: list[str] = []
        self._dataset_infos: dict[str, tuple[DatasetInfo, DatasetPolicy]] = {}

    @property
    def fatal_error_count(self) -> int:
        """Problems that make the EcoSIS output incomplete and must fail ingest.

        Counts curated (non-excluded) datasets with no bundle in the input
        directory, unreadable bundles, and spectra whose record failed model
        validation. Policy-sanctioned drops (excluded resources, duplicates,
        sampling, implausible scale, low coverage, …) are logged and counted
        in ``ingest_stats`` but are not fatal.

        Returns:
            The number of fatal problems seen by the last :meth:`load`.
        """
        return (
            len(self.missing_datasets)
            + sum(1 for s in self.ingest_stats if s.unreadable)
            + sum(s.build_failed for s in self.ingest_stats)
        )

    def _get_policies(self) -> list[DatasetPolicy]:
        """Return the policies in force.

        Returns:
            The explicit policies, or the curated file's.
        """
        if self._policies is not None:
            return self._policies
        return _load_curated_datasets()

    def source_name(self) -> str:
        """Return the collection id."""
        return COLLECTION_ID

    def collection_info(self) -> CollectionInfo:
        """Return the EcoSIS collection metadata.

        Returns:
            The footer ``collection`` object.
        """
        excluded = [p for p in self._get_policies() if p.inclusion == "exclude"]
        if not excluded:
            return ECOSIS_COLLECTION
        parts = []
        for policy in excluded:
            count = (
                f", {policy.available_spectra:,} spectra"
                if policy.available_spectra is not None
                else ""
            )
            parts.append(f"{policy.name} ({policy.id}{count}): {policy.reason}")
        notes = f"{ECOSIS_COLLECTION.inclusion_notes} Excluded packages: " + " ".join(parts)
        return ECOSIS_COLLECTION.model_copy(update={"inclusion_notes": notes})

    def datasets(self) -> dict[str, DatasetInfo]:
        """Return the packages that contributed spectra to the last :meth:`load`.

        Each entry carries the package metadata, its mapped licence, and
        ``inclusion``, ``inclusion_notes``, ``spectra_available`` and
        ``spectra_included`` from the policy and the ingest accounting.

        Returns:
            Dataset metadata keyed by EcoSIS package id, for every package
            with at least one included spectrum.
        """
        out: dict[str, DatasetInfo] = {}
        stats_by_id = {s.dataset_id: s for s in self.ingest_stats}
        for dataset_id, (info, policy) in sorted(self._dataset_infos.items()):
            stats = stats_by_id.get(dataset_id)
            if stats is None or stats.included == 0:
                continue
            inclusion, notes = _dataset_inclusion(policy, stats)
            out[dataset_id] = info.model_copy(
                update={
                    "inclusion": inclusion,
                    "inclusion_notes": notes,
                    "spectra_available": stats.available,
                    "spectra_included": stats.included,
                }
            )
        return out

    def download(self, target_dir: Path) -> Path:
        """Download curated EcoSIS datasets via the REST API.

        Fetches dataset metadata and all spectra for each non-excluded
        dataset in ``ecosis_datasets.json``, streaming each to
        ``{dataset_id}.json`` via a temporary ``.part`` file so a failed
        download never leaves a partial bundle behind. Sampled datasets are
        mirrored in full; sampling happens at ingest.

        Args:
            target_dir: Directory to save downloaded data into.

        Returns:
            Path to the EcoSIS data directory.

        Raises:
            RuntimeError: If any dataset could not be downloaded completely.
        """
        output_dir = target_dir / "ecosis"
        output_dir.mkdir(parents=True, exist_ok=True)

        policies = self._get_policies()
        wanted = [p for p in policies if p.inclusion != "exclude"]
        for policy in policies:
            if policy.inclusion == "exclude":
                logger.info("Skipping excluded EcoSIS dataset %s: %s", policy.name, policy.reason)
        logger.info("Downloading %d curated EcoSIS datasets", len(wanted))

        failed: list[str] = []
        for policy in tqdm(wanted, desc="Downloading EcoSIS datasets"):
            output_file = output_dir / f"{policy.id}.json"

            if output_file.exists():
                if _bundle_is_complete(output_file):
                    logger.info("Already downloaded: %s", policy.name)
                    continue
                logger.warning("Refetching incomplete bundle for %s", policy.name)

            logger.info("Fetching dataset: %s (%s)", policy.name, policy.id)
            part_file = output_file.with_name(output_file.name + ".part")
            try:
                metadata = _api_get(f"/package/{policy.id}")
                total = _declared_count(metadata)
                count = _write_bundle(part_file, metadata, _iter_spectra_pages(policy.id, total))
            except Exception:
                # Never leave a partial bundle — a truncated file would be
                # picked up by ingest as if it were the whole dataset.
                logger.error("Failed to download %s", policy.name, exc_info=True)
                part_file.unlink(missing_ok=True)
                failed.append(policy.name)
                continue

            if not _bundle_is_complete(part_file):
                # Overlapping or repeated pages can reach the declared count
                # with fewer distinct spectra; never install such a bundle.
                logger.error(
                    "Downloaded bundle for %s holds fewer distinct spectra than declared; "
                    "discarded",
                    policy.name,
                )
                part_file.unlink(missing_ok=True)
                failed.append(policy.name)
                continue
            part_file.replace(output_file)
            logger.info("Saved %d spectra for %s", count, policy.name)

        if failed:
            raise RuntimeError(
                f"Failed to download {len(failed)} EcoSIS dataset(s): {', '.join(failed)}. "
                f"Re-run the download to retry; complete bundles are kept and skipped."
            )

        return output_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse downloaded EcoSIS bundles, enforcing the inclusion policy.

        Bundles are streamed twice: a first pass summarises every spectrum
        (eligibility, duplicates, scale level, band validity) without keeping
        spectral arrays; the dataset scale and the sample are decided from
        those summaries; a second pass builds records for the chosen spectra.

        Args:
            source_dir: Directory containing ``{dataset_id}.json`` files.

        Yields:
            SpectrumRecord for each included spectrum.
        """
        json_files = sorted(source_dir.rglob("*.json"))
        policies = {p.id: p for p in self._get_policies()}
        self.ingest_stats = []
        self.missing_datasets = []
        self._dataset_infos = {}
        if not json_files:
            logger.warning("No EcoSIS JSON files found in %s", source_dir)
        logger.info("Loading %d EcoSIS dataset files", len(json_files))

        for json_file in json_files:
            yield from self._load_bundle(json_file, policies)

        seen = {s.dataset_id for s in self.ingest_stats}
        self.missing_datasets = [
            p.id for p in policies.values() if p.inclusion != "exclude" and p.id not in seen
        ]
        for policy in policies.values():
            if policy.inclusion == "exclude" and policy.id not in seen:
                logger.info(
                    "EcoSIS dataset %s: available=%s included=0 — excluded by policy, "
                    "not downloaded (%s)",
                    policy.name,
                    policy.available_spectra if policy.available_spectra is not None else "?",
                    policy.reason,
                )
        for dataset_id in self.missing_datasets:
            logger.error(
                "Curated EcoSIS dataset %s (%s) has no bundle in %s",
                dataset_id,
                policies[dataset_id].name,
                source_dir,
            )
        total_available = sum(s.available for s in self.ingest_stats)
        total_included = sum(s.included for s in self.ingest_stats)
        logger.info(
            "EcoSIS ingest summary: %d datasets, available=%d, included=%d",
            len(self.ingest_stats),
            total_available,
            total_included,
        )

    def _load_bundle(
        self, path: Path, policies: Mapping[str, DatasetPolicy]
    ) -> Iterator[SpectrumRecord]:
        """Ingest one bundle under its dataset policy.

        Args:
            path: Path to a ``{dataset_id}.json`` bundle.
            policies: Policies keyed by dataset id.

        Yields:
            SpectrumRecord for each included spectrum.

        Raises:
            RuntimeError: If the bundle changed between the two passes.
        """
        try:
            metadata = _read_bundle_metadata(path)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to read %s: %s", path.name, exc)
            return
        dataset_id = metadata.get("_id", path.stem)
        title = metadata.get("ecosis", {}).get("package_title", path.stem)
        declared = int(metadata.get("ecosis", {}).get("spectra_count", 0) or 0)
        policy = policies.get(dataset_id)
        if policy is None:
            logger.warning(
                "EcoSIS bundle %s (%s) is not in the curated dataset list; skipped",
                path.name,
                title,
            )
            self.ingest_stats.append(
                DatasetIngestStats(dataset_id, title, "uncurated", declared=declared)
            )
            return
        stats = DatasetIngestStats(dataset_id, title, policy.inclusion, declared=declared)
        self.ingest_stats.append(stats)
        if policy.inclusion == "exclude":
            stats.available = declared
            logger.info(
                "EcoSIS dataset %s: available=%d included=0 — excluded by policy (%s)",
                title,
                declared,
                policy.reason,
            )
            return
        if policy.available_spectra is not None and declared != policy.available_spectra:
            logger.warning(
                "EcoSIS dataset %s declares %d spectra but the policy file records %d; "
                "review its policy",
                title,
                declared,
                policy.available_spectra,
            )

        ctx = _dataset_context(metadata, policy, path.stem)
        self._dataset_infos[ctx.dataset_id] = (ctx.dataset, policy)
        try:
            candidates = self._summarise(path, policy, ctx, stats)
        except (OSError, ValueError) as exc:
            logger.error("EcoSIS bundle %s (%s) is unreadable; skipped: %s", path.name, title, exc)
            stats.unreadable = True
            return

        valid = self._resolve_scales(candidates, policy, stats, ctx.declared_units)

        sampling_info: dict[int, dict[str, Any]] = {}
        if policy.sampling is not None:
            selected = self._sample(valid, policy.sampling, sampling_info)
            stats.not_sampled = len(valid) - len(selected)
        else:
            selected = valid

        chosen = {c.index: c for c in selected}
        for index, spectrum in enumerate(_iter_bundle_spectra(path)):
            pick = chosen.get(index)
            if pick is None:
                continue
            spectrum_id = str(spectrum.get("_id") or "")
            if spectrum_id != pick.spectrum_id:
                raise RuntimeError(f"{path.name} changed while being ingested")
            try:
                record = self._build_record(
                    ctx, policy, spectrum, pick, stats, sampling_info.get(index)
                )
            except Exception:
                stats.build_failed += 1
                logger.warning(
                    "Failed to build record for %s in %s", spectrum_id, title, exc_info=True
                )
                continue
            stats.included += 1
            yield record

        logger.info(
            "EcoSIS dataset %s [%s]: %s",
            title,
            policy.inclusion,
            " ".join(f"{name}={getattr(stats, name)}" for name in _STAT_FIELDS),
        )

    @staticmethod
    def _summarise(
        path: Path, policy: DatasetPolicy, ctx: _DatasetContext, stats: DatasetIngestStats
    ) -> list[_Candidate]:
        """First pass: summarise every eligible, distinct spectrum of a bundle.

        Duplicates (same ``dedupe_key`` content) are collapsed here, before
        scale inference and sampling, keeping the copy with the lowest
        SHA-256-ranked id.

        Args:
            path: Bundle path.
            policy: The dataset policy.
            ctx: Dataset context (default quantity).
            stats: Accounting to update.

        Returns:
            One candidate per distinct spectrum that passed the resource,
            quantity, drop-list, parse and stratum checks, in bundle order.
        """
        by_content: dict[str, _Candidate] = {}
        identity_fields = _identity_fields(policy)
        for index, spectrum in enumerate(_iter_bundle_spectra(path)):
            stats.available += 1
            if not str(spectrum.get("_id") or "").strip():
                # Without the upstream id there is no stable spectrum id; an
                # invented one (e.g. the bundle position) could collide or
                # change between downloads.
                stats.missing_id += 1
                logger.warning("EcoSIS %s spectrum #%d dropped: it has no _id", policy.name, index)
                continue
            spectrum_id = str(spectrum["_id"])
            filename = str((spectrum.get("ecosis") or {}).get("filename") or "")
            if any(p.search(filename) for p in policy.exclude_resources):
                stats.excluded_resource += 1
                continue
            quantity = _spectrum_quantity(spectrum, policy, ctx.quantity)
            if quantity in UNSUPPORTED_QUANTITIES:
                stats.unsupported_quantity += 1
                continue
            if spectrum_id in policy.drop_spectra:
                stats.dropped_by_policy += 1
                logger.info(
                    "EcoSIS %s spectrum %s dropped by policy: %s",
                    policy.name,
                    spectrum_id,
                    policy.drop_spectra[spectrum_id],
                )
                continue
            try:
                _, values = _parse_datapoints(spectrum.get("datapoints") or {})
            except ValueError:
                stats.parse_failed += 1
                continue
            if len(values) < 2:
                stats.parse_failed += 1
                continue
            if len({v for v in values if math.isfinite(v)}) == 1:
                stats.constant_fill += 1
                logger.info(
                    "EcoSIS %s spectrum %s dropped: every value is identical (no-data fill)",
                    policy.name,
                    spectrum_id,
                )
                continue
            dominant = dominant_value_fraction(values)
            if dominant > DOMINANT_VALUE_FRACTION:
                stats.saturated += 1
                logger.info(
                    "EcoSIS %s spectrum %s dropped: %.0f%% of its bands hold one value "
                    "(saturated, clipped or partly filled)",
                    policy.name,
                    spectrum_id,
                    100 * dominant,
                )
                continue
            stratum: str | None = None
            unit: str | None = None
            if policy.sampling is not None:
                stratum = _stratum_key(spectrum, policy)
                if stratum is None:
                    stats.missing_stratum += 1
                    continue
                if policy.sampling.one_per:
                    unit_value = _field_str(spectrum, policy.sampling.one_per)
                    unit = _norm_key(unit_value) if unit_value else f"_id:{spectrum_id}"
            level = spectrum_level(values)
            if level is None:  # pragma: no cover — _parse_datapoints guarantees one
                stats.parse_failed += 1
                continue
            units_divisor: int | None = None
            if policy.units_field:
                units_value = _field_str(spectrum, policy.units_field)
                if units_value:
                    units_divisor = policy.units_divisors.get(_norm_key(units_value))
            cand = _Candidate(
                index=index,
                spectrum_id=spectrum_id,
                quantity=quantity,
                stratum=stratum,
                unit=unit,
                level=level,
                num_points=len(values),
                missing=sum(1 for v in values if not math.isfinite(v)),
                out_of_range={d: count_out_of_range(values, d) for d in SUPPORTED_DIVISORS},
                units_divisor=units_divisor,
                material_name=_material_name(
                    spectrum,
                    policy.identity,
                    _field_str(spectrum, policy.identity.target_type_field) or "",
                    ctx.title,
                ),
            )
            cand.duplicate_names = {cand.material_name}
            cand.duplicate_strata = {stratum} if stratum is not None else set()
            cand.identity_keys = {_identity_key(spectrum, identity_fields)}
            cand.label_values = {
                name: {_field_str(spectrum, name)} for name in _label_fields(policy)
            }
            if policy.conflicting_duplicates.link_field:
                link = _field_str(spectrum, policy.conflicting_duplicates.link_field)
                if link:
                    cand.link_values = {link}
            key = _content_key(spectrum, policy.dedupe_key)
            kept = by_content.get(key)
            if kept is None:
                by_content[key] = cand
                continue
            survivor, other = (
                (cand, kept)
                if _sample_rank(cand.spectrum_id) < _sample_rank(kept.spectrum_id)
                else (kept, cand)
            )
            survivor.duplicate_ids = sorted(
                survivor.duplicate_ids + other.duplicate_ids + [other.spectrum_id]
            )
            survivor.duplicate_names |= other.duplicate_names
            survivor.duplicate_strata |= other.duplicate_strata
            survivor.link_values |= other.link_values
            survivor.identity_keys |= other.identity_keys
            for name, seen in other.label_values.items():
                survivor.label_values.setdefault(name, set()).update(seen)
            survivor.group_size = kept.group_size + 1
            by_content[key] = survivor

        candidates: list[_Candidate] = []
        for cand in sorted(by_content.values(), key=lambda c: c.index):
            if cand.group_size >= _LARGE_DUPLICATE_GROUP:
                logger.warning(
                    "EcoSIS %s: %d rows share identical datapoints (kept %s) — likely a "
                    "fill-down or copy error in the upload",
                    policy.name,
                    cand.group_size,
                    cand.spectrum_id,
                )
            if (
                len(cand.duplicate_names) > 1
                and len(cand.duplicate_strata) <= 1
                and policy.conflicting_duplicates.action == "keep_one"
            ):
                # One measurement deliberately linked to several labelled
                # samples: keep it once under the policy's neutral name.
                cand.conflict_merged = True
                stats.duplicate += cand.group_size - 1
                stats.conflict_groups_kept += 1
                logger.info(
                    "EcoSIS %s: %d rows share identical datapoints labelled %s; kept one "
                    "as %r (policy keep_one)",
                    policy.name,
                    cand.group_size,
                    sorted(cand.duplicate_names),
                    policy.conflicting_duplicates.material_name,
                )
                candidates.append(cand)
                continue
            if len(cand.duplicate_names) > 1 or len(cand.duplicate_strata) > 1:
                # The same spectrum labelled as different materials: no copy's
                # label can be trusted, so the whole group is dropped.
                stats.conflicting_duplicate += cand.group_size
                logger.warning(
                    "EcoSIS %s: %d rows share identical datapoints but are labelled %s; "
                    "dropping the group (ids %s)",
                    policy.name,
                    cand.group_size,
                    sorted(cand.duplicate_names | cand.duplicate_strata),
                    sorted([cand.spectrum_id] + cand.duplicate_ids),
                )
                continue
            if cand.group_size >= _LARGE_DUPLICATE_GROUP and len(cand.identity_keys) > 1:
                # Many rows that claim to be different samples (they disagree
                # on a label or identity field, see ``_identity_fields``) with
                # identical datapoints: a fill-down or copy error. Rows that
                # differ only in other metadata (e.g. a measurement time) are
                # honest repeats of one sample and are collapsed as duplicates.
                # The datapoints belong to at most one of them and the shared
                # labels may be filled down too, so, as for conflicting labels,
                # no copy can be trusted and the group is dropped.
                stats.copy_error += cand.group_size
                logger.warning(
                    "EcoSIS %s: %d rows of different samples share identical datapoints "
                    "(a fill-down or copy error); dropping the group (kept id would have "
                    "been %s)",
                    policy.name,
                    cand.group_size,
                    cand.spectrum_id,
                )
                continue
            stats.duplicate += cand.group_size - 1
            candidates.append(cand)
        return candidates

    @staticmethod
    def _resolve_scales(
        candidates: list[_Candidate],
        policy: DatasetPolicy,
        stats: DatasetIngestStats,
        declared_units: Sequence[str] = (),
    ) -> list[_Candidate]:
        """Choose each spectrum's divisor and drop spectra on unjustifiable scales.

        A per-spectrum units field (``units_field``) is authoritative when its
        value is mapped. Otherwise the dataset base divisor applies, with
        upward escalation for mixed uploads. Spectra far darker than the
        dataset median after scaling (``DARK_LEVEL_RATIO``) are dropped as
        mis-scaled, whether or not they were divided. This runs before
        sampling, so a dark copy can never become a sampled representative.

        Args:
            candidates: Pass-1 candidates.
            policy: The dataset policy.
            stats: Accounting to update (``base_divisor`` is set here).
            declared_units: The package's ``Measurement Units`` values.

        Returns:
            Candidates with a justified divisor and enough valid bands.
        """
        title = stats.title
        inferred_levels = sorted(
            [c.level for c in candidates if c.units_divisor is None]
            or [c.level for c in candidates]
        )
        stats.base_divisor = policy.scale_divisor or divisor_from_levels(inferred_levels)
        median_raw = inferred_levels[len(inferred_levels) // 2] if inferred_levels else 0.0
        if (
            not policy.scale_divisor
            and stats.base_divisor == 1
            and 1.0 < median_raw <= UNIT_CEILING
            and any(u.strip() == "%" for u in declared_units)
        ):
            # A very dark percent-scale dataset (median level 1-1.5 %) and a
            # bright unit-scale one look alike; the declared units say percent
            # but the data were taken as unit scale. Flag it for review.
            logger.warning(
                "EcoSIS dataset %s declares Measurement Units '%%' but its median spectrum "
                "level %.3g was taken as unit scale; if it is really 0-100 %%, set "
                "scale_divisor 100 in its policy",
                title,
                median_raw,
            )
        if stats.base_divisor != 1:
            logger.info(
                "EcoSIS dataset %s: source scale 0-%d (%s); dividing by %d",
                title,
                stats.base_divisor,
                "policy override" if policy.scale_divisor else "inferred",
                stats.base_divisor,
            )

        scaled: list[_Candidate] = []
        for cand in candidates:
            if cand.units_divisor is not None:
                if cand.level / cand.units_divisor > UNIT_CEILING:
                    stats.units_conflict += 1
                    logger.warning(
                        "EcoSIS %s spectrum %s dropped: level %.4g contradicts its declared "
                        "units (divisor %d)",
                        title,
                        cand.spectrum_id,
                        cand.level,
                        cand.units_divisor,
                    )
                    continue
                cand.divisor = cand.units_divisor
            else:
                divisor = resolve_spectrum_divisor(cand.level, stats.base_divisor)
                if divisor is None:
                    stats.no_plausible_scale += 1
                    logger.warning(
                        "EcoSIS %s spectrum %s dropped: level %.4g fits no supported scale "
                        "(dataset divisor %d)",
                        title,
                        cand.spectrum_id,
                        cand.level,
                        stats.base_divisor,
                    )
                    continue
                cand.divisor = divisor
            scaled.append(cand)

        levels = sorted(c.level / c.divisor for c in scaled)
        median_level = levels[len(levels) // 2] if levels else 0.0
        valid: list[_Candidate] = []
        for cand in scaled:
            scaled_level = cand.level / cand.divisor
            if cand.units_divisor is None and scaled_level < median_level / DARK_LEVEL_RATIO:
                # Divided (divisor > 1): a unit-scale spectrum in a percent
                # dataset. Not divided: a spectrum the uploader already divided
                # by 100 (Madison PSR copies ~0.005 among leaves at ~0.47).
                stats.implausibly_dark += 1
                logger.warning(
                    "EcoSIS %s spectrum %s dropped: level %.4g after dividing by %d is "
                    "implausibly far below the dataset median %.4g (mis-scaled)",
                    title,
                    cand.spectrum_id,
                    scaled_level,
                    cand.divisor,
                    median_level,
                )
                continue
            bad = cand.missing + cand.out_of_range[cand.divisor]
            if 1.0 - bad / cand.num_points < MIN_COVERAGE:
                stats.low_coverage += 1
                logger.warning(
                    "EcoSIS %s spectrum %s dropped: only %d of %d bands valid after "
                    "scaling by %d",
                    title,
                    cand.spectrum_id,
                    cand.num_points - bad,
                    cand.num_points,
                    cand.divisor,
                )
                continue
            if cand.units_divisor is None and cand.divisor != stats.base_divisor:
                stats.escalated += 1
                logger.info(
                    "EcoSIS %s spectrum %s re-scaled by %d (dataset divisor %d)",
                    title,
                    cand.spectrum_id,
                    cand.divisor,
                    stats.base_divisor,
                )
            valid.append(cand)
        return valid

    @staticmethod
    def _sample(
        candidates: list[_Candidate],
        spec: SamplingSpec,
        info: dict[int, dict[str, Any]],
    ) -> list[_Candidate]:
        """Deterministically sample candidates, stratified by ``spec``.

        With ``one_per`` set, candidates are first grouped into sampling units
        (e.g. leaves); each unit is represented by its lowest-ranked spectrum
        and units — not spectra — are allocated and ranked.

        Args:
            candidates: Eligible spectra (any order).
            spec: Sampling parameters.
            info: Filled with per-selected-index sampling provenance.

        Returns:
            The selected candidates, in bundle order.
        """
        strata: dict[str, dict[str, _Candidate]] = {}
        for cand in candidates:
            units = strata.setdefault(cand.stratum or "", {})
            unit_key = cand.unit if cand.unit is not None else cand.spectrum_id
            current = units.get(unit_key)
            if current is None or _sample_rank(cand.spectrum_id) < _sample_rank(
                current.spectrum_id
            ):
                units[unit_key] = cand
        alloc = _allocate_strata({k: len(v) for k, v in strata.items()}, spec)
        total_selected = sum(alloc.values())
        selected: list[_Candidate] = []
        for key, units in strata.items():
            ranked = sorted(units.items(), key=lambda item: _sample_rank(item[0]))
            for _, cand in ranked[: alloc[key]]:
                selected.append(cand)
                info[cand.index] = {
                    "stratify_by": spec.stratify_by,
                    "stratum": key,
                    "stratum_available": len(units),
                    "stratum_selected": alloc[key],
                    "dataset_eligible": sum(len(u) for u in strata.values()),
                    "dataset_selected": total_selected,
                }
                if spec.one_per:
                    info[cand.index]["one_per"] = spec.one_per
        selected.sort(key=lambda c: c.index)
        return selected

    @staticmethod
    def _build_record(
        ctx: _DatasetContext,
        policy: DatasetPolicy,
        spectrum: dict[str, Any],
        cand: _Candidate,
        stats: DatasetIngestStats,
        sampling: dict[str, Any] | None,
    ) -> SpectrumRecord:
        """Second pass: build one record.

        Args:
            ctx: Dataset-level context.
            policy: The dataset policy.
            spectrum: The spectrum item.
            cand: Its pass-1 summary (carries the chosen divisor).
            stats: Accounting to update.
            sampling: Sampling provenance, for sampled datasets.

        Returns:
            The record, with values scaled to the unit convention and
            invalid bands as NaN.
        """
        wavelengths, raw_values = _parse_datapoints(spectrum.get("datapoints") or {})
        values, missing, masked = scale_and_mask(raw_values, cand.divisor)
        stats.missing_values += missing
        stats.masked_out_of_range += len(masked)

        identity = policy.identity
        spec_target = _field_str(spectrum, identity.target_type_field) or ""
        category = _material_category(spectrum, identity, spec_target, ctx)
        quantity = ECOSIS_QUANTITY_MAP[cand.quantity]
        material_name = _material_name(spectrum, identity, spec_target, ctx.title)
        # The sample id is the upload's own sample identifier ("Sample ID",
        # "SampleID", "sample_id" …). The USDA PLANTS symbol names the
        # species, not the sample, so it goes to extra.
        sample_id = _field_str(spectrum, SAMPLE_ID_FIELD)
        sample_description = _field_str(spectrum, SAMPLE_DESCRIPTION_FIELD)
        usda_symbol = _field_str(spectrum, USDA_SYMBOL_FIELD)

        # Dataset-level traceability lives in the footer ``datasets`` entry
        # referenced by ``dataset_id`` — don't duplicate it here.
        additional: dict[str, Any] = {}
        if cand.divisor != 1:
            additional["source_reflectance_divisor"] = cand.divisor
        if cand.divisor != stats.base_divisor:
            additional["dataset_reflectance_divisor"] = stats.base_divisor
        if missing or masked:
            additional["source_missing_band_count"] = missing
            additional["masked_band_count"] = len(masked)
            additional["masked_bands"] = [[wavelengths[i], raw_values[i]] for i in masked]
        if cand.duplicate_ids:
            additional["duplicate_ids"] = list(cand.duplicate_ids)
        # A duplicate group's members may disagree on sample labels (adjacent
        # pixels with different coordinates, one canopy measurement linked to
        # several trait samples). The kept copy's own value would be one
        # arbitrary choice, so such fields are left out and every value the
        # group carried is recorded instead.
        conflicts = _conflicting_labels(cand, policy)
        if conflicts:
            additional["duplicate_conflicting_fields"] = conflicts
        if SAMPLE_ID_FIELD in conflicts or cand.conflict_merged:
            sample_id = None
        if SAMPLE_DESCRIPTION_FIELD in conflicts:
            sample_description = None
        if USDA_SYMBOL_FIELD in conflicts:
            usda_symbol = None
        if usda_symbol is not None:
            # Some uploads put a common or genus name ("Grape", "Salix sp.") or
            # two joined symbols ("SPAL-SPCY") in the column; only a value
            # shaped like a USDA PLANTS symbol is stored as one.
            key = "usda_symbol" if USDA_SYMBOL_SHAPE.fullmatch(usda_symbol) else "source_plant_code"
            additional[key] = usda_symbol
        carried = {
            name: value
            for name in policy.carry_fields
            if name not in conflicts and (value := _field_str(spectrum, name)) is not None
        }
        if carried:
            additional["ecosis_metadata"] = carried
        if sampling is not None:
            additional["ecosis_sampling"] = sampling
        # EcoSIS band keys such as "400" and "400.0" parse to the same
        # wavelength; keep the axis strictly monotonic (spec section 4.3).
        axis_wavelengths, axis_values, axis_extra = monotonic_axis(wavelengths, values)
        additional.update(axis_extra)
        spectrum_units = [
            value
            for name in _SPECTRUM_WAVELENGTH_UNIT_FIELDS
            if (value := _field_str(spectrum, name)) is not None
        ]
        wavelength_unit, unit_note = resolve_wavelength_unit(
            axis_wavelengths, spectrum_units, ctx.wavelength_units
        )
        if unit_note is not None:
            additional["wavelength_unit_inferred"] = unit_note

        setup = _spectrum_setup(spectrum, policy.setup)

        def setup_value(name: str) -> Any:
            return setup[name] if name in setup else ctx.package_setup.get(name)

        keywords = _keywords(spectrum, identity, ctx.ecosystem)
        if cand.conflict_merged:
            # One measurement linked to several labelled samples (NGEE canopy
            # spectra whose field of view holds the harvested species).
            material_name = policy.conflicting_duplicates.material_name or material_name
            for linked in sorted(cand.duplicate_names):
                if linked.lower() not in keywords:
                    keywords.append(linked.lower())
            additional["linked_material_names"] = sorted(cand.duplicate_names)
            if cand.link_values:
                additional["linked_sample_ids"] = sorted(cand.link_values)

        return SpectrumRecord(
            id=format_spectrum_id(COLLECTION_ID, cand.spectrum_id),
            collection=COLLECTION_ID,
            original_id=cand.spectrum_id,
            # The dataset title is kept whole: a cut title is ambiguous.
            name=f"{material_name} ({ctx.title})",
            license=ctx.dataset.license or "NOASSERTION",
            dataset_id=ctx.dataset_id,
            material=Material(
                name=material_name,
                category=category,
                subcategory=spec_target or None,
                keywords=keywords,
            ),
            sample=Sample(id=sample_id, description=sample_description),
            measurement=Measurement(
                quantity=quantity,
                geometry=setup.get("geometry", ECOSIS_GEOMETRY),
                instrument=setup_value("instrument"),
                instrument_type=setup.get("instrument_type"),
                laboratory=ctx.organization or None,
                processing=list(ctx.processing),
                light_source=setup_value("light_source"),
                venue=setup_value("venue"),
                acquisition_method=setup_value("acquisition_method"),
                foreoptic=setup_value("foreoptic"),
                reference=policy.reference,
            ),
            spectral_data=SpectralData(
                wavelength_unit=wavelength_unit,
                wavelengths=axis_wavelengths,
                values=axis_values,
            ),
            extra=additional,
        )


# Per-dataset ingest counters reported in ``inclusion_notes``, with their wording.
_DROP_LABELS: tuple[tuple[str, str], ...] = (
    ("excluded_resource", "from resources that are not measured spectra"),
    ("unsupported_quantity", "of an unsupported quantity (absorptance)"),
    ("dropped_by_policy", "dropped by policy"),
    ("parse_failed", "without usable datapoints"),
    ("missing_id", "without an EcoSIS _id"),
    ("missing_stratum", "without a sampling stratum"),
    ("duplicate", "duplicate uploads collapsed"),
    ("conflicting_duplicate", "in duplicate groups labelled as different materials"),
    (
        "conflict_groups_kept",
        "duplicate groups of one measurement linked to several labelled samples, "
        "each kept once under a neutral name (their extra rows are counted as duplicates)",
    ),
    (
        "copy_error",
        "in fill-down copy-error groups (one spectrum repeated on rows of different samples)",
    ),
    ("units_conflict", "contradicting their declared units"),
    ("no_plausible_scale", "fitting no supported value scale"),
    ("implausibly_dark", "implausibly dark after scaling"),
    ("low_coverage", "with too few valid bands"),
    ("constant_fill", "constant no-data fills"),
    ("saturated", "saturated or clipped (over half of the bands hold one value)"),
    ("not_sampled", "not selected by sampling"),
    ("build_failed", "that failed to build"),
)


def _dataset_inclusion(
    policy: DatasetPolicy, stats: DatasetIngestStats
) -> tuple[Inclusion, str | None]:
    """Describe how much of a package the collection holds.

    Args:
        policy: The package's inclusion policy.
        stats: Its ingest accounting.

    Returns:
        ``(complete, None)`` when every spectrum of the bundle was included,
        otherwise ``(subset, notes)`` with the sampling rule and every
        non-zero drop count.
    """
    if policy.inclusion == "full" and stats.included == stats.available:
        return Inclusion.COMPLETE, None
    parts: list[str] = []
    if policy.sampling is not None:
        spec = policy.sampling
        caps = []
        if spec.max_total is not None:
            caps.append(f"at most {spec.max_total} in total")
        if spec.max_per_stratum is not None:
            caps.append(f"at most {spec.max_per_stratum} per stratum")
        if spec.one_per:
            caps.append(f"one per '{spec.one_per}'")
        rule = f"Deterministic stratified sample by '{spec.stratify_by}'"
        if caps:
            rule += f" ({', '.join(caps)})"
        parts.append(f"{rule}. {policy.reason}")
    drops = [
        f"{getattr(stats, name)} {label}" for name, label in _DROP_LABELS if getattr(stats, name)
    ]
    if drops:
        parts.append(f"Of {stats.available} spectra: " + "; ".join(drops) + ".")
    return Inclusion.SUBSET, " ".join(parts) or "Subset of the package."
