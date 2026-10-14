"""AnkerGames adapter (https://ankergames.net).

Verified against the live site on 2026-10-11:

* Laravel/Livewire site behind Cloudflare (pages are served normally). It is not WordPress, so it cannot be
  added as a user site; this adapter reads it directly.
* robots.txt allows the listings (``/games?page=N``, ``/genre/<slug>``), game pages (``/game/<slug>``) and the
  sitemaps, and disallows ``/search/``, ``/download/``, ``/torrent/``, ``/torrent-file/`` and ``/livewire/``.
  This adapter requests only allowed paths: search is answered from the site's own sitemaps (every game's
  slug and poster), never from ``/search/``.
* Listing cards (``<article>``) carry title, poster, version/build, first genre, year and size.
* Game pages publish JSON-LD ``VideoGame`` (description, genres, dates, size, version, posters) plus release
  group, developer, publisher, Steam link and an install guide ("launch … Game.exe").
* Downloads open in a window on the game page that loads through Livewire and ``/download/`` — both
  disallowed for tools — so the app never downloads from AnkerGames itself: the game page opens in the
  browser, the user clicks Download there, and the finished file joins the game in the Library.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from ezgd.catalog.normalize import canonical_genre, tokens
from ezgd.core.fsutil import parse_size
from ezgd.sources.base import (LayoutChangedError, ManualActionRequired, Page, ResolvedDownload, SourceAdapter,
                               SourceError, SourceInfo)
from ezgd.sources import parsing

log = logging.getLogger(__name__)

BASE = "https://ankergames.net"
SITE_HOSTS = {"ankergames.net", "www.ankergames.net"}
PAGE_SIZE = 56
GENRE_SLUGS = {"action": "Action", "adventure": "Adventure", "anime": "Anime", "classic": "Classic",
               "fighting": "Fighting", "history": "History", "horror": "Horror", "indie": "Indie",
               "multiplayer": "Multiplayer", "open-world": "Open World", "puzzle": "Puzzle", "racing": "Racing",
               "rpg": "RPG", "simulation": "Simulation", "sports": "Sports", "survival": "Survival", "vr": "VR"}
SITEMAPS = ("/sitemap_image_1.xml", "/sitemap_image_2.xml", "/sitemap_image_3.xml", "/sitemap_image_4.xml")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,200}$")
_SIZE = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:[KMGT]i?B)$", re.I)
_VERSION = re.compile(r"^(?:V|B|v|Build)\s?[\w.\-]+$")
_YEAR = re.compile(r"^(19|20)\d{2}$")
_EXE = re.compile(r"((?:[\w.\-]+[/\\])*[\w.\-]+\.exe)\b", re.I)
_CARD_GENRES = set(GENRE_SLUGS.values()) | {"Shooter", "Strategy", "Platformer", "Casual"}
_ADULT = {"nsfw", "sexual content", "nudity", "hentai", "adult"}
BROWSER_NOTE = ("AnkerGames hands out its files through the download window on the game page (and asks automated "
                "tools not to use its download links), so the app cannot download it for you. Click “Download” "
                "on the page — in the app's browser the file then joins this game in your Library.")


def slug_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in SITE_HOSTS:
        return None
    m = re.fullmatch(r"/game/([a-z0-9][a-z0-9-]*)/?", parts.path)
    return m.group(1) if m and _SLUG.match(m.group(1)) else None


def _genre(label: str) -> str | None:
    label = (label or "").strip()
    if not label or label.lower() in ("singleplayer", "atmospheric", "first-person", "third-person", "2d", "3d"):
        return None
    if label.upper() == "FPS":
        return "Shooter"
    return canonical_genre(label)


def _date(raw: str | None) -> float | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def title_from_slug(slug: str) -> str:
    small = {"of", "the", "and", "a", "an", "in", "on", "to", "for", "at", "by"}
    roman = re.compile(r"^(i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)$")
    words = slug.split("-")
    out = []
    for i, w in enumerate(words):
        if roman.match(w):
            out.append(w.upper())
        elif i and w in small:
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def parse_card(card, base: str = BASE) -> Game | None:
    a = card.select_one('a[href*="/game/"]')
    if a is None:
        return None
    url = urljoin(base + "/", a["href"])
    slug = slug_from_url(url)
    if slug is None:
        return None
    img = card.select_one("img")
    toks = list(card.stripped_strings)
    title = (img.get("alt", "").strip() if img else "") or (toks[0] if toks else "")
    if not title:
        return None
    g = Game(source="ankergames", source_game_id=slug, title=title, raw_title=title, url=f"{BASE}/game/{slug}",
             cover_url=urljoin(base + "/", img["src"]) if img and img.get("src") else None)
    for t in toks:
        if _SIZE.match(t) and not g.size_text:
            g.size_text, g.size_bytes = t, parse_size(t)
        elif _VERSION.match(t) and not g.version and t != title:
            g.version = t
        elif _YEAR.match(t) and not g.release_date:
            g.release_date = t
        elif not g.genres and t != title:
            ge = _genre(t)
            if ge and ge in _CARD_GENRES:
                g.genres = [ge]
    g.extra["adult"] = any(t.lower() in _ADULT for t in toks)
    return g


def parse_listing(html: str, base: str = BASE) -> tuple[list[Game], bool]:
    soup = BeautifulSoup(html, "lxml")
    if soup.body is None:
        raise LayoutChangedError("AnkerGames returned an empty page")
    cards = [c for c in soup.select("article") if c.select_one('a[href*="/game/"]')]
    games, seen = [], set()
    for c in cards:
        g = parse_card(c, base)
        if g and g.source_game_id not in seen:
            seen.add(g.source_game_id)
            games.append(g)
    if cards and not games:
        raise LayoutChangedError("AnkerGames game cards no longer match the expected layout")
    current = 1
    pages = [int(m.group(1)) for a in soup.select('a[href*="page="]') if (m := re.search(r"[?&]page=(\d+)", a["href"]))]
    cur = soup.select_one('[aria-current="page"]')
    if cur and cur.get_text(strip=True).isdigit():
        current = int(cur.get_text(strip=True))
    has_more = bool(pages) and max(pages) > current
    return games, has_more and bool(games)


def _ld(soup: BeautifulSoup) -> dict:
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or s.get_text() or "", strict=False)
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if isinstance(item, dict) and item.get("@type") == "VideoGame":
                return item
    return {}


def _after(tokens_: list[str], label: str) -> str | None:
    for i, t in enumerate(tokens_):
        if t.strip().lower() == label.lower():
            for nxt in tokens_[i + 1:i + 4]:
                if nxt.strip() not in ("/", "·", "•", ":"):
                    return nxt.strip()
    return None


def parse_details(html: str, game: Game) -> Game:
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one("main") or soup.body
    ld = _ld(soup)
    if main is None or (not ld and not soup.select_one("h1")):
        raise LayoutChangedError("AnkerGames game page has no game details")
    game.title = (ld.get("name") or (soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1")
                                     else "") or game.title).strip()
    if ld.get("description"):
        game.description = re.sub(r"\s+\n", "\n", str(ld["description"])).strip()[:3000]
    images = ld.get("image") or []
    images = [images] if isinstance(images, str) else images
    poster = next((i for i in images if "/poster/" in i), images[0] if images else None)
    og = soup.select_one('meta[property="og:image"]')
    game.cover_url = poster or (og["content"] if og and og.get("content") else game.cover_url)
    genres = [g for g in (_genre(x) for x in (ld.get("genre") or [])) if g]
    if genres:
        game.genres = list(dict.fromkeys(genres))
    raw_genres = [str(x).lower() for x in (ld.get("genre") or [])]
    if ld.get("fileSize"):
        game.size_text = str(ld["fileSize"])
        game.size_bytes = parse_size(game.size_text)
    if ld.get("softwareVersion"):
        game.version = str(ld["softwareVersion"])
    if ld.get("datePublished"):
        game.release_date = str(ld["datePublished"])
    if ld.get("dateModified"):
        game.updated_at = _date(str(ld["dateModified"])) or game.updated_at
    if ld.get("operatingSystem"):
        game.platforms = [p.strip() for p in str(ld["operatingSystem"]).split(",") if p.strip()]
    toks = list(main.stripped_strings)
    for label_, key in (("Release Group", "release_group"), ("Developer", "developer"), ("Publisher", "publisher")):
        v = _after(toks, label_)
        if v:
            game.extra[key] = v
    build = _after(toks, "Current Build")
    if build and build.isdigit():
        game.extra["build"] = build
    m = re.search(r"store\.steampowered\.com/app/(\d+)", html)
    if m:
        game.steam_app_id = int(m.group(1))
    guide = next((h for h in main.find_all(["h2", "h3"]) if "installation guide" in h.get_text().lower()), None)
    if guide is not None:
        box = guide.parent              # climb to the block that holds the steps (not just the heading)
        while box is not None and box.name != "main" and len(box.get_text(" ", strip=True)) < 150:
            box = box.parent
        text = re.sub(r"([/\\])\s+", r"\1", box.get_text(" ", strip=True))[:6000] if box is not None else ""
        if "pre-installed" in text.lower() or "portable" in text.lower():
            game.extra["preinstalled"] = True
        launch = re.search(r"launch the game from (.+?)(?: inside| in the|\.\s|$)", text, re.I)
        exes = _EXE.findall(launch.group(1)) if launch else []
        if exes:
            game.extra["launch_exe_hint"] = exes[-1]
    game.extra["adult"] = any(g in _ADULT for g in raw_genres)
    game.download_methods = [DownloadMethod(
        kind=MethodKind.EXTERNAL, label="Download in your browser", url=game.url, host="ankergames.net",
        supported=False, note=BROWSER_NOTE, size_bytes=game.size_bytes)]
    game.details_checked_at = time.time()
    return game


def parse_sitemap(xml: str) -> list[tuple[str, str | None]]:
    """(slug, poster URL) for every game in one of the site's image sitemaps."""
    out = []
    for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block)
        slug = slug_from_url(loc.group(1)) if loc else None
        if not slug:
            continue
        imgs = re.findall(r"<image:loc>\s*([^<\s]+)\s*</image:loc>", block)
        poster = next((i for i in imgs if "/poster/" in i), imgs[0] if imgs else None)
        out.append((slug, poster))
    return out


def search_index(index: list[tuple[str, str | None]], query: str) -> list[tuple[str, str | None]]:
    """Slugs whose words contain every word of the query (the last one may be unfinished), best first."""
    q = tokens(query)
    if not q:
        return []
    hits = []
    for slug, poster in index:
        words = slug.split("-")
        if all(w in words for w in q[:-1]) and any(w.startswith(q[-1]) for w in words):
            exact = "-".join(q) == slug
            prefix = slug.startswith("-".join(q))
            hits.append((not exact, not prefix, len(words), slug, poster))
    hits.sort()
    return [(h[3], h[4]) for h in hits]


class AnkerGamesAdapter(SourceAdapter):
    info = SourceInfo(
        id="ankergames",
        name="AnkerGames",
        homepage=BASE,
        description="Pre-installed PC games with install guides and tested builds (ankergames.net).",
        capabilities={"search", "pagination", "genres"},
        notes=[
            "AnkerGames hands out files through a download window on the game page, which it asks tools not to "
            "use, so downloads happen in the browser. In the app's browser the file joins the game in your Library.",
            "Search uses the site's own game list (its sitemaps), because the site asks tools not to use its "
            "search page.",
            "Games the site marks NSFW are hidden unless you turn them on in Settings.",
        ],
        page_size=PAGE_SIZE,
    )

    def __init__(self, fetcher, base: str = BASE):
        super().__init__(fetcher)
        self.base = base.rstrip("/")
        self._robots: RobotFileParser | None = None
        self._index_cache: tuple[float, list[tuple[str, str | None]]] | None = None

    async def _allowed(self, url: str) -> bool:
        if self._robots is None:
            rp = RobotFileParser()
            try:
                res = await self.fetcher.get(self.base + "/robots.txt", ttl=24 * 3600)
                rp.parse(res.text().splitlines() if res.status == 200 else [])
            except Exception:                    # robots.txt unreachable: rely on the built-in path rules
                rp.parse([])
            self._robots = rp
        return self._robots.can_fetch("*", url)

    async def _get(self, path: str, params: dict | None = None, ttl: float | None = None) -> tuple[str, bool]:
        url = f"{self.base}{path}" + (f"?{urlencode(params)}" if params else "")
        if re.match(r"^/(search|download|torrent|torrent-file|livewire)", path) or not await self._allowed(url):
            raise SourceError("AnkerGames asks tools not to read that page (robots.txt), so the app does not.")
        res = await self.fetcher.get(url, ttl=ttl)
        if res.status == 404:
            raise SourceError("AnkerGames returned 404 (not found)")
        if res.status in (401, 403, 503):
            raise SourceError(f"AnkerGames refused the request (HTTP {res.status}); the site may be showing a "
                              "browser check — open it in your browser.")
        if res.status >= 400:
            raise SourceError(f"AnkerGames returned HTTP {res.status}")
        return res.text(), res.stale

    async def genres(self) -> list[str]:
        return sorted(GENRE_SLUGS.values())

    async def latest(self, page: int = 1, genre: str | None = None) -> Page:
        page = max(1, page)
        params = {"page": page} if page > 1 else None
        if genre:
            slug = next((s for s, n in GENRE_SLUGS.items() if n == genre), None)
            if slug is None:
                return Page([], page, False)
            html, stale = await self._get(f"/genre/{slug}", params)
        else:
            html, stale = await self._get("/games", params)
        games, more = await parsing.parse(parse_listing, html, self.base)
        return Page(games, page, more, None, stale)

    async def _index(self) -> list[tuple[str, str | None]]:
        if self._index_cache and time.time() - self._index_cache[0] < 3600:
            return self._index_cache[1]             # parsing ~4 MB of sitemaps for every search is wasteful
        out: list[tuple[str, str | None]] = []
        for path in SITEMAPS:
            try:
                xml, _ = await self._get(path, ttl=24 * 3600)
            except SourceError:
                if out:
                    break                         # fewer sitemaps than expected
                raise
            found = await parsing.parse(parse_sitemap, xml)
            if not found:
                break
            out += found
        if out:
            self._index_cache = (time.time(), out)
        return out

    async def search(self, query: str, page: int = 1) -> Page:
        query = query.strip()
        if len(query) < 2:
            return Page([], page, False)
        hits = search_index(await self._index(), query)
        start = (max(1, page) - 1) * 30
        games = [Game(source="ankergames", source_game_id=slug, title=title_from_slug(slug),
                      raw_title=title_from_slug(slug), url=f"{BASE}/game/{slug}", cover_url=poster)
                 for slug, poster in hits[start:start + 30]]
        return Page(games, page, len(hits) > start + 30, len(hits))

    async def details(self, game: Game) -> Game:
        slug = slug_from_url(game.url)
        if slug is None or slug != game.source_game_id:
            raise SourceError("refusing to fetch a URL that is not an AnkerGames game page")
        html, _ = await self._get(f"/game/{slug}", ttl=1800)
        return await parsing.parse(parse_details, html, game)

    async def resolve(self, method: DownloadMethod, game: Game) -> ResolvedDownload:
        raise ManualActionRequired(method.note or BROWSER_NOTE, game.url)
