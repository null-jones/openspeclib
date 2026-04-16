"""Pydantic models defining the OpenSpecLib unified data structure.

These models serve as the single source of truth for the spectral library schema.
JSON Schema files in schemas/ are generated from these models.
"""

import datetime as _dt
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class SourceLibrary(str, Enum):
    """Supported source spectral libraries."""

    USGS_SPLIB07 = "usgs_splib07"
    ECOSTRESS = "ecostress"
    RELAB = "relab"
    ASU_TES = "asu_tes"
    BISHOP = "bishop"
    ECOSIS = "ecosis"


class MaterialCategory(str, Enum):
    """Controlled vocabulary for material classification."""

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


class MeasurementTechnique(str, Enum):
    """Type of spectral measurement."""

    REFLECTANCE = "reflectance"
    EMISSIVITY = "emissivity"
    ABSORBANCE = "absorbance"
    TRANSMITTANCE = "transmittance"


class WavelengthUnit(str, Enum):
    """Wavelength or wavenumber units."""

    MICROMETERS = "um"
    NANOMETERS = "nm"
    WAVENUMBERS = "cm-1"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """Provenance information linking a spectrum to its origin library."""

    library: SourceLibrary = Field(description="Source spectral library identifier.")
    library_version: str = Field(description="Version of the source library.")
    original_id: str = Field(description="Identifier used in the source library.")
    filename: Optional[str] = Field(
        default=None, description="Original filename in the source archive."
    )
    url: str = Field(description="DOI or URL for the source library.")
    license: str = Field(description="License governing the source data.")
    citation: str = Field(description="Recommended citation string.")


class Material(BaseModel):
    """Classification of the material whose spectrum was measured."""

    name: str = Field(description="Material name (e.g. Olivine, Quartz, Oak leaf).")
    category: MaterialCategory = Field(description="Top-level material category.")
    subcategory: Optional[str] = Field(
        default=None,
        description="Source-specific subcategory (e.g. ECOSTRESS SubClass, mineral group).",
    )
    formula: Optional[str] = Field(default=None, description="Chemical formula where applicable.")
    keywords: list[str] = Field(default_factory=list, description="Searchable terms for discovery.")


class Sample(BaseModel):
    """Information about the physical sample that was measured."""

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
    """Instrument and conditions under which the spectrum was acquired."""

    instrument: Optional[str] = Field(default=None, description="Instrument name and model.")
    instrument_type: Optional[str] = Field(
        default=None,
        description="Category of instrument (e.g. laboratory_spectrometer, ftir).",
    )
    laboratory: Optional[str] = Field(
        default=None, description="Laboratory or facility where the measurement was performed."
    )
    technique: MeasurementTechnique = Field(
        description="Measurement technique (reflectance, emissivity, absorbance, transmittance)."
    )
    geometry: Optional[str] = Field(
        default=None,
        description="Measurement geometry (e.g. biconical, hemispherical).",
    )
    date: Optional[_dt.date] = Field(
        default=None, description="Date the measurement was acquired (ISO 8601)."
    )


class SpectralData(BaseModel):
    """Spectral measurement data and associated axis information."""

    type: MeasurementTechnique = Field(
        description="Type of spectral values (reflectance, emissivity, absorbance, transmittance)."
    )
    wavelength_unit: WavelengthUnit = Field(
        description="Unit of the wavelength axis (um, nm, or cm-1)."
    )
    wavelength_min: float = Field(description="Minimum value on the wavelength axis.")
    wavelength_max: float = Field(description="Maximum value on the wavelength axis.")
    num_points: int = Field(ge=1, description="Number of spectral data points.")
    wavelengths: list[float] = Field(description="Wavelength (or wavenumber) axis values.")
    values: list[float] = Field(description="Spectral measurement values.")
    bandpass: Optional[list[float]] = Field(
        default=None,
        description="Bandpass (FWHM) at each wavelength position, if available.",
    )


class SpectralDataSummary(BaseModel):
    """Spectral data metadata for catalog entries (no raw arrays)."""

    type: MeasurementTechnique = Field(description="Type of spectral values.")
    wavelength_unit: WavelengthUnit = Field(description="Unit of the wavelength axis.")
    wavelength_min: float = Field(description="Minimum value on the wavelength axis.")
    wavelength_max: float = Field(description="Maximum value on the wavelength axis.")
    num_points: int = Field(ge=1, description="Number of spectral data points.")


class Quality(BaseModel):
    """Data quality indicators for a spectrum."""

    has_bad_bands: bool = Field(
        default=False, description="Whether any bad or invalid bands are present."
    )
    bad_band_count: int = Field(default=0, ge=0, description="Number of invalid data points.")
    coverage_fraction: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fraction of data points that are valid (0.0–1.0).",
    )
    notes: Optional[str] = Field(default=None, description="Additional quality notes.")


# ---------------------------------------------------------------------------
# Top-level record models
# ---------------------------------------------------------------------------


class SpectrumRecord(BaseModel):
    """A single spectrum with full spectral data — used in library chunk files."""

    id: str = Field(description="Globally unique identifier ({source_library}:{original_id}).")
    name: str = Field(description="Human-readable display name.")
    source: Source = Field(description="Provenance information.")
    material: Material = Field(description="Material classification.")
    sample: Sample = Field(description="Sample information.")
    measurement: Measurement = Field(description="Measurement conditions.")
    spectral_data: SpectralData = Field(description="Spectral measurement data.")
    additional_properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Source-specific metadata not captured by the standard fields.",
    )
    quality: Quality = Field(default_factory=Quality, description="Data quality indicators.")


class CatalogRecord(BaseModel):
    """A spectrum's metadata for the catalog index — no raw spectral arrays."""

    id: str = Field(description="Globally unique identifier.")
    name: str = Field(description="Human-readable display name.")
    chunk_file: str = Field(
        description="Relative path to the library chunk file containing full spectral data."
    )
    source: Source = Field(description="Provenance information.")
    material: Material = Field(description="Material classification.")
    sample: Sample = Field(description="Sample information.")
    measurement: Measurement = Field(description="Measurement conditions.")
    spectral_data: SpectralDataSummary = Field(description="Spectral data summary (no arrays).")
    additional_properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Source-specific metadata.",
    )
    quality: Quality = Field(default_factory=Quality, description="Data quality indicators.")

    @classmethod
    def from_spectrum(cls, record: SpectrumRecord, chunk_file: str) -> "CatalogRecord":
        """Create a catalog entry from a full spectrum record.

        Args:
            record: The full spectrum record to summarise.
            chunk_file: Relative path to the chunk file containing this spectrum.

        Returns:
            A ``CatalogRecord`` with spectral arrays omitted.
        """
        return cls(
            id=record.id,
            name=record.name,
            chunk_file=chunk_file,
            source=record.source,
            material=record.material,
            sample=record.sample,
            measurement=record.measurement,
            spectral_data=SpectralDataSummary(
                type=record.spectral_data.type,
                wavelength_unit=record.spectral_data.wavelength_unit,
                wavelength_min=record.spectral_data.wavelength_min,
                wavelength_max=record.spectral_data.wavelength_max,
                num_points=record.spectral_data.num_points,
            ),
            additional_properties=record.additional_properties,
            quality=record.quality,
        )


# ---------------------------------------------------------------------------
# File-level models
# ---------------------------------------------------------------------------


class SourceInfo(BaseModel):
    """Metadata about a source library included in the catalog."""

    name: str = Field(description="Full name of the source library.")
    version: str = Field(description="Version of the source library.")
    url: str = Field(description="DOI or URL for the source library.")
    license: str = Field(description="License governing the source data.")
    license_url: Optional[str] = Field(
        default=None, description="URL to the full license text, if available."
    )
    citation: str = Field(description="Recommended citation string.")
    citation_doi: Optional[str] = Field(
        default=None, description="DOI for the citation, if available."
    )
    spectrum_count: int = Field(ge=0, description="Number of spectra from this source.")


class CatalogStatistics(BaseModel):
    """Aggregate statistics for the full library."""

    total_spectra: int = Field(ge=0, description="Total number of spectra in the library.")
    categories: dict[str, int] = Field(
        default_factory=dict,
        description="Spectrum count per material category.",
    )


class CatalogFile(BaseModel):
    """Top-level structure of the catalog.json file."""

    openspeclib_version: str = Field(description="Version of the OpenSpecLib schema.")
    generated_at: _dt.datetime = Field(description="Timestamp when the catalog was generated.")
    sources: dict[str, SourceInfo] = Field(description="Metadata for each source library included.")
    statistics: CatalogStatistics = Field(description="Aggregate library statistics.")
    spectra: list[CatalogRecord] = Field(description="Catalog entries for all spectra.")


class LibraryChunkFile(BaseModel):
    """In-memory representation of a source library Parquet file (spectra/{source}.parquet)."""

    openspeclib_version: str = Field(description="Version of the OpenSpecLib schema.")
    source: str = Field(description="Source library identifier for this file.")
    spectrum_count: int = Field(ge=0, description="Number of spectra in this file.")
    spectra: list[SpectrumRecord] = Field(description="Full spectrum records.")


class LicenseEntry(BaseModel):
    """Licensing and citation information for a single source library."""

    name: str = Field(description="Full name of the source library.")
    version: str = Field(description="Version of the source library.")
    url: str = Field(description="DOI or URL for the source library.")
    license: str = Field(description="License governing the source data.")
    license_url: Optional[str] = Field(
        default=None, description="URL to the full license text, if available."
    )
    citation: str = Field(description="Recommended citation string.")
    citation_doi: Optional[str] = Field(
        default=None, description="DOI for the citation, if available."
    )


class LicensesFile(BaseModel):
    """Top-level structure of the licenses.json file.

    Provides a standalone index of licensing and citation information for
    each source library, keyed by the same source identifiers used in the
    catalog and Parquet files (e.g. ``usgs_splib07``, ``ecostress``).
    """

    openspeclib_version: str = Field(description="Version of the OpenSpecLib schema.")
    generated_at: _dt.datetime = Field(description="Timestamp when the file was generated.")
    notice: str = Field(
        description=(
            "Important notice that licensing terms differ between source libraries "
            "and users must check the terms for each source they use."
        ),
    )
    sources: dict[str, LicenseEntry] = Field(
        description="Licensing and citation info keyed by source library identifier."
    )
