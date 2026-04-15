"""Abstract base class for spectral library loaders."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from openspeclib.models import SpectrumRecord


class BaseLoader(ABC):
    """Abstract base for all spectral library source loaders.

    Each loader is responsible for:
    1. Downloading the source archive (download)
    2. Parsing native files into SpectrumRecord objects (load)

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

    @abstractmethod
    def source_name(self) -> str:
        """Return the source library identifier (e.g. 'usgs_splib07')."""
        ...

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
