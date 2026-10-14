"""Normalized catalog records shared by every source adapter.

Fields that a source does not provide stay ``None`` (or empty); nothing here is
guessed. ``download_methods is None`` means "details not fetched yet", which is
different from ``[]`` ("checked; no methods found").
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from ezgd.catalog.normalize import dedup_key


class MethodKind(StrEnum):
    TORRENT = "torrent"      # .torrent file or magnet link → embedded engine
    DIRECT = "direct"        # verified direct HTTP(S) file → embedded HTTP engine
    MIRRORS = "mirrors"      # an index of further methods that the adapter can expand
    EXTERNAL = "external"    # third-party page needing a browser (captcha, login, JS, ads)


@dataclass
class DownloadMethod:
    kind: MethodKind
    label: str
    url: str
    host: str | None = None
    supported: bool = False          # can the app perform this download itself?
    note: str | None = None          # why unsupported / what to expect
    resolver: str | None = None      # adapter-specific resolver id
    file_name: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None      # "sha256:<hex>" from a trusted API, else None
    archive_password: str | None = None  # password the source publishes for its archives
    flagged_dangerous: bool = False  # the source itself warns about this mirror

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = str(self.kind)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DownloadMethod":
        d = dict(d)
        d["kind"] = MethodKind(d["kind"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Game:
    source: str
    source_game_id: str
    title: str
    url: str
    raw_title: str | None = None
    cover_url: str | None = None
    description: str | None = None
    genres: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    release_date: str | None = None
    version: str | None = None
    size_bytes: int | None = None     # approximate size as stated by the source
    size_text: str | None = None
    steam_app_id: int | None = None
    download_methods: list[DownloadMethod] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    published_at: float | None = None
    updated_at: float | None = None
    details_checked_at: float | None = None
    cached_at: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_game_id}"

    @property
    def dedup_key(self) -> str:
        return dedup_key(self.title)

    @property
    def method_kinds(self) -> set[str]:
        return {str(m.kind) for m in (self.download_methods or [])}

    @property
    def withdrawn(self) -> bool:
        """The source has withdrawn this game (e.g. OnlineFix "guide closed")."""
        return self.extra.get("guide_closed") is True

    @property
    def adult(self) -> bool:
        """The source marks this game 18+ / NSFW."""
        return self.extra.get("adult") is True

    @property
    def withdrawn_known(self) -> bool:
        """Whether we know if it is withdrawn (carousel items alone do not say)."""
        return "guide_closed" in self.extra or self.source != "onlinefix"

    @property
    def sort_time(self) -> float:
        return self.published_at or self.updated_at or 0.0

    # --------------------------------------------------------------- storage
    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "source_game_id": self.source_game_id,
            "title": self.title, "raw_title": self.raw_title, "dedup_key": self.dedup_key,
            "url": self.url, "cover_url": self.cover_url, "description": self.description,
            "genres": json.dumps(self.genres, ensure_ascii=False),
            "platforms": json.dumps(self.platforms, ensure_ascii=False),
            "release_date": self.release_date, "version": self.version,
            "size_bytes": self.size_bytes, "size_text": self.size_text,
            "steam_app_id": self.steam_app_id,
            "download_methods": None if self.download_methods is None else
            json.dumps([m.to_dict() for m in self.download_methods], ensure_ascii=False),
            "extra": json.dumps(self.extra, ensure_ascii=False),
            "published_at": self.published_at, "updated_at": self.updated_at,
            "details_checked_at": self.details_checked_at, "cached_at": self.cached_at,
        }

    @classmethod
    def from_row(cls, row) -> "Game":
        r = dict(row)
        methods = r.get("download_methods")
        return cls(
            source=r["source"], source_game_id=r["source_game_id"], title=r["title"], url=r["url"],
            raw_title=r.get("raw_title"), cover_url=r.get("cover_url"), description=r.get("description"),
            genres=json.loads(r.get("genres") or "[]"), platforms=json.loads(r.get("platforms") or "[]"),
            release_date=r.get("release_date"), version=r.get("version"),
            size_bytes=r.get("size_bytes"), size_text=r.get("size_text"), steam_app_id=r.get("steam_app_id"),
            download_methods=None if methods is None else
            [DownloadMethod.from_dict(m) for m in json.loads(methods)],
            extra=json.loads(r.get("extra") or "{}"), published_at=r.get("published_at"),
            updated_at=r.get("updated_at"), details_checked_at=r.get("details_checked_at"),
            cached_at=r.get("cached_at") or time.time(),
        )

    def merge_listing(self, newer: "Game") -> "Game":
        """Combine a fresh listing record with cached details (details are kept)."""
        for name in ("title", "raw_title", "url", "cover_url", "published_at", "updated_at", "version"):
            val = getattr(newer, name)
            if val:
                setattr(self, name, val)
        for name in ("genres", "platforms"):
            if getattr(newer, name):
                setattr(self, name, getattr(newer, name))
        self.extra = {**self.extra, **newer.extra}
        if newer.steam_app_id:
            self.steam_app_id = newer.steam_app_id
        self.cached_at = newer.cached_at
        return self


@dataclass
class GameGroup:
    """The same game offered by one or more sources (duplicate detection result)."""

    key: str
    entries: list[Game]

    @property
    def primary(self) -> Game:
        # prefer the entry with the most information / newest listing
        return max(self.entries, key=lambda g: (g.cover_url is not None, g.sort_time))

    @property
    def title(self) -> str:
        return self.primary.title

    @property
    def sources(self) -> list[str]:
        return sorted({g.source for g in self.entries})

    @property
    def genres(self) -> list[str]:
        seen: list[str] = []
        for g in self.entries:
            for x in g.genres:
                if x not in seen:
                    seen.append(x)
        return seen

    @property
    def platforms(self) -> list[str]:
        return sorted({p for g in self.entries for p in g.platforms})

    @property
    def method_kinds(self) -> set[str]:
        return set().union(*(g.method_kinds for g in self.entries)) if self.entries else set()

    @property
    def sort_time(self) -> float:
        return max(g.sort_time for g in self.entries)

    @property
    def is_duplicate(self) -> bool:
        return len(self.entries) > 1
