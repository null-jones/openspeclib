"""Catalog utilities for working with OpenSpecLib catalog files."""

from pathlib import Path

from openspeclib.models import CatalogFile


def load_catalog(catalog_path: Path) -> CatalogFile:
    """Load and parse a catalog.json file.

    Args:
        catalog_path: Path to the catalog.json file.

    Returns:
        A validated CatalogFile object.
    """
    text = catalog_path.read_text(encoding="utf-8")
    return CatalogFile.model_validate_json(text)


def get_source_info(catalog: CatalogFile) -> dict[str, int]:
    """Return spectrum counts per source library.

    Args:
        catalog: A loaded catalog file.

    Returns:
        Mapping of source library name to spectrum count.
    """
    return {name: info.spectrum_count for name, info in catalog.sources.items()}


def get_category_counts(catalog: CatalogFile) -> dict[str, int]:
    """Return spectrum counts per material category.

    Args:
        catalog: A loaded catalog file.

    Returns:
        Mapping of material category to spectrum count.
    """
    return dict(catalog.statistics.categories)
