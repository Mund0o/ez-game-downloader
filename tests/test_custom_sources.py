"""User-added sites: refused with a clear reason unless they are readable game download sites."""

import json

import httpx
import pytest

from ezgd.sources.custom import SiteRejected, WordPressSiteAdapter, check_site, normalise, refused_platform

BASE = "https://repacks.example"
JSON = "application/json; charset=UTF-8"


def game_post(i: int, magnet=True) -> dict:
    links = (f'<a href="magnet:?xt=urn:btih:{i:040x}&dn=Game{i}">Magnet</a>' if magnet else "") + \
        f'<a href="https://pixeldrain.com/u/abc{i:05d}">Pixeldrain</a>'
    return {"id": i, "link": f"{BASE}/game-{i}/", "title": {"rendered": f"Game {i} – v1.0 + Repack"},
            "categories": [1], "date_gmt": "2026-10-01T10:00:00", "modified_gmt": "2026-10-02T10:00:00",
            "content": {"rendered": f"<p>Game {i} is a PC game.</p><ul><li>Genre: Action</li></ul>"
                                    f"<h3>System Requirements</h3><p>DirectX 11, GPU: GTX 1060</p>{links}"}}


def recipe_post(i: int) -> dict:
    return {"id": i, "link": f"{BASE}/soup-{i}/", "title": {"rendered": f"Soup recipe {i}"}, "categories": [1],
            "content": {"rendered": "<p>Chop the onions.</p><a href='https://pixeldrain.com/u/xyz00001'>PDF</a>"}}


def site(routes, posts, robots="User-agent: *\nDisallow: /wp-admin/\n", home_status=200, home_headers=None):
    routes.add(f"{BASE}/robots.txt", robots, content_type="text/plain")
    routes.add(f"{BASE}/", "<html><body>home</body></html>", status=home_status, headers=home_headers or {})
    routes.add(f"{BASE}/wp-json/wp/v2/posts?*", json.dumps(posts), content_type=JSON,
               headers={"x-wp-total": str(len(posts)), "x-wp-totalpages": "1"})
    routes.add(f"{BASE}/wp-json/", json.dumps({"name": "Example Repacks"}), content_type=JSON)
    routes.add(f"{BASE}/wp-json/wp/v2/categories*", json.dumps([{"id": 1, "name": "Action", "slug": "action"}]),
               content_type=JSON)


def test_platforms_and_addresses_are_refused():
    for host, word in [("youtube.com", "video site"), ("music.youtube.com", "video site"),
                       ("open.spotify.com", "music"), ("www.netflix.com", "video"), ("amazon.de", "not a game"),
                       ("store.steampowered.com", "official store"), ("www.gog.com", "official store"),
                       ("steamrip.com", "already built into the app"), ("reddit.com", "discussion")]:
        assert word in refused_platform(host), host
    assert refused_platform("repacks.example") is None
    for bad, word in [("", "Enter the site"), ("ftp://x.example", "not a web address"),
                      ("http://192.168.1.10", "IP address"), ("localhost", "IP address")]:
        with pytest.raises(SiteRejected, match=word):
            normalise(bad)
    assert normalise("repacks.example/some/page") == ("https://repacks.example", "repacks.example")


async def test_rejections_explain_why(routes, mock_fetcher):
    site(routes, [game_post(i) for i in range(5)], robots="User-agent: *\nDisallow: /\n")
    with pytest.raises(SiteRejected, match="robots.txt"):
        await check_site(mock_fetcher, BASE)


async def test_cloudflare_and_not_wordpress(routes, mock_fetcher):
    site(routes, [], home_status=403, home_headers={"cf-mitigated": "challenge"})
    with pytest.raises(SiteRejected, match="Cloudflare bot check"):
        await check_site(mock_fetcher, BASE)


async def test_not_wordpress(routes, mock_fetcher):
    routes.add(f"{BASE}/robots.txt", "", status=404)
    routes.add(f"{BASE}/", "<html>a shop</html>")
    with pytest.raises(SiteRejected, match="not a WordPress site"):
        await check_site(mock_fetcher, BASE)


async def test_wordpress_blog_without_game_downloads(routes, mock_fetcher):
    site(routes, [recipe_post(i) for i in range(10)])
    with pytest.raises(SiteRejected, match="does not look like a game download site: only 0 of its 10"):
        await check_site(mock_fetcher, BASE)


async def test_game_site_is_added_and_works(routes, mock_fetcher):
    posts = [game_post(i) for i in range(1, 9)] + [recipe_post(100), recipe_post(101)]
    site(routes, posts)
    check = await check_site(mock_fetcher, "repacks.example")
    assert check.source_id == "site:repacks.example" and check.name == "Example Repacks"
    assert check.posts_with_downloads == 8 and check.in_app >= 16
    a = WordPressSiteAdapter(mock_fetcher, check.source_id, check.name, check.base_url)
    page = await a.latest()
    assert len(page.games) == 10 and page.games[0].source == "site:repacks.example"
    routes.add(f"{BASE}/wp-json/wp/v2/posts/3?*", json.dumps(game_post(3)), content_type=JSON)
    g = await a.details(next(g for g in page.games if g.source_game_id == "3"))
    kinds = {m.resolver for m in g.download_methods}
    assert kinds == {"magnet", "pixeldrain"} and all(m.supported for m in g.download_methods)
    magnet = next(m for m in g.download_methods if m.resolver == "magnet")
    r = await a.resolve(magnet, g)
    assert r.kind == "torrent" and r.url.startswith("magnet:?")


async def test_core_add_persist_remove(app_paths, routes):
    from ezgd.core.app_core import AppCore
    site(routes, [game_post(i) for i in range(1, 6)])
    events = []

    async def started():
        c = AppCore(app_paths, transport=httpx.MockTransport(routes), enable_torrents=False)
        c.set_emitter(lambda n, p: events.append(n))
        await c.start()
        return c
    core = await started()
    with pytest.raises(SiteRejected, match="video site"):
        await core.add_custom_source("https://www.youtube.com/watch?v=x")
    r = await core.add_custom_source(BASE)
    assert r["id"] == "site:repacks.example" and "sources_changed" in events
    assert "site:repacks.example" in core.registry.adapters and core.settings.get("sources_enabled")[r["id"]]
    res = await core.latest(1, sources=[r["id"]])
    assert len(res.games) == 5
    with pytest.raises(SiteRejected, match="already in your sources"):
        await core.add_custom_source(BASE)
    await core.shutdown()
    core = await started()                                    # survives a restart
    try:
        info = {s["id"]: s for s in core.sources_info()}
        assert info[r["id"]]["custom"] and info[r["id"]]["name"] == "Example Repacks"
        assert not info["steamrip"]["custom"]
        with pytest.raises(Exception, match="Only sites you added"):
            await core.remove_custom_source("steamrip")
        await core.remove_custom_source(r["id"])
        assert r["id"] not in core.registry.adapters
    finally:
        await core.shutdown()
