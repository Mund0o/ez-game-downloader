"""A site whose address could not be found for a moment is tried again before the player sees an error (a friend,
cracked-games.org, 2026-10-28: "could not resolve … Name or service not known", fine on his second Try again)."""

import pytest

from ezgd.database.repositories import HttpCacheRepository
from ezgd.net import client as client_mod
from ezgd.net.client import GuardedBackend, SafeHttpClient
from ezgd.net.urls import LookupFailed, UnsafeURLError
from ezgd.sources.cache import CachedFetcher, SourceUnavailableError


@pytest.fixture
def lookups(monkeypatch):
    """resolve_public that fails ``state["fail"]`` times (``state["error"]``), then answers 127.0.0.1."""
    state = {"fail": 0, "calls": 0, "error": lambda host: LookupFailed(f"{host} could not be found (Name or service "
                                                                       f"not known)")}

    async def fake(host, port, *, allow_private=False):
        state["calls"] += 1
        if state["calls"] <= state["fail"]:
            raise state["error"](host)
        return ["127.0.0.1"]
    monkeypatch.setattr(client_mod, "resolve_public", fake)
    monkeypatch.setattr(GuardedBackend, "RETRY_DELAYS", (0.01, 0.02))
    return state


async def test_a_lookup_hiccup_is_tried_again(server, lookups):
    server.add("/x", data=b"page")
    lookups["fail"] = 2                                  # the third try finds it
    c = SafeHttpClient(allow_private=True, timeout=5)
    try:
        res = await c.fetch(server.url("/x"))
    finally:
        await c.aclose()
    assert res.status == 200 and res.content == b"page"
    assert lookups["calls"] == 3


async def test_a_site_still_not_found_says_so_in_words(server, lookups, db):
    lookups["fail"] = 99
    c = SafeHttpClient(allow_private=True, timeout=5)
    f = CachedFetcher(c, HttpCacheRepository(db))
    try:
        with pytest.raises(SourceUnavailableError) as err:
            await f.get(server.url("/never-cached"))
    finally:
        await c.aclose()
    assert lookups["calls"] == 3                         # three tries, then the error
    text = str(err.value)
    assert "127.0.0.1 could not be found (Name or service not known)" in text
    assert "try again" in text and "OSError" not in text


async def test_unsafe_addresses_are_never_tried_again(server, lookups):
    lookups["fail"] = 99
    lookups["error"] = lambda host: UnsafeURLError(f"{host} resolves to non-public address 10.0.0.1")
    c = SafeHttpClient(allow_private=True, timeout=5)
    try:
        with pytest.raises(UnsafeURLError):
            await c.fetch(server.url("/x"))
    finally:
        await c.aclose()
    assert lookups["calls"] == 1


async def test_a_slow_failure_is_not_repeated(server, lookups, monkeypatch):
    monkeypatch.setattr(GuardedBackend, "QUICK_FAILURE", -1.0)     # every failure counts as one that took long
    lookups["fail"] = 99
    c = SafeHttpClient(allow_private=True, timeout=5)
    try:
        with pytest.raises(LookupFailed):
            await c.fetch(server.url("/x"))
    finally:
        await c.aclose()
    assert lookups["calls"] == 1
