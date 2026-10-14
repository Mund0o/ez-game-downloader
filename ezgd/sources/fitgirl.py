"""FitGirl Repacks adapter (https://fitgirl-repacks.site).

Verified against the live site on 2026-10-11:

* WordPress site (behind DDoS-Guard, pages served normally) with the standard public REST API
  (``/wp-json/wp/v2/posts``, ``/categories``, ``/tags``) and ``X-WP-Total``/``X-WP-TotalPages`` headers.
  robots.txt allows everything (``Disallow:`` empty).
* Category 5 "Lossless Repack" holds the game posts; other categories are site news ("Updates Digest",
  "Upcoming Repacks", "Uncategorized"), Russian movies and awards — those are never listed. Games also in
  "Hypervisor Bypass" need a kernel-level Denuvo bypass on Windows and do not run under Proton; they are
  listed with a warning. "Switch Emulated"/"PS3 Emulated" posts bundle an emulator.
* Posts have no featured image; the cover is the first image of the post. The info paragraph lists
  "Genres/Tags", "Company/Companies", "Languages", "Original Size" and "Repack Size"; titles look like
  ``Game – v1.2.3 + 4 DLCs`` or ``Game, v1.03 + 5 DLCs/Bonuses``.
* Downloads: a magnet link per post (downloaded in the app with the torrent engine) plus direct file-host
  links (DataNodes, FuckingFast, FileKeeper …) split into ``.partN.rar`` files; those hosts block apps, so
  they open in the in-app browser.
* A repack is not a ready-to-play folder: it is ``setup.exe`` plus compressed ``fg-*.bin`` data. The game
  has to be installed by running setup.exe first — see :mod:`ezgd.library.repack`, which the Library uses
  for that step (only when the user presses Install).
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from ezgd.core.fsutil import parse_size
from ezgd.sources.base import (LayoutChangedError, ManualActionRequired, Page, ResolvedDownload, SourceError,
                               SourceInfo)
from ezgd.sources.hosts import HOST_LABELS, classify_host_link
from ezgd.sources.steamrip import PAGE_SIZE, SteamRipAdapter, _wp_time
from ezgd.sources import parsing

log = logging.getLogger(__name__)

BASE = "https://fitgirl-repacks.site"
SITE_HOSTS = {"fitgirl-repacks.site", "www.fitgirl-repacks.site"}
CAT_REPACK = 5
CAT_HYPERVISOR = 319
CAT_SWITCH = 43
CAT_PS3 = 42
LIST_FIELDS = "id,date,date_gmt,modified,modified_gmt,slug,link,title,categories,tags,content"
# FitGirl tags that are genres (the rest describe the camera, art style or setting: 3D, Side, Isometric …)
GENRE_TAGS = {"action": "Action", "adventure": "Adventure", "rpg": "RPG", "action rpg": "RPG", "strategy": "Strategy",
              "horror": "Horror", "shooter": "Shooter", "racing": "Racing", "simulation": "Simulation",
              "sports": "Sports", "puzzle": "Puzzle", "logic": "Puzzle", "survival": "Survival",
              "arcade": "Arcade", "fighting": "Fighting", "platform": "Platformer", "jump and run": "Platformer",
              "platform adventure": "Platformer", "stealth": "Stealth", "open world": "Open World",
              "sandbox": "Sandbox", "managerial": "Management", "management": "Management", "tactics": "Strategy",
              "turn-based": "Strategy", "real-time": "Strategy", "visual novel": "Visual Novel",
              "driving": "Racing", "beat 'em up (brawler)": "Fighting", "casual": "Casual", "indie": "Indie"}
HYPERVISOR_WARNING = ("This repack needs a “hypervisor bypass” for its Denuvo protection: on Windows you have to "
                      "switch off security features (VBS, driver signature checks) and load a kernel driver. It does "
                      "not run under Proton/Wine on Linux. Only use it if you understand the risks.")
INSTALL_NOTE = ("FitGirl repacks are installers: after the download, press “Install game” in your Library. The "
                "app runs the repack's setup.exe (with Proton on Linux) and then finds the installed game for Play.")
_VERSION = re.compile(r"(?:\s+[–—-]\s+|,\s+)((?:v|build|b|r|update|patch)\s?\d.*)$", re.I)
_EXTRAS = re.compile(r"\s+\+\s+(.+)$")
_TAG = re.compile(r"\s*\[([^\]]{2,40})\]\s*$")
_FIELD = re.compile(r"^(Genres/Tags|Companies|Company|Languages|Original Size|Repack Size)\s*:\s*(.+)$", re.I)
_NUMBER = re.compile(r"^#(\d+)")


def split_title(raw: str) -> tuple[str, str | None, str | None]:
    """(title, version + extras, tag) from ``HOT WHEELS Infinite Rush – v1859562 + 6 DLCs [Monkey Repack]``."""
    t = htmllib.unescape(raw or "").replace("’", "'").strip()
    tag = None
    m = _TAG.search(t)
    if m:
        tag, t = m.group(1).strip(), t[:m.start()].strip()
    m = _VERSION.search(t)
    if m:
        return t[:m.start()].strip(" –—-,"), m.group(1).strip(), tag
    m = _EXTRAS.search(t)
    if m and not re.search(r"\+\s*$", t[:m.start()]):
        return t[:m.start()].strip(" –—-,"), "+ " + m.group(1).strip(), tag
    return t, None, tag


def _genres_from_names(names: list[str]) -> list[str]:
    out = []
    for n in names:
        g = GENRE_TAGS.get(n.strip().lower())
        if g and g not in out:
            out.append(g)
    return out


def _cover(soup: BeautifulSoup) -> str | None:
    for img in soup.select("img[src]"):
        src = img["src"].strip()
        if src.startswith("//"):
            src = "https:" + src
        host = (urlsplit(src).hostname or "").lower()
        if src.startswith("http") and "torrent-stats" not in host and not src.lower().endswith(".gif"):
            return src.replace("http://", "https://", 1) if src.startswith("http://") else src
    return None


def _info(soup: BeautifulSoup) -> dict[str, str]:
    """The "Genres/Tags: … Companies: … Repack Size: …" lines of the first paragraph with them."""
    out: dict[str, str] = {}
    for p in soup.find_all("p"):
        text = p.get_text("\n", strip=True)
        if "Repack Size" not in text and "Genres/Tags" not in text:
            continue
        joined = re.sub(r"\n(?=[^\n:]{1,60}(?:\n|$))", " ", text)     # "Genres/Tags:\nStrategy,\nRPG" → one line
        for line in re.split(r"\n(?=[A-Z][\w/ ]{2,20}\s*:)", joined):
            m = _FIELD.match(re.sub(r"\s+", " ", line).strip())
            if m:
                out.setdefault(m.group(1).lower(), m.group(2).strip())
        if out:
            break
    return out


def parse_listing_posts(data: list, tags: dict[int, str]) -> list[Game]:
    """Every post of an API page (run in the parse helper process)."""
    return [g for g in (parse_listing_post(p, tags) for p in data) if g]


def parse_listing_post(post: dict, tags: dict[int, str] | None = None) -> Game | None:
    if not isinstance(post, dict) or "id" not in post or "link" not in post:
        return None
    cats = post.get("categories") or []
    if cats and CAT_REPACK not in cats:
        return None                                     # site news, digests, movies
    raw = ((post.get("title") or {}).get("rendered") or "").strip()
    title, version, tag = split_title(raw)
    if not title:
        return None
    content = ((post.get("content") or {}).get("rendered") or "") if isinstance(post.get("content"), dict) else ""
    soup = BeautifulSoup(content, "lxml") if content else None
    info = _info(soup) if soup else {}
    names = [t.strip() for t in info.get("genres/tags", "").split(",") if t.strip()]
    if not names and tags:
        names = [tags[i] for i in post.get("tags") or [] if i in tags]
    g = Game(source="fitgirl", source_game_id=str(post["id"]), title=title, raw_title=htmllib.unescape(raw),
             url=post["link"], cover_url=_cover(soup) if soup else None, genres=_genres_from_names(names),
             version=version, published_at=_wp_time(post, "date"), updated_at=_wp_time(post, "modified"),
             extra={"slug": post.get("slug")} if post.get("slug") else {})
    if info.get("repack size"):
        g.size_text = info["repack size"]
        g.size_bytes = parse_size(re.sub(r"^from\s+", "", info["repack size"], flags=re.I).split("[")[0].strip())
    if tag:
        g.extra["repack_by"] = tag
    if CAT_HYPERVISOR in cats:
        g.extra["hypervisor"] = True
    if CAT_SWITCH in cats or CAT_PS3 in cats:
        g.extra["emulated"] = "Nintendo Switch" if CAT_SWITCH in cats else "PlayStation 3"
    g.extra["installer"] = "fitgirl"
    return g


def _part_name(a) -> str | None:
    text = a.get_text(" ", strip=True)
    frag = urlsplit(a["href"]).fragment
    for cand in (text, frag, urlsplit(a["href"]).path.rsplit("/", 1)[-1]):
        cand = htmllib.unescape(cand or "").replace("–", "--").strip()
        if re.search(r"\.(rar|zip|7z|\d{3}|bin)$", cand, re.I):
            return cand
    return None


def download_methods(soup: BeautifulSoup) -> list[DownloadMethod]:
    """Magnet first (in-app), then each file host's parts (browser)."""
    methods: list[DownloadMethod] = []
    seen: set[str] = set()
    for a in soup.select('a[href^="magnet:"]'):
        href = a["href"].strip()
        m = re.search(r"btih:([0-9a-z]+)", href, re.I)
        key = m.group(1).lower() if m else href
        if key in seen:
            continue
        seen.add(key)
        methods.append(DownloadMethod(
            MethodKind.TORRENT, "Torrent (FitGirl's magnet link)", href, host="magnet", supported=True,
            resolver="magnet", note="Downloaded in the app with the built-in torrent engine — the way FitGirl "
                                    "recommends. Optional/selective files are included."))
    for li in soup.select("li"):
        head = li.find("a", href=True)
        label_text = head.get_text(" ", strip=True) if head else ""
        hm = re.match(r"Filehoster:\s*(.+)$", label_text, re.I)
        if not hm:
            continue
        hoster = hm.group(1).strip()
        # parts are listed in the spoiler right after the hoster line (inside the <li> or right after it)
        holder = li.select_one(".su-spoiler-content")
        if holder is None:
            sib = li.find_next_sibling()
            holder = sib.select_one(".su-spoiler-content") if sib is not None and sib.name == "div" else None
        parts = [a for a in (holder.select("a[href]") if holder else []) if a["href"].startswith("http")]
        for a in parts:
            href = a["href"].strip()
            if href in seen:
                continue
            seen.add(href)
            name = _part_name(a)
            m = classify_host_link(href)
            host = (urlsplit(href).hostname or "").lower().removeprefix("www.")
            m.label = f"{HOST_LABELS.get(host, hoster)} — {name}" if name else HOST_LABELS.get(host, hoster)
            m.file_name = name
            m.note = (f"{hoster} pages block apps, so this opens in the browser. Download every part, then the app "
                      "joins and extracts them and the repack's installer can run.")
            methods.append(m)
    return methods


def parse_details(content_html: str, game: Game) -> Game:
    soup = BeautifulSoup(content_html, "lxml")
    if soup.body is None:
        raise LayoutChangedError("The FitGirl post has no content")
    for s in soup.select("script, style"):
        s.decompose()
    info = _info(soup)
    if not info and not soup.select_one('a[href^="magnet:"]'):
        raise LayoutChangedError("This FitGirl post has no repack details (the page layout may have changed)")
    names = [t.strip() for t in info.get("genres/tags", "").split(",") if t.strip()]
    if names:
        game.genres = _genres_from_names(names) or game.genres
        game.extra["tags"] = names
    who = info.get("companies") or info.get("company")
    if who:
        game.extra["developer"] = who
    if info.get("languages"):
        game.extra["languages"] = info["languages"]
    if info.get("original size"):
        game.extra["original_size"] = info["original size"]
    if info.get("repack size"):
        game.size_text = info["repack size"]
        game.size_bytes = parse_size(re.sub(r"^from\s+", "", info["repack size"], flags=re.I).split("[")[0].strip())
        if "selective" in info["repack size"].lower():
            game.extra["selective"] = True
    cover = _cover(soup)
    if cover:
        game.cover_url = cover
    h3 = soup.find("h3")
    if h3:
        m = _NUMBER.match(h3.get_text(" ", strip=True))
        if m:
            game.extra["repack_number"] = int(m.group(1))
    # "Repack Features" list: what the repack is based on, install time, disk space after install
    feats_head = next((h for h in soup.find_all("h3") if "repack features" in h.get_text().lower()), None)
    feats = feats_head.find_next("ul") if feats_head else None
    if feats is not None:
        items = [li.get_text(" ", strip=True) for li in feats.find_all("li", recursive=False)] or \
            [ln.strip() for ln in feats.get_text("\n").splitlines() if ln.strip()]
        items = [re.split(r"\n", i)[0][:220] for i in items]
        game.extra["repack_features"] = items[:12]
        for i in items:
            low = i.lower()
            if low.startswith("based on"):
                m = re.match(r"(?:scene\s+)?(\S+)", i[len("based on"):].strip(" :"), re.I)
                if m:
                    game.extra["release_group"] = m.group(1)[:80]
            elif low.startswith("installation takes"):
                game.extra["install_time"] = i[len("installation takes"):].strip()
            elif low.startswith("hdd space after installation"):
                game.extra["installed_size"] = i.split(":", 1)[-1].strip()
    # description: the "Game Description" spoiler
    desc = None
    for sp in soup.select(".su-spoiler"):
        title = sp.select_one(".su-spoiler-title")
        if title and "description" in title.get_text().lower():
            body = sp.select_one(".su-spoiler-content")
            if body is not None:
                desc = body.get_text("\n", strip=True)
            break
    if desc:
        game.description = re.sub(r"\n{2,}", "\n\n", desc)[:3000]
    game.extra["installer"] = "fitgirl"
    game.download_methods = download_methods(soup)
    game.details_checked_at = time.time()
    return game


class FitGirlAdapter(SteamRipAdapter):
    info = SourceInfo(
        id="fitgirl",
        name="FitGirl Repacks",
        homepage=BASE,
        description="Compressed, lossless repacks of PC games that install with their own setup (fitgirl-repacks.site).",
        capabilities={"search", "pagination", "genres"},
        notes=[
            "Read through the site's public WordPress API with caching and rate limits.",
            "Torrents (magnet links) download in the app. Direct file-host links open in the in-app browser.",
            "A repack has to be installed before it can be played: press “Install game” in your Library — the "
            "app runs the repack's setup.exe and then finds the game's executable. Nothing runs by itself.",
            "Repacks marked “Hypervisor Bypass” need a kernel-level Denuvo bypass and do not run on Linux.",
        ],
        page_size=PAGE_SIZE,
    )

    def __init__(self, fetcher, base: str = BASE):
        super().__init__(fetcher, base)
        self._tags: dict[int, str] | None = None

    async def tags(self) -> dict[int, str]:
        if self._tags is None:
            body, _, _ = await self._json("tags", {"per_page": 100, "orderby": "count", "order": "desc",
                                                   "_fields": "id,name"}, ttl=24 * 3600)
            try:
                self._tags = {int(t["id"]): htmllib.unescape(t["name"]) for t in json.loads(body)
                              if isinstance(t, dict)}
            except (ValueError, KeyError, TypeError) as exc:
                raise LayoutChangedError("FitGirl tags response changed") from exc
        return self._tags

    async def genres(self) -> list[str]:
        return sorted(set(GENRE_TAGS.values()))

    async def _fitgirl_page(self, body: bytes, headers: dict, page: int, stale: bool, tags: dict[int, str]) -> Page:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise LayoutChangedError("FitGirl's WordPress API did not return JSON") from exc
        if isinstance(data, dict) and data.get("code") == "rest_post_invalid_page_number":
            return Page([], page, False, None, stale)
        if not isinstance(data, list):
            raise LayoutChangedError("FitGirl's WordPress API returned an unexpected structure")
        games = await parsing.parse(parse_listing_posts, data, tags)
        total, pages = headers.get("x-wp-total"), headers.get("x-wp-totalpages")
        has_more = int(pages) > page if pages and pages.isdigit() else len(data) >= PAGE_SIZE
        return Page(games, page, has_more and bool(data), int(total) if total and total.isdigit() else None, stale)

    async def latest(self, page: int = 1, genre: str | None = None) -> Page:
        tags = await self.tags()
        params = {"per_page": PAGE_SIZE, "page": max(1, page), "categories": CAT_REPACK, "_fields": LIST_FIELDS}
        if genre:
            ids = [i for i, n in tags.items() if GENRE_TAGS.get(n.lower()) == genre]
            if not ids:
                return Page([], page, False)
            params["tags"] = ",".join(map(str, ids))
        body, headers, stale = await self._json("posts", params)
        return await self._fitgirl_page(body, headers, page, stale, tags)

    async def search(self, query: str, page: int = 1) -> Page:
        query = query.strip()
        if len(query) < 2:
            return Page([], page, False)
        tags = await self.tags()
        params = {"search": query, "search_columns": "post_title", "per_page": PAGE_SIZE, "page": max(1, page),
                  "categories": CAT_REPACK, "_fields": LIST_FIELDS}
        body, headers, stale = await self._json("posts", params, ttl=1800)
        return await self._fitgirl_page(body, headers, page, stale, tags)

    async def details(self, game: Game) -> Game:
        if (urlsplit(game.url).hostname or "").lower() not in SITE_HOSTS | {urlsplit(self.base).hostname}:
            raise SourceError("refusing to fetch a URL that is not on FitGirl Repacks")
        if not game.source_game_id.isdigit():
            raise SourceError("invalid FitGirl post id")
        tags = await self.tags()
        body, _, _ = await self._json(f"posts/{game.source_game_id}", {"_fields": LIST_FIELDS}, ttl=1800)
        try:
            post = json.loads(body)
        except ValueError as exc:
            raise LayoutChangedError("FitGirl's API did not return JSON") from exc
        if not isinstance(post, dict) or "content" not in post:
            raise LayoutChangedError("FitGirl post response has no content")
        fresh = parse_listing_post(post, tags)
        if fresh:
            game.merge_listing(fresh)
            for k, v in fresh.extra.items():
                game.extra.setdefault(k, v)
        game = await parsing.parse(parse_details, (post.get("content") or {}).get("rendered") or "", game)
        if game.extra.get("hypervisor"):
            game.extra["warning"] = HYPERVISOR_WARNING
        return game

    async def resolve(self, method: DownloadMethod, game: Game) -> ResolvedDownload:
        if method.resolver == "magnet":
            return ResolvedDownload("torrent", url=method.url)
        raise ManualActionRequired(method.note or "Open this link in your browser.", method.url)
