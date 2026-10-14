"""Source registry: fan-out operations across enabled adapters with health tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ezgd.catalog.models import DownloadMethod, Game
from ezgd.database.repositories import GameRepository, SourceRepository
from ezgd.net.urls import UnsafeURLError
from ezgd.sources.base import (FeaturedSection, LayoutChangedError, ManualActionRequired, Page,
                               ResolvedDownload, SourceAdapter, SourceError)
from ezgd.sources.cache import SourceUnavailableError
from ezgd.sources.hosts import browser_url

log = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_STALE = "offline-cache"
STATUS_UNAVAILABLE = "unavailable"
STATUS_LAYOUT = "layout-changed"
STATUS_ERROR = "error"


@dataclass
class SourceStatus:
    state: str = "unknown"
    message: str | None = None
    at: float = 0.0


@dataclass
class MultiResult:
    games: list[Game] = field(default_factory=list)
    has_more: dict[str, bool] = field(default_factory=dict)
    totals: dict[str, int | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    stale: set[str] = field(default_factory=set)


def describe_error(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, SourceUnavailableError):
        return STATUS_UNAVAILABLE, f"Site unreachable: {exc}"
    if isinstance(exc, LayoutChangedError):
        return STATUS_LAYOUT, f"Page layout not recognised: {exc}"
    if isinstance(exc, asyncio.TimeoutError):
        return STATUS_UNAVAILABLE, "The site did not respond in time."
    if isinstance(exc, UnsafeURLError):
        return STATUS_ERROR, f"Blocked unsafe URL: {exc}"
    if isinstance(exc, SourceError):
        return STATUS_ERROR, str(exc)
    return STATUS_ERROR, f"{type(exc).__name__}: {exc}"


class SourceRegistry:
    def __init__(self, adapters: list[SourceAdapter], games: GameRepository, sources: SourceRepository,
                 enabled: dict[str, bool] | None = None, timeout: float = 40.0):
        self.adapters = {a.id: a for a in adapters}
        self.games = games
        self.sources = sources
        self.enabled = {a.id: True for a in adapters}
        self.enabled.update(enabled or {})
        self.timeout = timeout
        self.hide_withdrawn = True           # e.g. OnlineFix "guide closed" games are not shown
        self.hide_adult = True               # games the source itself marks 18+
        self.status: dict[str, SourceStatus] = {a.id: SourceStatus() for a in adapters}
        for a in adapters:
            sources.ensure(a.id, self.enabled[a.id])

    def add_adapter(self, adapter: SourceAdapter, enabled: bool = True) -> None:
        self.adapters[adapter.id] = adapter
        self.enabled.setdefault(adapter.id, enabled)
        self.status.setdefault(adapter.id, SourceStatus())
        self.sources.ensure(adapter.id, self.enabled[adapter.id])

    def remove_adapter(self, source_id: str) -> None:
        self.adapters.pop(source_id, None)
        self.enabled.pop(source_id, None)
        self.status.pop(source_id, None)

    def set_enabled(self, enabled: dict[str, bool]) -> None:
        self.enabled.update({k: bool(v) for k, v in enabled.items() if k in self.adapters})

    def active(self) -> list[SourceAdapter]:
        return [a for a in self.adapters.values() if self.enabled.get(a.id, True)]

    def adapter(self, source_id: str) -> SourceAdapter:
        try:
            return self.adapters[source_id]
        except KeyError:
            raise SourceError(f"unknown source '{source_id}'") from None

    def _mark(self, source_id: str, state: str, message: str | None = None) -> None:
        self.status[source_id] = SourceStatus(state, message, time.time())
        try:
            self.sources.set_status(source_id, state, message)
        except Exception:  # status bookkeeping must never break a request
            log.exception("could not persist source status")

    async def _guard(self, adapter: SourceAdapter, coro):
        try:
            result = await asyncio.wait_for(coro, self.timeout)
        except Exception as exc:
            state, msg = describe_error(exc)
            log.warning("[%s] %s", adapter.id, msg)
            self._mark(adapter.id, state, msg)
            raise
        stale = getattr(result, "stale", False)
        self._mark(adapter.id, STATUS_STALE if stale else STATUS_OK,
                   "Showing cached results; the site could not be reached." if stale else None)
        return result

    def visible(self, games: list[Game]) -> list[Game]:
        return [g for g in games if not (self.hide_withdrawn and g.withdrawn)
                and not (self.hide_adult and g.adult)]

    async def _fan_out(self, make, sources: set[str] | None) -> MultiResult:
        targets = [a for a in self.active() if not sources or a.id in sources]
        out = MultiResult()
        results = await asyncio.gather(*(self._guard(a, make(a)) for a in targets), return_exceptions=True)
        for a, res in zip(targets, results):
            if isinstance(res, BaseException):
                out.errors[a.id] = describe_error(res)[1]
                continue
            page: Page = res
            stored = self.games.upsert_many(page.games)
            out.games.extend(self.visible(stored))
            out.has_more[a.id] = page.has_more
            out.totals[a.id] = page.total
            if page.stale:
                out.stale.add(a.id)
        return out

    async def search(self, query: str, page: int = 1, sources: set[str] | None = None) -> MultiResult:
        res = await self._fan_out(lambda a: a.search(query, page), sources)
        if res.errors and page == 1:
            # offline fallback: cached titles from the failing sources
            failing = [s for s in res.errors if not sources or s in sources]
            res.games.extend(self.visible(self.games.search_local(query, failing)))
        return res

    async def latest(self, page: int = 1, sources: set[str] | None = None, genre: str | None = None) -> MultiResult:
        res = await self._fan_out(lambda a: a.latest(page, genre), sources)
        if res.errors and page == 1:
            res.games.extend(self.visible(self.games.recent(list(res.errors), limit=40)))
        return res

    async def featured(self, source_id: str, max_age: float = 3600, force: bool = False) -> tuple[list[FeaturedSection], float | None, str | None]:
        """Featured sections with listing cache; returns (sections, fetched_at, error)."""
        adapter = self.adapter(source_id)
        cached_sections: list[FeaturedSection] = []
        fetched_at = None
        for key, (title, site_label) in adapter.featured_keys().items():
            games, at = self.games.load_listing(source_id, f"featured:{key}")
            if games:
                cached_sections.append(FeaturedSection(key, title, site_label, games))
                fetched_at = at if fetched_at is None else min(fetched_at, at)
        if cached_sections and fetched_at and not force and time.time() - fetched_at < max_age:
            return self._visible_sections(cached_sections), fetched_at, None
        try:
            sections = await self._guard(adapter, adapter.featured())
        except Exception as exc:
            return self._visible_sections(cached_sections), fetched_at, describe_error(exc)[1]
        for s in sections:
            self.games.save_listing(source_id, f"featured:{s.key}", s.games)
            s.games = self.games.load_listing(source_id, f"featured:{s.key}")[0]
        return self._visible_sections(sections), time.time(), None

    def _visible_sections(self, sections: list[FeaturedSection]) -> list[FeaturedSection]:
        for s in sections:
            s.games = self.visible(s.games)
        return [s for s in sections if s.games]

    async def verify_withdrawn(self, games: list[Game]) -> list[str]:
        """Open the pages of games whose withdrawn state is unknown (rate-limited); return withdrawn ids."""
        hidden = []
        for g in games:
            if g.withdrawn_known:
                continue
            try:
                fresh = await self.details(g)
            except Exception as exc:
                log.info("could not verify %s: %s", g.id, exc)
                continue
            if fresh.withdrawn:
                hidden.append(fresh.id)
        return hidden

    async def details(self, game: Game) -> Game:
        adapter = self.adapter(game.source)
        fresh = await self._guard(adapter, adapter.details(game))
        self.games.save(fresh)
        return fresh

    async def resolve(self, game: Game, method: DownloadMethod) -> ResolvedDownload:
        if not method.supported:
            raise ManualActionRequired(method.note or "This link must be opened in a browser.",
                                       browser_url(method, game.url))
        adapter = self.adapter(game.source)
        return await asyncio.wait_for(adapter.resolve(method, game), self.timeout)

    async def genres(self) -> list[str]:
        out: set[str] = set()
        results = await asyncio.gather(*(asyncio.wait_for(a.genres(), self.timeout) for a in self.active()),
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                out.update(r)
        return sorted(out)
