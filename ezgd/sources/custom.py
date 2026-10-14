"""User-added sources: game download sites the user adds themselves, when the app can read them properly.

Supported kind: WordPress sites with the standard public posts API (``/wp-json/wp/v2/posts``) — the same
interface SteamRIP is read through. Before a site is added it is checked, and refused with a plain reason when:

* it is a platform that is not a game download site (video, music, social, shopping …) or an official store;
* it is already built in;
* its robots.txt asks automated tools not to read it;
* it is behind a bot check (Cloudflare challenge) or a login, or cannot be reached;
* it does not offer the WordPress posts API (so there is no reliable way to read its games);
* its newest posts do not actually contain PC game downloads (torrent / magnet / file-host links + game info).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from ezgd.catalog.models import DownloadMethod
from ezgd.net.urls import UnsafeURLError
from ezgd.sources.base import ManualActionRequired, ResolvedDownload, SourceError, SourceInfo
from ezgd.sources.cache import SourceUnavailableError
from ezgd.sources.hosts import resolve_pixeldrain
from ezgd.sources.steamrip import SteamRipAdapter, download_links

log = logging.getLogger(__name__)


class SiteRejected(Exception):
    """The site cannot be added; ``str(exc)`` is the plain-language reason shown to the user."""


# --------------------------------------------------------------------- refused outright
_NOT_GAME_SITES = {
    # video / streaming
    "youtube.com": "YouTube is a video site", "youtu.be": "YouTube is a video site",
    "twitch.tv": "Twitch is a live-streaming site", "netflix.com": "Netflix is a video streaming service",
    "vimeo.com": "Vimeo is a video site", "dailymotion.com": "Dailymotion is a video site",
    "tiktok.com": "TikTok is a video app", "disneyplus.com": "Disney+ is a video streaming service",
    "hulu.com": "Hulu is a video streaming service", "primevideo.com": "Prime Video is a video streaming service",
    "max.com": "Max is a video streaming service", "crunchyroll.com": "Crunchyroll is an anime streaming service",
    "kick.com": "Kick is a live-streaming site",
    # music / audio
    "spotify.com": "Spotify is a music streaming service", "soundcloud.com": "SoundCloud is a music site",
    "deezer.com": "Deezer is a music streaming service", "tidal.com": "TIDAL is a music streaming service",
    "bandcamp.com": "Bandcamp is a music store", "music.apple.com": "Apple Music is a music streaming service",
    "pandora.com": "Pandora is a music streaming service",
    # social / community
    "facebook.com": "Facebook is a social network", "fb.com": "Facebook is a social network",
    "instagram.com": "Instagram is a social network", "x.com": "X (Twitter) is a social network",
    "twitter.com": "Twitter is a social network", "reddit.com": "Reddit is a discussion site",
    "discord.com": "Discord is a chat app", "discord.gg": "Discord is a chat app",
    "pinterest.com": "Pinterest is a social network", "linkedin.com": "LinkedIn is a social network",
    "threads.net": "Threads is a social network", "bsky.app": "Bluesky is a social network",
    "telegram.org": "Telegram is a chat app", "t.me": "Telegram is a chat app", "whatsapp.com": "WhatsApp is a chat app",
    # general / shopping / reference
    "google.com": "Google is a search engine", "bing.com": "Bing is a search engine",
    "duckduckgo.com": "DuckDuckGo is a search engine", "wikipedia.org": "Wikipedia is an encyclopedia",
    "amazon.com": "Amazon is a shop", "ebay.com": "eBay is a shop", "aliexpress.com": "AliExpress is a shop",
    "github.com": "GitHub hosts source code, not a game catalogue", "microsoft.com": "Microsoft's site is not a game download site",
    "apple.com": "Apple's site is not a game download site",
    # file hosts (the app already handles their links from the sources' game pages)
    "mega.nz": "MEGA is a file host, not a game site", "mediafire.com": "MediaFire is a file host, not a game site",
    "gofile.io": "Gofile is a file host, not a game site", "pixeldrain.com": "Pixeldrain is a file host, not a game site",
    "1fichier.com": "1fichier is a file host, not a game site", "buzzheavier.com": "Buzzheavier is a file host",
    "drive.google.com": "Google Drive is a file host",
}
_OFFICIAL_STORES = {
    "steampowered.com": "Steam", "steamcommunity.com": "Steam", "epicgames.com": "the Epic Games Store",
    "gog.com": "GOG", "ea.com": "EA", "ubisoft.com": "Ubisoft", "xbox.com": "Xbox", "playstation.com": "PlayStation",
    "nintendo.com": "Nintendo", "itch.io": "itch.io", "humblebundle.com": "Humble Bundle", "battle.net": "Battle.net",
    "rockstargames.com": "Rockstar Games", "greenmangaming.com": "Green Man Gaming", "fanatical.com": "Fanatical",
}
_BUILT_IN = {"steamrip.com": "SteamRIP", "online-fix.me": "OnlineFix", "cracked-games.org": "cracked-games.org",
             "fitgirl-repacks.site": "FitGirl Repacks",
             "ankergames.net": "AnkerGames"}

_GAME_WORDS = re.compile(r"(system requirements|minimum requirements|\brepack\b|\bpc game\b|directx|\bgpu\b|"
                         r"graphics:|processor:|memory:|storage:|genre\b|developer\b|\bgame size\b|pre-installed|"
                         r"\bfree download\b|\bmultiplayer\b|\bco-op\b|\bsteam\b|crack(ed)?\b|\bdlc\b)", re.I)
SAMPLE = 10              # newest posts inspected
MIN_WITH_DOWNLOADS = 3   # … at least this many must contain game downloads


def _parents(host: str):
    parts = host.split(".")
    for i in range(len(parts) - 1):
        yield ".".join(parts[i:])


def refused_platform(host: str) -> str | None:
    host = host.lower().removeprefix("www.")
    for p in _parents(host):
        if p in _BUILT_IN:
            return f"{_BUILT_IN[p]} is already built into the app — turn it on in Settings → Sources."
        if p in _OFFICIAL_STORES:
            return (f"{_OFFICIAL_STORES[p]} is an official store: buy and download its games in its own app or site. "
                    "Only game download sites like SteamRIP can be added.")
        if p in _NOT_GAME_SITES:
            return f"{_NOT_GAME_SITES[p]}, not a game download site. Only sites like SteamRIP can be added."
    # country variants: amazon.de, google.co.uk …
    first = host.split(".")[-2] if host.count(".") >= 1 else host
    if first in ("google", "amazon", "ebay", "youtube", "spotify", "netflix"):
        return f"{first.capitalize()} is not a game download site. Only sites like SteamRIP can be added."
    return None


def normalise(url: str) -> tuple[str, str]:
    """(base URL, host) from what the user typed; raises SiteRejected for anything that is not a web address."""
    url = (url or "").strip()
    if not url:
        raise SiteRejected("Enter the site's web address, for example https://example.com.")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SiteRejected("That is not a web address. Enter it like https://example.com.")
    host = parts.hostname.lower()
    if re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host) or "." not in host:
        raise SiteRejected("Enter the site's name (like example.com), not an IP address or a local computer name.")
    return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else ""), host


@dataclass
class SiteCheck:
    source_id: str
    name: str
    base_url: str
    host: str
    kind: str = "wordpress"
    posts_checked: int = 0
    posts_with_downloads: int = 0
    in_app: int = 0                 # downloads the app can do itself (magnet / .torrent / Pixeldrain)
    examples: list[str] = field(default_factory=list)

    def details(self) -> dict:
        return {"host": self.host, "posts_checked": self.posts_checked,
                "posts_with_downloads": self.posts_with_downloads, "in_app": self.in_app,
                "examples": self.examples[:3]}


async def check_site(fetcher, url: str, *, existing: set[str] = frozenset()) -> SiteCheck:
    """Decide whether ``url`` can be added; raises SiteRejected with the reason when not."""
    base, host = normalise(url)
    reason = refused_platform(host)
    if reason:
        raise SiteRejected(reason)
    source_id = "site:" + host.removeprefix("www.")
    if source_id in existing:
        raise SiteRejected(f"{host} is already in your sources.")

    async def get(path: str, ttl: float = 300):
        try:
            return await fetcher.get(base + path, ttl=ttl, max_bytes=8 * 1024 * 1024)
        except UnsafeURLError as exc:
            raise SiteRejected(f"The app will not connect to that address ({exc}).") from exc
        except SourceUnavailableError as exc:
            raise SiteRejected(f"The site could not be reached: {exc}. Check the address and your connection.") \
                from exc

    # robots.txt: respect a site that asks tools not to read it
    robots = await get("/robots.txt", ttl=3600)
    if robots.status == 200:
        rp = RobotFileParser()
        rp.parse(robots.text().splitlines())
        if not rp.can_fetch("*", base + "/wp-json/wp/v2/posts") or not rp.can_fetch("*", base + "/"):
            raise SiteRejected(f"{host} asks automated tools not to read it (its robots.txt disallows it), so the "
                               "app does not add it.")

    home = await get("/")
    if home.status in (403, 503) and ("cf-mitigated" in home.headers or b"Just a moment" in home.body[:4000]
                                      or b"challenge-platform" in home.body[:20000]):
        raise SiteRejected(f"{host} is behind a Cloudflare bot check. The app does not get around those, so it "
                           "cannot read the site.")
    if home.status in (401, 403):
        raise SiteRejected(f"{host} refused access (HTTP {home.status}) — it needs a login or blocks apps.")
    if home.status >= 400:
        raise SiteRejected(f"{host} answered with an error (HTTP {home.status}). Check the address.")

    api = await get(f"/wp-json/wp/v2/posts?per_page={SAMPLE}&_fields=id,title,link,content,categories")
    data = None
    if api.status == 200:
        try:
            data = json.loads(api.body)
        except ValueError:
            data = None
    if not isinstance(data, list):
        if api.status in (401, 403):
            raise SiteRejected(f"{host} has its WordPress posts API switched off or locked, so the app has no "
                               "reliable way to read its games.")
        raise SiteRejected(f"{host} is not a WordPress site with a public posts API (like SteamRIP), so the app has "
                           "no reliable way to read its games. Other kinds of sites cannot be added.")
    if not data:
        raise SiteRejected(f"{host} has no posts to read.")

    check = SiteCheck(source_id, host, base, host)
    for post in data[:SAMPLE]:
        if not isinstance(post, dict):
            continue
        check.posts_checked += 1
        html = ((post.get("content") or {}).get("rendered") or "") if isinstance(post.get("content"), dict) else ""
        title = BeautifulSoup(((post.get("title") or {}).get("rendered") or ""), "lxml").get_text(" ", strip=True)
        soup = BeautifulSoup(html, "lxml")
        links = download_links(soup)
        if links and (_GAME_WORDS.search(soup.get_text(" ", strip=True)) or _GAME_WORDS.search(title)):
            check.posts_with_downloads += 1
            check.in_app += sum(1 for m in links if m.supported)
            if title:
                check.examples.append(title)
    if check.posts_with_downloads < MIN_WITH_DOWNLOADS:
        raise SiteRejected(
            f"{host} does not look like a game download site: only {check.posts_with_downloads} of its "
            f"{check.posts_checked} newest posts contain game downloads (torrent/magnet or file-host links with game "
            "details). Only sites like SteamRIP can be added.")
    try:
        meta = await get("/wp-json/", ttl=3600)
        name = (json.loads(meta.body).get("name") or "").strip() if meta.status == 200 else ""
    except (ValueError, AttributeError):
        name = ""
    check.name = BeautifulSoup(name, "lxml").get_text(" ", strip=True)[:40] if name else host
    return check


class WordPressSiteAdapter(SteamRipAdapter):
    """A user-added WordPress game site, read through its public posts API (like SteamRIP)."""

    def __init__(self, fetcher, source_id: str, name: str, base_url: str):
        super().__init__(fetcher, base_url)
        self.generic = True
        self.info = SourceInfo(
            id=source_id, name=name, homepage=base_url,
            description=f"Added by you ({urlsplit(base_url).hostname}).",
            capabilities={"search", "pagination", "genres"},
            notes=["Added by you. Read through its public WordPress posts API with caching and rate limits.",
                   "Magnet and .torrent links download in the app; file-host links open in the in-app browser."],
            page_size=24)

    async def resolve(self, method: DownloadMethod, game) -> ResolvedDownload:
        if method.resolver == "magnet":
            return ResolvedDownload("torrent", url=method.url)
        if method.resolver == "torrent-file":
            res = await self.fetcher.get(method.url, ttl=0, max_bytes=8 * 1024 * 1024, force=True)
            if res.status != 200 or not res.body.startswith(b"d"):
                raise SourceError(f"The .torrent file could not be fetched (HTTP {res.status}).")
            return ResolvedDownload("torrent", torrent_bytes=res.body)
        if method.resolver == "pixeldrain":
            return await resolve_pixeldrain(self.fetcher, method)
        raise ManualActionRequired(method.note or "Open this link in your browser.", method.url)
