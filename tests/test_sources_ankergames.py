"""AnkerGames adapter: listings, game pages, sitemap search and robots.txt limits."""

import pytest

from conftest import fixture_bytes
from ezgd.catalog.models import Game, MethodKind
from ezgd.sources import ADAPTER_CLASSES
from ezgd.sources import ankergames as ag
from ezgd.sources.base import ManualActionRequired, SourceError

BASE = "https://ankergames.net"
ROBOTS = b"User-agent: *\nDisallow: /download/\nDisallow: /search/\nDisallow: /livewire/\n"
SITEMAP = b"""<?xml version="1.0"?><urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
<url><loc>https://ankergames.net/game/atomfall</loc>
  <image:image><image:loc>https://ankergames.net/uploads/poster/10-2026/cover-a.jpg</image:loc></image:image></url>
<url><loc>https://ankergames.net/game/sniper-elite-4</loc>
  <image:image><image:loc>https://ankergames.net/uploads/screenshots/x.jpg</image:loc></image:image>
  <image:image><image:loc>https://ankergames.net/uploads/poster/se4.jpg</image:loc></image:image></url>
<url><loc>https://ankergames.net/game/sniper-elite-3</loc></url>
<url><loc>https://evil.example/game/x</loc></url>
</urlset>"""


def test_listing_cards():
    games, more = ag.parse_listing(fixture_bytes("ankergames_games.html").decode())
    assert more and len(games) == 6
    g = games[0]
    assert g.id == "ankergames:paperhead" and g.url == f"{BASE}/game/paperhead" and g.title == "PAPERHEAD"
    assert g.version == "V 4.2" and g.genres == ["Action"] and g.release_date == "2026"
    assert g.size_text == "4.90 GB" and g.cover_url.startswith(f"{BASE}/uploads/poster/")
    assert games[1].version == "B 23932740"                         # build numbers are kept as shown


def test_game_page_details():
    g = Game("ankergames", "atomfall", "Atomfall", f"{BASE}/game/atomfall")
    g = ag.parse_details(fixture_bytes("ankergames_game_atomfall.html").decode(), g)
    assert g.title == "Atomfall" and g.version == "v1.2.2" and g.size_text == "59 GB"
    assert g.steam_app_id == 801800 and g.release_date == "2025-03-27" and g.platforms == ["Windows"]
    assert {"Action", "Survival", "Shooter"} <= set(g.genres)          # FPS → Shooter
    assert g.extra["release_group"] == "voices38" and g.extra["developer"] == "Rebellion"
    assert g.extra["launch_exe_hint"] == "bin/Atomfall_dx12.exe" and g.extra["preinstalled"] is True
    assert g.description.startswith("A survival-action game") and "/poster/" in g.cover_url
    [m] = g.download_methods
    assert m.kind is MethodKind.EXTERNAL and not m.supported and m.url == g.url and "Download" in m.note


def test_sitemap_search():
    index = ag.parse_sitemap(SITEMAP.decode())
    assert index == [("atomfall", f"{BASE}/uploads/poster/10-2026/cover-a.jpg"),
                     ("sniper-elite-4", f"{BASE}/uploads/poster/se4.jpg"), ("sniper-elite-3", None)]
    assert [s for s, _ in ag.search_index(index, "sniper elite")] == ["sniper-elite-3", "sniper-elite-4"]
    assert [s for s, _ in ag.search_index(index, "atomf")] == ["atomfall"]       # unfinished last word
    assert ag.search_index(index, "elite 5") == []
    assert ag.title_from_slug("ori-and-the-will-of-the-wisps") == "Ori and the Will of the Wisps"
    assert ag.title_from_slug("grand-theft-auto-v") == "Grand Theft Auto V"


async def test_adapter_respects_robots_and_searches_sitemaps(routes, mock_fetcher):
    routes.add(f"{BASE}/robots.txt", ROBOTS, content_type="text/plain")
    routes.add(f"{BASE}/sitemap_image_1.xml", SITEMAP, content_type="application/xml")
    routes.add(f"{BASE}/sitemap_image_2.xml", b"", status=404)
    routes.add(f"{BASE}/games*", fixture_bytes("ankergames_games.html"), content_type="text/html")
    a = ag.AnkerGamesAdapter(mock_fetcher)
    page = await a.search("atomfall")
    assert [g.id for g in page.games] == ["ankergames:atomfall"] and page.games[0].cover_url
    assert not [c for c in routes.calls if "/search" in str(c.url)]          # never the disallowed search page
    assert len((await a.latest(2)).games) == 6
    assert any(c.url.params.get("page") == "2" for c in routes.calls if c.url.path == "/games")
    with pytest.raises(SourceError, match="robots.txt"):
        await a._get("/download/atomfall")
    with pytest.raises(SourceError):
        await a.details(Game("ankergames", "x", "x", "https://evil.example/game/x"))
    game = Game("ankergames", "atomfall", "Atomfall", f"{BASE}/game/atomfall")
    game = ag.parse_details(fixture_bytes("ankergames_game_atomfall.html").decode(), game)
    with pytest.raises(ManualActionRequired):                 # downloads always happen in the browser
        await a.resolve(game.download_methods[0], game)


def test_ankergames_is_built_in():
    from ezgd.sources.custom import refused_platform
    from ezgd.ui import theme
    assert "ankergames" in ADAPTER_CLASSES and theme.source_name("ankergames") == "AnkerGames"
    assert "already built into the app" in refused_platform("ankergames.net")
