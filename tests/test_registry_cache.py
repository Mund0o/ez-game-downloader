import httpx
import pytest

from conftest import fixture_bytes
from ezgd.database.repositories import GameRepository, HttpCacheRepository, SourceRepository
from ezgd.net.client import SafeHttpClient
from ezgd.sources.cache import CachedFetcher, SourceUnavailableError
from ezgd.sources.onlinefix import OnlineFixAdapter
from ezgd.sources.registry import STATUS_LAYOUT, STATUS_OK, STATUS_STALE, STATUS_UNAVAILABLE, SourceRegistry
from ezgd.sources.steamrip import SteamRipAdapter

CP = "text/html; charset=windows-1251"


async def test_conditional_request_and_stale_fallback(db):
    state = {"mode": "ok", "calls": []}

    def handler(req):
        state["calls"].append(dict(req.headers))
        if state["mode"] == "down":
            raise httpx.ConnectError("boom")
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"body-v1", headers={"etag": '"v1"', "content-type": "text/plain"})

    client = SafeHttpClient(transport=httpx.MockTransport(handler))
    f = CachedFetcher(client, HttpCacheRepository(db), default_ttl=0)
    try:
        r1 = await f.get("https://example.org/a")
        assert r1.body == b"body-v1" and not r1.from_cache
        r2 = await f.get("https://example.org/a")          # ttl 0 → revalidate → 304
        assert r2.body == b"body-v1" and r2.from_cache and not r2.stale
        assert state["calls"][-1]["if-none-match"] == '"v1"'
        state["mode"] = "down"
        r3 = await f.get("https://example.org/a")
        assert r3.stale and r3.body == b"body-v1"
        with pytest.raises(SourceUnavailableError):
            await f.get("https://example.org/never-cached")
    finally:
        await client.aclose()


async def test_server_errors_do_not_poison_cache(db):
    seq = [200, 503]

    def handler(req):
        code = seq.pop(0) if seq else 200
        return httpx.Response(code, content=b"x" if code == 200 else b"err")
    client = SafeHttpClient(transport=httpx.MockTransport(handler))
    f = CachedFetcher(client, HttpCacheRepository(db), default_ttl=0)
    try:
        await f.get("https://example.org/b")
        r = await f.get("https://example.org/b")
        assert r.stale and r.body == b"x"
    finally:
        await client.aclose()


def make_registry(db, handler):
    client = SafeHttpClient(transport=httpx.MockTransport(handler))
    fetcher = CachedFetcher(client, HttpCacheRepository(db), default_ttl=3600)
    reg = SourceRegistry([OnlineFixAdapter(fetcher), SteamRipAdapter(fetcher)], GameRepository(db),
                         SourceRepository(db), timeout=5)
    return reg, client


async def test_one_source_down_does_not_break_search(db):
    def handler(req):
        if req.url.host == "steamrip.com":
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, content=fixture_bytes("onlinefix_search.html"), headers={"content-type": CP})

    reg, client = make_registry(db, handler)
    try:
        res = await reg.search("dayz")
    finally:
        await client.aclose()
    assert [g.title for g in res.games] == ["DayZ (DayZavr)"]
    assert "steamrip" in res.errors and "unreachable" in res.errors["steamrip"].lower()
    assert reg.status["onlinefix"].state == STATUS_OK
    assert reg.status["steamrip"].state == STATUS_UNAVAILABLE
    assert SourceRepository(db).get("steamrip")["status"] == STATUS_UNAVAILABLE


async def test_layout_change_reported_and_offline_cache_used(db):
    mode = {"v": "ok"}

    def handler(req):
        if req.url.host == "steamrip.com":
            raise httpx.ConnectError("off")
        if mode["v"] == "broken":
            return httpx.Response(200, content=b"<html><body>new design</body></html>", headers={"content-type": CP})
        return httpx.Response(200, content=fixture_bytes("onlinefix_home.html"), headers={"content-type": CP})

    reg, client = make_registry(db, handler)
    try:
        first = await reg.latest(1, sources={"onlinefix"})
        assert len(first.games) == 3
        GameRepository(db)  # cached
        HttpCacheRepository(db).clear()
        mode["v"] = "broken"
        res = await reg.latest(1, sources={"onlinefix"})
    finally:
        await client.aclose()
    assert reg.status["onlinefix"].state == STATUS_LAYOUT
    assert {g.title for g in res.games} >= {"DayZ (DayZavr)"}       # served from the local catalog


async def test_featured_cached_between_calls(db):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=fixture_bytes("onlinefix_home.html"), headers={"content-type": CP})
    reg, client = make_registry(db, handler)
    try:
        s1, at1, err1 = await reg.featured("onlinefix")
        s2, at2, err2 = await reg.featured("onlinefix")
    finally:
        await client.aclose()
    assert err1 is None and err2 is None
    assert [s.key for s in s1] == ["popular", "updates"] == [s.key for s in s2]
    assert len(calls) == 1


async def test_stale_status(db):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] > 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, content=fixture_bytes("onlinefix_home.html"), headers={"content-type": CP})
    reg, client = make_registry(db, handler)
    reg.adapters["onlinefix"].fetcher.default_ttl = 0
    try:
        await reg.latest(1, sources={"onlinefix"})
        res = await reg.latest(1, sources={"onlinefix"})
    finally:
        await client.aclose()
    assert "onlinefix" in res.stale and reg.status["onlinefix"].state == STATUS_STALE


async def test_withdrawn_games_are_hidden(db):
    def handler(req):
        if req.url.host == "steamrip.com":
            return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})
        return httpx.Response(200, content=fixture_bytes("onlinefix_search_closed.html"), headers={"content-type": CP})
    reg, client = make_registry(db, handler)
    try:
        res = await reg.search("minecraft dungeons", sources={"onlinefix"})
        assert [g.title for g in res.games] == ["Minecraft Dungeons"]
        reg.hide_withdrawn = False
        HttpCacheRepository(db).clear()
        res = await reg.search("minecraft dungeons", sources={"onlinefix"})
        assert sorted(g.title for g in res.games) == ["Minecraft Dungeons", "Minecraft Dungeons II"]
    finally:
        await client.aclose()
