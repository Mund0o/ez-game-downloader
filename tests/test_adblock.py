"""Ad blocklist: matching, validated updates, fallback to the bundled list, conditional checks."""

import httpx
import pytest

from ezgd.net import adblock


@pytest.fixture(autouse=True)
def _restore():
    yield
    adblock.set_data_dir(None)
    adblock.reload()


def big_list(n=31_000, extra=()):
    lines = ["# Title: StevenBlack/hosts", "# Date: 09 October 2026 10:00:00 (UTC)", "127.0.0.1 localhost",
             "0.0.0.0 0.0.0.0"]
    lines += [f"0.0.0.0 ads{i}.example-ads.net" for i in range(n)] + [f"0.0.0.0 {d}" for d in extra]
    return "\n".join(lines) + "\n"


def test_matching_and_never_blocked_sites():
    assert adblock.is_blocked("x.y.popads.net") and adblock.is_blocked("pagead2.googlesyndication.com")
    for ok in ("bzzhr.to", "steamrip.com", "online-fix.me", "challenges.cloudflare.com", "www.google.com", ""):
        assert not adblock.is_blocked(ok)
    assert not adblock.is_blocked("ads.popads.net", {"popads.net"})       # a site the user chose to use
    domains, date = adblock.parse_hosts(big_list(5, ["bad host name", "localhost"]))
    assert len(domains) == 5 and date.startswith("09 October 2026")


def test_updates_are_validated_and_swapped_in(tmp_path):
    adblock.set_data_dir(tmp_path)
    adblock.reload()
    assert adblock.info()["source"] == "bundled"
    with pytest.raises(ValueError, match="only 12 domains"):
        adblock.install_update(big_list(12))                            # truncated / not the list
    assert adblock.info()["source"] == "bundled" and not (tmp_path / adblock.LIST_FILE).exists()
    info = adblock.install_update(big_list(extra=["fresh-popunder.example"]))
    assert info["source"] == "downloaded" and info["date"].startswith("09 October 2026")
    assert adblock.is_blocked("fresh-popunder.example") and adblock.is_blocked("x.monetag.com")   # + EXTRA
    (tmp_path / adblock.LIST_FILE).write_bytes(b"not gzip")               # damaged file → bundled list again
    adblock.reload()
    assert adblock.info()["source"] == "bundled" and adblock.is_blocked("popads.net")


async def test_core_update_conditional_and_errors(app_paths):
    from ezgd.core.app_core import AppCore
    calls = []
    state = {"mode": "ok"}

    filters = "[Adblock Plus 2.0]\n" + "\n".join(f"||ad{i}.example^" for i in range(25_000)) + "\n##.ad-banner\n"

    def handler(req: httpx.Request) -> httpx.Response:
        if state["mode"] == "fail":
            return httpx.Response(500, content=b"down")
        if "StevenBlack" not in str(req.url):          # EasyList & co.
            if req.headers.get("if-none-match") == '"f1"':
                return httpx.Response(304)
            return httpx.Response(200, content=filters.encode(), headers={"etag": '"f1"'})
        calls.append(dict(req.headers))
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=big_list().encode(), headers={"etag": '"v1"'})
    core = AppCore(app_paths, transport=httpx.MockTransport(handler), enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        r = await core.update_adblock()
        assert r["result"] == "updated" and r["source"] == "downloaded" and r["count"] >= 31_000
        assert r["filters_available"] and r["filter_lists"] == 6 and r["filter_rules"] >= 6 * 25_000
        r = await core.update_adblock()
        assert r["result"] == "up to date" and calls[-1].get("if-none-match") == '"v1"'
        state["mode"] = "fail"
        with pytest.raises(Exception, match="Could not update the ad blocklist"):
            await core.update_adblock(force=True)
        st = core.adblock_status()
        assert st["source"] == "downloaded" and "500" in st["last_error"]   # the working list is kept
        assert core.settings.get("browser_adblock_auto_update") is True
    finally:
        await core.shutdown()
        from ezgd.net import filterlists
        filterlists.set_data_dir(None)              # later tests use the bundled lists again
        filterlists._ENGINE = None
