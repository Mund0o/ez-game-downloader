"""OnlineFix adapter (https://online-fix.me).

Verified against the live site on 2026-10-09:

* ``onlinefix.me`` (no hyphen) is a parked domain that redirects through an ad
  network; the real site is ``online-fix.me`` (DataLife Engine, windows-1251).
* Listing pages ``/``, ``/page/N/``, ``/games/<genre>/page/N/`` contain
  ``article.news`` cards; search is ``/index.php?do=search&subaction=search&story=…``
  with 21 ``div.news`` results per page (``search_start``).
* The home page exposes two curated carousels — "Популярное" (Popular) and
  "Обновления" (Updates). There is no separate "picks" page; these carousels
  are what the OnlineFix Picks section shows.
* Game pages offer four buttons:
  - ``uploads.online-fix.me:2053/torrents/<Game>/`` – public nginx index with the
    ``.torrent`` file → supported (embedded torrent engine);
  - ``uploads.online-fix.me:2053/uploads/<Game>/`` – listing is public but the
    files answer HTTP 403 without a signed-in session → browser only;
  - ``drive.online-fix.me:2053/<Game>`` – needs a personal API key → browser only;
  - ``hosters.online-fix.me:2053/<Game>`` – JSON list of third-party mirrors.
* All archives use the password published at the top of every game page.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from ezgd.catalog.normalize import canonical_genre, clean_title
from ezgd.sources.base import (FeaturedSection, LayoutChangedError, ManualActionRequired, Page,
                               ResolvedDownload, SourceAdapter, SourceError, SourceInfo)
from ezgd.sources.hosts import classify_host_link, resolve_pixeldrain
from ezgd.sources import parsing

log = logging.getLogger(__name__)

BASE = "https://online-fix.me"
SITE_HOSTS = {"online-fix.me", "www.online-fix.me"}
UPLOADS_HOST = "uploads.online-fix.me"
HOSTERS_HOST = "hosters.online-fix.me"
DRIVE_HOST = "drive.online-fix.me"
SEARCH_PAGE_SIZE = 21
MSK = timezone(timedelta(hours=3))

GENRE_SLUGS = {
    "Arcade": "arcade", "Survival": "survival", "Racing": "racing", "Fighting": "fighting",
    "Sandbox": "sandbox", "RPG": "rpg", "Simulation": "simulator", "Strategy": "strategy",
    "Horror": "horror", "Shooter": "shooter", "Puzzle": "puzzles", "Adventure": "adventures", "VR": "vr",
}
FEATURED_TITLES = {
    "популярное": ("popular", "Popular"),
    "обновления": ("updates", "Recently updated"),
}
_RU_MONTHS = {m: i for i, m in enumerate(
    ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
     "октября", "ноября", "декабря"], start=1)}
_RU_MONTHS.update({m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)})
_RU_DATE = re.compile(r"(\d{1,2})\s+([а-яa-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})", re.I)
_RU_REL = re.compile(r"(Сегодня|Вчера|Today|Yesterday),?\s+(\d{1,2}):(\d{2})", re.I)
_ID_RE = re.compile(r"/(\d+)-[^/]+\.html$")


def parse_ru_datetime(text: str, now: datetime | None = None) -> float | None:
    """Parse '15 августа 2026, 12:35', 'Сегодня, 10:00' or 'Вчера, 22:00' (Moscow time)."""
    if not text:
        return None
    m = _RU_DATE.search(text)
    if m:
        month = _RU_MONTHS.get(m.group(2).lower())
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(1)), int(m.group(4)), int(m.group(5)),
                                tzinfo=MSK).timestamp()
            except ValueError:
                return None
    m = _RU_REL.search(text)
    if m:
        base = (now or datetime.now(MSK)).astimezone(MSK)
        if m.group(1).lower() in ("вчера", "yesterday"):
            base -= timedelta(days=1)
        return base.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0).timestamp()
    return None


def _iso(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        return None


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def check_not_parked(soup: BeautifulSoup, raw: str) -> None:
    title = (soup.title.get_text(strip=True) if soup.title else "").lower()
    if "parklogic" in raw or (title == "redirecting..." and not soup.select_one("#dle-content")):
        raise LayoutChangedError("The domain is serving a parking/redirect page instead of OnlineFix. "
                                 "Check the source address in Settings.")


def _genre_from_url(url: str) -> tuple[list[str], dict]:
    m = re.search(r"/games/([a-z0-9_-]+)/", url)
    if not m:
        return [], {}
    slug = m.group(1)
    if slug == "officialservers":
        return [], {"official_servers": True}
    g = canonical_genre(slug)
    return ([g] if g else []), {}


# Posts are written in Russian or English (both occur on the same listing page).
L_RELEASE = r"(?:Релиз игры|Game release)"
L_VIA = r"(?:Игра через|Play via)"
L_VERSION = r"(?:Версия игры|Game version)"
L_INFO = ("Информация о игре", "Информация об игре", "Game information")
L_PASSWORD = ("Пароль", "Password")
# "1. Скачиваем игру Little Nightmares III - Free Trial & Friend's Pass через Steam." — pages that only offer the fix:
# the game itself is free on Steam (demo / Friend's Pass / free-to-play), and the fix goes into its folder
_STEAM_BASE = re.compile(r"(?:Скачиваем|Устанавливаем) игру\s+(.{3,120}?)\s+(?:через|в|из) Steam|"
                         r"(?:Download|Install) the game\s+(.{3,120}?)\s+(?:via|through|from|on|in) Steam", re.I)
_LAUNCH_EXE = re.compile(r"(?:Запускаем игру через|Запускаем|Launch the game (?:via|through)|Run)\s+"
                         r"([^\s,()]+\.exe)\b", re.I)
# OnlineFix withdraws some guides ("Причина: Руководство закрыто." / "Руководство закрыто, используйте
# версию от RUNE и Parsec." — seen 2026-10-09 on Minecraft Dungeons II). Those games are hidden.
CLOSED_RE = re.compile(r"Руководство\s+закрыто|guide\s+(?:is\s+|has\s+been\s+)?closed", re.I)
_MODE_NAMES = {"кооператив": "Co-op", "cooperative": "Co-op", "co-op": "Co-op",
               "мультиплеер": "Multiplayer", "multiplayer": "Multiplayer"}


def parse_modes(node: Tag) -> dict[str, bool]:
    """Read 'Кооператив ✓ \\ Мультиплеер ✗' (icons are fa-check / fa-times spans)."""
    out: dict[str, bool] = {}
    for span in node.select("span.fa-check, span.fa-times"):
        prev = span.previous_sibling
        while prev is not None and isinstance(prev, str) and not prev.strip():
            prev = prev.previous_sibling
        if not isinstance(prev, str):
            continue
        word = re.split(r"[\\/]", prev)[-1].strip().lower()
        word = re.sub(r"^\W+|\W+$", "", word)
        name = _MODE_NAMES.get(word)
        if name:
            out[name] = "fa-check" in (span.get("class") or [])
    return out


def parse_card(node: Tag, base: str = BASE) -> Game | None:
    link = node.select_one("a.big-link[href]") or node.select_one("h2.title")
    if link is not None and link.name != "a":
        link = link.find_parent("a")
    title_el = node.select_one("h2.title")
    if link is None or title_el is None:
        return None
    url = urljoin(base, link["href"])
    m = _ID_RE.search(urlsplit(url).path)
    if not m:
        return None
    raw = title_el.get_text(" ", strip=True)
    title, _ = clean_title(raw)
    img = node.select_one(".image img")
    cover = None
    if img is not None:
        cover = img.get("data-src") or img.get("src")
        cover = urljoin(base, cover) if cover else None
    t = node.select_one("time[datetime]")
    published = _iso(t["datetime"]) if t else None
    edit = node.select_one(".edit")
    edit_text = edit.get_text(" ", strip=True) if edit else ""
    version = None
    vm = re.search(r"(?:верси[ия]|version)\s+([^\s]+?)\.?\s*$", edit_text, re.I)
    if vm:
        version = vm.group(1)
    preview = node.select_one(".preview-text")
    ptext = preview.get_text("\n", strip=True) if preview else ""
    ptext_flat = re.sub(r"\s*\n\s*", " ", ptext)
    genres, extra = _genre_from_url(url)
    # always set explicitly so a guide that OnlineFix reopens is shown again on the next listing
    extra["guide_closed"] = bool(CLOSED_RE.search(edit_text))
    via = re.search(L_VIA + r":\s*(.+?)\s*(?:Режимы|Modes|$)", ptext_flat)
    if via:
        extra["network"] = via.group(1).strip()
    if preview is not None:
        modes = parse_modes(preview)
        if modes:
            extra["modes"] = [k for k, v in modes.items() if v]
    rel = re.search(L_RELEASE + r":\s*([\d.]+)", ptext_flat)
    return Game(
        source="onlinefix", source_game_id=m.group(1), title=title, raw_title=raw, url=url,
        cover_url=cover, genres=genres, release_date=rel.group(1) if rel else None, version=version,
        published_at=published, updated_at=parse_ru_datetime(edit_text), extra=extra,
    )


def parse_listing(html: str, base: str = BASE) -> tuple[list[Game], bool, int | None]:
    soup = _soup(html)
    check_not_parked(soup, html)
    content = soup.select_one("#dle-content")
    if content is None:
        raise LayoutChangedError("OnlineFix listing page has no #dle-content container")
    games = [g for g in (parse_card(n, base) for n in content.select(".news")) if g]
    if not games and content.select(".news"):
        raise LayoutChangedError("OnlineFix cards were found but none could be parsed")
    pag = soup.select_one(".pagination")
    has_more, last_page = False, None
    if pag is not None:
        nums = [int(a.get_text(strip=True)) for a in pag.select("a") if a.get_text(strip=True).isdigit()]
        last_page = max(nums) if nums else None
        has_more = any("ДАЛЕЕ" in a.get_text(strip=True).upper() for a in pag.select("a[href]"))
    return games, has_more, last_page


def parse_search(html: str, base: str = BASE) -> tuple[list[Game], bool]:
    soup = _soup(html)
    check_not_parked(soup, html)
    content = soup.select_one("#dle-content")
    if content is None:
        raise LayoutChangedError("OnlineFix search page has no #dle-content container")
    games = [g for g in (parse_card(n, base) for n in content.select(".news")) if g]
    return games, len(games) >= SEARCH_PAGE_SIZE


def parse_featured(html: str, base: str = BASE) -> list[FeaturedSection]:
    soup = _soup(html)
    check_not_parked(soup, html)
    # carousel items carry no update reason; the cards on the same page do
    cards = [g for g in (parse_card(n, base) for n in soup.select("#dle-content .news")) if g]
    closed_ids = {g.source_game_id for g in cards if g.extra.get("guide_closed")}
    open_ids = {g.source_game_id for g in cards if not g.extra.get("guide_closed")}
    sections = []
    for slider in soup.select(".horizontal-slider"):
        head = slider.select_one(":scope > .title")
        items = slider.select(".container a[href]")
        if head is None or not items:
            continue
        label = head.get_text(strip=True)
        key, section_title = FEATURED_TITLES.get(label.lower(),
                                                 (re.sub(r"\W+", "-", label.lower()).strip("-"), label))
        games = []
        for a in items:
            url = urljoin(base, a["href"])
            m = _ID_RE.search(urlsplit(url).path)
            if not m:
                continue
            raw = a.get("title") or (a.select_one("p").get_text(strip=True) if a.select_one("p") else "")
            title, _ = clean_title(raw)
            img = a.select_one("img")
            cover = urljoin(base, img.get("src") or img.get("data-src")) if img is not None and \
                (img.get("src") or img.get("data-src")) else None
            when = a.select_one("p.text-gray")
            genres, extra = _genre_from_url(url)
            if m.group(1) in closed_ids:
                extra["guide_closed"] = True
            elif m.group(1) in open_ids:
                extra["guide_closed"] = False
            if any(g.source_game_id == m.group(1) for g in games):
                continue                          # the site sometimes lists a game twice in one carousel
            games.append(Game(source="onlinefix", source_game_id=m.group(1), title=title, raw_title=raw,
                              url=url, cover_url=cover, genres=genres, extra=extra,
                              updated_at=parse_ru_datetime(when.get_text(strip=True)) if when else None))
        if games:
            sections.append(FeaturedSection(key, section_title, label, games))
    return sections


def _classify_button(href: str, label: str, password: str | None) -> DownloadMethod:
    parts = urlsplit(href)
    host = (parts.hostname or "").lower()
    path = parts.path
    if host == UPLOADS_HOST and path.startswith("/torrents/"):
        return DownloadMethod(MethodKind.TORRENT, "Torrent", href, host=host, supported=True,
                              resolver="onlinefix_torrent_index", archive_password=password,
                              note="The .torrent file is fetched from OnlineFix's public torrent index and "
                                   "downloaded with the built-in torrent engine.")
    if host == UPLOADS_HOST and path.startswith("/uploads/"):
        return DownloadMethod(MethodKind.EXTERNAL, "OnlineFix server", href, host=host, supported=False,
                              archive_password=password,
                              note="Files on the OnlineFix server answer HTTP 403 unless you are signed in on the "
                                   "website; open it in your browser.")
    if host == DRIVE_HOST:
        return DownloadMethod(MethodKind.EXTERNAL, "Online-Fix Drive", href, host=host, supported=False,
                              archive_password=password,
                              note="Online-Fix Drive requires a personal API key from your account.")
    if host == HOSTERS_HOST:
        return DownloadMethod(MethodKind.MIRRORS, "Online-Fix Hosters", href, host=host, supported=True,
                              resolver="onlinefix_hosters", archive_password=password,
                              note="Index of third-party mirrors.")
    return classify_host_link(href, label=label or None, archive_password=password)


def parse_details(html: str, game: Game, base: str = BASE) -> Game:
    soup = _soup(html)
    check_not_parked(soup, html)
    story = soup.select_one(".full-story")
    if story is None:
        raise LayoutChangedError("OnlineFix game page has no .full-story block")
    h1 = story.select_one("#news-title")
    if h1:
        game.raw_title = h1.get_text(" ", strip=True)
        game.title = clean_title(game.raw_title)[0]
    pub = story.select_one("[itemprop=datePublished]")
    mod = story.select_one("[itemprop=dateModified]")
    game.published_at = _iso(pub.get_text(strip=True)) if pub else game.published_at
    game.updated_at = _iso(mod.get_text(strip=True)) if mod else game.updated_at
    body = story.select_one("[itemprop=articleBody]") or story
    text = body.get_text("\n", strip=True)
    flat = re.sub(r"\s*\n\s*", " ", text)

    edited = story.select_one(".edited-block")
    edited_text = edited.get_text(" ", strip=True) if edited else ""
    notice = next((ln for ln in text.splitlines() if CLOSED_RE.search(ln)), None)
    game.extra["guide_closed"] = bool(CLOSED_RE.search(edited_text) or notice)
    if notice:
        game.extra["closed_notice"] = notice.strip()
    else:
        game.extra.pop("closed_notice", None)

    password = None
    for b in story.find_all("b"):
        if any(lbl in b.get_text() for lbl in L_PASSWORD):
            p = b.find_next("p")
            if p is not None and p.get_text(strip=True):
                password = p.get_text(strip=True)
            break
    if password:
        game.extra["archive_password"] = password

    for b in body.find_all("b"):
        if any(lbl in b.get_text() for lbl in L_INFO):
            sib = b.find_next_sibling("i")
            if sib is not None:
                game.description = sib.get_text(" ", strip=True)
            break
    rel = re.search(L_RELEASE + r":\s*([\d.]+)", flat)
    if rel:
        game.release_date = rel.group(1)
    ver = re.search(L_VERSION + r":\s*([^\s]+)", flat)
    if ver:
        game.version = ver.group(1)
    via = re.search(L_VIA + r":\s*([^\n]+)", text)
    if via:
        game.extra["network"] = via.group(1).strip()
    exe = _LAUNCH_EXE.search(flat)
    if exe:
        game.extra["launch_exe_hint"] = exe.group(1)
    base_game = _STEAM_BASE.search(flat)
    if base_game:
        game.extra["steam_base_game"] = (base_game.group(1) or base_game.group(2)).strip(" .:«»\"")
    coop = story.select_one("[class^=coop]")
    multi = story.select_one("[class^=multi]")
    for el, key in ((coop, "coop_players"), (multi, "multiplayer_players")):
        if el is not None and re.search(r"(КООПЕРАТИВ|МУЛЬТИПЛЕЕР|COOPERATIVE|MULTIPLAYER)", el.get_text(), re.I):
            n = re.search(r"(\d+)", el.get_text())
            if n:
                game.extra[key] = int(n.group(1))
    steam_link = body.find("a", attrs={"title": "store.steampowered.com"})
    if steam_link is not None:
        game.extra["has_steam_page"] = True
    genres, extra = _genre_from_url(game.url)
    if genres:
        game.genres = genres
    game.extra.update(extra)

    methods: list[DownloadMethod] = []
    seen = set()
    for a in body.select("a.btn[href]"):
        href = urljoin(base, a["href"])
        if href in seen or not href.startswith(("http://", "https://")):
            continue
        seen.add(href)
        methods.append(_classify_button(href, a.get_text(" ", strip=True), password))
    game.download_methods = methods
    game.details_checked_at = time.time()
    return game


def parse_torrent_index(html: str, index_url: str) -> list[dict]:
    """Parse an nginx autoindex listing → [{name, url, date, size_text}] (newest first)."""
    soup = _soup(html)
    pre = soup.select_one("pre")
    if pre is None:
        raise LayoutChangedError("torrent index page is not a directory listing")
    host = urlsplit(index_url).hostname
    out = []
    for a in pre.select("a[href]"):
        href = a["href"]
        if not href.lower().endswith(".torrent"):
            continue
        url = urljoin(index_url, href)
        if urlsplit(url).hostname != host:      # never follow links off the index host
            continue
        tail = a.next_sibling if isinstance(a.next_sibling, str) else ""
        dm = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})\s+(\S+)", tail or "")
        ts = None
        if dm:
            try:
                ts = datetime.strptime(dm.group(1), "%d-%b-%Y %H:%M").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                ts = None
        out.append({"name": a.get_text(strip=True), "url": url, "date": ts,
                    "size_text": dm.group(2) if dm else None})
    out.sort(key=lambda d: d["date"] or 0, reverse=True)
    return out


def parse_hosters(html: str, password: str | None) -> list[DownloadMethod]:
    soup = _soup(html)
    options = soup.select(".option[data-links]")
    if not options:
        # new releases show the normal page with no mirrors yet; anything else is a real layout change
        if soup.select_one(".carousel-container, #filesContainer, .box-container .game-name"):
            return []
        raise LayoutChangedError("OnlineFix Hosters page layout changed")
    methods = []
    for opt in options:
        host_label = opt.get_text(strip=True)
        try:
            links = json.loads(opt["data-links"])
        except (ValueError, KeyError):
            continue
        for item in links if isinstance(links, list) else []:
            url = item.get("direct_link") if isinstance(item, dict) else None
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            fname = item.get("file_name")
            label = f"{host_label} — {fname}" if fname else host_label
            if fname and "fix_repair" in fname.lower():
                label += " (fix files only)"
            methods.append(classify_host_link(url, label=label, file_name=fname,
                                              dangerous=bool(item.get("is_dangerous")),
                                              archive_password=password))
    return methods


class OnlineFixAdapter(SourceAdapter):
    info = SourceInfo(
        id="onlinefix",
        name="OnlineFix",
        homepage=BASE,
        description="Multiplayer fixes and games (online-fix.me). Russian-language site.",
        capabilities={"search", "pagination", "genres", "featured", "torrent"},
        notes=[
            "The address onlinefix.me is a parked domain; this adapter uses online-fix.me.",
            "Torrents are downloaded in-app. Server, Drive and most mirror downloads require your browser.",
            "All OnlineFix archives are password-protected (password: online-fix.me). The app enters it "
            "automatically when extracting; you need it if you unpack an archive yourself.",
        ],
        page_size=21,
        # every game page states "Пароль един для всех архивов: online-fix.me"
        # ("one password for all archives"); a password parsed from the page takes precedence
        archive_password="online-fix.me",
    )

    def __init__(self, fetcher, base: str = BASE):
        super().__init__(fetcher)
        self.base = base.rstrip("/")

    def _check_site_url(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if host not in SITE_HOSTS and host != urlsplit(self.base).hostname:
            raise SourceError(f"refusing to fetch non-OnlineFix URL: {host}")

    async def _html(self, url: str, ttl: float | None = None, force: bool = False) -> tuple[str, bool]:
        res = await self.fetcher.get(url, ttl=ttl, force=force)
        if res.status == 404:
            raise SourceError("OnlineFix returned 404 (page not found)")
        if res.status >= 400:
            raise SourceError(f"OnlineFix returned HTTP {res.status}")
        return res.text("cp1251"), res.stale

    async def latest(self, page: int = 1, genre: str | None = None) -> Page:
        slug = GENRE_SLUGS.get(genre) if genre else None
        if genre and not slug:
            return Page([], page, False)
        root = f"{self.base}/games/{slug}/" if slug else f"{self.base}/"
        url = root if page <= 1 else f"{root}page/{page}/"
        html, stale = await self._html(url)
        games, has_more, last = await parsing.parse(parse_listing, html, self.base)
        return Page(games, page, has_more, None, stale)

    async def search(self, query: str, page: int = 1) -> Page:
        query = query.strip()
        if len(query) < 3:            # DLE ignores very short queries
            return Page([], page, False)
        story = quote(query.encode("cp1251", errors="ignore"))
        start = max(1, page)
        url = (f"{self.base}/index.php?do=search&subaction=search&search_start={start}"
               f"&full_search=0&result_from={(start - 1) * SEARCH_PAGE_SIZE + 1}&story={story}")
        html, stale = await self._html(url, ttl=1800)
        games, has_more = await parsing.parse(parse_search, html, self.base)
        return Page(games, page, has_more, None, stale)

    async def featured(self) -> list[FeaturedSection]:
        html, _ = await self._html(f"{self.base}/")
        return await parsing.parse(parse_featured, html, self.base)

    def featured_keys(self) -> dict[str, tuple[str, str]]:
        return {key: (title, site_label.capitalize()) for site_label, (key, title) in FEATURED_TITLES.items()}

    async def genres(self) -> list[str]:
        return sorted(GENRE_SLUGS)

    async def details(self, game: Game) -> Game:
        self._check_site_url(game.url)
        html, _ = await self._html(game.url, ttl=1800)
        game = await parsing.parse(parse_details, html, game, self.base)
        expanded: list[DownloadMethod] = []
        for m in game.download_methods or []:
            if m.kind is MethodKind.MIRRORS:
                try:
                    mirrors = await self.expand(m, game)
                    if not mirrors:
                        m.kind, m.supported = MethodKind.EXTERNAL, False
                        m.note = "OnlineFix has not listed any mirrors for this game yet."
                        mirrors = [m]
                    expanded.extend(mirrors)
                except Exception as exc:  # mirrors are optional; keep a browser fallback
                    log.warning("Could not load OnlineFix mirror list: %s", exc)
                    m.kind, m.supported = MethodKind.EXTERNAL, False
                    m.note = f"Mirror list could not be loaded ({exc}); open it in your browser."
                    expanded.append(m)
            else:
                expanded.append(m)
        # supported methods first, torrent before direct, then external
        order = {MethodKind.TORRENT: 0, MethodKind.DIRECT: 1, MethodKind.MIRRORS: 2, MethodKind.EXTERNAL: 3}
        game.download_methods = sorted(expanded, key=lambda x: (not x.supported, order[x.kind],
                                                                "fix files only" in x.label))
        return game

    async def expand(self, method: DownloadMethod, game: Game) -> list[DownloadMethod]:
        if method.resolver != "onlinefix_hosters":
            return []
        if (urlsplit(method.url).hostname or "").lower() != HOSTERS_HOST:
            raise SourceError("unexpected hosters URL")
        res = await self.fetcher.get(method.url, ttl=1800)
        if res.status >= 400:
            raise SourceError(f"hosters page returned HTTP {res.status}")
        return parse_hosters(res.text(), method.archive_password)

    async def resolve(self, method: DownloadMethod, game: Game) -> ResolvedDownload:
        if method.resolver == "onlinefix_torrent_index":
            if (urlsplit(method.url).hostname or "").lower() != UPLOADS_HOST:
                raise SourceError("unexpected torrent index host")
            res = await self.fetcher.get(method.url, ttl=300, force=True)
            if res.status in (401, 403):
                # Some releases' torrent folders are password-protected (nginx HTTP auth) for signed-in site
                # users only. That is an access control: never try to get around it — remember the method as
                # browser-only and send the user to the game page instead.
                method.kind, method.supported, method.resolver = MethodKind.EXTERNAL, False, None
                method.note = (f"OnlineFix only gives this game's torrent to signed-in users (HTTP {res.status}). "
                               f"Open the game page in your browser, or use another download option.")
                raise ManualActionRequired(method.note, game.url)
            if res.status >= 400:
                raise SourceError(f"OnlineFix's torrent index returned HTTP {res.status}; try again later or use "
                                  f"another download option.")
            entries = parse_torrent_index(res.text(), method.url)
            if not entries:
                raise SourceError("the torrent index lists no .torrent files for this game")
            chosen = entries[0]
            tor = await self.fetcher.get(chosen["url"], ttl=300, force=True, max_bytes=16 * 1024 * 1024)
            if tor.status in (401, 403):
                method.kind, method.supported, method.resolver = MethodKind.EXTERNAL, False, None
                method.note = (f"OnlineFix only gives this game's torrent to signed-in users (HTTP {tor.status}). "
                               f"Open the game page in your browser, or use another download option.")
                raise ManualActionRequired(method.note, game.url)
            if tor.status != 200:
                raise SourceError(f".torrent download returned HTTP {tor.status}")
            if not tor.body.startswith(b"d") or b"4:info" not in tor.body:
                raise SourceError("the server returned something that is not a .torrent file")
            return ResolvedDownload("torrent", torrent_bytes=tor.body, file_name=chosen["name"],
                                    archive_password=method.archive_password)
        if method.resolver == "pixeldrain":
            return await resolve_pixeldrain(self.fetcher, method)
        raise ManualActionRequired(method.note or "This link must be opened in a browser.", method.url)
