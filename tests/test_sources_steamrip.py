import json

import pytest

from conftest import fixture_bytes
from ezgd.catalog.models import Game, MethodKind
from ezgd.sources import steamrip as sr
from ezgd.sources.base import LayoutChangedError, ManualActionRequired, SourceError
from ezgd.sources.hosts import classify_host_link, pixeldrain_id, resolve_pixeldrain

API = "https://steamrip.com/wp-json/wp/v2"
CATS = {c["id"]: c["name"] for c in json.loads(fixture_bytes("steamrip_categories.json"))}
JSON = "application/json; charset=UTF-8"


def test_posts_are_normalized():
    games = sr.parse_posts(fixture_bytes("steamrip_posts.json"), CATS)
    assert [g.title for g in games] == ["Order of the Sinking Star", "Gah!", "Stickility"]
    g = games[0]
    assert g.id == "steamrip:59703" and g.url.startswith("https://steamrip.com/")
    assert "Adventure" in g.genres and "Indie" in g.genres
    assert g.cover_url.endswith(".jpg") and g.published_at
    assert g.download_methods is None


def test_post_details():
    post = json.loads(fixture_bytes("steamrip_post_peak.json"))
    g = sr.parse_post(post, CATS)
    assert g.title == "PEAK" and g.version == "v2.6.b + Co-op"
    g = sr.parse_content(post["content"]["rendered"], g)
    assert g.platforms == ["PC"] and g.size_text == "1.42 GB" and g.size_bytes > 1_400_000_000
    assert g.steam_app_id == 3527290
    assert g.extra["developer"] == "Landcrab" and g.extra["preinstalled"] is True
    assert g.description.startswith("PEAK is a co-op climbing game")
    assert [(m.label, m.kind, m.supported) for m in g.download_methods] == [
        ("Gofile", MethodKind.EXTERNAL, False), ("Buzzheavier", MethodKind.EXTERNAL, False)]
    assert all(m.note for m in g.download_methods)


def test_api_error_shapes():
    assert sr.parse_posts(json.dumps({"code": "rest_post_invalid_page_number"}).encode(), CATS) == []
    with pytest.raises(SourceError):
        sr.parse_posts(json.dumps({"code": "rest_forbidden", "message": "nope"}).encode(), CATS)
    with pytest.raises(LayoutChangedError):
        sr.parse_posts(b"<html>cloudflare</html>", CATS)
    with pytest.raises(LayoutChangedError):
        sr.parse_posts(b'{"unexpected": true}', CATS)


def test_malformed_posts_are_skipped():
    data = json.dumps([{"id": 1}, {"id": 2, "link": "https://steamrip.com/x/", "title": {"rendered": ""}},
                       {"id": 3, "link": "https://steamrip.com/ok/", "title": {"rendered": "Ok &#8211; Game Free Download"},
                        "categories": [999999]}]).encode()
    games = sr.parse_posts(data, CATS)
    assert [g.title for g in games] == ["Ok – Game"]
    assert games[0].genres == [] and games[0].cover_url is None


async def test_adapter_listing_search_details(routes, mock_fetcher):
    routes.add(f"{API}/categories*", fixture_bytes("steamrip_categories.json"), content_type=JSON)
    routes.add(f"{API}/posts?per_page=*", fixture_bytes("steamrip_posts.json"), content_type=JSON,
               headers={"x-wp-total": "4884", "x-wp-totalpages": "1628"})
    routes.add(f"{API}/posts?search=*", fixture_bytes("steamrip_posts.json"), content_type=JSON,
               headers={"x-wp-total": "3", "x-wp-totalpages": "1"})
    routes.add(f"{API}/posts/43214?*", fixture_bytes("steamrip_post_peak.json"), content_type=JSON)
    a = sr.SteamRipAdapter(mock_fetcher)
    page = await a.latest(1)
    assert len(page.games) == 3 and page.has_more and page.total == 4884
    res = await a.search("peak")
    assert not res.has_more and res.total == 3
    search_call = [c for c in routes.calls if "search=" in str(c.url)][0]
    assert search_call.url.params["search_columns"] == "post_title"
    g = await a.details(Game("steamrip", "43214", "PEAK", "https://steamrip.com/peak-free-download/"))
    assert g.steam_app_id == 3527290 and g.download_methods
    with pytest.raises(ManualActionRequired):
        await a.resolve(g.download_methods[0], g)
    assert "RPG" in await a.genres()


async def test_genre_filter_uses_category_ids(routes, mock_fetcher):
    routes.add(f"{API}/categories*", fixture_bytes("steamrip_categories.json"), content_type=JSON)
    routes.add(f"{API}/posts?*", b"[]", content_type=JSON, headers={"x-wp-totalpages": "0"})
    a = sr.SteamRipAdapter(mock_fetcher)
    page = await a.latest(1, genre="Shooter")
    assert page.games == [] and not page.has_more
    call = [c for c in routes.calls if "/posts?" in str(c.url)][0]
    assert set(call.url.params["categories"].split(",")) == {"52", "41"}   # FPS + Shooters


async def test_details_refuses_foreign_url(mock_fetcher):
    a = sr.SteamRipAdapter(mock_fetcher)
    with pytest.raises(SourceError):
        await a.details(Game("steamrip", "1", "x", "https://evil.example/x"))


def test_host_classification():
    assert pixeldrain_id("https://pixeldrain.com/u/B3tFyvQC") == "B3tFyvQC"
    assert pixeldrain_id("https://pixeldrain.com/l/abcdefgh") is None
    assert pixeldrain_id("https://evil.com/u/B3tFyvQC") is None
    m = classify_host_link("//bzzhr.to/abc")
    assert m.url == "https://bzzhr.to/abc" and m.kind is MethodKind.EXTERNAL and "Cloudflare" in m.note
    assert classify_host_link("https://unknown-host.example/f").kind is MethodKind.EXTERNAL


async def test_pixeldrain_resolution(routes, mock_fetcher):
    info = json.loads(fixture_bytes("pixeldrain_info.json"))
    routes.add("https://pixeldrain.com/api/file/B3tFyvQC/info", json.dumps(info), content_type=JSON)
    m = classify_host_link("https://pixeldrain.com/u/B3tFyvQC", archive_password="pw")
    r = await resolve_pixeldrain(mock_fetcher, m)
    assert r.kind == "http" and r.url == "https://pixeldrain.com/api/file/B3tFyvQC?download"
    assert r.checksum == "sha256:" + info["hash_sha256"] and r.size_bytes == info["size"]
    assert r.archive_password == "pw"

    info["availability"] = "file_rate_limited_captcha_required"
    info["availability_message"] = "This file is using too much bandwidth"
    routes.add("https://pixeldrain.com/api/file/B3tFyvQC/info", json.dumps(info), content_type=JSON)
    with pytest.raises(ManualActionRequired, match="browser"):
        await resolve_pixeldrain(mock_fetcher, m)

    routes.add("https://pixeldrain.com/api/file/B3tFyvQC/info", '{"success":false}', status=404, content_type=JSON)
    with pytest.raises(SourceError, match="no longer exists"):
        await resolve_pixeldrain(mock_fetcher, m)


async def test_fileditch_opens_via_the_game_page(db):
    """FileDitch bounces visits that do not come from SteamRIP's page to steamrip.com (Silver Pines, 2026-10-10):
    the app opens the game page instead of the bare link (it never fakes a Referer)."""
    from ezgd.database.repositories import GameRepository, SourceRepository
    from ezgd.sources.hosts import browser_url, opens_from_source_page
    from ezgd.sources.registry import SourceRegistry
    page = "https://steamrip.com/silver-pines-free-download/"
    fd = classify_host_link("https://fileditchfiles.st/balpha28/ad123/Silver.Pines-SteamRIP.com.rar")
    bz = classify_host_link("https://bzzhr.to/g741t4u0ebl4")            # Buzzheavier does the same
    go = classify_host_link("https://gofile.io/d/c9BPVSYm")
    assert opens_from_source_page(fd.url) and opens_from_source_page(bz.url) and not opens_from_source_page(go.url)
    assert browser_url(fd, page) == page and browser_url(bz, page) == page and browser_url(go, page) == go.url
    assert "click “FileDitch” there" in fd.note and "click “Buzzheavier” there" in bz.note
    game = Game("steamrip", "59671", "Silver Pines", page, download_methods=[fd, go])
    reg = SourceRegistry([sr.SteamRipAdapter(None)], GameRepository(db), SourceRepository(db))
    with pytest.raises(ManualActionRequired) as exc:
        await reg.resolve(game, fd)
    assert exc.value.url == page
    with pytest.raises(ManualActionRequired) as exc:
        await reg.resolve(game, go)
    assert exc.value.url == go.url
