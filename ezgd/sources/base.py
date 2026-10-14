"""Common source-adapter interface.

Adapters translate one website into normalized :class:`Game` records and
:class:`DownloadMethod` entries; nothing outside ``ezgd.sources`` parses HTML.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from ezgd.catalog.models import DownloadMethod, Game
from ezgd.sources.cache import CachedFetcher, SourceUnavailableError

PARSER_VERSION = 1


class SourceError(Exception):
    """Base class for adapter errors that the UI shows to the user."""


class LayoutChangedError(SourceError):
    """The page was fetched but no longer matches the structure the parser expects."""


class ManualActionRequired(SourceError):
    """The link needs a browser (captcha, login, ad gate, unsupported host)."""

    def __init__(self, message: str, url: str):
        super().__init__(message)
        self.url = url


__all__ = ["SourceAdapter", "SourceInfo", "Page", "FeaturedSection", "ResolvedDownload", "SourceError",
           "LayoutChangedError", "ManualActionRequired", "SourceUnavailableError", "PARSER_VERSION"]


@dataclass
class SourceInfo:
    id: str
    name: str
    homepage: str
    description: str
    capabilities: set[str] = field(default_factory=set)     # search, pagination, genres, featured
    notes: list[str] = field(default_factory=list)          # user-facing limitations
    page_size: int = 20
    archive_password: str | None = None                     # password the site says ALL its archives use
    # may the app read it without the user asking (update checks, wishlist)? False for sites whose robots.txt
    # asks tools not to crawl them — those are only checked when the user presses "Check now"
    background_ok: bool = True


@dataclass
class Page:
    games: list[Game]
    page: int
    has_more: bool
    total: int | None = None
    stale: bool = False          # served from cache because the site was unreachable


@dataclass
class FeaturedSection:
    key: str                     # stable id, e.g. "popular"
    title: str                   # English label shown in the UI
    source_label: str            # the heading as shown on the site
    games: list[Game]


@dataclass
class ResolvedDownload:
    """A concrete, permitted transfer the download manager can start."""

    kind: str                                 # "torrent" | "http"
    url: str | None = None                    # http(s) file URL or magnet URI
    torrent_bytes: bytes | None = None
    file_name: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    referer: str | None = None
    archive_password: str | None = None


class SourceAdapter(abc.ABC):
    info: SourceInfo

    def __init__(self, fetcher: CachedFetcher):
        self.fetcher = fetcher

    @property
    def id(self) -> str:
        return self.info.id

    @abc.abstractmethod
    async def latest(self, page: int = 1, genre: str | None = None) -> Page:
        """Newest listings (optionally restricted to one canonical genre)."""

    @abc.abstractmethod
    async def search(self, query: str, page: int = 1) -> Page:
        ...

    @abc.abstractmethod
    async def details(self, game: Game) -> Game:
        """Fetch the game page and fill description, metadata and download methods."""

    async def featured(self) -> list[FeaturedSection]:
        return []

    def featured_keys(self) -> dict[str, tuple[str, str]]:
        """{section key: (English title, heading as shown on the site)} for cached sections."""
        return {}

    async def genres(self) -> list[str]:
        return []

    async def expand(self, method: DownloadMethod, game: Game) -> list[DownloadMethod]:
        """Turn a MIRRORS method into the concrete methods it lists."""
        return []

    @abc.abstractmethod
    async def resolve(self, method: DownloadMethod, game: Game) -> ResolvedDownload:
        """Return a concrete transfer or raise :class:`ManualActionRequired`."""
