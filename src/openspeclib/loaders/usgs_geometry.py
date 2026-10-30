"""Measurement geometry of USGS Spectral Library Version 7 spectra.

The geometry of a splib07a spectrum follows from its spectrometer and, for the
Beckman and ASD instruments that were used in more than one set-up, from the
spectrum's own sample description in the archive's ``HTMLmetadata/`` folder.
Every decision is made by a named rule; the rule that fired is recorded in the
record's ``extra["geometry_rule"]`` so each value can be traced to its evidence.

Evidence: Kokaly et al. (2017), USGS Data Series 1035, shipped in the archive as
``usgs_splib07/DS1035.pdf`` (page numbers are the report's printed pages):

* p. 4, Beckman: "Measurements of powdered mineral samples were made using a
  special attachment to the integrating sphere to allow particulate materials
  to be measured in a horizontal position, typically in a 31.7-millimeter (mm)
  diameter sample cup ... these Beckman measurements are described as biconical
  reflectance factors (BCRF)". "Beckman measurements of rock samples were placed
  directly over the sample port of the integrating sphere and are
  bihemispherical reflectance (BHR) measurements".
* p. 4, ASD in the laboratory: "used a directional light source and fiber-optic
  probe to collect light ... Measurements of this type are described as BCRF".
* p. 7, ASD in the field: "Field measurements of this type have been described
  traditionally as BRDF or BRF and, more recently, as hemispherical-conical
  reflectance factors (HCRF)".
* p. 7, Nicolet: "Nicolet spectra were not measured with an integrating sphere.
  These measurements are described as BCRF".
* p. 7, AVIRIS: "these remotely sensed data have been traditionally referred to
  as BRF or BRDF and, more recently, as HCRF".

A spectrum whose set-up neither DS1035 nor its own description establishes
stays ``unknown`` (spec section 4.2).
"""

import html
import re
from dataclasses import dataclass
from pathlib import Path

from openspeclib.models import Geometry, MaterialCategory


@dataclass(frozen=True)
class GeometryDecision:
    """A geometry and the name of the rule that produced it.

    Attributes:
        geometry: The measurement geometry.
        rule: Identifier of the rule (recorded in ``extra["geometry_rule"]``).
        instrument_type: Instrument category overriding the one implied by the
            spectrometer code, or ``None`` to keep it.
    """

    geometry: Geometry
    rule: str
    instrument_type: str | None = None


# Free-text sections of an HTMLmetadata sample description. The rest of the
# page is a template (citation, sample location, purity codes) whose wording,
# e.g. "USGS Denver Spectroscopy Lab", must not trigger the rules below.
_DESCRIPTION_SECTIONS = (
    "SAMPLE_DESCRIPTION",
    "SPECTROSCOPIC_DISCUSSION",
    "MICROSCOPIC_EXAMINATION",
    "COMPOSITION_DISCUSSION",
)


def html_description(page: str) -> str:
    """Extract the free-text sample description from an HTMLmetadata page.

    Args:
        page: Raw HTML of a ``HTMLmetadata/<title>.html`` page.

    Returns:
        The text of the description sections, tags removed and whitespace
        collapsed; empty if the page has none.
    """
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    parts = []
    for name in _DESCRIPTION_SECTIONS:
        match = re.search(rf"{name}:(.*?)END_{name}", text, re.S)
        if match:
            parts.append(match.group(1))
    return " ".join(" ".join(parts).split())


# Per-spectrum decisions the patterns below cannot make, with the evidence.
GEOMETRY_EXCEPTIONS: dict[str, GeometryDecision] = {
    # Coded BECK, but the description says it is an average of field spectra:
    # "an average of 174 spectra collected of the Stonewall Playa surface ...
    # while holding the spectrometer fiber optic about 1 meter above the
    # surface" (DS1035 p. 7: field spectra are HCRF).
    "splib07a_Stonewall_Playa_CU93-52A_a11_BECKa_AREF": GeometryDecision(
        Geometry.HEMISPHERICAL_CONICAL, "exception_field_average", "field_spectrometer"
    ),
    # "spectra of its powder" and of "a crystal" are both discussed; which one
    # this spectrum is, is not stated.
    "splib07a_Fluorapatite_WS416_BECKa_AREF": GeometryDecision(
        Geometry.UNKNOWN, "exception_crystal_or_powder"
    ),
    # Not a USGS measurement: "The spectrum shown is the original data
    # interpolated to our wavelengths and water first surface reflectance added."
    "splib07a_Seawater_Coast_Chl_SW1_BECKa_AREF": GeometryDecision(
        Geometry.UNKNOWN, "exception_external_data"
    ),
    "splib07a_Seawater_Open_Ocean_SW2_lwch_BECKa_AREF": GeometryDecision(
        Geometry.UNKNOWN, "exception_external_data"
    ),
}

# Melting-snow sequence: "This sample of fresh Colorado snow was collected in an
# ice chest an[d] brought into the laboratory where [it] slowly melted";
# laboratory ASD measurements are BCRF (DS1035 p. 4). The ``+0.5_veg`` /
# ``+0.25_veg`` spectra are computed mixtures (``COMPUTED_SPECTRUM``).
_MELTING_SNOW_LAB = re.compile(r"^splib07a_Melting_snow_mSnw\d+a?(?:_\(slush\))?_ASD")

# Computed spectra: DS1035 p. 3, the library "contains physically constructed
# as well as mathematically computed mixtures". Only descriptions that say the
# spectrum was calculated are matched: "Calculated areal (linear) mixture",
# "areal mixture (linear) calculated using spectra of pure endmembers",
# "calculated linear mixture", "calculated from pure endmember spectra",
# "mathematical constructs simulating areal mixtures", "these computed spectra"
# (melting snow plus vegetation), "a series of shifted and ratioed spectra
# computed" (lawn grass GDS91b), "Computed areal mixture" (the AMX33-35
# playa/grass and cheatgrass/montmorillonite mixtures). A computed spectrum is
# not a measurement, so it gets no geometry (and the ``simulated`` processing
# flag in ``usgs.py``).
COMPUTED_SPECTRUM = re.compile(
    r"\bcalculated (?:areal|linear)\b"
    r"|\bmixture \(linear\) calculated\b"
    r"|\bcalculated from pure endmember spectra\b"
    r"|\bmathematical constructs?\b"
    r"|\bthese computed spectra\b"
    r"|\bshifted and ratioed spectra computed\b"
    r"|\bcomputed areal mixture\b",
    re.I,
)

# A spectrum measured with another instrument and convolved to the channels of
# the one its filename names: "Thirty spectra of the mat were measured in the
# field using a field spectrometer. These spectra were averaged and convolved to
# the wavelength and bandpass characteristics of the Airborne Visible and
# InfraRed Imaging Spectrometer (AVIRIS)" (Bacterial_mat_YNP-B1_AVIRISb, the only
# splib07a description that says so). Such a spectrum is ``resampled``, and its
# set-up is that of the measuring instrument, not of AVIRIS.
CONVOLVED_SPECTRUM = re.compile(r"\bconvolved to\b|\bresampled to\b", re.I)
_FIELD_SPECTROMETER = re.compile(r"\bfield spectro(?:meter|radiometer)\b", re.I)

# Grain-size token in a title (``165um``, ``74-250um``, ``lt30um``): a powder.
_GRAIN_SIZE_TOKEN = re.compile(r"^(?:lt|gt)?\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?um$", re.I)

# Hunt & Salisbury size fraction (``HS188.3B``). Their HTMLmetadata state the
# convention: "Grain size fractions are indicated by the extension after the
# sample number: .1B = .2B = .3B = 74-250 µm .4B = 250-1200 µm".
_HS_SIZE_FRACTION = re.compile(r"^HS\d+\.[1-4]B$")

# The description says the measured material is particulate: sieved, ground,
# crushed, a powder, or examined as grains with a measured grain-size
# distribution (optical examination of the powder by G. Swayze).
_POWDER_DESCRIPTION = re.compile(
    r"\bsiev(?:e|ed|ing)\b"
    r"|\bhand[- ]ground\b"
    r"|\bground (?:in an? (?:\w+ )?(?:mortar|mill)|to (?:a|pass|less|<|\d)|and sieved|from)\b"
    r"|\bpowder(?:ed)?\b"
    r"|\bcrushed sample\b|\bsample was crushed\b"
    r"|\b(?:avg\.?|average) grain size"
    r"|\bgrain size distribution",
    re.I,
)

# The description says a solid surface (rock face, crust, coating) was measured,
# i.e. the sample sat over the sphere's port: BHR per DS1035 p. 4.
_SURFACE_DESCRIPTION = re.compile(
    r"\((?:fresh|weathered)(?: interior)? surface\)"
    r"|surface measured spectrally"
    r"|original surface was preserved and spectrally measured"
    r"|spectral measurement is principally of the \w+ coating",
    re.I,
)

# ASD with its own light: contact probe or a lamp ("The contact probe
# attachement (an artificial light source ...)", "measured in the laboratory
# under a tungsten lamp"). Lamp-and-probe set-ups are BCRF (DS1035 p. 4).
_ASD_ARTIFICIAL_LIGHT = re.compile(
    r"contact probe|artificial light source|tungsten lamp|halogen lamp", re.I
)

# ASD measured in the laboratory ("measured in the laboratory", "spectral
# measurements were made in the laboratory"): BCRF (DS1035 p. 4).
_ASD_LABORATORY = re.compile(
    r"\bmeasured in the laboratory\b|measurements were made in the laboratory", re.I
)

# ASD field measurement under natural light: an explicit field-measurement
# phrase AND an outdoor viewing/illumination detail. "Field" alone (a "field
# description", a sample "collected from [a] large field", or contact-probe
# measurements made in the field) does not qualify.
_ASD_FIELD = re.compile(
    r"\bfield measurements?\b|\bfield spectra\b|measured in the field|collected in the field",
    re.I,
)
_ASD_OUTDOOR_VIEW = re.compile(
    r"bare fib(?:er|re)"
    r"|fiber optic\b[^.]{0,80}\b(?:held|pointed)"
    r"|\b(?:held|holding)\b[^.]{0,80}\babove\b"
    r"|\bnadir\b"
    r"|\blocal time\b"
    r"|\bsun(?:light)?\b|\bsolar\b|\bsky\b|\bcloud",
    re.I,
)

# Chapters whose ASD spectra are all laboratory measurements. Checked against
# every HTMLmetadata description of their ASD spectra: none describes a field
# measurement (sample-collection minerals; reagent organic compounds).
_ASD_LABORATORY_CATEGORIES = frozenset(
    {MaterialCategory.MINERAL, MaterialCategory.ORGANIC_COMPOUND}
)


def usgs_geometry(
    filename: str, category: MaterialCategory, description: str | None
) -> GeometryDecision:
    """Decide the measurement geometry of a splib07a spectrum.

    Args:
        filename: Spectrum filename (``splib07a_<...>_<Spectrometer>_<Type>.txt``).
        category: Material category of the spectrum's chapter.
        description: Free-text sample description from its HTMLmetadata page
            (see :func:`html_description`), or ``None`` if not available.

    Returns:
        The geometry with the rule that produced it.
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    data_type = parts[-1].upper()
    code = parts[-2].upper() if len(parts) >= 3 else ""
    text = description or ""

    if stem in GEOMETRY_EXCEPTIONS:
        return GEOMETRY_EXCEPTIONS[stem]
    if data_type == "TRAN":
        return GeometryDecision(Geometry.UNKNOWN, "transmittance_not_documented")
    if COMPUTED_SPECTRUM.search(text):
        return GeometryDecision(Geometry.UNKNOWN, "computed_spectrum")
    if CONVOLVED_SPECTRUM.search(text) and _FIELD_SPECTROMETER.search(text):
        return _convolved_field_geometry(text)
    if code.startswith("NIC4"):
        return GeometryDecision(Geometry.BICONICAL, "nicolet_bcrf")
    if code.startswith("AVIRIS"):
        return GeometryDecision(Geometry.HEMISPHERICAL_CONICAL, "aviris_hcrf")
    if code.startswith("ASD"):
        return _asd_geometry(stem, category, text)
    if code.startswith("BECK"):
        return _beckman_geometry(parts, category, text)
    return GeometryDecision(Geometry.UNKNOWN, "spectrometer_not_documented")


def _asd_geometry(stem: str, category: MaterialCategory, text: str) -> GeometryDecision:
    """Apply the ASD rules (laboratory BCRF vs field HCRF).

    Args:
        stem: Spectrum filename stem.
        category: Material category of the spectrum's chapter.
        text: Free-text sample description.

    Returns:
        The geometry with the rule that produced it.
    """
    if _ASD_ARTIFICIAL_LIGHT.search(text):
        return GeometryDecision(Geometry.BICONICAL, "asd_artificial_light")
    if _ASD_LABORATORY.search(text) or _MELTING_SNOW_LAB.match(stem):
        return GeometryDecision(Geometry.BICONICAL, "asd_laboratory")
    if _ASD_FIELD.search(text) and _ASD_OUTDOOR_VIEW.search(text):
        return GeometryDecision(Geometry.HEMISPHERICAL_CONICAL, "asd_field_natural_light")
    if category in _ASD_LABORATORY_CATEGORIES:
        return GeometryDecision(Geometry.BICONICAL, "asd_laboratory_chapter")
    # A sieved size fraction or a powder is a laboratory preparation, so its
    # ASD spectrum is a laboratory (lamp and fibre) measurement.
    if _is_powder(stem.split("_")[1:-2], text):
        return GeometryDecision(Geometry.BICONICAL, "asd_laboratory_powder")
    return GeometryDecision(Geometry.UNKNOWN, "asd_setup_not_stated")


def _convolved_field_geometry(text: str) -> GeometryDecision:
    """Decide the geometry of a field-spectrometer spectrum convolved to another instrument.

    The set-up is the field spectrometer's, so the ASD field rule applies: a
    field measurement with an outdoor viewing detail is HCRF (DS1035 p. 7);
    otherwise the set-up is not stated.

    Args:
        text: Free-text sample description.

    Returns:
        The geometry with the rule that produced it; the instrument type is
        always ``field_spectrometer``.
    """
    if _ASD_FIELD.search(text) and _ASD_OUTDOOR_VIEW.search(text):
        return GeometryDecision(
            Geometry.HEMISPHERICAL_CONICAL,
            "field_spectrometer_convolved_natural_light",
            "field_spectrometer",
        )
    return GeometryDecision(
        Geometry.UNKNOWN, "field_spectrometer_convolved_setup_not_stated", "field_spectrometer"
    )


def _is_powder(sample_tokens: list[str], text: str) -> bool:
    """Tell whether a spectrum's sample is a powder or sieved size fraction.

    Args:
        sample_tokens: Title tokens between the material name and spectrometer.
        text: Free-text sample description.

    Returns:
        ``True`` if the title carries a grain size or a Hunt & Salisbury size
        fraction, or the description says the sample is particulate.
    """
    return (
        any(_GRAIN_SIZE_TOKEN.match(t) or _HS_SIZE_FRACTION.match(t) for t in sample_tokens)
        or _POWDER_DESCRIPTION.search(text) is not None
    )


def _beckman_geometry(parts: list[str], category: MaterialCategory, text: str) -> GeometryDecision:
    """Apply the Beckman rules (powder in a cup BCRF vs surface over the port BHR).

    Args:
        parts: Underscore-separated parts of the filename stem.
        category: Material category of the spectrum's chapter.
        text: Free-text sample description.

    Returns:
        The geometry with the rule that produced it.
    """
    sample_tokens = parts[1:-2]
    if any(_GRAIN_SIZE_TOKEN.match(t) for t in sample_tokens):
        return GeometryDecision(Geometry.BICONICAL, "beckman_powder_title_grain_size")
    if any(_HS_SIZE_FRACTION.match(t) for t in sample_tokens):
        return GeometryDecision(Geometry.BICONICAL, "beckman_powder_hs_size_fraction")
    if _SURFACE_DESCRIPTION.search(text):
        return GeometryDecision(Geometry.BIHEMISPHERICAL, "beckman_surface_over_port")
    # Coatings were deposited on substrates (e.g. "an eye-dropper was used to
    # deposit a thin coating on a microscope glass thin section"), so powder
    # wording there does not mean a sample cup.
    if category is not MaterialCategory.MIXTURE and _POWDER_DESCRIPTION.search(text):
        return GeometryDecision(Geometry.BICONICAL, "beckman_powder_description")
    return GeometryDecision(Geometry.UNKNOWN, "beckman_setup_not_stated")
