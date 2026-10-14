"""SteamRIP adapter (https://steamrip.com).

Verified against the live site on 2026-10-09:

* WordPress site exposing the standard public REST API
  (``/wp-json/wp/v2/posts``, ``/categories``), with ``X-WP-Total``/
  ``X-WP-TotalPages`` headers, ``Last-Modified`` and ``max-age=86400``.
  ``search_columns=post_title`` gives title-only search.
* Post content lists "GAME INFO" (Genre, Developer, Platform, Game Size,
  Released By, Version) and ``a.shortc-button`` links to file hosts.
* No torrents or magnet links are offered. Observed hosts: Buzzheavier
  (Cloudflare challenge), FileDitch (browser gate), Gofile (token), MegaDB
  (captcha) — none can be downloaded without a browser, so they are shown as
  external links. Pixeldrain links, if they appear, are supported.
* robots.txt disallows generic crawlers. This adapter therefore never crawls:
  it only requests what the user is looking at (a page of results, a search,
  a game), caches responses and is rate-limited.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup

from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from ezgd.catalog.normalize import canonical_genre, clean_title
from ezgd.core.fsutil import parse_size
from ezgd.sources.base import (LayoutChangedError, ManualActionRequired, Page, ResolvedDownload,
                               SourceAdapter, SourceError, SourceInfo)
from ezgd.sources.hosts import classify_host_link, resolve_pixeldrain
from ezgd.sources import parsing

log = logging.getLogger(__name__)

BASE = "https://steamrip.com"
SITE_HOSTS = {"steamrip.com", "www.steamrip.com"}
PAGE_SIZE = 24
LIST_FIELDS = "id,date,date_gmt,modified,modified_gmt,slug,link,title,categories,_links,_embedded"
_STEAM_APP = re.compile(r"/steam/apps/(\d+)/")


def _wp_time(post: dict, key: str) -> float | None:
    raw = post.get(f"{key}_gmt") or post.get(key)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _cover(post: dict) -> str | None:
    media = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
    if not media or not isinstance(media[0], dict):
        return None
    m = media[0]
    sizes = (m.get("media_details") or {}).get("sizes") or {}
    for key in ("jannah-image-large", "medium_large", "medium", "full"):
        if isinstance(sizes.get(key), dict) and sizes[key].get("source_url"):
            return sizes[key]["source_url"]
    return m.get("source_url")


def parse_post(post: dict, categories: dict[int, str], source: str = "steamrip") -> Game | None:
    if not isinstance(post, dict) or "id" not in post or "link" not in post:
        return None
    raw = htmllib.unescape(((post.get("title") or {}).get("rendered") or "").strip())
    if not raw:
        return None
    title, version = clean_title(raw)
    genres = []
    for cid in post.get("categories") or []:
        g = canonical_genre(categories.get(cid, "")) if cid in categories else None
        if g and g not in genres:
            genres.append(g)
    return Game(source=source, source_game_id=str(post["id"]), title=title, raw_title=raw,
                url=post["link"], cover_url=_cover(post), genres=genres, version=version,
                published_at=_wp_time(post, "date"), updated_at=_wp_time(post, "modified"),
                extra={"slug": post.get("slug")} if post.get("slug") else {})


def parse_posts(body: bytes, categories: dict[int, str], source: str = "steamrip") -> list[Game]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise LayoutChangedError("The site's WordPress API did not return JSON") from exc
    if isinstance(data, dict) and data.get("code"):
        if data.get("code") == "rest_post_invalid_page_number":
            return []
        raise SourceError(f"WordPress API error: {data.get('message') or data['code']}")
    if not isinstance(data, list):
        raise LayoutChangedError("The site's WordPress API returned an unexpected structure")
    return [g for g in (parse_post(p, categories, source) for p in data) if g]


def download_links(soup: BeautifulSoup) -> list[DownloadMethod]:
    """Generic: magnet links, .torrent files and links to known file hosts anywhere in a post."""
    from ezgd.sources.hosts import HOST_LABELS, pixeldrain_id
    out, seen = [], set()
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if href.startswith("//"):
            href = "https:" + href
        if href in seen:
            continue
        text = a.get_text(" ", strip=True)[:80]
        if href.lower().startswith("magnet:?"):
            m = re.search(r"btih:([0-9a-z]+)", href, re.I)
            key = ("btih", m.group(1).lower()) if m else href
            if key in seen:
                continue
            seen.add(key)
            out.append(DownloadMethod(MethodKind.TORRENT, text if text and "magnet" not in text.lower() else "Magnet link",
                                      href, host="magnet", supported=True, resolver="magnet",
                                      note="Downloaded in the app with the built-in torrent engine."))
            continue
        if not href.startswith(("http://", "https://")):
            continue
        host = (urlsplit(href).hostname or "").lower().removeprefix("www.")
        if urlsplit(href).path.lower().endswith(".torrent"):
            seen.add(href)
            out.append(DownloadMethod(MethodKind.TORRENT, text or "Torrent file", href, host=host, supported=True,
                                      resolver="torrent-file", note="The .torrent is fetched and downloaded in the app."))
        elif host in HOST_LABELS or pixeldrain_id(href):
            seen.add(href)
            m = classify_host_link(href)
            if text and text.lower() not in ("download", "download here", "click here"):
                m.label = f"{m.label} — {text}" if text.lower() != m.label.lower() else m.label
            out.append(m)
    # what the app can download itself first (torrents, Pixeldrain), then browser-only hosts; order kept otherwise
    return sorted(out, key=lambda m: (not m.supported, m.kind != MethodKind.TORRENT))


def parse_content(content_html: str, game: Game, generic: bool = False) -> Game:
    soup = BeautifulSoup(content_html, "lxml")
    if soup.body is None:
        raise LayoutChangedError("The post has no content")
    for s in soup.select("script, style"):
        s.decompose()
    # description: paragraphs before the first section heading (SCREENSHOTS / SYSTEM REQUIREMENTS)
    paras = []
    for el in soup.body.find_all(["p", "h4", "hr"]):
        if el.name in ("h4", "hr") and paras:
            if el.name == "hr":
                break
            continue
        if el.name == "p":
            txt = el.get_text(" ", strip=True)
            if txt and not el.find("a", class_="shortc-button"):
                paras.append(txt)
        if sum(len(p) for p in paras) > 1500:
            break
    if paras:
        game.description = "\n\n".join(paras)[:2000]

    info: dict[str, str] = {}
    for li in soup.select("li"):
        txt = re.sub(r"\s+", " ", li.get_text(" ", strip=True))
        m = re.match(r"^(Genre|Developer|Publisher|Platform|Game Size|Released By|Version|Release Date|OS)\s*:\s*(.+)$",
                     txt, re.I)
        if m:
            info.setdefault(m.group(1).lower(), m.group(2).strip())
        elif txt.lower() == "pre-installed game":
            game.extra["preinstalled"] = True
    if "genre" in info:
        gs = [canonical_genre(x) for x in info["genre"].split(",")]
        merged = list(game.genres)
        for g in gs:
            if g and g not in merged:
                merged.append(g)
        game.genres = merged
    if "platform" in info:
        game.platforms = [p.strip() for p in re.split(r"[,/]", info["platform"]) if p.strip()]
    if "game size" in info:
        game.size_text = info["game size"]
        game.size_bytes = parse_size(info["game size"])
    if "version" in info:
        game.version = info["version"].lstrip(": ").strip()
    for k in ("developer", "publisher", "released by", "release date"):
        if k in info:
            game.extra[k.replace(" ", "_")] = info[k]
    if "os" in info:
        game.extra["requirements_os"] = info["os"]
    m = _STEAM_APP.search(content_html)
    if m:
        game.steam_app_id = int(m.group(1))

    if generic:                                # a user-added WordPress site: any download links in the post
        game.download_methods = download_links(soup)
        game.details_checked_at = time.time()
        return game
    methods: list[DownloadMethod] = []
    seen = set()
    buttons = soup.select("a.shortc-button[href]") or [
        a for a in soup.select("a[href]") if "download here" in a.get_text(" ", strip=True).lower()]
    for a in buttons:
        href = a["href"].strip()
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        methods.append(classify_host_link(href))
    game.download_methods = methods
    game.details_checked_at = time.time()
    return game


class SteamRipAdapter(SourceAdapter):
    info = SourceInfo(
        id="steamrip",
        name="SteamRIP",
        homepage=BASE,
        description="Pre-installed PC game archives (steamrip.com).",
        capabilities={"search", "pagination", "genres"},
        notes=[
            "SteamRIP's robots.txt disallows crawlers, so the app only fetches what you browse or search "
            "for, with caching and rate limits; it never indexes the whole site in the background.",
            "SteamRIP links to file hosts that need a browser (Cloudflare challenges, captchas or account "
            "tokens). Open the link, then add the final file with 'Add download' or import the archive.",
        ],
        page_size=PAGE_SIZE,
        background_ok=False,
    )

    def __init__(self, fetcher, base: str = BASE):
        super().__init__(fetcher)
        self.base = base.rstrip("/")
        self.api = f"{self.base}/wp-json/wp/v2"
        self._categories: dict[int, str] | None = None
        self.generic = False                      # True for user-added WordPress sites (generic link finding)

    async def _json(self, path: str, params: dict, ttl: float | None = None) -> tuple[bytes, dict, bool]:
        url = f"{self.api}/{path}?{urlencode(params)}"
        res = await self.fetcher.get(url, ttl=ttl)
        if res.status == 400:
            # WordPress answers 400 for pages past the end
            return res.body, res.headers, res.stale
        if res.status == 404:
            raise SourceError(f"{self.info.name} returned 404 (not found)")
        if res.status in (401, 403):
            raise SourceError(f"{self.info.name} refused the request (HTTP {res.status})")
        if res.status >= 400:
            raise SourceError(f"{self.info.name} returned HTTP {res.status}")
        return res.body, res.headers, res.stale

    async def categories(self) -> dict[int, str]:
        if self._categories is None:
            body, _, _ = await self._json("categories", {"per_page": 100, "_fields": "id,name,slug,count"},
                                          ttl=24 * 3600)
            try:
                data = json.loads(body)
                self._categories = {int(c["id"]): htmllib.unescape(c["name"]) for c in data if isinstance(c, dict)}
            except (ValueError, KeyError, TypeError) as exc:
                raise LayoutChangedError(f"{self.info.name} categories response changed") from exc
        return self._categories

    async def genres(self) -> list[str]:
        cats = await self.categories()
        return sorted({g for g in (canonical_genre(n) for n in cats.values()) if g})

    async def _page(self, body: bytes, headers: dict, page: int, stale: bool, cats: dict[int, str]) -> Page:
        games = await parsing.parse(parse_posts, body, cats, self.info.id)
        total = headers.get("x-wp-total")
        pages = headers.get("x-wp-totalpages")
        total_i = int(total) if total and total.isdigit() else None
        has_more = int(pages) > page if pages and pages.isdigit() else len(games) >= PAGE_SIZE
        return Page(games, page, has_more and bool(games), total_i, stale)

    async def latest(self, page: int = 1, genre: str | None = None) -> Page:
        cats = await self.categories()
        params = {"per_page": PAGE_SIZE, "page": max(1, page), "_embed": "wp:featuredmedia", "_fields": LIST_FIELDS}
        if genre:
            ids = [cid for cid, name in cats.items() if canonical_genre(name) == genre]
            if not ids:
                return Page([], page, False)
            params["categories"] = ",".join(map(str, ids))
        body, headers, stale = await self._json("posts", params)
        return await self._page(body, headers, page, stale, cats)

    async def search(self, query: str, page: int = 1) -> Page:
        query = query.strip()
        if len(query) < 2:
            return Page([], page, False)
        cats = await self.categories()
        params = {"search": query, "search_columns": "post_title", "per_page": PAGE_SIZE, "page": max(1, page),
                  "_embed": "wp:featuredmedia", "_fields": LIST_FIELDS}
        body, headers, stale = await self._json("posts", params, ttl=1800)
        return await self._page(body, headers, page, stale, cats)

    async def details(self, game: Game) -> Game:
        if (urlsplit(game.url).hostname or "").lower() not in SITE_HOSTS | {urlsplit(self.base).hostname}:
            raise SourceError(f"refusing to fetch a URL that is not on {self.info.name}")
        if not game.source_game_id.isdigit():
            raise SourceError(f"invalid {self.info.name} post id")
        cats = await self.categories()
        body, _, _ = await self._json(f"posts/{game.source_game_id}",
                                      {"_embed": "wp:featuredmedia",
                                       "_fields": LIST_FIELDS + ",content"}, ttl=1800)
        try:
            post = json.loads(body)
        except ValueError as exc:
            raise LayoutChangedError(f"{self.info.name} API did not return JSON") from exc
        if not isinstance(post, dict) or "content" not in post:
            raise LayoutChangedError(f"{self.info.name} post response has no content")
        fresh = parse_post(post, cats, self.info.id)
        if fresh:
            game.merge_listing(fresh)
        return await parsing.parse(parse_content, (post.get("content") or {}).get("rendered") or "", game,
                                       self.generic)

    async def resolve(self, method: DownloadMethod, game: Game) -> ResolvedDownload:
        if method.resolver == "pixeldrain":
            return await resolve_pixeldrain(self.fetcher, method)
        if method.kind is MethodKind.EXTERNAL:
            raise ManualActionRequired(method.note or "Open this link in your browser.", method.url)
        raise SourceError("unsupported download method")
