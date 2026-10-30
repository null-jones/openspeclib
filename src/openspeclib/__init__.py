"""OpenSpecLib — Open-source amalgamated spectral library and processing toolkit.

Read Format v1 files with :func:`openspeclib.open` (one collection file) or
:func:`openspeclib.open_index` (an index over several), both from
:mod:`openspeclib.reader`.
"""

from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"

__all__ = ["__version__", "open", "open_index"]

if TYPE_CHECKING:
    from openspeclib.reader import open, open_index


def __getattr__(name: str) -> Any:
    """Import the reader lazily so that ``import openspeclib`` stays light.

    Args:
        name: Attribute requested from the package.

    Returns:
        :func:`openspeclib.reader.open` or :func:`openspeclib.reader.open_index`.

    Raises:
        AttributeError: For any other name.
    """
    if name in ("open", "open_index"):
        from openspeclib import reader

        return getattr(reader, name)
    raise AttributeError(f"module 'openspeclib' has no attribute {name!r}")
