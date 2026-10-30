"""Loader for the USGS Spectral Library Version 7 (Speclib 07).

The USGS library organizes spectra as ASCII text files within chapter
directories (by material type). Each spectrum file contains a single
column of reflectance values. Wavelength and bandpass axes are stored
in separate shared files per spectrometer.

The raw archive is mirrored on Hugging Face Datasets as a bit-identical
copy of the upstream USGS ScienceBase release; see ``USGS_HF_REPO`` below.

Reference: https://doi.org/10.5066/F7RR1WDJ
"""

import html
import logging
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from huggingface_hub import hf_hub_download
from tqdm import tqdm

from openspeclib.loaders.base import (
    BaseLoader,
    mask_invalid_values,
    sort_by_wavelength,
    spectrum_id,
)
from openspeclib.loaders.usgs_geometry import (
    COMPUTED_SPECTRUM,
    CONVOLVED_SPECTRUM,
    html_description,
    usgs_geometry,
)
from openspeclib.models import (
    CollectionInfo,
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

# Raw archive is mirrored on Hugging Face Datasets. The file is the
# upstream ScienceBase ZIP, uploaded unmodified so its SHA-256 matches
# the original publication at https://doi.org/10.5066/F7RR1WDJ.
USGS_HF_REPO = "null-jones/usgs_splib07"
USGS_HF_FILENAME = "usgs_splib07.zip"
USGS_HF_REVISION = "main"

COLLECTION_ID = "usgs_splib07"

USGS_LICENSE = "LicenseRef-USGS-PublicDomain"
_USGS_COPYRIGHT_URL = (
    "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits"
)

USGS_COLLECTION = CollectionInfo(
    id=COLLECTION_ID,
    title="USGS Spectral Library Version 7",
    description=(
        "Laboratory and field spectra of minerals, rocks, soils, vegetation, man-made "
        "materials and other substances measured at the USGS Spectroscopy Laboratory, "
        "Denver, spanning about 0.2 to 216 micrometres."
    ),
    version="7a",
    url="https://doi.org/10.5066/F7RR1WDJ",
    doi="10.5066/F7RR1WDJ",
    publisher="U.S. Geological Survey",
    license=USGS_LICENSE,
    license_refs={
        USGS_LICENSE: LicenseRef(
            name="U.S. Government work, public domain",
            text=(
                "A work of the United States Government, not subject to copyright "
                "protection in the United States. The USGS metadata (useconst) asks users "
                "who modify the data to describe their modifications, and appreciates "
                "acknowledgment of the U.S. Geological Survey."
            ),
            url=_USGS_COPYRIGHT_URL,
        )
    },
    citation=(
        "Kokaly, R.F., Clark, R.N., Swayze, G.A., Livo, K.E., Hoefen, T.M., "
        "Pearson, N.C., Wise, R.A., Benzel, W.M., Lowers, H.A., Driscoll, R.L., "
        "and Klein, A.J., 2017, USGS Spectral Library Version 7: "
        "U.S. Geological Survey Data Series 1035, 61 p., https://doi.org/10.3133/ds1035."
    ),
    inclusion=Inclusion.SUBSET,
    inclusion_notes=(
        "The measured spectra of splib07a only. The oversampled splib07b copies, the "
        "spectra convolved or resampled to other instruments (the cv* and rs* "
        "directories) and the per-band error-bar files are derived from the same "
        "measurements and are not included."
    ),
    keywords=["minerals", "rocks", "soils", "vegetation", "laboratory spectroscopy"],
)

# --- Measurement semantics (geometry, reference, instrument type) ----------
#
# Geometry is decided per spectrum by ``usgs_geometry.usgs_geometry`` from the
# spectrometer and the spectrum's HTMLmetadata description, with the evidence
# from Kokaly et al. (2017), USGS Data Series 1035 (``DS1035.pdf`` in the
# archive) documented there. The rule that fired is kept in
# ``extra["geometry_rule"]``.

# Spectrometer-code prefix -> instrument category (DS1035 table 1: Beckman 5270
# laboratory spectrophotometer; ASD field-portable spectrometers; Nicolet FTIR;
# AVIRIS airborne imaging spectrometer).
_INSTRUMENT_TYPES: tuple[tuple[str, str], ...] = (
    ("ASD", "field_spectrometer"),
    ("AVIRIS", "imaging_spectrometer"),
    ("BECK", "laboratory_spectrometer"),
    ("NIC4", "ftir"),
)

# Trailing filename token -> reference standard. DS1035 p. 10: "Whenever
# possible, reflectance data have been converted to absolute reflectance (AREF).
# However, Nicolet spectra were measured with a stainless steel mirror as the
# reference standard and are relative reflectance (RREF)." RTGC (AVIRIS,
# radiative-transfer ground-calibrated) and TRAN have no reference standard
# stated and are left unset.
USGS_DATA_TYPE_REFERENCE: dict[str, str] = {
    "AREF": "absolute",
    "RREF": "stainless_steel_mirror",
}

# Spectrometer-code prefixes whose reflectance spectra were scaled to another
# instrument's level. DS1035 p. 12: "we have scaled Nicolet spectra to match the
# reflectance level measured with the Beckman and (or) ASD spectrometers at a
# point in the 1.8-2.5 micron region".
_SCALED_TO_REFERENCE_LEVEL = ("NIC4",)

# Spectra whose description says the values are another source's data
# resampled to USGS channels: "The spectrum shown is the original data
# interpolated to our wavelengths and water first surface reflectance added."
_INTERPOLATED = re.compile(r"original data interpolated to our wavelengths", re.I)

# The Liquids chapter holds water, ice and snow; ``other`` hides that.
# Material-name prefix -> category.
_LIQUIDS_CATEGORIES: tuple[tuple[str, MaterialCategory], ...] = (
    ("Seawater", MaterialCategory.WATER),
    ("Water+", MaterialCategory.WATER),
    ("Red_Coated_Algea_Water", MaterialCategory.WATER),
    ("H2O-Ice", MaterialCategory.SNOW_ICE),
    ("Melting_snow", MaterialCategory.SNOW_ICE),
)

# A computed spectrum whose description calls it a mixture ("Calculated areal
# (linear) mixture", "mathematical constructs simulating areal mixtures",
# "Constructed mixtures sequence") is recategorised as ``mixture``. Computed
# spectra that are not mixtures (lawn grass GDS91b "shifted and ratioed
# spectra") keep their category.
_MIXTURE_WORD = re.compile(r"\bmixtures?\b", re.I)

# A measured laboratory mixture is a mixture whatever chapter it is filed in
# (rule ``laboratory_mixture``, with the chapter's category kept in
# ``extra["original_category"]``): an ``AMX<n>`` title token (the USGS "areal
# mixture" series, e.g. ``Antigorite+.33DryGrass_AMX25`` in chapter V) or a
# description of a sample prepared by mixing materials ("Samples were prepared
# mixtures of Scotts expanded vermiculite ... plus fibrous Libby amphibole",
# "Sample was prepared by adding organic compound to a fine ground Basalt",
# "1 ml of benzene solvent mixed with clay", "Clay saturated with TCE
# solvent"). Chapter S has its own, wider rule (``chapter_s_category``).
_AMX_TOKEN = re.compile(r"^AMX\d+$")
_PREPARED_MIXTURE = re.compile(
    r"\bprepared mixtures?\b|\bprepared by adding\b|\bmixed with clay\b|\bclay saturated with\b",
    re.I,
)
# The 0% member of a mixture series is the unmixed reference, not a mixture:
# ``Vermiculite+0wt%amph._GDS327`` is the pure GDS327 vermiculite of the
# "vermiculite plus Libby amphibole" series (``+1wt%``, ``+5wt%``). A title
# whose every added component (each part after a ``+``) is 0% (``+0wt%``,
# ``+0%``, ``+0.0vol%``) keeps its chapter's category (rule
# ``zero_fraction_reference``).
_ZERO_FRACTION = re.compile(r"^0+(?:\.0+)?(?:wt|vol)?%", re.I)

# Chapter S ("Soils and Mixtures") holds soils, rocks, natural mineral
# assemblages and laboratory mixtures; every one of its HTMLmetadata pages but
# one has ``DOCUMENTATION_FORMAT: MIXTURE``. Its spectra are classified from
# the title and description by the named rules below (recorded in
# ``extra["category_rule"]``); a spectrum no rule places stays ``mixture``.
#
# * Laboratory mixtures: an ``AMX<n>`` title token (USGS "areal mixture"
#   series) or a description of a sample made from pure phases ("Samples
#   constructed from ground and sieved pure minerals", "physical mixture of
#   ground calcite and kaolinite prepared in the laboratory", "2 wt% + 98 wt%").
_CHAPTER_S_CONSTRUCTED = re.compile(
    r"\bsamples? constructed from\b|\bprepared in the laboratory\b"
    r"|\bpure (?:phases|minerals)\b|\d ?wt ?%",
    re.I,
)
# * Coatings, precipitates, crusts and efflorescences on a substrate ("a
#   Fe-mineral precipitate on a rock", "a jarosite coating on a hydrothermally
#   altered rhyolite", "Goethite coated Chlorite Phyllite") are not the rock
#   underneath: ``mixture``.
_CHAPTER_S_COATING_WORD = re.compile(
    r"\bcoat(?:ing|ed|ng)?s?\b|\bprecipitates?\b|\bcrusts?\b|\befflor\w*", re.I
)
_CHAPTER_S_COATING_ON = re.compile(
    r"\bcoatings? (?:on|of)\b|\bsurface coating\b|\bcoating an?\b", re.I
)
# * Rocks: a rock name in the title (``Basalt_fresh``, ``Chlorite_Phyllite``,
#   ``Hematitic_Alt._Tuff``, ``Magnetite_skarn``), or a description whose
#   opening sentences say what rock the sample is or was taken from ("Basalt -
#   gray vesicular flow rock", "Talc conglomerate from waste rock pile", "The
#   sample was collected from the altered lower Cambrian Harkless Phyllite",
#   "Na-Alunite + Dickite in an altered rock"). A mineral sample whose
#   description only says where the mineral occurs ("This Zeolite occurs in
#   cavities in basalt's and related rocks") or what its deposit altered is not
#   a rock.
_ROCK_WORDS = (
    r"(?:rock|basalt|limestone|phyllite|tuff|rhyolite|conglomerate|monzonite|skarn|hornfels)"
)
_CHAPTER_S_ROCK_TITLE = re.compile(
    r"^(?:basalt|limestone|phyllite|phyl|tuff|rhyolite|skarn|hornfels)$", re.I
)
_CHAPTER_S_ROCK_STATEMENT = re.compile(
    r"\b(?:sample (?:is|was|may have been part of|of)|collected from|composed of|consists of"
    r"|originally)\b[^.]{0,110}?\b" + _ROCK_WORDS + r"\b"
    r"|^(?:field description: |sample is an? )?[\w\s(),+-]{0,60}?\b"
    r"(?:rock|conglomerate|phyllite|skarn)\b(?! pile)"
    r"|^weathered rock\b",
    re.I,
)
# * Soils: ``Sand`` or ``Playa`` in the title (beach and dune sands, the
#   Stonewall Playa surface), or a description of a soil or playa surface
#   ("collected from the surface of the dry Stonewall Mud playa").
_CHAPTER_S_SOIL_TITLE = re.compile(r"^(?:sand|playa)$", re.I)
_CHAPTER_S_SOIL_STATEMENT = re.compile(
    r"\bsurface of the (?:dry )?[\w ]{0,30}?\bplaya\b|\bsandy [\w -]{0,30}?\bsoil\b", re.I
)
# * Single minerals: a title naming one mineral (its leading word, e.g.
#   ``Pyrite`` in ``Pyrite_LV95-6A_Weath_on_Tail``, ``Alunite`` in
#   ``Alunite0.35K+.65Na``) whose description says the sample is that mineral
#   ("Weathered Pyrite from the surface of the Strayhorse Gulch mine tailings")
#   or phases of that one mineral group ("This sample contains two alunite
#   phases": K- and Na-alunite, one group). Format v1 ``mineral`` is "a single
#   mineral species or mineral group". Only spectra no other rule places are
#   tested; a mineral named with another (``Covellite-Pyrite``,
#   ``ClinozoisiteEpidote``) stays ``mixture``.
_CHAPTER_S_MINERAL_WORD = re.compile(r"^[A-Z][a-z]{3,}")
# Number of opening description sentences the rock rule reads.
_OPENING_SENTENCES = 3


def _single_mineral(title_tokens: list[str], sentences: list[str]) -> bool:
    """Tell whether a chapter S description says the sample is one mineral.

    Args:
        title_tokens: The title split on ``_`` (see :func:`chapter_s_category`).
        sentences: The description's sentences.

    Returns:
        ``True`` if the first sentence starts with the title's leading mineral
        word (optionally "Weathered"), or the description says the sample
        "contains two <mineral> phases".
    """
    match = _CHAPTER_S_MINERAL_WORD.match(title_tokens[0]) if title_tokens else None
    if match is None or not sentences:
        return False
    # Nothing else in the first token may name a material: element symbols and
    # proportions (``Alunite0.35K+.65Na``) are fine, another mineral
    # (``ClinozoisiteEpidote``, ``Chabazite+Thom+Wair``) is not.
    if re.search(r"[A-Za-z]{3,}", title_tokens[0][match.end() :]):
        return False
    mineral = re.escape(match.group(0))
    if re.match(rf"(?:weathered\s+)?{mineral}\b", sentences[0], re.I):
        return True
    return any(re.search(rf"\bcontains two {mineral} phases\b", s, re.I) for s in sentences)


def _sentences(text: str) -> list[str]:
    """Split a description into sentences (a full stop followed by a capital).

    Args:
        text: Free-text description.

    Returns:
        The sentences, stripped.
    """
    return [s.strip() for s in re.split(r"(?<=\.)\s+(?=[A-Z])", text) if s.strip()]


def chapter_s_category(title_tokens: list[str], description: str) -> tuple[MaterialCategory, str]:
    """Classify a chapter S ("Soils and Mixtures") spectrum.

    Args:
        title_tokens: The title split on ``_`` (material and sample tokens,
            without the library prefix, spectrometer and data type).
        description: Free-text sample description (may be empty).

    Returns:
        The category and the name of the rule that decided it.
    """
    words = [w for t in title_tokens for w in re.split(r"[-+.]", t) if w]
    sentences = _sentences(description)
    first = sentences[0] if sentences else ""
    if any(_AMX_TOKEN.match(t) for t in title_tokens) or _CHAPTER_S_CONSTRUCTED.search(description):
        return MaterialCategory.MIXTURE, "chapter_s_laboratory_mixture"
    if (
        any(_CHAPTER_S_COATING_WORD.fullmatch(w) for w in words)
        or _CHAPTER_S_COATING_WORD.search(first)
        or _CHAPTER_S_COATING_ON.search(description)
    ):
        return MaterialCategory.MIXTURE, "chapter_s_coating_or_precipitate"
    if any(_CHAPTER_S_ROCK_TITLE.match(w) for w in words):
        return MaterialCategory.ROCK, "chapter_s_rock_in_title"
    if any(_CHAPTER_S_SOIL_TITLE.match(w) for w in words) or _CHAPTER_S_SOIL_STATEMENT.search(
        description
    ):
        return MaterialCategory.SOIL, "chapter_s_soil"
    if any(_CHAPTER_S_ROCK_STATEMENT.search(s) for s in sentences[:_OPENING_SENTENCES]):
        return MaterialCategory.ROCK, "chapter_s_rock_in_description"
    if _single_mineral(title_tokens, sentences):
        return MaterialCategory.MINERAL, "chapter_s_single_mineral"
    return MaterialCategory.MIXTURE, "chapter_s_mixture"


def _mixture_rule(filepath: Path, text: str, computed: bool) -> str | None:
    """Name the rule that makes a spectrum outside chapter S rules a mixture.

    Args:
        filepath: Path to the spectrum file.
        text: Free-text sample description.
        computed: Whether the spectrum is computed (``COMPUTED_SPECTRUM``).

    Returns:
        ``"computed_mixture"`` for a computed mixture (any chapter),
        ``"laboratory_mixture"`` for a measured laboratory mixture outside
        chapter S (see ``_AMX_TOKEN``, ``_PREPARED_MIXTURE``), else ``None``.
    """
    if computed and _MIXTURE_WORD.search(text):
        return "computed_mixture"
    if _is_chapter_s(filepath) or _zero_fraction_reference(filepath):
        return None
    tokens = filepath.stem.split("_")[1:-2]
    if any(_AMX_TOKEN.match(t) for t in tokens) or _PREPARED_MIXTURE.search(text):
        return "laboratory_mixture"
    return None


def _zero_fraction_reference(filepath: Path) -> bool:
    """Tell whether a spectrum is the 0% member of a mixture series.

    Args:
        filepath: Path to the spectrum file.

    Returns:
        ``True`` when the title adds at least one component (``+``) and every
        added component is 0% (see ``_ZERO_FRACTION``), e.g.
        ``Vermiculite+0wt%amph._GDS327``.
    """
    added = "_".join(filepath.stem.split("_")[1:-2]).split("+")[1:]
    return bool(added) and all(_ZERO_FRACTION.match(part) for part in added)


def _rock_reference_sample(filepath: Path, category: MaterialCategory) -> bool:
    """Tell whether a spectrum outside chapters R and S is an unmixed rock.

    Args:
        filepath: Path to the spectrum file.
        category: The category its chapter gives.

    Returns:
        ``True`` when the title's leading word is a rock name
        (``_CHAPTER_S_ROCK_TITLE``, e.g. ``Basalt_BHVO-2F_100_``) and the title
        names no second material (no ``+``).
    """
    if category in (MaterialCategory.ROCK, MaterialCategory.MIXTURE) or _is_chapter_s(filepath):
        return False
    tokens = filepath.stem.split("_")[1:-2]
    return (
        bool(tokens)
        and _CHAPTER_S_ROCK_TITLE.match(tokens[0]) is not None
        and "+" not in "_".join(tokens)
    )


# USGS "deleted channel" marker (written as ``-1.2300000e+034`` in the
# ASCII files). Converted to NaN at ingest; see ``loaders.base``.
BAD_VALUE = -1.23e34

# Trailing filename token -> measured quantity. ``AREF`` (absolute
# reflectance), ``RREF`` (relative reflectance) and ``RTGC`` (ground-calibrated
# reflectance) are reflectance; ``TRAN`` is transmittance (e.g. the Mylar
# reference-standard spectrum). Unknown tokens default to reflectance.
USGS_DATA_TYPE_QUANTITY: dict[str, Quantity] = {
    "AREF": Quantity.REFLECTANCE,
    "RREF": Quantity.REFLECTANCE,
    "RTGC": Quantity.REFLECTANCE,
    "TRAN": Quantity.TRANSMITTANCE,
}

USGS_CHAPTER_MAP: dict[str, MaterialCategory] = {
    "chapterlliquids": MaterialCategory.OTHER,
    "chaptermminerals": MaterialCategory.MINERAL,
    "chapterrrocks": MaterialCategory.ROCK,
    "chapterssoilsandmixtures": MaterialCategory.SOIL,
    "chapterssoils": MaterialCategory.SOIL,
    "chaptervvegetation": MaterialCategory.VEGETATION,
    "chapterwwater": MaterialCategory.WATER,
    "chapteriice": MaterialCategory.SNOW_ICE,
    "chapteraartificialmaterials": MaterialCategory.MAN_MADE,
    "chapteremeteorites": MaterialCategory.METEORITE,
    "chapterccoatings": MaterialCategory.MIXTURE,
    "chapteroorganiccompounds": MaterialCategory.ORGANIC_COMPOUND,
}


def _classify_from_path(filepath: Path) -> MaterialCategory:
    """Determine material category from the file's parent directory name.

    Args:
        filepath: Path to the spectrum file whose directory is inspected.

    Returns:
        The matching material category, or ``OTHER`` if not found.
    """
    for part in filepath.parts:
        key = re.sub(r"[\s\-_]+", "", part.strip().lower())
        for chapter_key, category in USGS_CHAPTER_MAP.items():
            if chapter_key in key:
                return category
    return MaterialCategory.OTHER


def _detect_spectrometer(filename: str) -> str:
    """Extract spectrometer abbreviation from the USGS filename convention.

    USGS splib07a/b filenames follow the pattern
    ``splib07a_<Material>[_<SampleID...>]_<Spectrometer>_<DataType>.txt``
    where ``<DataType>`` is the final segment (e.g. ``AREF``, ``RREF``,
    ``RTGC``, ``TRAN``) and ``<Spectrometer>`` is the segment immediately
    before it (e.g. ``BECKa``, ``ASDFRb``, ``NIC4a``, ``AVIRISb``).

    Args:
        filename: Spectrum filename (basename, not full path).

    Returns:
        Spectrometer code string, or ``"unknown"`` if not detected.
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 3:
        return parts[-2]
    return "unknown"


# Prefix -> wavelength-file family. All ASD sub-families (ASDFR/ASDHR/ASDNG)
# share one wavelength axis file named ``...Wavelengths_ASD_...``.
_WAVELENGTH_FAMILIES: tuple[tuple[str, str], ...] = (
    ("ASD", "asd"),
    ("AVIRIS", "aviris"),
    ("BECK", "beck"),
    ("NIC4", "nic4"),
)

# Prefix -> bandpass-file family. The ASD sub-families each have their own
# FWHM file (standard/high-res/next-gen) so they must be distinguished.
_BANDPASS_FAMILIES: tuple[tuple[str, str], ...] = (
    ("ASDFR", "asdfr"),
    ("ASDHR", "asdhr"),
    ("ASDNG", "asdng"),
    ("AVIRIS", "aviris"),
    ("BECK", "beck"),
    ("NIC4", "nic4"),
)


def _match_family(code: str, families: tuple[tuple[str, str], ...]) -> str | None:
    """Return the family token for ``code`` from a prefix mapping, or ``None``.

    Args:
        code: Spectrometer code (case-insensitive, e.g. ``"ASDFRa"``).
        families: Ordered ``(prefix, family_token)`` pairs. First prefix
            match wins, so more specific prefixes should come first.

    Returns:
        The matching family token, or ``None`` if no prefix matches.
    """
    upper = code.upper()
    for prefix, family in families:
        if upper.startswith(prefix):
            return family
    return None


# --- Material name, sample id and sample temperature -----------------------
#
# DS1035 ("SPECPR Data Files"): "The data record has a title containing a short
# sample name and a unique sample identifier number/code. At the end of each
# title are string identifiers with spectrometer and spectral purity codes, and
# a keyword indicating the measurement type." That 40-character SPECPR title is
# the first line of every splib07a ASCII file
# (``splib07a Record=10723: Topaz Glen_Cove_#6            BECKb AREF``): words
# inside the name or inside the sample id are joined with ``_``, and the parts
# are separated by spaces. The filename writes those spaces as ``_`` too, so the
# boundary between the name and the sample id is only in the record title.
# ``parse_sample_name`` reads it from there and refines it with the spectrum's
# HTMLmetadata page (``TITLE`` and ``SAMPLE_ID`` fields).

_RECORD_TITLE = re.compile(r"\bRecord=\d+:\s?(.*)$")
# A title word giving the measurement temperature in kelvin (``85K``, ``293K``,
# ``T~77K`` for liquid methane). It is a measurement condition, not part of the
# sample id, and is kept in ``extra["sample_temperature_k"]``.
_TEMPERATURE_WORD = re.compile(r"^(?:T~)?(\d+(?:\.\d+)?)K$")
# A temperature written onto a grain-size or frost descriptor with no space
# (``MedFrost85K``, ``CrsFrost85K``, ``MedFrst87K``, ``fr86K`` in ``fine
# fr86K``, ``<75um85K``; ``lt75um85K`` in a filename). The temperature goes to
# ``extra["sample_temperature_k"]`` and the descriptor stays in the sample id.
# Only these descriptors are split: a code ending in digits and ``K`` is not a
# temperature.
_GLUED_TEMPERATURE = re.compile(r"^(.*\D)(\d+(?:\.\d+)?)K$")
_TEMPERATURE_DESCRIPTOR = re.compile(
    r"^(?:(?:med|crs|coarse|fine)?(?:frost|frst|fr)|(?:<|>|lt|gt)?\d+(?:\.\d+)?um)$", re.I
)
# A heating treatment (``Basalt BHVO-2F Htd16hr 480K``: heated 16 hours at
# 480 K). The temperature after it is part of the treatment, not the
# measurement temperature, and stays in the sample id.
_HEATING_WORD = re.compile(r"^Htd", re.I)
# A lower-case word continuing a name (``oxide_#4820``, ``sediment``), not a
# code (``water40%...``).
_NAME_WORD = re.compile(r"^[a-z]+(?:_|$)")
# ``SAMPLE_ID: 12917 Stock No.``: a supplier stock number.
_STOCK_NUMBER = re.compile(r"^(\d+) stock no", re.I)
# A supplier catalogue number (``SA-267953``, ``SA-H2198``, ``JTB-9084``,
# ``FS-A996``): upper-case letters, a hyphen, an optional letter and at least
# three digits. Written onto the end of a name with ``_``
# (``Iron_Powder_SA-267953 <10 um``) it is the sample id, as in the sibling
# ``Nickel_Powder SA-268283 <1um``.
_CATALOGUE_NUMBER = re.compile(r"^[A-Z]{1,4}-[A-Z]?\d{3,}[A-Z0-9-]*$")


@dataclass(frozen=True)
class SampleMetadata:
    """Fields of a spectrum's HTMLmetadata page.

    Attributes:
        description: The free-text sample description (see
            :func:`~openspeclib.loaders.usgs_geometry.html_description`).
        title: The page's ``TITLE`` field without the trailing ``DESCRIPT``
            keyword, e.g. ``"Samarium_Oxide GDS36"``.
        sample_id: The page's ``SAMPLE_ID`` field, e.g. ``"GDS36"``,
            ``"HS184.1B, HS184.2B"`` or ``"2-Methylnaphthalene SA-442359"``.
    """

    description: str = ""
    title: str = ""
    sample_id: str = ""


@dataclass(frozen=True)
class SampleName:
    """A spectrum's material name, sample id and measurement temperature.

    Attributes:
        material_name: The material name, words separated by spaces and ``+``
            written as `` + ``.
        sample_id: The sample identifier and qualifiers, ``_``-joined as in
            the title (may be empty).
        temperature_k: The measurement temperature in kelvin, or ``None``.
    """

    material_name: str
    sample_id: str
    temperature_k: float | None = None


def _html_field(text: str, key: str) -> str:
    """Return one ``KEY: value`` field of a tag-stripped HTMLmetadata page.

    Args:
        text: Page text with tags removed and entities unescaped.
        key: Field name, e.g. ``"SAMPLE_ID"``.

    Returns:
        The value up to the next ``FIELD:`` keyword, whitespace collapsed;
        empty if the page has no such field.
    """
    match = re.search(rf"(?<![A-Z_]){key}:(.*?)(?=\s[A-Z][A-Z_]{{3,}}:|$)", text, re.S)
    return " ".join(match.group(1).split()) if match else ""


def sample_metadata(page: str) -> SampleMetadata:
    """Extract the fields the loader uses from an HTMLmetadata page.

    Args:
        page: Raw HTML of a ``HTMLmetadata/<title>.html`` page.

    Returns:
        The description, ``TITLE`` (without ``DESCRIPT``) and ``SAMPLE_ID``.
    """
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    title = _html_field(text, "TITLE")
    if title.endswith("DESCRIPT"):
        title = title[: -len("DESCRIPT")].strip()
    return SampleMetadata(
        description=html_description(page),
        title=title,
        sample_id=_html_field(text, "SAMPLE_ID"),
    )


def read_record_title(filepath: Path) -> str | None:
    """Read the SPECPR record title from the first line of a spectrum file.

    Args:
        filepath: Path to a splib07a ASCII spectrum file.

    Returns:
        The title (``"Topaz Glen_Cove_#6            BECKb AREF"``), or
        ``None`` if the file has no record header line.
    """
    with filepath.open(encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    match = _RECORD_TITLE.search(first.rstrip("\r\n"))
    return match.group(1).strip() if match else None


def _title_words(title: str | None, filename: str) -> list[str] | None:
    """Split a record title into its name and sample-id words.

    Args:
        title: The SPECPR record title, or ``None``.
        filename: The spectrum filename, whose last two tokens are the
            spectrometer code and data type the title ends with.

    Returns:
        The words before the spectrometer code, or ``None`` if there is no
        title or it does not end with the filename's spectrometer and data
        type.
    """
    if not title:
        return None
    words = title.split()
    tail = [t.lower() for t in Path(filename).stem.split("_")[-2:]]
    if len(words) < 3 or [w.lower() for w in words[-2:]] != tail:
        return None
    return words[:-2]


def _html_sample_ids(sample_id_field: str, name: str) -> list[str]:
    """List the sample identifiers an HTMLmetadata ``SAMPLE_ID`` field gives.

    The field is a single id (``GDS36``), a list (``HS184.1B, HS184.2B``), a
    name followed by an id (``2-Methylnaphthalene SA-442359``,
    ``BHVO-2F, 1-Decyne SA-244384``), a stock number (``12917 Stock No.``) or
    ``None assigned``. The last word of each comma-separated item that
    contains a digit is an id, unless it is a temperature or the start of
    the material name (``BHVO-2F`` in ``BHVO-2F+4%_1-Decyne``).

    Args:
        sample_id_field: The ``SAMPLE_ID`` field value.
        name: The material name as written in the title (``_``-joined).

    Returns:
        The identifiers, in order.
    """
    stock = _STOCK_NUMBER.match(sample_id_field)
    if stock:
        return [stock.group(1)]
    ids = []
    for item in sample_id_field.split(", "):
        words = item.split()
        if not words:
            continue
        last = words[-1]
        if (
            re.search(r"\d", last)
            and not _TEMPERATURE_WORD.match(last)
            and not name.startswith(last)
        ):
            ids.append(last)
    return ids


def _html_material_words(sample_id_field: str, ids: list[str]) -> set[str]:
    """List the words an HTMLmetadata ``SAMPLE_ID`` field gives as material.

    In ``BHVO-2F, Decane SA-30550`` the id is ``SA-30550`` and ``Decane``
    names a material of the sample (basalt BHVO-2F with decane). Words with a
    digit are left out: they are codes (``GDS23`` in ``GDS23 74-250um``,
    ``CM9`` in ``CM9 (API9)``), not material names.

    Args:
        sample_id_field: The ``SAMPLE_ID`` field value.
        ids: The identifiers it gives (see :func:`_html_sample_ids`).

    Returns:
        The field's words that are not identifiers and contain no digit.
    """
    if _STOCK_NUMBER.match(sample_id_field):
        return set()
    return {
        w
        for item in sample_id_field.split(", ")
        for w in item.split()
        if w not in ids and not re.search(r"\d", w)
    }


def _split_temperature(word: str) -> tuple[str, float] | None:
    """Split a measurement temperature off a title word.

    Args:
        word: One title word.

    Returns:
        ``(rest, kelvin)``: ``("", 85.0)`` for a temperature word (``85K``,
        ``T~77K``), ``("MedFrost", 85.0)`` for a temperature written onto a
        grain-size or frost descriptor (``MedFrost85K``, see
        ``_GLUED_TEMPERATURE``); ``None`` if the word gives no temperature.
    """
    match = _TEMPERATURE_WORD.match(word)
    if match:
        return "", float(match.group(1))
    match = _GLUED_TEMPERATURE.match(word)
    if match and _TEMPERATURE_DESCRIPTOR.match(match.group(1)):
        return match.group(1), float(match.group(2))
    return None


def _format_name(name: str) -> str:
    """Write a title name for display: ``_`` as a space, ``+`` as `` + ``.

    Args:
        name: Name as written in the title, e.g. ``"Kaol+Muscov_intimate"``.

    Returns:
        E.g. ``"Kaol + Muscov intimate"``.
    """
    return " ".join(name.replace("_", " ").replace("+", " + ").split())


def parse_sample_name(
    words: list[str], html_title: str = "", html_sample_id: str = ""
) -> SampleName:
    """Derive the material name, sample id and temperature of a spectrum.

    Rules, in order:

    1. The first title word is the material name and the remaining words the
       sample id (DS1035: "a short sample name and a unique sample identifier
       number/code"): ``Topaz Little_3_Mine_#41`` is "Topaz" with sample id
       ``Little_3_Mine_#41``.
    2. Where the HTMLmetadata ``TITLE`` joins the first title words into one
       name (title ``Samarium Oxide GDS36``, page ``Samarium_Oxide GDS36``),
       the joined name is used.
    3. In a title with no sample id word, a sample id the ``SAMPLE_ID`` field
       names, written onto the end of the name (``Vermillion_GDS779``,
       ``Brilliant(Hansa)yellowGDS785``), is moved to the sample id. In any
       title, a supplier catalogue number written onto the end of the name
       is moved too (``Iron_Powder_SA-267953 <10 um`` is "Iron Powder" with
       sample id ``SA-267953_<10_um``, like ``Nickel_Powder SA-268283
       <1um``). Where
       the ``SAMPLE_ID`` field names one id, lower-case title words between
       the name and that id are part of the name (``Iron oxide_#4820
       GDS782`` is "Iron oxide #4820", ``Marsh sediment DWV3-0511 dry`` is
       "Marsh sediment"). If that id is not in the title, the leading title
       words the field gives as material rather than id are part of the
       name (``BHVO-2F+7% Decane`` with ``SAMPLE_ID`` ``BHVO-2F, Decane
       SA-30550`` is "BHVO-2F + 7% Decane", and rule 5 then gives it
       ``SA-30550``).
    4. A temperature word (``85K``) is the measurement temperature, not part
       of the sample id, unless it follows a heating treatment (``Htd16hr``).
       So is a temperature written onto a grain-size or frost descriptor
       (``MedFrost85K``, ``<75um85K``), whose descriptor stays in the sample
       id.
    5. A title with no sample id left takes the single id its ``SAMPLE_ID``
       field gives (``1-2-3-Trimethylbenzene 85K`` -> ``SA-T73202``).

    Args:
        words: The title words before the spectrometer code (see
            :func:`_title_words`).
        html_title: The page ``TITLE`` field (without ``DESCRIPT``).
        html_sample_id: The page ``SAMPLE_ID`` field.

    Returns:
        The parsed name, sample id and temperature.
    """
    if not words:
        return SampleName("", "")
    name, rest = words[0], list(words[1:])
    page_words = html_title.split()
    if page_words:
        for count in range(len(words), 1, -1):
            if page_words[0] == "_".join(words[:count]):
                name, rest = page_words[0], list(words[count:])
                break
    ids = _html_sample_ids(html_sample_id, name)
    has_id = any(not _TEMPERATURE_WORD.match(w) for w in rest)
    for sample in [] if has_id else ids:
        if name.endswith(sample) and len(name) > len(sample):
            name = name[: -len(sample)].rstrip("_")
            rest.insert(0, sample)
            break
    name_parts = name.split("_")
    if len(name_parts) > 1 and _CATALOGUE_NUMBER.match(name_parts[-1]):
        name, rest = "_".join(name_parts[:-1]), [name_parts[-1], *rest]
    if len(ids) == 1:
        start = next((i for i, w in enumerate(rest) if w.startswith(ids[0])), None)
        if start is not None:
            lead = rest[:start]
            if lead and all(_NAME_WORD.match(w) for w in lead):
                name, rest = "_".join([name, *lead]), rest[start:]
        else:
            material = _html_material_words(html_sample_id, ids)
            count = 0
            while count < len(rest) and rest[count] in material:
                count += 1
            if count:
                name, rest = "_".join([name, *rest[:count]]), rest[count:]
    temperature: float | None = None
    kept: list[str] = []
    for i, word in enumerate(rest):
        split = _split_temperature(word)
        if split and not (i > 0 and _HEATING_WORD.match(rest[i - 1])) and temperature is None:
            descriptor, temperature = split
            if descriptor:
                kept.append(descriptor)
        else:
            kept.append(word)
    sample_id = "_".join(kept)
    if not sample_id and len(ids) == 1:
        sample_id = ids[0]
    return SampleName(_format_name(name), sample_id, temperature)


def _filename_words(filename: str) -> list[str]:
    """Split a filename into title words, for a file without a record title.

    The filename writes the title's spaces as ``_`` too, so it does not mark
    where the name ends: each ``_`` token becomes a word, and
    :func:`parse_sample_name` then takes the first as the name.

    Args:
        filename: Spectrum filename (basename, not full path), following
            ``splib07a_<Material>[_<SampleID...>]_<Spectrometer>_<DataType>.txt``.

    Returns:
        The tokens between the library prefix and the spectrometer code.
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 4:
        return parts[1:-2]
    if len(parts) >= 3:
        return parts[1:-1]
    return parts


def _parse_name_from_filename(filename: str) -> tuple[str, str]:
    """Extract a name and sample id from a filename alone.

    Used only for a file without a record title line (see
    :func:`_filename_words`); real splib07a files are named from their record
    title by :func:`parse_sample_name`.

    Args:
        filename: Spectrum filename (basename, not full path).

    Returns:
        Tuple of ``(material_name, sample_id)``; the sample id may be empty.
    """
    parsed = parse_sample_name(_filename_words(filename))
    return parsed.material_name or Path(filename).stem, parsed.sample_id


def _read_single_column(filepath: Path) -> list[float]:
    """Read a single-column ASCII data file, skipping comment/header lines.

    Args:
        filepath: Path to the single-column text file.

    Returns:
        List of parsed float values.
    """
    values: list[float] = []
    for line in filepath.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        try:
            values.append(float(stripped))
        except ValueError:
            continue
    return values


def _library_prefix(filename: str) -> str:
    """Return the library prefix (first underscore-delimited token) of a filename.

    Args:
        filename: Spectrum or axis filename (basename, not full path).

    Returns:
        The leading token (e.g. ``"splib07a"``), or the full stem if no
        underscore is present.
    """
    return Path(filename).stem.split("_", 1)[0]


def _find_axis_file(
    spectrum_path: Path,
    source_dir: Path,
    glob_patterns: tuple[str, ...],
    family: str,
) -> Path | None:
    """Locate an axis file (wavelength or bandpass) for a spectrum.

    Matches are ranked by (1) same library prefix as the spectrum
    (e.g. both ``splib07a_``), then (2) any file whose stem contains the
    family token as a standalone underscore-delimited segment. The
    prefix preference is what keeps an ``splib07a`` spectrum from
    binding to an ``splib07b`` axis — an rglob-order hazard that
    silently truncated ingest on Linux filesystems.

    Args:
        spectrum_path: Path to the spectrum file whose axis is needed.
        source_dir: Root of the USGS data directory to search.
        glob_patterns: Filename glob patterns to enumerate candidates
            (e.g. ``("*[Ww]avelength*",)`` or
            ``("*[Bb]andpass*", "*[Ff]whm*")``).
        family: Lowercased family token to match inside the axis
            filename (e.g. ``"asd"``, ``"asdfr"``).

    Returns:
        The best-ranked matching axis file, or ``None`` if none found.
    """
    prefix = _library_prefix(spectrum_path.name)
    candidates: list[Path] = []
    for pattern in glob_patterns:
        candidates.extend(source_dir.rglob(pattern))

    preferred: Path | None = None
    fallback: Path | None = None
    for axis_file in candidates:
        tokens = axis_file.stem.lower().split("_")
        if family not in tokens:
            continue
        if axis_file.name.startswith(f"{prefix}_") and preferred is None:
            preferred = axis_file
        elif fallback is None:
            fallback = axis_file
    return preferred or fallback


def _find_wavelength_file(spectrum_path: Path, source_dir: Path) -> Path | None:
    """Find the wavelength file corresponding to a spectrum file.

    USGS stores wavelength files with names like
    ``splib07a_Wavelengths_ASD_0.35-2.5_microns_2151_ch.txt``. The
    instrument family (``ASD``, ``AVIRIS``, ``BECK``, ``NIC4``) appears
    as an underscore-delimited token, and all sub-family spectrometers
    (e.g. ``ASDFRa``, ``ASDHRb``, ``ASDNGc``) share the same axis.

    The glob is restricted to ``.txt`` to skip ``GIFplots/`` companion
    images (``splib07a_Wavelengths_*.gif``) which otherwise share the
    same prefix + family tokens and — with ``rglob`` ordering that is
    filesystem-dependent — can be picked ahead of the real axis file on
    Linux, silently collapsing spectra to a single garbage point.

    Args:
        spectrum_path: Path to the spectrum file.
        source_dir: Root of the USGS data directory to search.

    Returns:
        Path to the matching wavelength file, or ``None`` if not found.
    """
    spectrometer = _detect_spectrometer(spectrum_path.name)
    family = _match_family(spectrometer, _WAVELENGTH_FAMILIES)
    if family is None:
        return None
    return _find_axis_file(spectrum_path, source_dir, ("*[Ww]avelength*.txt",), family)


def _find_bandpass_file(spectrum_path: Path, source_dir: Path) -> Path | None:
    """Find the bandpass/FWHM file for the spectrometer.

    Unlike wavelengths, each ASD sub-family (``ASDFR``, ``ASDHR``,
    ``ASDNG``) has its own FWHM file at a different native resolution,
    so this function preserves that distinction. The glob is restricted
    to ``.txt`` for the same reason as :func:`_find_wavelength_file`.

    Args:
        spectrum_path: Path to the spectrum file.
        source_dir: Root of the USGS data directory to search.

    Returns:
        Path to the matching bandpass file, or ``None`` if not found.
    """
    spectrometer = _detect_spectrometer(spectrum_path.name)
    family = _match_family(spectrometer, _BANDPASS_FAMILIES)
    if family is None:
        return None
    return _find_axis_file(
        spectrum_path, source_dir, ("*[Bb]andpass*.txt", "*[Ff]whm*.txt"), family
    )


def _material_category(
    stem: str, chapter_category: MaterialCategory, filepath: Path | None = None, text: str = ""
) -> tuple[MaterialCategory, str | None]:
    """Refine the chapter's material category for one spectrum.

    Args:
        stem: Spectrum filename stem.
        chapter_category: Category derived from the chapter directory.
        filepath: Path to the spectrum file, used to recognise chapter S.
        text: Free-text sample description.

    Returns:
        The category and, for chapter S, the rule that decided it. The Liquids
        chapter is split into water and snow/ice; chapter S into soil, rock and
        mixture (see :func:`chapter_s_category`).
    """
    if filepath is not None and _is_chapter_s(filepath):
        return chapter_s_category(stem.split("_")[1:-2], text)
    if chapter_category is not MaterialCategory.OTHER:
        return chapter_category, None
    name = stem.split("_", 1)[-1]
    for prefix, category in _LIQUIDS_CATEGORIES:
        if name.startswith(prefix):
            return category, None
    return chapter_category, None


def _is_chapter_s(filepath: Path) -> bool:
    """Tell whether a spectrum is filed in chapter S ("Soils and Mixtures").

    Args:
        filepath: Path to the spectrum file.

    Returns:
        ``True`` if a path component is the chapter S directory.
    """
    return any(
        re.sub(r"[\s\-_]+", "", part.lower()).startswith("chapterssoils") for part in filepath.parts
    )


def find_html_metadata_dir(source_dir: Path) -> Path | None:
    """Locate the archive's ``HTMLmetadata`` folder.

    Args:
        source_dir: Root of the USGS data directory.

    Returns:
        The folder, or ``None`` if the archive does not contain one.
    """
    for candidate in (source_dir / "usgs_splib07" / "HTMLmetadata", source_dir / "HTMLmetadata"):
        if candidate.is_dir():
            return candidate
    return next((p for p in source_dir.rglob("HTMLmetadata") if p.is_dir()), None)


def read_sample_metadata(html_dir: Path | None, spectrum_path: Path) -> SampleMetadata | None:
    """Read the HTMLmetadata page of a spectrum.

    The HTMLmetadata page of ``splib07a_<title>.txt`` is ``<title>.html``.

    Args:
        html_dir: The archive's ``HTMLmetadata`` folder, or ``None``.
        spectrum_path: Path to the spectrum file.

    Returns:
        The page's fields, or ``None`` if there is no page.
    """
    if html_dir is None:
        return None
    title = spectrum_path.stem.split("_", 1)[-1]
    page = html_dir / f"{title}.html"
    if not page.is_file():
        return None
    return sample_metadata(page.read_text(encoding="utf-8", errors="replace"))


def read_description(html_dir: Path | None, spectrum_path: Path) -> str | None:
    """Read the free-text sample description of a spectrum.

    Args:
        html_dir: The archive's ``HTMLmetadata`` folder, or ``None``.
        spectrum_path: Path to the spectrum file.

    Returns:
        The description text, or ``None`` if there is no page.
    """
    metadata = read_sample_metadata(html_dir, spectrum_path)
    return metadata.description if metadata is not None else None


def parse_usgs_file(
    filepath: Path,
    wavelengths: list[float],
    bandpass: list[float] | None,
    source_dir: Path,
    description: str | None = None,
    metadata: SampleMetadata | None = None,
) -> SpectrumRecord:
    """Parse a single USGS spectrum ASCII file.

    Args:
        filepath: Path to the spectrum .txt file.
        wavelengths: Pre-loaded wavelength axis for this spectrometer.
        bandpass: Pre-loaded bandpass values, or None.
        source_dir: Root of the USGS data directory (for path-based classification).
        description: Free-text sample description from the spectrum's
            HTMLmetadata page (see :func:`read_description`), or ``None``.
            Used for the geometry rules and processing flags. Defaults to
            ``metadata.description``.
        metadata: The spectrum's HTMLmetadata page fields (see
            :func:`read_sample_metadata`), or ``None``. Its ``TITLE`` and
            ``SAMPLE_ID`` refine the name read from the record title (see
            :func:`parse_sample_name`).

    The material name and sample id come from the record title on the file's
    first line (the filename when there is none); a measurement temperature
    in the title goes to ``extra["sample_temperature_k"]``.

    Deleted channels (``BAD_VALUE``) are stored as NaN and counted in
    ``extra["source_missing_band_count"]``. A non-monotonic axis (AVIRIS) is
    put in wavelength order, recorded in ``extra`` by
    :meth:`~openspeclib.loaders.base.AxisOrder.extra`.

    Returns:
        A SpectrumRecord.

    Raises:
        ValueError: If the file contains no data values.
    """
    values = _read_single_column(filepath)

    if not values:
        raise ValueError(f"No data values found in {filepath}")

    # Trim to match wavelength array length
    n = min(len(values), len(wavelengths))
    raw = values[:n]
    values = mask_invalid_values(raw, sentinels=(BAD_VALUE,))
    extra: dict[str, Any] = {}
    deleted = sum(1 for r, v in zip(raw, values) if math.isnan(v) and not math.isnan(r))
    if deleted:
        # Deleted channels (BAD_VALUE) and other fills masked to NaN.
        extra["source_missing_band_count"] = deleted
    wl = wavelengths[:n]
    bp = bandpass[:n] if bandpass and len(bandpass) >= n else None
    # AVIRIS axes are not monotonic (the four detectors overlap); Format v1
    # needs a monotonic axis, so such bands are put in wavelength order and
    # the source order is recorded (spec section 4.3).
    axis = sort_by_wavelength(wl, values, bp)
    extra.update(axis.extra(wl))
    wl = axis.wavelengths
    values = axis.arrays[0] or []
    bp = axis.arrays[1]

    page = metadata or SampleMetadata()
    words = _title_words(read_record_title(filepath), filepath.name)
    parsed = parse_sample_name(
        words if words else _filename_words(filepath.name), page.title, page.sample_id
    )
    material_name = parsed.material_name or filepath.stem
    sample_id = parsed.sample_id
    if parsed.temperature_k is not None:
        temperature = parsed.temperature_k
        extra["sample_temperature_k"] = (
            int(temperature) if temperature.is_integer() else temperature
        )
    chapter_category = _classify_from_path(filepath)
    spectrometer = _detect_spectrometer(filepath.name)
    data_type = filepath.stem.rsplit("_", 1)[-1].upper()
    quantity = USGS_DATA_TYPE_QUANTITY.get(data_type, Quantity.REFLECTANCE)
    original_id = filepath.stem
    text = description if description is not None else page.description

    decision = usgs_geometry(filepath.name, chapter_category, text)
    extra["geometry_rule"] = decision.rule
    instrument_type = decision.instrument_type or _match_family(spectrometer, _INSTRUMENT_TYPES)

    processing: list[str] = []
    computed = bool(COMPUTED_SPECTRUM.search(text))
    if computed:
        processing.append("simulated")
    if _INTERPOLATED.search(text):
        processing.append("interpolated")
    if CONVOLVED_SPECTRUM.search(text):
        # Measured with another instrument and convolved to this spectrometer's
        # channels (the filename code names the channel set, not the instrument).
        processing.append("resampled")
        extra["resampled_to_channels_of"] = spectrometer
    if (
        quantity == Quantity.REFLECTANCE
        and not computed
        and spectrometer.upper().startswith(_SCALED_TO_REFERENCE_LEVEL)
    ):
        processing.append("scaled_to_reference_level")

    category, category_rule = _material_category(filepath.stem, chapter_category, filepath, text)
    mixture_rule = _mixture_rule(filepath, text, computed)
    if mixture_rule is not None and category is not MaterialCategory.MIXTURE:
        # A computed or laboratory mixture of several materials is a mixture,
        # whatever chapter it is filed in; the chapter's category is kept for
        # reference.
        extra["original_category"] = category.value
        category = MaterialCategory.MIXTURE
        category_rule = mixture_rule
    elif mixture_rule is None and _rock_reference_sample(filepath, category):
        # The unmixed basalt reference of the chapter O "BHVO-2F + organic"
        # series is a rock, not an organic compound.
        extra["original_category"] = category.value
        category = MaterialCategory.ROCK
        category_rule = "rock_in_title"
    elif mixture_rule is None and category_rule is None and _zero_fraction_reference(filepath):
        # The 0% member of a laboratory mixture series (``+0wt%``) is the
        # unmixed reference; it keeps its chapter's category.
        category_rule = "zero_fraction_reference"
    if category_rule is not None:
        extra["category_rule"] = category_rule

    return SpectrumRecord(
        id=spectrum_id(COLLECTION_ID, original_id),
        collection=COLLECTION_ID,
        original_id=original_id,
        name=f"{material_name} {sample_id}".strip(),
        license=USGS_LICENSE,
        material=Material(
            name=material_name,
            category=category,
            keywords=[material_name.lower()],
        ),
        sample=Sample(
            id=sample_id or None,
            description=f"{material_name}, measured with {spectrometer}",
        ),
        measurement=Measurement(
            quantity=quantity,
            geometry=decision.geometry,
            instrument=spectrometer,
            instrument_type=instrument_type,
            laboratory="USGS Spectroscopy Lab",
            processing=processing,
            reference=USGS_DATA_TYPE_REFERENCE.get(data_type),
        ),
        spectral_data=SpectralData(
            wavelength_unit=WavelengthUnit.MICROMETERS,
            wavelengths=wl,
            values=values,
            bandpass_fwhm=bp,
        ),
        extra=extra,
    )


class UsgsLoader(BaseLoader):
    """Loader for the USGS Spectral Library Version 7."""

    def source_name(self) -> str:
        """Return the collection id."""
        return COLLECTION_ID

    def collection_info(self) -> CollectionInfo:
        """Return the USGS Spectral Library Version 7 collection metadata.

        Returns:
            The footer ``collection`` object.
        """
        return USGS_COLLECTION

    def download(self, target_dir: Path) -> Path:
        """Download and extract the USGS Speclib 07 archive.

        Fetches the upstream ZIP from the Hugging Face dataset mirror at
        ``USGS_HF_REPO`` and extracts it into ``target_dir``. If the
        extracted directory already exists, the download and extraction
        steps are skipped — safe to call repeatedly from cached CI runs.

        Args:
            target_dir: Directory to download and extract into.

        Returns:
            Path to the extracted USGS data directory.

        Raises:
            RuntimeError: If the downloaded file is not a valid ZIP archive.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        extract_dir = target_dir / "usgs_splib07"

        # Fast path: extraction already done (e.g. restored from CI cache).
        if extract_dir.exists() and any(extract_dir.iterdir()):
            logger.info("USGS data already extracted at %s", extract_dir)
            return extract_dir

        logger.info(
            "Downloading USGS Speclib 07 from Hugging Face: %s (%s @ %s)",
            USGS_HF_REPO,
            USGS_HF_FILENAME,
            USGS_HF_REVISION,
        )
        archive_path = Path(
            hf_hub_download(
                repo_id=USGS_HF_REPO,
                filename=USGS_HF_FILENAME,
                repo_type="dataset",
                revision=USGS_HF_REVISION,
                local_dir=str(target_dir),
            )
        )

        if not zipfile.is_zipfile(archive_path):
            raise RuntimeError(
                f"File downloaded from {USGS_HF_REPO}/{USGS_HF_FILENAME} "
                f"is not a valid ZIP archive."
            )

        logger.info("Extracting USGS archive to %s", extract_dir)
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)

        return extract_dir

    def load(self, source_dir: Path) -> Iterator[SpectrumRecord]:
        """Parse USGS ASCII spectrum files.

        Searches for spectrum data files, matches them with wavelength
        files by spectrometer, and yields SpectrumRecord objects.
        """
        # Scope ingest to the splib07a main library (standard-resolution
        # canonical sampling). The splib07b archive and the various
        # ``..._cvAVIRIS*`` / ``..._cvASD*`` subdirectories are
        # re-convolved/resampled copies of the same underlying spectra;
        # including them would double-count. Errorbars files are the
        # per-band 1-sigma uncertainties, not spectra in their own right.
        all_txt = sorted(source_dir.rglob("*.txt"))
        spectrum_files = [
            f
            for f in all_txt
            if "errorbars" not in (p.lower() for p in f.parts)
            and not any(
                kw in f.name.lower()
                for kw in [
                    "wavelength",
                    "wavenumber",
                    "bandpass",
                    "fwhm",
                    "readme",
                    "description",
                ]
            )
            and re.match(r"splib07a_", f.name)
        ]

        logger.info("Found %d USGS spectrum files", len(spectrum_files))
        html_dir = find_html_metadata_dir(source_dir)
        if html_dir is None:
            logger.warning(
                "No HTMLmetadata folder under %s; geometry falls back to the "
                "spectrometer-only rules",
                source_dir,
            )

        # Cache wavelength/bandpass arrays per spectrometer
        wl_cache: dict[str, list[float]] = {}
        bp_cache: dict[str, list[float] | None] = {}

        # Outcome tallies per spectrometer so the final summary makes silent
        # drops visible in CI logs even when tqdm reshuffles stderr output.
        skipped_no_wl: dict[str, int] = {}
        parsed: dict[str, int] = {}
        parse_errors: dict[str, int] = {}

        for filepath in tqdm(spectrum_files, desc="Processing USGS"):
            spectrometer = _detect_spectrometer(filepath.name)

            # Load wavelength file if not cached
            if spectrometer not in wl_cache:
                wl_file = _find_wavelength_file(filepath, source_dir)
                if wl_file:
                    wl_cache[spectrometer] = _read_single_column(wl_file)
                    logger.info(
                        "Using wavelength file %s for spectrometer %s (%d points)",
                        wl_file.name,
                        spectrometer,
                        len(wl_cache[spectrometer]),
                    )
                else:
                    logger.warning(
                        "No wavelength file found for spectrometer %s (e.g. %s)",
                        spectrometer,
                        filepath.name,
                    )
                    wl_cache[spectrometer] = []

                bp_file = _find_bandpass_file(filepath, source_dir)
                bp_cache[spectrometer] = _read_single_column(bp_file) if bp_file else None

            wavelengths = wl_cache[spectrometer]
            if not wavelengths:
                skipped_no_wl[spectrometer] = skipped_no_wl.get(spectrometer, 0) + 1
                continue

            try:
                yield parse_usgs_file(
                    filepath,
                    wavelengths,
                    bp_cache[spectrometer],
                    source_dir,
                    metadata=read_sample_metadata(html_dir, filepath),
                )
                parsed[spectrometer] = parsed.get(spectrometer, 0) + 1
            except Exception:
                parse_errors[spectrometer] = parse_errors.get(spectrometer, 0) + 1
                logger.warning("Failed to parse %s", filepath, exc_info=True)

        total_parsed = sum(parsed.values())
        total_skipped = sum(skipped_no_wl.values())
        total_errors = sum(parse_errors.values())
        logger.info(
            "USGS ingest summary: parsed=%d, skipped_no_wavelength=%d, parse_errors=%d",
            total_parsed,
            total_skipped,
            total_errors,
        )
        if skipped_no_wl:
            logger.warning(
                "Spectrometers with missing wavelength axis (dropped): %s",
                ", ".join(f"{spec}×{n}" for spec, n in sorted(skipped_no_wl.items())),
            )
