"""cracked-games.org adapter (https://cracked-games.org).

Verified against the live site on 2026-10-09:

* Laravel/Livewire site behind Cloudflare. robots.txt allows ``/`` and the
  ``/games`` listing with its filter/sort/pagination parameters, but disallows
  ``/download``, ``/search``, ``/api/``, ``/livewire/`` and account pages.
  This adapter only requests ``/games?page=N``, ``/games?search=…``,
  ``/games/genre/<slug>`` and game pages (``/<slug>-free-download``), which
  robots.txt explicitly allows with ``Allow: /*-free-download`` (the longer,
  more specific rule also wins for slugs that begin with "download-").
* Listing cards (``div.gc``) carry title, cover, genre, size and build; adult
  games are marked "18+" and blurred by the site itself.
* Game pages publish JSON-LD (genres, dates, OS, developer), the release
  group, "Current Release" build, the archive password and CRC32/SHA-256
  checksums of the archive.
* Downloads are a POST form with a CSRF token and a signed, time-limited
  token, and ``/download`` is disallowed by robots.txt. The app therefore
  never downloads from this site: the game page is opened in the browser and
  the finished file can be imported (its SHA-256 is checked against the
  published value).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from ezgd.catalog.normalize import canonical_genre
from ezgd.core.fsutil import parse_size
from ezgd.sources.base import (LayoutChangedError, ManualActionRequired, Page, ResolvedDownload,
                               SourceAdapter, SourceError, SourceInfo)
from ezgd.sources import parsing

log = logging.getLogger(__name__)

BASE = "https://cracked-games.org"
SITE_HOSTS = {"cracked-games.org", "www.cracked-games.org"}
PASSWORD = "cracked-games.org"
PAGE_SIZE = 30
# genre slugs offered by the site's /games/genre/<slug> pages
GENRE_SLUGS = {"action": "Action", "adventure": "Adventure", "casual": "Casual", "indie": "Indie",
               "racing": "Racing", "rpg": "RPG", "simulation": "Simulation"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,200}-free-download$")
_SIZE = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:[KMGT]i?B)$", re.I)
_BUILD = re.compile(r"^(?:Build|v(?:ersion)?)\s*\S+", re.I)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.I)
_CRC32 = re.compile(r"^[0-9a-f]{8}$", re.I)
_RUN_EXE = re.compile(r"^[\w .()\-+\\/]+\.exe$", re.I)
_UPLOADED = re.compile(r"^Uploaded\s+(.+)$", re.I)
BROWSER_NOTE = ("cracked-games.org only hands out files through a signed form on the game page (and asks "
                "automated tools not to use it), so the app cannot download it for you. Click “Download Now” "
                "on the page in your browser, then use “Import downloaded file” here — the app checks the "
                "file against the SHA-256 the site publishes and extracts it with the archive password.")


def _genre(label: str) -> str | None:
    label = (label or "").strip()
    if not label:
        return None
    if re.fullmatch(r"role-playing \(rpg\)", label, re.I):
        return "RPG"
    return canonical_genre(label)


def _date(raw: str | None) -> float | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def slug_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in SITE_HOSTS:
        return None
    slug = parts.path.strip("/")
    return slug if _SLUG.match(slug) else None


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_card(card, base: str = BASE) -> Game | None:
    a = card.select_one('a[href$="-free-download"]')
    if a is None:
        return None
    url = urljoin(base + "/", a["href"])
    slug = slug_from_url(url) or urlsplit(url).path.strip("/")
    if not _SLUG.match(slug):
        return None
    img = card.select_one("img")
    h3 = card.select_one("h3")
    title = (h3.get_text(" ", strip=True) if h3 else "") or (img.get("alt", "").strip() if img else "")
    if not title:
        return None
    game = Game(source="crackedgames", source_game_id=slug, title=title, raw_title=title, url=url,
                cover_url=urljoin(base + "/", img["src"]) if img and img.get("src") else None)
    tokens = list(card.stripped_strings)
    game.extra["adult"] = "18+" in tokens or "NSFW" in tokens
    meta = h3.find_next_sibling("div") if h3 else None
    for t in (list(meta.stripped_strings) if meta else tokens):
        if _SIZE.match(t) and not game.size_text:
            game.size_text = t
            game.size_bytes = parse_size(t)
        elif _BUILD.match(t) and not game.version:
            game.version = t
    if meta:
        first = meta.select_one("span")
        g = _genre(first.get_text(strip=True)) if first else None
        if g and not _SIZE.match(first.get_text(strip=True)):
            game.genres = [g]
    return game


def parse_listing(html: str, base: str = BASE) -> tuple[list[Game], bool]:
    soup = _soup(html)
    if soup.body is None:
        raise LayoutChangedError("cracked-games.org returned an empty page")
    cards = soup.select("div.gc")
    games, seen = [], set()
    for c in cards:
        g = parse_card(c, base)
        if g and g.source_game_id not in seen:
            seen.add(g.source_game_id)
            games.append(g)
    if cards and not games:
        raise LayoutChangedError("cracked-games.org game cards no longer match the expected layout")
    has_more = any(re.search(r"[?&]page=\d+", a["href"]) and a.get_text(strip=True).lower() in ("next", "›", "»")
                   for a in soup.select("a[href]"))
    if not has_more:
        has_more = bool(soup.select_one('a[rel="next"]')) or _next_page_link(soup)
    return games, has_more and bool(games)


def _next_page_link(soup) -> bool:
    current = 1
    cur = soup.select_one('[aria-current="page"]')
    if cur and cur.get_text(strip=True).isdigit():
        current = int(cur.get_text(strip=True))
    for a in soup.select("a[href]"):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m and int(m.group(1)) > current:
            return True
    return False


def _value_after(tokens: list[str], label: str, check=None) -> str | None:
    for i, t in enumerate(tokens):
        if t.strip().lower() == label.lower():
            for nxt in tokens[i + 1:i + 4]:
                if check is None or check(nxt):
                    return nxt.strip()
    return None


def parse_details(html: str, game: Game) -> Game:
    soup = _soup(html)
    main = soup.select_one("main")
    h1 = (main or soup).select_one("h1")
    if main is None or h1 is None:
        raise LayoutChangedError("cracked-games.org game page has no title")
    game.title = h1.get_text(" ", strip=True) or game.title
    ld = {}
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "VideoGame":
            ld = data
            break
    genres = [g for g in (_genre(x) for x in (ld.get("genre") or [])) if g]
    if genres:
        game.genres = list(dict.fromkeys(genres))
    if ld.get("operatingSystem"):
        game.platforms = [p.strip() for p in str(ld["operatingSystem"]).split(",") if p.strip()]
    if ld.get("datePublished"):
        game.release_date = ld["datePublished"]
        game.published_at = _date(ld["datePublished"]) or game.published_at
    if ld.get("dateModified"):
        game.updated_at = _date(ld["dateModified"]) or game.updated_at
    for k in ("author", "publisher"):
        who = ld.get(k)
        if isinstance(who, dict) and who.get("name"):
            game.extra["developer" if k == "author" else "publisher"] = who["name"]
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content") and not game.cover_url:
        game.cover_url = og["content"]

    about = main.select_one('[x-ref="aboutContent"]')
    if about is None:
        h = next((h for h in main.find_all(["h2", "h3"]) if "about the game" in h.get_text().lower()), None)
        about = h.find_parent("div").find_parent("div") if h and h.find_parent("div") else None
    if about is not None:
        paras = [p.get_text(" ", strip=True) for p in about.find_all("p")]
        text = "\n\n".join(p for p in paras if p)
        if text:
            game.description = text[:3000]
    if not game.description and ld.get("description"):
        game.description = ld["description"]

    tokens = list(main.stripped_strings)
    build = _value_after(tokens, "Current Release")
    if build:
        game.version = build
    for t in tokens:
        m = _UPLOADED.match(t)
        if m:
            game.extra["uploaded"] = m.group(1).strip()
            break
    for i, t in enumerate(tokens[:-1]):
        # "How to Install … 3 · Run · Game\start_protected_game.exe · to play" (generic wording is ignored)
        if t == "Run" and _RUN_EXE.match(tokens[i + 1]):
            game.extra["launch_exe_hint"] = tokens[i + 1].strip()
            break
    sizes = [t for t in tokens if _SIZE.match(t)]
    btn = next((re.search(r"\(([^()]+)\)\s*$", t) for t in tokens if t.lower().startswith("download (")), None)
    size = btn.group(1) if btn and _SIZE.match(btn.group(1)) else (sizes[0] if sizes else None)
    if size:
        game.size_text = size
        game.size_bytes = parse_size(size)
    password = _value_after(tokens, "Archive password")
    if password and password.lower() != "copy":
        game.extra["archive_password"] = password
    sha = _value_after(tokens, "SHA-256", lambda t: bool(_SHA256.match(t.strip())))
    crc = _value_after(tokens, "CRC32", lambda t: bool(_CRC32.match(t.strip())))
    if sha:
        game.extra["sha256"] = sha.lower()
    if crc:
        game.extra["crc32"] = crc.upper()
    mp = next((t for t in tokens if t.lower().startswith("multiplayer ·")), None)
    if mp:
        i = tokens.index(mp)
        detail = " ".join(tokens[i + 2:i + 5]).strip()
        game.extra["multiplayer"] = f"{mp}: {detail}" if detail else mp
    group = main.select_one(".fa-check-circle + span")
    if group and group.get_text(strip=True):
        game.extra["release_group"] = group.get_text(strip=True)
    if any(t == "18+" for t in tokens[:40]):
        game.extra["adult"] = True
    game.extra.setdefault("adult", False)

    has_form = bool(soup.select_one('form[action$="/download"]')) or "Download Now" in tokens
    game.download_methods = [DownloadMethod(
        kind=MethodKind.EXTERNAL, label="Download in your browser", url=game.url, host="cracked-games.org",
        supported=False, note=BROWSER_NOTE, size_bytes=game.size_bytes,
        checksum=f"sha256:{game.extra['sha256']}" if sha else None,
        archive_password=game.extra.get("archive_password") or PASSWORD)] if has_form else []
    game.details_checked_at = time.time()
    return game


class CrackedGamesAdapter(SourceAdapter):
    info = SourceInfo(
        id="crackedgames",
        name="cracked-games.org",
        homepage=BASE,
        description="Pre-installed PC game archives with published checksums (cracked-games.org).",
        capabilities={"search", "pagination", "genres"},
        notes=[
            "Downloads on cracked-games.org go through a signed form on the game page, so they happen in your "
            "browser. Afterwards, import the file from the game's page: the app verifies its SHA-256 against "
            "the value the site publishes and extracts it.",
            "Every archive uses the password “cracked-games.org”.",
            "Games the site marks 18+ are hidden unless you turn them on in Settings.",
        ],
        page_size=PAGE_SIZE,
        archive_password=PASSWORD,
    )

    def __init__(self, fetcher, base: str = BASE):
        super().__init__(fetcher)
        self.base = base.rstrip("/")

    async def _get(self, path: str, params: dict | None = None, ttl: float | None = None) -> tuple[str, bool]:
        url = f"{self.base}{path}" + (f"?{urlencode(params)}" if params else "")
        res = await self.fetcher.get(url, ttl=ttl)
        if res.status == 404:
            raise SourceError("cracked-games.org returned 404 (not found)")
        if res.status in (401, 403):
            raise SourceError(f"cracked-games.org refused the request (HTTP {res.status}); the site may be "
                              "showing a browser check — open it in your browser.")
        if res.status >= 400:
            raise SourceError(f"cracked-games.org returned HTTP {res.status}")
        return res.text(), res.stale

    async def genres(self) -> list[str]:
        return sorted(GENRE_SLUGS.values())

    async def latest(self, page: int = 1, genre: str | None = None) -> Page:
        page = max(1, page)
        params = {"page": page} if page > 1 else None
        if genre:
            slug = next((s for s, name in GENRE_SLUGS.items() if name == genre), None)
            if slug is None:
                return Page([], page, False)
            html, stale = await self._get(f"/games/genre/{slug}", params)
        else:
            html, stale = await self._get("/games", params)
        games, more = await parsing.parse(parse_listing, html, self.base)
        return Page(games, page, more, None, stale)

    async def search(self, query: str, page: int = 1) -> Page:
        query = query.strip()
        if len(query) < 2:
            return Page([], page, False)
        params = {"search": query}
        if page > 1:
            params["page"] = page
        # /games?search= is the allowed listing filter; /search itself is disallowed by robots.txt
        html, stale = await self._get("/games", params, ttl=1800)
        games, more = await parsing.parse(parse_listing, html, self.base)
        return Page(games, page, more, None, stale)

    async def details(self, game: Game) -> Game:
        slug = slug_from_url(game.url)
        if slug is None or slug != game.source_game_id:
            raise SourceError("refusing to fetch a non-cracked-games.org URL")
        html, _ = await self._get(f"/{slug}", ttl=1800)
        return await parsing.parse(parse_details, html, game)

    async def resolve(self, method: DownloadMethod, game: Game) -> ResolvedDownload:
        raise ManualActionRequired(method.note or BROWSER_NOTE, game.url)
