"""Live checks against the real websites. Deselected by default; run with:  pytest -m live

They verify that the parsers still match the sites and that permitted download
references resolve. They never transfer game content: torrent resolution only
fetches the small .torrent metadata file, Pixeldrain only its info JSON.
"""

import pytest

from ezgd.catalog.models import MethodKind
from ezgd.database.repositories import HttpCacheRepository
from ezgd.downloads.torrent_engine import inspect_torrent
from ezgd.net.client import SafeHttpClient
from ezgd.net.ratelimit import HostRateLimiter
from ezgd.sources.cache import CachedFetcher
from ezgd.sources.ankergames import AnkerGamesAdapter
from ezgd.sources.crackedgames import CrackedGamesAdapter
from ezgd.sources.fitgirl import FitGirlAdapter
from ezgd.sources.onlinefix import OnlineFixAdapter
from ezgd.sources.steamrip import SteamRipAdapter

pytestmark = pytest.mark.live


@pytest.fixture
async def live_fetcher(db):
    client = SafeHttpClient(rate_limiter=HostRateLimiter(1.0), timeout=30)
    yield CachedFetcher(client, HttpCacheRepository(db), default_ttl=3600)
    await client.aclose()


async def test_onlinefix_live(live_fetcher):
    a = OnlineFixAdapter(live_fetcher)
    page = await a.latest(1)
    assert len(page.games) >= 10 and page.has_more
    assert all(g.url.startswith("https://online-fix.me/") for g in page.games)
    sections = await a.featured()
    assert {s.key for s in sections} >= {"popular", "updates"}
    found = await a.search("peak")
    assert any(g.title.lower() == "peak" for g in found.games)
    game = next(g for g in found.games if g.title.lower() == "peak")
    game = await a.details(game)
    torrent = next(m for m in game.download_methods if m.kind is MethodKind.TORRENT)
    res = await a.resolve(torrent, game)
    meta = inspect_torrent(res.torrent_bytes)            # metadata only — nothing is downloaded
    assert meta.total_size > 100 * 1024 * 1024 and meta.files
    pix = [m for m in game.download_methods if m.resolver == "pixeldrain"]
    if pix:
        from ezgd.sources.base import ManualActionRequired
        try:
            r = await a.resolve(pix[0], game)
            assert r.url.startswith("https://pixeldrain.com/api/file/")
        except ManualActionRequired:
            pass                                          # captcha currently required → correct fallback


async def test_steamrip_live(live_fetcher):
    a = SteamRipAdapter(live_fetcher)
    page = await a.latest(1)
    assert len(page.games) == 24 and page.has_more and page.total and page.total > 1000
    found = await a.search("peak")
    peak = next(g for g in found.games if g.title == "PEAK")
    peak = await a.details(peak)
    assert peak.steam_app_id and peak.download_methods
    assert all(m.kind in (MethodKind.EXTERNAL, MethodKind.DIRECT) for m in peak.download_methods)


async def test_crackedgames_live(live_fetcher):
    a = CrackedGamesAdapter(live_fetcher)
    page = await a.latest(1)
    assert len(page.games) >= 20 and page.has_more
    assert all(g.url.startswith("https://cracked-games.org/") and g.url.endswith("-free-download")
               for g in page.games)
    found = await a.search("hytale")
    game = next(g for g in found.games if g.title.lower() == "hytale")
    game = await a.details(game)
    assert game.extra["archive_password"] == "cracked-games.org"
    assert len(game.extra.get("sha256", "")) == 64
    [m] = game.download_methods
    assert m.kind is MethodKind.EXTERNAL and not m.supported



async def test_fitgirl_live(live_fetcher):
    a = FitGirlAdapter(live_fetcher)
    page = await a.latest(1)
    assert len(page.games) >= 15 and page.has_more
    assert all(g.url.startswith("https://fitgirl-repacks.site/") and g.cover_url for g in page.games)
    found = await a.search("elden ring")
    game = await a.details(found.games[0])
    assert game.download_methods[0].kind is MethodKind.TORRENT and game.download_methods[0].supported
    assert game.extra["installer"] == "fitgirl" and game.size_bytes


async def test_ankergames_live(live_fetcher):
    a = AnkerGamesAdapter(live_fetcher)
    page = await a.latest(1)
    assert len(page.games) >= 30 and page.has_more
    assert all(g.url.startswith("https://ankergames.net/game/") and g.cover_url for g in page.games)
    assert (await a.latest(2)).games[0].id != page.games[0].id
    assert (await a.latest(1, genre="RPG")).games
    found = await a.search("atomfall")
    assert found.games[0].source_game_id == "atomfall" and found.games[0].cover_url
    game = await a.details(found.games[0])
    assert game.title == "Atomfall" and game.steam_app_id == 801800 and game.size_bytes
    assert game.extra.get("launch_exe_hint", "").endswith(".exe")
    [m] = game.download_methods
    assert m.kind is MethodKind.EXTERNAL and not m.supported
