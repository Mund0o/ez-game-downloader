"""End-to-end: catalog → details → resolve → torrent download (local swarm) → extraction → library."""

import asyncio
import io
import zipfile

import httpx
import pytest

from conftest import fixture_bytes, make_torrent
from ezgd.core.app_core import AppCore
from ezgd.downloads.models import DownloadStatus
from ezgd.library.models import LibraryStatus
from test_torrent import LOCAL, seeder

CP = "text/html; charset=windows-1251"


def game_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PEAK/PEAK.exe", b"MZ" + b"\0" * 30_000)
        zf.writestr("PEAK/data.pak", b"D" * 50_000)
    return buf.getvalue()


@pytest.fixture
def site(routes, tmp_path):
    tor = make_torrent({"PEAK.v2.6.b-OFME.zip": game_zip(), "Fix Repair/fix.zip": game_zip()},
                       tmp_path / "seed", "PEAK")
    routes.add("https://online-fix.me/", fixture_bytes("onlinefix_home.html"), content_type=CP)
    routes.add("https://online-fix.me/games/adventures/17801-peak-po-seti.html",
               fixture_bytes("onlinefix_detail_peak.html"), content_type=CP)
    routes.add("https://hosters.online-fix.me:2053/PEAK", fixture_bytes("onlinefix_hosters.html"))
    routes.add("https://uploads.online-fix.me:2053/torrents/PEAK/", fixture_bytes("onlinefix_torrent_index.html"))
    routes.add("https://uploads.online-fix.me:2053/torrents/PEAK/PEAK.v2.6.b-OFME.torrent", tor,
               content_type="application/x-bittorrent")
    routes.add("https://online-fix.me/index.php*", fixture_bytes("onlinefix_search.html"), content_type=CP)
    routes.add("https://steamrip.com/*", b"[]", content_type="application/json")
    return tor


@pytest.fixture
async def core(app_paths, routes):
    c = AppCore(app_paths, transport=httpx.MockTransport(routes), torrent_overrides=LOCAL)
    events = []
    c.set_emitter(lambda n, p: events.append((n, p)))
    await c.start()
    c.settings.set("download_dir", str(app_paths.data.parent / "downloads"))
    c.settings.set("install_dir", str(app_paths.data.parent / "games"))
    c.events = events
    yield c
    await c.shutdown()


async def test_game_to_library_pipeline(core, site, tmp_path):
    seed_ses, _ = seeder(site, tmp_path / "seed")
    games = (await core.latest(1, sources=["onlinefix"])).games
    assert games
    from ezgd.catalog.models import Game
    core.games.save(Game("onlinefix", "17801", "PEAK", "https://online-fix.me/games/adventures/17801-peak-po-seti.html"))
    game = await core.details("onlinefix:17801")
    idx = next(i for i, m in enumerate(game.download_methods) if m.kind == "torrent")
    prep = await core.prepare_download(game.id, idx)
    assert prep["kind"] == "torrent" and prep["password"] == "online-fix.me" and prep["source"] == "onlinefix"
    assert {f["path"] for f in prep["files"]} == {"PEAK/PEAK.v2.6.b-OFME.zip", "PEAK/Fix Repair/fix.zip"}
    snap = await core.start_prepared(prep["token"], core.settings.settings.download_dir)
    tid = snap["id"]

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 40
    last_offer = 0.0
    while loop.time() < deadline:
        h = core.torrents.handles.get(tid) if core.torrents else None
        # no trackers/DHT in tests: keep offering the local seeder until a connection sticks
        if h is not None and h.is_valid() and h.status().num_peers == 0 and loop.time() - last_offer > 1.5:
            h.connect_peer(("127.0.0.1", seed_ses.listen_port()))
            last_offer = loop.time()
        entries = await core.library_entries()
        if entries and entries[0].status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED):
            break
        await asyncio.sleep(0.2)
    task = core.downloads.tasks[tid]
    assert task.status is DownloadStatus.COMPLETED, task.error
    entry = (await core.library_entries())[0]
    assert entry.status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED), entry.notes
    assert entry.game_id == "onlinefix:17801"
    assert (core.paths.data.parent / "games" / "PEAK" / "PEAK" / "PEAK.exe").exists()
    assert "Fix Repair archive was not needed" in (entry.notes or "")   # left alone: the game has the fix
    cands = await core.library_call("candidates", entry.id)
    assert cands[0].path.name == "PEAK.exe"                       # hint from the game page
    names = [n for n, _ in core.events]
    assert "download_completed" in names and "extraction" in names and "library_changed" in names
    assert task.id not in core.torrents.handles                   # not seeding by default


async def test_archive_password_fallbacks(core, tmp_path):
    from ezgd.catalog.models import Game
    assert core.source_password("onlinefix") == "online-fix.me"
    assert core.source_password("steamrip") is None
    folder = tmp_path / "g"
    folder.mkdir()
    # page did not show a password → falls back to the one OnlineFix states for all archives
    core.games.save(Game("onlinefix", "5", "No Pw Page", "https://online-fix.me/games/rpg/5-x.html"))
    e = await core.library_call("import_folder", "No Pw Page", str(folder), "onlinefix:5")
    assert await core.entry_password(e.id) == "online-fix.me"
    # a password parsed from the game page wins
    g = core.games.get("onlinefix:5")
    g.extra["archive_password"] = "page-specific"
    core.games.save(g)
    assert await core.entry_password(e.id) == "page-specific"
    core.games.save(Game("steamrip", "9", "SR Game", "https://steamrip.com/x/"))
    e2 = await core.library_call("import_folder", "SR Game", str(folder), "steamrip:9")
    assert await core.entry_password(e2.id) is None


async def test_external_methods_need_browser(core, site):
    from ezgd.catalog.models import Game
    from ezgd.sources.base import ManualActionRequired
    core.games.save(Game("onlinefix", "17801", "PEAK", "https://online-fix.me/games/adventures/17801-peak-po-seti.html"))
    game = await core.details("onlinefix:17801")
    idx = next(i for i, m in enumerate(game.download_methods) if m.label == "Online-Fix Drive")
    with pytest.raises(ManualActionRequired):
        await core.prepare_download(game.id, idx)


async def test_duplicates_across_sources_and_settings_live_update(core, site):
    from ezgd.catalog.models import Game
    core.games.upsert_many([Game("onlinefix", "1", "PEAK", "https://online-fix.me/a/1-peak.html"),
                            Game("steamrip", "2", "PEAK™", "https://steamrip.com/peak/")])
    rel = await core.related("onlinefix:1")
    assert [g.id for g in rel] == ["steamrip:2"]
    await core.set_setting("http_speed_limit_kbps", 256)
    assert core.bucket.rate == 256 * 1024
    await core.set_setting("sources_enabled", {"onlinefix": True, "steamrip": False, "crackedgames": False,
                                                 "fitgirl": False, "ankergames": False})
    assert [a.id for a in core.registry.active()] == ["onlinefix"]


async def test_core_restart_recovers_queue(app_paths, routes, server, tmp_path):
    from conftest import random_bytes
    data = random_bytes(3_000_000, seed=77)
    server.add("/f.bin", data=data, chunk_delay=0.01)
    c1 = AppCore(app_paths, allow_private_network=True, enable_torrents=False)
    await c1.start()
    snap = await c1.add_link(server.url("/f.bin"), str(tmp_path / "dl"))
    loop = asyncio.get_running_loop()
    while c1.downloads.tasks[snap["id"]].downloaded_bytes < 300_000:
        await asyncio.sleep(0.05)
    await c1.shutdown()
    assert (app_paths.backups / "last-exit.sqlite3").exists()

    c2 = AppCore(app_paths, allow_private_network=True, enable_torrents=False)
    await c2.start()
    try:
        deadline = loop.time() + 30
        while c2.downloads.tasks[snap["id"]].status is not DownloadStatus.COMPLETED and loop.time() < deadline:
            await asyncio.sleep(0.1)
        assert (tmp_path / "dl" / "f.bin").read_bytes() == data
    finally:
        await c2.shutdown()


async def test_signed_in_only_torrent_is_remembered(core, site, routes):
    from ezgd.catalog.models import Game, MethodKind
    from ezgd.sources.base import ManualActionRequired
    routes.add("https://uploads.online-fix.me:2053/torrents/PEAK/", "<html>401 Authorization Required</html>",
               status=401)
    core.games.save(Game("onlinefix", "17801", "PEAK", "https://online-fix.me/games/adventures/17801-peak-po-seti.html"))
    game = await core.details("onlinefix:17801")
    idx = next(i for i, m in enumerate(game.download_methods) if m.kind == "torrent")
    with pytest.raises(ManualActionRequired):
        await core.prepare_download(game.id, idx)
    stored = core.games.get(game.id).download_methods[idx]
    assert stored.kind is MethodKind.EXTERNAL and not stored.supported and "signed-in" in stored.note


async def test_unknown_carousel_games_are_verified_and_hidden(core, routes):
    """Picks carousels carry no 'guide closed' marker: the core checks those pages in the background."""
    import asyncio
    from conftest import fixture_bytes as fb
    slider = ('<html><body><div id="dle-content"><div class="horizontal-slider"><div class="title">Популярное</div>'
              '<div class="container"><div><a href="/games/adventures/18260-minecraft-dungeons-ii-po-seti.html" '
              'title="Minecraft Dungeons II по сети"><p>Minecraft Dungeons II по сети</p></a></div>'
              '<div><a href="/games/adventures/17801-peak-po-seti.html" title="PEAK по сети"><p>PEAK по сети</p></a>'
              '</div></div></div></div></body></html>')
    routes.add("https://online-fix.me/", slider.encode("cp1251"), content_type=CP)
    routes.add("https://online-fix.me/games/adventures/18260-minecraft-dungeons-ii-po-seti.html",
               fb("onlinefix_detail_closed.html"), content_type=CP)
    routes.add("https://online-fix.me/games/adventures/17801-peak-po-seti.html", fb("onlinefix_detail_peak.html"),
               content_type=CP)
    routes.add("https://hosters.online-fix.me:2053/*", fb("onlinefix_hosters_empty.html"))
    sections, _, err = await core.featured(force=True)
    assert err is None and len(sections[0].games) == 2          # not known yet
    await asyncio.wait_for(core._verify_task, 30)
    hidden = [p for n, p in core.events if n == "games_hidden"]
    assert hidden == [["onlinefix:18260"]]
    sections, _, _ = await core.featured()
    assert [g.title for g in sections[0].games] == ["PEAK"]
    # even with a stale download option, a withdrawn game cannot be downloaded
    from ezgd.catalog.models import DownloadMethod, MethodKind
    from ezgd.sources.base import SourceError
    g = core.games.get("onlinefix:18260")
    g.download_methods = [DownloadMethod(MethodKind.TORRENT, "Torrent",
                                         "https://uploads.online-fix.me:2053/torrents/x/", supported=True)]
    core.games.save(g)
    with pytest.raises(SourceError, match="closed this guide"):
        await core.prepare_download(g.id, 0)


async def test_browser_torrent_file_becomes_an_app_download(core, tmp_path):
    """AnkerGames (and other sites) can hand out a .torrent in the browser: it goes to the built-in engine."""
    from ezgd.catalog.models import Game
    core.games.upsert_many([Game("ankergames", "atomfall", "Atomfall", "https://ankergames.net/game/atomfall")])
    t = tmp_path / "Atomfall.torrent"
    t.write_bytes(make_torrent({"Atomfall/Atomfall.rar": b"x" * 40000}, tmp_path / "payload"))
    task = await core.add_browser_torrent("ankergames:atomfall", str(t))
    assert not t.exists()
    d = core.downloads.tasks[task["id"]]
    assert d.game_id == "ankergames:atomfall" and d.title == "Atomfall" and d.source == "ankergames"
    bad = tmp_path / "bad.torrent"
    bad.write_bytes(b"<html>not a torrent</html>")
    with pytest.raises(Exception, match="not valid"):
        await core.add_browser_torrent("ankergames:atomfall", str(bad))


async def test_a_failing_background_job_is_never_silent(core):
    """An import whose zip could not be read failed in a background job without a word (0.1.0)."""
    import asyncio

    async def boom():
        raise NotImplementedError("That compression method is not supported")
    core._watch_task(asyncio.create_task(boom()), "Installing failed", "Some Game", 5)
    await asyncio.sleep(0.05)
    notes = [p for n, p in core.events if n == "notify"]
    assert notes and notes[-1][0] == "Installing failed" and "compression" in notes[-1][1] and \
        notes[-1][2] == {"library": 5}
