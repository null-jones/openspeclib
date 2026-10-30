"""Pydantic models for OpenSpecLib Format v1 records and collection metadata.

Loaders build one nested :class:`SpectrumRecord` per spectrum; the storage
layer (:mod:`openspeclib.storage`) flattens it to the snake_case columns of a
Format v1 collection file (``docs/spec/format-v1.md``). Collection-level
provenance and licensing live in :class:`CollectionInfo` and
:class:`DatasetInfo`, which become the ``openspeclib`` footer of that file.

Derived values are not stored on the record: the wavelength extent in
nanometres, the point count and band-validity counts all follow from the
arrays and are computed when the file is written or read.
"""

import datetime as _dt
import re
from enum import Enum
from typing import Any, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Version of the OpenSpecLib Format specification this package writes.
FORMAT_VERSION = "1.0.0"

# Collection id grammar (rule F5).
COLLECTION_ID_PATTERN = r"^[a-z0-9][a-z0-9_]{1,62}$"
# Characters allowed in the local part of a spectrum id, and its length limit (F5).
LOCAL_ID_ALLOWED = r"A-Za-z0-9._~-"
LOCAL_ID_MAX_LENGTH = 200
_LOCAL_ID_RE = re.compile(rf"^[{LOCAL_ID_ALLOWED}]{{1,{LOCAL_ID_MAX_LENGTH}}}$")
_COLLECTION_ID_RE = re.compile(COLLECTION_ID_PATTERN)
# A DOI as the footer schema requires it (no ``doi:`` or URL prefix).
DOI_PATTERN = r"^10\.[0-9]{4,}/\S+$"
# Keys of ``collection.license_refs`` (SPDX idstring: letters, digits, ``.``, ``-``).
LICENSE_REF_PATTERN = r"^LicenseRef-[A-Za-z0-9.-]+$"
_LICENSE_REF_RE = re.compile(LICENSE_REF_PATTERN)
# Finite values above this magnitude are fill/sentinel values (rule F9).
SENTINEL_MAGNITUDE = 1e10


class MaterialCategory(str, Enum):
    """Vocabulary ``material_category``: top-level material class."""

    MINERAL = "mineral"
    ROCK = "rock"
    SOIL = "soil"
    VEGETATION = "vegetation"
    NPV = "npv"
    WATER = "water"
    SNOW_ICE = "snow_ice"
    MAN_MADE = "man_made"
    METEORITE = "meteorite"
    LUNAR = "lunar"
    ORGANIC_COMPOUND = "organic_compound"
    MIXTURE = "mixture"
    OTHER = "other"


class Quantity(str, Enum):
    """Vocabulary ``quantity``: the physical quantity of ``values``.

    Absorbance is decadic, A = log10(1/x).
    """

    REFLECTANCE = "reflectance"
    TRANSMITTANCE = "transmittance"
    EMISSIVITY = "emissivity"
    ABSORBANCE = "absorbance"


class Geometry(str, Enum):
    """Vocabulary ``geometry``: illumination/viewing geometry (Schaepman-Strub et al. 2006)."""

    BIDIRECTIONAL = "bidirectional"
    BICONICAL = "biconical"
    DIRECTIONAL_HEMISPHERICAL = "directional_hemispherical"
    HEMISPHERICAL_DIRECTIONAL = "hemispherical_directional"
    HEMISPHERICAL_CONICAL = "hemispherical_conical"
    BIHEMISPHERICAL = "bihemispherical"
    DIRECTIONAL = "directional"
    UNKNOWN = "unknown"


class WavelengthUnit(str, Enum):
    """Vocabulary ``wavelength_unit``: unit of the native spectral axis."""

    MICROMETERS = "um"
    NANOMETERS = "nm"
    WAVENUMBERS = "cm-1"


class Inclusion(str, Enum):
    """Vocabulary ``inclusion``: whether a collection or dataset is complete."""

    COMPLETE = "complete"
    SUBSET = "subset"


def is_valid_collection_id(value: str) -> bool:
    """Check a collection id against the F5 grammar.

    Args:
        value: Candidate collection id.

    Returns:
        Whether ``value`` matches ``^[a-z0-9][a-z0-9_]{1,62}$``.
    """
    return bool(_COLLECTION_ID_RE.match(value))


def license_refs_used(expression: str) -> set[str]:
    """Validate a licence expression (rule F11) and return its ``LicenseRef-`` ids.

    Args:
        expression: An SPDX license expression, a ``LicenseRef-`` id or
            ``NOASSERTION`` (on its own).

    Returns:
        The ``LicenseRef-`` identifiers the expression uses.

    Raises:
        ValueError: If the expression is not valid (e.g. lower-case
            operators, free text such as ``Creative Commons Attribution``,
            ``NOASSERTION`` combined with other terms, or a ``LicenseRef-``
            with characters other than letters, digits, ``.`` and ``-``).
    """
    from openspeclib.conformance import check_spdx_expression

    error, refs = check_spdx_expression(expression)
    if error is not None:
        raise ValueError(f"licence {expression!r} is not a valid SPDX expression: {error}")
    return refs


def is_valid_local_id(value: str) -> bool:
    """Check the local part of a spectrum id against the F5 grammar.

    Args:
        value: Candidate local id (the part after ``{collection}:``).

    Returns:
        Whether ``value`` is 1–200 characters from ``[A-Za-z0-9._~-]``.
    """
    return bool(_LOCAL_ID_RE.match(value))


# ---------------------------------------------------------------------------
# Per-record sub-models
# ---------------------------------------------------------------------------


class Material(BaseModel):
    """What was measured (core ``material_*`` columns plus the ``material`` extension)."""

    name: str = Field(description="Material name (e.g. Olivine, Quartz, Oak leaf).")
    category: MaterialCategory = Field(description="Top-level material category.")
    subcategory: Optional[str] = Field(
        default=None,
        description="Source-specific subcategory (e.g. ECOSTRESS SubClass, mineral group).",
    )
    formula: Optional[str] = Field(default=None, description="Chemical formula where applicable.")
    keywords: list[str] = Field(default_factory=list, description="Searchable terms for discovery.")


class Sample(BaseModel):
    """The physical sample (``sample`` extension)."""

    id: Optional[str] = Field(default=None, description="Sample identifier from the source.")
    description: Optional[str] = Field(default=None, description="Free-text sample description.")
    particle_size: Optional[str] = Field(
        default=None, description="Particle size or size range as reported."
    )
    origin: Optional[str] = Field(
        default=None, description="Geographic or geological origin of the sample."
    )
    owner: Optional[str] = Field(
        default=None, description="Institution or individual owning the sample."
    )
    collection_date: Optional[_dt.date] = Field(
        default=None, description="Date the sample was collected (ISO 8601)."
    )
    preparation: Optional[str] = Field(
        default=None, description="Description of sample preparation procedures."
    )


class Measurement(BaseModel):
    """What was measured and how (core ``quantity``/``geometry`` plus ``measurement_*``)."""

    quantity: Quantity = Field(description="Physical quantity of the values.")
    geometry: Geometry = Field(
        description=(
            "Illumination/viewing geometry. ``unknown`` when the source does not state it; "
            "never guessed."
        )
    )
    instrument: Optional[str] = Field(default=None, description="Instrument name and model.")
    instrument_type: Optional[str] = Field(
        default=None,
        description="Category of instrument (e.g. laboratory_spectrometer, ftir).",
    )
    laboratory: Optional[str] = Field(
        default=None, description="Laboratory or facility where the measurement was performed."
    )
    date: Optional[_dt.date] = Field(
        default=None, description="Date the measurement was acquired (ISO 8601)."
    )
    processing: list[str] = Field(
        default_factory=list,
        description=(
            "Processing flags applied to the values, in order (e.g. ``averaged``, "
            "``interpolated``, ``resampled``, ``simulated``, ``absorbance_from_reflectance``)."
        ),
    )
    light_source: Optional[str] = Field(
        default=None, description="Illumination source (e.g. sun, lamp)."
    )
    venue: Optional[str] = Field(
        default=None, description="Measurement venue (e.g. field, greenhouse, laboratory)."
    )
    acquisition_method: Optional[str] = Field(
        default=None,
        description="Acquisition method (e.g. contact, integrating_sphere, imaging_spectrometer).",
    )
    foreoptic: Optional[str] = Field(
        default=None,
        description="Foreoptic used for the measurement (e.g. leaf clip, bare fibre).",
    )
    reference: Optional[str] = Field(
        default=None,
        description="Reference standard the values are relative to (e.g. spectralon, absolute).",
    )


class SpectralData(BaseModel):
    """The spectral axis and the arrays measured along it."""

    # Missing bands are NaN in ``values``. Pydantic's default JSON output
    # writes NaN as null, which then fails to parse back as a float, so the
    # ingest -> combine JSONL round trip would reject every gappy spectrum.
    # Emit the NaN constant instead (read back by pydantic and Python's json).
    model_config = ConfigDict(ser_json_inf_nan="constants")

    wavelength_unit: WavelengthUnit = Field(
        description="Unit of the wavelength axis (um, nm, or cm-1), native to the source."
    )
    wavelengths: list[float] = Field(description="Wavelength (or wavenumber) axis values.")
    values: list[float] = Field(description="Spectral values; NaN marks a missing band.")
    bandpass_fwhm: Optional[list[float]] = Field(
        default=None,
        description="Full width at half maximum per band, in ``wavelength_unit``.",
    )
    values_stddev: Optional[list[float]] = Field(
        default=None, description="Standard deviation of each value."
    )

    @model_validator(mode="after")
    def _check_lengths(self) -> "SpectralData":
        """Enforce the per-row array rules F8, F9 and F13 of Format v1.

        Returns:
            The validated model.

        Raises:
            ValueError: If the axis has fewer than 2 points, is not finite and
                positive, or is not strictly monotonic; if an array differs in
                length from the axis; if ``values`` holds an infinity or a
                sentinel-sized number (``|v| > 1e10``, which includes anything
                that would overflow float32); or if ``bandpass_fwhm`` /
                ``values_stddev`` hold a negative or infinite number.
        """
        n = len(self.wavelengths)
        if n < 2:
            raise ValueError(f"wavelengths has {n} point(s); at least 2 are required")
        axis = np.asarray(self.wavelengths, dtype=np.float64)
        if not (np.all(np.isfinite(axis)) and np.all(axis > 0)):
            raise ValueError("wavelengths must all be finite and greater than 0")
        steps = np.diff(axis)
        if not (np.all(steps > 0) or np.all(steps < 0)):
            raise ValueError(
                "wavelengths must be strictly increasing or strictly decreasing "
                "(reorder or de-duplicate bands, see loaders.base.sort_by_wavelength)"
            )
        if len(self.values) != n:
            raise ValueError(f"values has {len(self.values)} points, wavelengths {n}")
        values = np.asarray(self.values, dtype=np.float64)
        bad = ~np.isnan(values) & ~(np.abs(values) <= SENTINEL_MAGNITUDE)
        if np.any(bad):
            first = int(np.argmax(bad))
            raise ValueError(
                f"values[{first}] = {values[first]!r}: values must be finite with magnitude "
                f"<= {SENTINEL_MAGNITUDE:g}, or NaN for a missing band"
            )
        for name in ("bandpass_fwhm", "values_stddev"):
            array = getattr(self, name)
            if array is None:
                continue
            if len(array) != n:
                raise ValueError(f"{name} has {len(array)} points, wavelengths {n}")
            data = np.asarray(array, dtype=np.float64)
            if np.any(~np.isnan(data) & ~(np.isfinite(data) & (data >= 0))):
                raise ValueError(f"{name} must be finite and non-negative, or NaN")
            # Stored as float32: anything larger would become +inf (F13).
            if np.any(data > np.finfo(np.float32).max):
                raise ValueError(f"{name} values must fit in float32 (<= 3.4028235e38)")
        return self


class Quality(BaseModel):
    """Free-text quality information (``quality`` extension)."""

    notes: Optional[str] = Field(default=None, description="Additional quality notes.")


# ---------------------------------------------------------------------------
# Spectrum record
# ---------------------------------------------------------------------------


class SpectrumRecord(BaseModel):
    """One spectrum, as produced by a loader and stored as one collection-file row."""

    id: str = Field(description="Spectrum id ``{collection}:{local_id}`` (rule F5).")
    collection: str = Field(description="Collection id; equals the footer ``collection.id``.")
    original_id: str = Field(
        min_length=1, description="The identifier the upstream source uses, unchanged."
    )
    name: str = Field(description="Human-readable display label.")
    license: str = Field(
        min_length=1,
        description="SPDX expression, ``LicenseRef-`` identifier or ``NOASSERTION``.",
    )
    dataset_id: Optional[str] = Field(
        default=None,
        description="Key of the contributing dataset (aggregated collections only).",
    )
    material: Material = Field(description="Material classification.")
    sample: Sample = Field(default_factory=Sample, description="Sample information.")
    measurement: Measurement = Field(description="Measurement conditions.")
    spectral_data: SpectralData = Field(description="Spectral axis and values.")
    quality: Quality = Field(default_factory=Quality, description="Quality notes.")
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Producer-specific metadata not captured by the standard fields.",
    )

    @field_validator("original_id")
    @classmethod
    def _check_original_id(cls, value: str) -> str:
        """Reject a whitespace-only upstream id (rule F2).

        Args:
            value: The upstream id.

        Returns:
            The unchanged id.

        Raises:
            ValueError: If the id is empty or whitespace only.
        """
        if not value.strip():
            raise ValueError("original_id must not be empty or whitespace only")
        return value

    @field_validator("license")
    @classmethod
    def _check_license(cls, value: str) -> str:
        """Validate the licence expression syntax (rule F11).

        Args:
            value: The licence expression.

        Returns:
            The unchanged expression.
        """
        license_refs_used(value)
        return value

    @field_validator("collection")
    @classmethod
    def _check_collection(cls, value: str) -> str:
        """Validate the collection id grammar.

        Args:
            value: The collection id.

        Returns:
            The unchanged collection id.

        Raises:
            ValueError: If the id does not match the F5 grammar.
        """
        if not is_valid_collection_id(value):
            raise ValueError(f"collection id {value!r} does not match {COLLECTION_ID_PATTERN}")
        return value

    @model_validator(mode="after")
    def _check_id(self) -> "SpectrumRecord":
        """Validate that ``id`` is ``{collection}:{local_id}`` with a legal local id.

        Returns:
            The validated record.

        Raises:
            ValueError: If the id does not follow rule F5.
        """
        prefix = f"{self.collection}:"
        if not self.id.startswith(prefix) or not is_valid_local_id(self.id[len(prefix) :]):
            raise ValueError(
                f"id {self.id!r} must be '{prefix}' followed by 1-{LOCAL_ID_MAX_LENGTH} "
                f"characters from [{LOCAL_ID_ALLOWED}]"
            )
        return self


# ---------------------------------------------------------------------------
# Collection metadata (the ``openspeclib`` footer)
# ---------------------------------------------------------------------------


class LicenseRef(BaseModel):
    """Definition of a ``LicenseRef-`` identifier (footer ``collection.license_refs``)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Human-readable licence name.")
    text: Optional[str] = Field(default=None, description="Licence or terms text.")
    url: Optional[str] = Field(default=None, description="URL of the licence or terms.")


class CollectionInfo(BaseModel):
    """The footer ``collection`` object: identity, provenance and licence of a collection."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=COLLECTION_ID_PATTERN, description="Collection id.")
    title: str = Field(min_length=1, description="Collection title.")
    description: Optional[str] = Field(default=None, description="Free-text description.")
    version: Optional[str] = Field(default=None, description="Version of the source collection.")
    url: Optional[str] = Field(default=None, description="Landing page or DOI URL.")
    doi: Optional[str] = Field(
        default=None, pattern=DOI_PATTERN, description="DOI of the collection (bare, 10.x/...)."
    )
    publisher: Optional[str] = Field(default=None, description="Publishing organisation.")
    license: str = Field(
        min_length=1,
        description="SPDX expression, ``LicenseRef-`` identifier or ``NOASSERTION``.",
    )
    license_note: Optional[str] = Field(default=None, description="Licence clarification.")
    license_refs: dict[str, LicenseRef] = Field(
        default_factory=dict,
        description="Definitions of every ``LicenseRef-`` identifier used in the file.",
    )
    citation: str = Field(min_length=1, description="Recommended citation.")
    inclusion: Inclusion = Field(description="``complete`` or ``subset``.")
    inclusion_notes: Optional[str] = Field(
        default=None, description="How a subset was chosen (required for ``subset``)."
    )
    keywords: list[str] = Field(default_factory=list, description="Search keywords.")

    @model_validator(mode="after")
    def _check_subset_notes(self) -> "CollectionInfo":
        """Require ``inclusion_notes`` for a subset.

        Returns:
            The validated model.

        Raises:
            ValueError: If a subset has no notes.
        """
        if self.inclusion is Inclusion.SUBSET and not self.inclusion_notes:
            raise ValueError("a subset collection must explain its selection in inclusion_notes")
        return self

    @model_validator(mode="after")
    def _check_licenses(self) -> "CollectionInfo":
        """Validate the licence, the ``license_refs`` keys and their use (F11).

        Returns:
            The validated model.

        Raises:
            ValueError: If the licence is not a valid expression, a
                ``license_refs`` key is not ``LicenseRef-[A-Za-z0-9.-]+``, or
                the licence uses an undefined ``LicenseRef-``.
        """
        for key in self.license_refs:
            if not _LICENSE_REF_RE.match(key):
                raise ValueError(
                    f"license_refs key {key!r} must match {LICENSE_REF_PATTERN} "
                    "(letters, digits, '.' and '-' only)"
                )
        undefined = license_refs_used(self.license) - set(self.license_refs)
        if undefined:
            raise ValueError(f"collection licence uses undefined {sorted(undefined)}")
        return self

    def to_footer(self) -> dict[str, Any]:
        """Render the footer ``collection`` object.

        Returns:
            A JSON-serialisable dict valid against ``#/$defs/collection``,
            with unset optional fields omitted.
        """
        data = self.model_dump(mode="json", exclude_none=True)
        if not data.get("license_refs"):
            data.pop("license_refs", None)
        if not data.get("keywords"):
            data.pop("keywords", None)
        return data


class DatasetInfo(BaseModel):
    """A contributing dataset of an aggregated collection (footer ``datasets`` entry)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Dataset key, referenced by rows' ``dataset_id``.")
    title: str = Field(min_length=1, description="Dataset title.")
    description: Optional[str] = Field(default=None, description="Free-text description.")
    url: Optional[str] = Field(default=None, description="Dataset landing page.")
    doi: Optional[str] = Field(
        default=None, pattern=DOI_PATTERN, description="Dataset DOI (bare, 10.x/...)."
    )
    license: Optional[str] = Field(
        default=None,
        description="SPDX expression, ``LicenseRef-`` identifier or ``NOASSERTION``.",
    )

    @field_validator("license")
    @classmethod
    def _check_license(cls, value: Optional[str]) -> Optional[str]:
        """Validate the licence expression syntax (rule F11).

        Args:
            value: The licence expression, or ``None``.

        Returns:
            The unchanged expression.
        """
        if value is not None:
            license_refs_used(value)
        return value

    license_note: Optional[str] = Field(default=None, description="Licence clarification.")
    citation: Optional[str] = Field(default=None, description="Recommended citation.")
    authors: Optional[str] = Field(default=None, description="Authors (semicolon-delimited).")
    organization: Optional[str] = Field(default=None, description="Contributing organisation.")
    inclusion: Optional[Inclusion] = Field(default=None, description="``complete`` or ``subset``.")
    inclusion_notes: Optional[str] = Field(default=None, description="How a subset was chosen.")
    spectra_available: Optional[int] = Field(
        default=None, ge=0, description="Spectra the source publishes for this dataset."
    )
    spectra_included: Optional[int] = Field(
        default=None, ge=0, description="Spectra of this dataset in the collection."
    )

    def to_footer(self) -> dict[str, Any]:
        """Render the footer ``datasets`` entry (the id is the map key, not a field).

        Returns:
            A JSON-serialisable dict valid against ``#/$defs/dataset``.
        """
        return self.model_dump(mode="json", exclude_none=True, exclude={"id"})


class CollectionMetadata(BaseModel):
    """Collection and dataset metadata written next to an ingested JSONL file.

    ``openspeclib ingest`` writes it as ``{collection}.collection.json`` so
    that ``combine`` can build the footer without re-running the loader.
    """

    collection: CollectionInfo = Field(description="The footer ``collection`` object.")
    datasets: dict[str, DatasetInfo] = Field(
        default_factory=dict, description="Contributing datasets keyed by dataset id."
    )
