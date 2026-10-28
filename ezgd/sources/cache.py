"""Caching fetcher: TTL cache in SQLite plus conditional requests (ETag/Last-Modified).

When a site is unreachable a stale cached copy is served (flagged ``stale``)
so the catalog keeps working offline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Mapping

import httpx

from ezgd.database.repositories import CachedResponse, HttpCacheRepository
from ezgd.net.client import FetchResult, HttpError, SafeHttpClient
from ezgd.net.urls import LookupFailed, UnsafeURLError, host_of

log = logging.getLogger(__name__)

KEEP_HEADERS = ("content-type", "x-wp-total", "x-wp-totalpages", "etag", "last-modified", "cf-mitigated", "server")


class SourceUnavailableError(Exception):
    """Network problem or server error; the site could not be reached."""


def _why(url: str, exc: BaseException) -> str:
    """Why a page could not be loaded, as the player reads it. A site that could not be found or reached (after the
    connection's own tries, see net.client.GuardedBackend) says so in words — it showed "OSError: could not resolve
    cracked-games.org: Name or service not known"."""
    if isinstance(exc, LookupFailed):
        what = str(exc)
    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        what = f"could not connect to {host_of(url)} ({str(exc) or type(exc).__name__})"
    else:
        return f"{type(exc).__name__}: {exc}"
    return f"{what}. The site or the connection is down for the moment — try again in a minute."


@dataclass
class Fetched:
    url: str
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    from_cache: bool = False
    stale: bool = False

    def text(self, fallback: str = "utf-8") -> str:
        ct = self.headers.get("content-type", "")
        enc = None
        for part in ct.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset":
                enc = v.strip("\"' ")
        try:
            return self.body.decode(enc or fallback, errors="replace")
        except LookupError:
            return self.body.decode(fallback, errors="replace")


class CachedFetcher:
    def __init__(self, client: SafeHttpClient, cache: HttpCacheRepository | None, default_ttl: float = 3600):
        self.client = client
        self.cache = cache
        self.default_ttl = default_ttl

    async def get(self, url: str, *, ttl: float | None = None, headers: Mapping[str, str] | None = None,
                  max_bytes: int = 8 * 1024 * 1024, force: bool = False) -> Fetched:
        ttl = self.default_ttl if ttl is None else ttl
        cached = self.cache.get(url) if self.cache else None
        if cached and cached.fresh and not force:
            return Fetched(url, cached.status, cached.body, cached.headers, from_cache=True)
        req_headers = dict(headers or {})
        if cached:
            if cached.etag:
                req_headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                req_headers["If-Modified-Since"] = cached.last_modified
        try:
            res: FetchResult = await self.client.fetch(url, headers=req_headers, max_bytes=max_bytes)
        except UnsafeURLError:
            raise
        except (httpx.HTTPError, OSError, HttpError) as exc:
            if cached:
                log.warning("Serving stale cache for %s (%s)", url, exc)
                return Fetched(url, cached.status, cached.body, cached.headers, from_cache=True, stale=True)
            raise SourceUnavailableError(_why(url, exc)) from exc

        if res.status == 304 and cached:
            if self.cache:
                self.cache.touch(url, time.time() + ttl)
            return Fetched(url, cached.status, cached.body, cached.headers, from_cache=True)
        if res.status in (429,) or res.status >= 500:
            if cached:
                return Fetched(url, cached.status, cached.body, cached.headers, from_cache=True, stale=True)
            raise SourceUnavailableError(f"server answered HTTP {res.status}")
        kept = {k: res.headers[k] for k in KEEP_HEADERS if k in res.headers}
        if res.status == 200 and self.cache:
            self.cache.put(CachedResponse(url, res.status, res.headers.get("etag"),
                                          res.headers.get("last-modified"), res.headers.get("content-type"),
                                          kept, res.content, time.time(), time.time() + ttl))
        return Fetched(res.url, res.status, res.content, kept)
