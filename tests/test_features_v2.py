"""Save backups, game updates, resumed installs, wishlist, storage, learning, app updates, controllers, stopping
games completely."""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ezgd.catalog.models import Game
from ezgd.core.app_core import AppCore
from ezgd.sources.base import Page, SourceAdapter, SourceInfo

linux_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes / Proton prefixes")


def zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


class FakeSite(SourceAdapter):
    """A source whose pages say what the test wants (version, search results)."""

    def __init__(self, fetcher, sid="fake", background_ok=True):
        super().__init__(fetcher)
        self.info = SourceInfo(id=sid, name=sid.title(), homepage=f"https://{sid}.example/", description="",
                               capabilities={"search"}, background_ok=background_ok)
        self.version = "1.0.0"
        self.found: list[Game] = []
        self.searches = 0
        self.details_calls = 0

    async def latest(self, page=1, genre=None):
        return Page([], page, False)

    async def search(self, query, page=1):
        self.searches += 1
        return Page(list(self.found), page, False)

    async def details(self, game):
        self.details_calls += 1
        game.version = self.version
        game.details_checked_at = time.time()
        game.download_methods = []
        return game

    async def resolve(self, method, game):
        raise NotImplementedError


@pytest.fixture
async def core(app_paths, routes, tmp_path):
    c = AppCore(app_paths, transport=httpx.MockTransport(routes), enable_torrents=False)
    events = []
    c.set_emitter(lambda n, p: events.append((n, p)))
    await c.start()
    c.settings.set("install_dir", str(tmp_path / "games"))
    c.settings.set("download_dir", str(tmp_path / "dl"))
    c.events = events
    site = FakeSite(c.fetcher)
    c.registry.add_adapter(site)
    c.site = site
    yield c
    await c.shutdown()


async def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


def make_saves(prefix: Path, extra: bytes = b"") -> Path:
    u = prefix / "pfx" / "drive_c" / "users"
    save = u / "steamuser" / "AppData" / "Roaming" / "Hollow" / "save1.dat"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"progress" + extra)
    (u / "steamuser" / "AppData" / "Roaming" / "Hollow" / "shader_cache").mkdir()
    (u / "steamuser" / "AppData" / "Roaming" / "Hollow" / "shader_cache" / "big.bin").write_bytes(b"x" * 1000)
    (u / "steamuser" / "AppData" / "Roaming" / "Hollow" / "game.log").write_text("log")
    (u / "steamuser" / "AppData" / "Local" / "Temp").mkdir(parents=True)
    (u / "steamuser" / "AppData" / "Local" / "Temp" / "t.tmp").write_text("t")
    (u / "steamuser" / "AppData" / "Local" / "dxvk").mkdir(parents=True)
    (u / "steamuser" / "AppData" / "Local" / "dxvk" / "x.dxvk.bin").write_text("c")
    ofx = u / "Public" / "Documents" / "OnlineFix" / "123" / "Saves"
    ofx.mkdir(parents=True)
    (ofx / "slot.sav").write_bytes(b"onlinefix save")
    os.symlink(u / "steamuser" / "AppData", u / "steamuser" / "Application Data")     # Wine-style link
    return save


# ------------------------------------------------------------------- saves
def test_save_backup_collects_saves_only_and_restores(tmp_path):
    from ezgd.library import saves
    prefix = tmp_path / "game-1"
    save = make_saves(prefix)
    names = [n for _f, n, _s, _m in saves.collect(prefix)]
    assert names == ["Public/Documents/OnlineFix/123/Saves/slot.sav", "steamuser/AppData/Roaming/Hollow/save1.dat"]
    folder = tmp_path / "bk"
    b = saves.backup(prefix, folder, title="Hollow", reason="auto", entry_id=1)
    assert b is not None and b.files == 2 and b.path.is_file()
    assert saves.backup(prefix, folder, title="Hollow", reason="auto") is None          # nothing changed
    save.write_bytes(b"lost progress")
    n = saves.restore(b.path, prefix)
    assert n == 2 and save.read_bytes() == b"progress"
    listed = saves.list_backups(folder)
    assert listed[0].reason == "auto" and listed[0].folders


def test_save_restore_refuses_paths_outside_the_prefix(tmp_path):
    from ezgd.library import saves
    prefix = tmp_path / "p"
    (prefix / "pfx" / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../../outside.txt", "x")
        z.writestr("steamuser/../../escape.txt", "x")
        z.writestr("/abs.txt", "x")
        z.writestr("Other/x.txt", "x")
        z.writestr("steamuser/ok.txt", "fine")
    assert saves.restore(evil, prefix) == 1
    assert (prefix / "pfx" / "drive_c" / "users" / "steamuser" / "ok.txt").read_text() == "fine"
    assert not (tmp_path / "outside.txt").exists() and not (prefix / "pfx" / "escape.txt").exists()


def test_save_restore_needs_the_prefix_and_backups_are_pruned(tmp_path):
    from ezgd.library import saves
    with pytest.raises(saves.SaveBackupError):
        saves.restore(tmp_path / "x.zip", tmp_path / "nothing")
    prefix = tmp_path / "game-2"
    save = make_saves(prefix)
    folder = tmp_path / "bk"
    for i in range(14):
        save.write_bytes(f"v{i}".encode())
        os.utime(save, (time.time() + i, time.time() + i))
        assert saves.backup(prefix, folder, title="G", reason="auto") is not None
    assert len(saves.list_backups(folder)) == saves.KEEP["auto"]


def test_umu_style_prefix_layout(tmp_path):
    from ezgd.library import saves
    prefix = tmp_path / "umu"
    f = prefix / "drive_c" / "users" / "steamuser" / "Saved Games" / "G" / "a.sav"
    f.parent.mkdir(parents=True)
    f.write_text("s")
    assert [n for _f, n, _s, _m in saves.collect(prefix)] == ["steamuser/Saved Games/G/a.sav"]


@linux_only
async def test_library_backups_restore_with_undo_and_auto_after_play(core, tmp_path):
    folder = tmp_path / "games" / "Hollow"
    folder.mkdir(parents=True)
    e = core.library.import_folder("Hollow", folder)
    with pytest.raises(Exception, match="no saves to back up yet"):
        core.library.backup_saves(e.id)
    save = make_saves(core.library.prefix_dir(e.id))
    first = core.library.backup_saves(e.id)
    assert first and first["files"] == 2
    save.write_bytes(b"newer progress")
    r = core.library.restore_save_backup(e.id, first["path"])
    assert r["files"] == 2 and save.read_bytes() == b"progress"
    assert r["safety"] and r["safety"]["reason"] == "before-restore"          # the state before is kept
    info = core.library.save_backups(e.id)
    assert {b["reason"] for b in info["backups"]} == {"manual", "before-restore"} and info["current_files"] == 2
    with pytest.raises(Exception):
        core.library.restore_save_backup(e.id, str(tmp_path / "elsewhere.zip"))
    save.write_bytes(b"played again")
    core.library._auto_backup(e.id, 30)                       # a 30 s session: no automatic backup
    core.library._auto_backup(e.id, 600)
    await wait_for(lambda: any(b["reason"] == "auto" for b in core.library.save_backups(e.id)["backups"]))
    # the backups belong to the title: a reinstall (new entry) finds them
    e2 = core.library.import_folder("Hollow", folder)
    assert len(core.library.save_backups(e2.id)["backups"]) == 3


@linux_only
async def test_uninstall_backs_up_saves_first(core, tmp_path):
    folder = tmp_path / "games" / "Hollow"
    folder.mkdir(parents=True)
    (folder / "game.exe").write_bytes(b"MZ")
    e = core.library.import_folder("Hollow", folder)
    make_saves(core.library.prefix_dir(e.id))
    await core.uninstall_game(e.id, ["prefix"])
    assert not core.library.prefix_dir(e.id).exists()
    from ezgd.library import saves
    kept = saves.list_backups(core.paths.save_backups / saves.key_for("Hollow"))
    assert kept and kept[0].reason == "before-uninstall"


# ------------------------------------------------------------------- game updates
def test_versions_and_installed_release():
    from ezgd.library import updates
    assert updates.installed_release(["G.v1.0.9-OFME.rar", "G_Fix_Repair_Steam.rar"], "1.0.10") == \
        {"version": "1.0.9", "from": "archive", "archive": "G.v1.0.9-OFME.rar"}
    assert updates.installed_release(["G-AnkerGames.zip"], "v1.7.0") == {"version": "1.7.0", "from": "page"}
    assert updates.status("1.0.9", "1.0.10") == "update"
    assert updates.status("1.0.10", "1.0.10") == "current"
    assert updates.status("1.2", "build 77") == "different"
    assert updates.compare("2.0", "10.0") == -1


async def test_update_check_download_over_the_game_keeps_entry_and_saves(core, tmp_path):
    core.games.save(Game("fake", "1", "Hollow", "https://fake.example/hollow"))
    v1 = tmp_path / "dl" / "Hollow.v1.0.0.zip"
    v1.parent.mkdir(parents=True)
    v1.write_bytes(zip_bytes({"Hollow/Hollow.exe": b"MZ1", "Hollow/data.pak": b"old"}))
    r = await core.import_downloaded_file("fake:1", str(v1))
    eid = r["entry_id"]
    await wait_for(lambda: any(x["status"] == "completed" for x in core.extraction_repo.for_library(eid))
                   and (core.library.get(eid).release or {}).get("version"))   # (written just after: was flaky)
    e = core.library.get(eid)
    assert e.release["version"] == "1.0.0" and e.release["from"] == "archive"
    if sys.platform != "win32":
        make_saves(core.library.prefix_dir(eid))

    core.site.version = "1.1.0"
    res = await core.check_game_updates(eid, manual=True)
    assert res[0]["status"] == "update" and res[0]["latest"] == "1.1.0"
    notes = [p for n, p in core.events if n == "notify" and p[0] == "Update available"]
    assert len(notes) == 1 and notes[0][2] == {"library": eid}
    await core.check_game_updates(eid, manual=True)
    assert len([p for n, p in core.events if n == "notify" and p[0] == "Update available"]) == 1   # once

    info = await core.begin_game_update(eid)
    assert info["dest"].endswith("Hollow") and core.library.get(eid).release["updating"]
    v2 = tmp_path / "dl" / "Hollow.v1.1.0.zip"
    v2.write_bytes(zip_bytes({"Hollow/Hollow.exe": b"MZ2", "Hollow/data.pak": b"new"}))
    r2 = await core.import_downloaded_file("fake:1", str(v2))
    assert r2["entry_id"] == eid and len(core.library.all()) == 1          # the same game, not a second one
    await wait_for(lambda: core.library.get(eid).release.get("version") == "1.1.0")
    e = core.library.get(eid)
    assert e.release["status"] == "current" and not e.release.get("updating") and e.release["updated_from"] == "1.0.0"
    assert (Path(info["dest"]) / "Hollow" / "data.pak").read_bytes() == b"new"
    if sys.platform != "win32":
        reasons = [b["reason"] for b in core.library.save_backups(eid)["backups"]]
        assert "before-update" in reasons


async def test_background_update_checks_respect_sites_that_ask_not_to_be_crawled(core, tmp_path):
    shy = FakeSite(core.fetcher, "shy", background_ok=False)
    core.registry.add_adapter(shy)
    core.games.save(Game("shy", "1", "Quiet", "https://shy.example/q"))
    folder = tmp_path / "games" / "Quiet"
    folder.mkdir(parents=True)
    e = core.library.import_folder("Quiet", folder, "shy:1")
    core.library.repo.update(core.library.update(e.id, title="Quiet"))
    e = core.library.get(e.id)
    e.status = e.status.__class__("installed")
    e.release = {"version": "1.0.0"}
    core.library.repo.update(e)
    await core.check_game_updates()
    assert shy.details_calls == 0                           # background: never read
    await core.check_game_updates(manual=True)
    assert shy.details_calls == 1                           # the user asked


async def test_first_check_of_a_page_only_release_is_the_baseline(core, tmp_path):
    core.games.save(Game("fake", "7", "Old", "https://fake.example/old", version="2.0"))
    folder = tmp_path / "games" / "Old"
    folder.mkdir(parents=True)
    e = core.library.import_folder("Old", folder, "fake:7")
    e.status = e.status.__class__("installed")
    core.library.repo.update(e)
    core.site.version = "2.1"
    r = (await core.check_game_updates(e.id, manual=True))[0]
    assert r["status"] == "current" and r["installed"] == "2.1"   # nothing known about the install: no alarm
    core.site.version = "2.2"
    assert (await core.check_game_updates(e.id, manual=True))[0]["status"] == "update"


# ------------------------------------------------------------------- resumed installs
async def test_interrupted_install_is_finished_on_the_next_start(core, tmp_path):
    core.games.save(Game("fake", "3", "Cut", "https://fake.example/cut"))
    arc = tmp_path / "dl" / "Cut.v2.0.zip"
    arc.parent.mkdir(parents=True)
    arc.write_bytes(zip_bytes({"Cut/Cut.exe": b"MZ", "Cut/a.pak": b"a" * 5000}))
    e = core.library.import_folder("Cut", arc, "fake:3")
    dest = tmp_path / "games" / "Cut"
    ext = core.extraction_repo.create(None, e.id, str(arc), str(dest))
    core.extraction_repo.update(ext, status="running")             # the app was closed during this
    assert core.post.recover() == 1
    assert core.post.resumable and core.post.resumable[0]["archive"] == str(arc)
    assert (await core.interrupted_install(e.id))["name"] == "Cut.v2.0.zip"
    await core._resume_installs()           # (the start-up task may have taken it first: either finishes it)
    await wait_for(lambda: (dest / "Cut" / "a.pak").exists() and core.library.get(e.id).status.value != "extracting")
    assert core.library.get(e.id).install_path == str(dest)
    assert any(p[0] == "Finishing the install" for n, p in core.events if n == "notify")
    assert await core.interrupted_install(e.id) is None


async def test_closing_the_app_stops_an_install_and_it_resumes_next_time(core, tmp_path, monkeypatch):
    """A 40 GB install used to keep the app's process alive (invisibly) for hours after it was closed."""
    import asyncio
    import threading
    from ezgd.installation import archives as ar
    core.games.save(Game("fake", "4", "Big", "https://fake.example/big"))
    arc = tmp_path / "dl" / "Big.zip"
    arc.parent.mkdir(parents=True)
    arc.write_bytes(zip_bytes({"Big/Big.exe": b"MZ"}))
    e = core.library.import_folder("Big", arc, "fake:4")
    started, stopped = threading.Event(), threading.Event()

    def slow_extract(archive, dest, *, cancel=None, **_kw):       # stands in for hours of unpacking
        started.set()
        while not cancel.wait(0.05):
            pass
        stopped.set()
        raise ar.ExtractionCancelled("Extraction cancelled")
    monkeypatch.setattr(ar, "extract", slow_extract)
    task = asyncio.create_task(core.post.extract(e, arc, tmp_path / "games" / "Big"))
    await asyncio.to_thread(started.wait, 5)
    core.post.stop_all()                                         # what shutdown() does first
    assert await asyncio.wait_for(task, 5) is False
    assert stopped.is_set()
    row = core.extraction_repo.for_library(e.id)[-1]
    assert row["status"] == "running"                            # so the next start finishes it
    core.post._shutting_down = False
    assert core.post.recover() == 1 and core.post.resumable[0]["archive"] == str(arc)


# ------------------------------------------------------------------- wishlist
def steam_items(appid: int, name: str, coming_soon=False) -> bytes:
    return json.dumps({"response": {"store_items": [{
        "appid": appid, "id": appid, "success": 1, "name": name, "is_coming_soon": coming_soon,
        "assets": {"asset_url_format": f"steam/apps/{appid}/${{FILENAME}}?t=1",
                   "library_capsule": "abc/library_600x900.jpg", "library_hero": "abc/library_hero.jpg",
                   "header": "abc/header.jpg"},
        "release": {"steam_release_date": 1700000000} if not coming_soon else {"is_coming_soon": True}}]}}).encode()


def test_wishlist_matching_rules():
    from ezgd.catalog import wishlist as wl
    item = {"title": "ELDEN RING", "dedup_key": "elden ring", "steam_app_id": 1245620}
    assert wl.same_game(item, Game("a", "1", "ELDEN RING Deluxe Edition", "u"))
    assert wl.same_game(item, Game("a", "2", "Something", "u", steam_app_id=1245620))
    assert not wl.same_game(item, Game("a", "3", "ELDEN RING", "u", steam_app_id=999))        # another app id
    assert not wl.same_game(item, Game("a", "4", "ELDEN RING NIGHTREIGN", "u"))
    assert wl.search_text("DOOM: The Dark Ages™") == "DOOM The Dark Ages"


async def test_wishlist_add_from_steam_and_find_on_a_source(core, routes):
    routes.add("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/*", steam_items(4242, "Silk Song"),
               content_type="application/json")
    it = await core.wishlist_add(4242)
    assert it["title"] == "Silk Song" and it["steam_app_id"] == 4242 and "library_600x900" in it["cover_url"]
    assert (await core.wishlist())[0]["status"] == "waiting"
    assert (await core.wishlist_add(4242))["id"] == it["id"]                       # no duplicates
    core.site.found = [Game("fake", "9", "Silk Song", "https://fake.example/silk")]
    res = await core.wishlist_check(it["id"], manual=True)
    assert res[0]["status"] == "available" and res[0]["matches"][0]["game_id"] == "fake:9"
    notes = [p for n, p in core.events if n == "notify" and p[0] == "Wishlist: now available"]
    assert len(notes) == 1 and "Fake" in notes[0][1]
    await core.wishlist_check(it["id"], manual=True)
    assert len([p for n, p in core.events if n == "notify" and p[0] == "Wishlist: now available"]) == 1
    # a closed guide is not "available"
    g = core.games.get("fake:9")
    g.extra["guide_closed"] = True
    core.games.save(g)
    core.site.found = [g]
    assert (await core.wishlist_check(it["id"], manual=True))[0]["status"] == "closed"


async def test_wishlist_background_check_skips_shy_sites_and_unreleased_games(core, routes):
    routes.add("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/*", steam_items(77, "Later", True),
               content_type="application/json")
    shy = FakeSite(core.fetcher, "shy", background_ok=False)
    core.registry.add_adapter(shy)
    it = await core.wishlist_add(77)
    assert it["coming_soon"]
    await asyncio.sleep(0.2)
    before = core.site.searches, shy.searches
    await core.wishlist_check()                              # background round
    assert (core.site.searches, shy.searches) == before      # not released: the sites are not asked
    await core.wishlist_check(manual=True)
    assert core.site.searches == before[0] + 1 and shy.searches == before[1] + 1


async def test_wishlist_toggle_from_a_game_page_and_library_status(core, tmp_path):
    core.games.save(Game("fake", "5", "Toggle Me", "https://fake.example/t"))
    assert await core.wishlist_toggle("fake:5") is True
    assert (await core.wishlist_item_for("fake:5"))["title"] == "Toggle Me"
    folder = tmp_path / "games" / "Toggle Me"
    folder.mkdir(parents=True)
    core.library.import_folder("Toggle Me", folder, "fake:5")
    assert (await core.wishlist())[0]["status"] == "library"
    assert await core.wishlist_toggle("fake:5") is False
    assert await core.wishlist() == []


# ------------------------------------------------------------------- storage
async def test_storage_report_and_unused_prefixes(core, tmp_path):
    folder = tmp_path / "games" / "Big"
    folder.mkdir(parents=True)
    (folder / "data.bin").write_bytes(b"x" * 100_000)
    e = core.library.import_folder("Big", folder)
    pfx = core.library.prefix_dir(e.id)
    (pfx / "pfx").mkdir(parents=True)
    (pfx / "pfx" / "system.reg").write_bytes(b"r" * 3000)
    old = pfx.with_name(pfx.name + ".old-20260101-000000")
    old.mkdir()
    (old / "f").write_bytes(b"o" * 2000)
    gone = pfx.with_name("game-999")
    gone.mkdir()
    r = await core.storage_report()
    g = r["games"][0]
    assert g["title"] == "Big" and g["files"] == 100_000 and g["prefix"] == 3000
    olds = {Path(x["path"]).name: x for x in r["old_prefixes"]}
    assert set(olds) == {old.name, "game-999"} and r["disks"]
    d = await core.delete_old_prefixes([str(old), str(pfx), str(tmp_path)])
    assert d["deleted"] == [str(old)] and len(d["failed"]) == 2
    assert pfx.exists() and not old.exists() and tmp_path.exists()


# ------------------------------------------------------------------- learning
async def test_fix_outcomes_change_the_ranking(core, tmp_path):
    folder = tmp_path / "games" / "L"
    folder.mkdir(parents=True)
    e = core.library.import_folder("L", folder)
    e.source = "onlinefix"
    core.library.repo.update(e)
    (core.paths.data / "fix_outcomes.json").write_text(json.dumps([
        {"game": "L", "worked": False, "keys": ["winetricks:vcrun2022"]},
        {"game": "Other", "worked": True, "keys": ["enable_fix:no_steam_overlay"], "source": "onlinefix"},
        {"game": "Third", "worked": True, "keys": ["enable_fix:no_steam_overlay"]},
    ]))
    findings = [{"title": "a", "confidence": 0.8, "fix": {"kind": "winetricks", "value": "vcrun2022"}},
                {"title": "b", "confidence": 0.5, "fix": {"kind": "enable_fix", "value": "no_steam_overlay"}},
                {"title": "c", "confidence": 0.6, "fix": None}]
    core._apply_learning(core.library.get(e.id), findings)
    by = {f["title"]: f for f in findings}
    assert by["a"]["confidence"] == 0.4 and "did not help" in by["a"]["learned"]
    assert by["b"]["confidence"] > 0.6 and "2 other games like this one" in by["b"]["learned"]
    assert [f["title"] for f in findings] == ["b", "c", "a"]


def test_followup_remembers_fix_keys(tmp_path):
    core = SimpleNamespace(paths=SimpleNamespace(data=tmp_path), emit=lambda *a: None,
                           library=SimpleNamespace(get=lambda i: SimpleNamespace(title="G", source="fitgirl")))
    for name in ("_followup_path", "_followups", "_expect_followup", "_record_outcome", "_outcomes_path",
                 "_outcome_context"):
        setattr(core, name, getattr(AppCore, name).__get__(core))
    core._expect_followup(1, "installing vcrun2022", ["winetricks:vcrun2022"])
    AppCore._game_exited(core, 1, 0, 900)
    rec = json.loads((tmp_path / "fix_outcomes.json").read_text())[-1]
    assert rec["worked"] and rec["keys"] == ["winetricks:vcrun2022"] and rec["source"] == "fitgirl"


# ------------------------------------------------------------------- app updates
def test_repo_names():
    from ezgd.core import selfupdate
    assert selfupdate.parse_repo("mundo/ezgd") == "mundo/ezgd"
    assert selfupdate.parse_repo("https://github.com/mundo/ezgd/releases") == "mundo/ezgd"
    assert selfupdate.parse_repo("github.com/mundo/ezgd.git") == "mundo/ezgd"
    for bad in ("", "nonsense", "https://example.com/a/b", "a/b/c d"):
        with pytest.raises(selfupdate.UpdateError):
            selfupdate.parse_repo(bad)
    assert selfupdate.relaunch_args(["--home", "/x", "--page", "games", "--quit-after", "3"]) == ["--home", "/x"]
    assert selfupdate.relaunch_args(["--home", "rel"]) == ["--home", os.path.abspath("rel")]   # (started elsewhere)
    assert selfupdate.relaunch_args(["--home=rel"]) == ["--home=" + os.path.abspath("rel")]


def release_tarball(version: str, marker: Path | None = None) -> bytes:
    script = f"#!/bin/sh\necho ezgd {version}\n"
    if marker is not None:
        script = f"#!/bin/sh\nif [ \"$1\" = --version ]; then echo ezgd {version}; exit 0; fi\n" \
                 f"echo started \"$@\" > '{marker}'\n" \
                 f"pwd -P > '{marker}.cwd'\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = script.encode()
        ti = tarfile.TarInfo("EZGameDownloader/ezgd")
        ti.size, ti.mode = len(data), 0o755
        tf.addfile(ti, io.BytesIO(data))
        ti2 = tarfile.TarInfo("EZGameDownloader/_internal/lib.txt")
        ti2.size = 2
        tf.addfile(ti2, io.BytesIO(b"ok"))
    return buf.getvalue()


@linux_only
async def test_app_update_check_download_verify_stage_and_swap(routes, tmp_path, monkeypatch):
    import hashlib
    from ezgd.core import selfupdate
    from ezgd.net.client import SafeHttpClient
    marker = tmp_path / "relaunched.txt"
    blob = release_tarball("9.9.9", marker)
    asset = "https://github.com/o/r/releases/download/v9.9.9/EZGameDownloader-linux-x86_64.tar.gz"
    routes.add("https://api.github.com/repos/o/r/releases/latest", json.dumps({
        "tag_name": "v9.9.9", "name": "9.9.9", "body": "notes", "html_url": "https://github.com/o/r/releases/v9.9.9",
        "assets": [{"name": "EZGameDownloader-linux-x86_64.tar.gz", "browser_download_url": asset, "size": len(blob),
                    "digest": "sha256:" + hashlib.sha256(blob).hexdigest()}]}), content_type="application/json")
    routes.add(asset, blob, content_type="application/octet-stream")
    client = SafeHttpClient(transport=httpx.MockTransport(routes))
    try:
        info = await selfupdate.check(client, "o/r")
        assert info["newer"] and info["version"] == "9.9.9" and info["sha256"]
        archive = await selfupdate.download(client, info, tmp_path / "updates")
        bad = dict(info, sha256="0" * 64)
        with pytest.raises(selfupdate.UpdateError, match="does not match"):
            await selfupdate.download(client, bad, tmp_path / "updates2")
        assert not list((tmp_path / "updates2").glob("*"))
        no_sum = dict(info, sha256=None, sums_url=None)
        with pytest.raises(selfupdate.UpdateError, match="no SHA-256"):
            await selfupdate.download(client, no_sum, tmp_path / "updates3")
    finally:
        await client.aclose()
    app = tmp_path / "apps" / "EZGameDownloader"
    app.mkdir(parents=True)
    (app / "ezgd").write_text("#!/bin/sh\necho old\n")
    with pytest.raises(selfupdate.UpdateError, match="expected 9.9.8"):
        selfupdate.stage(archive, app, "9.9.8")                 # the build must report the version it claims
    new = selfupdate.stage(archive, app, "9.9.9")
    assert (new / "ezgd").is_file() and selfupdate.staged_version(app)
    waiter = subprocess.Popen(["sleep", "1"])                    # stands in for the running app
    monkeypatch.chdir(app)                                       # the app was started from its own folder
    selfupdate.schedule_swap(app, tmp_path, tmp_path, ["--home", "/somewhere"], pid=waiter.pid)
    monkeypatch.chdir(tmp_path)
    waiter.wait()
    end = time.monotonic() + 15
    while time.monotonic() < end and not marker.exists():
        await asyncio.sleep(0.1)
    assert marker.read_text().strip() == "started --home /somewhere"
    # started in the new app folder — not the old one it was started from, deleted a minute later (the new version's
    # helper processes then failed with "No such file or directory")
    end = time.monotonic() + 5
    while time.monotonic() < end and not Path(f"{marker}.cwd").exists():
        await asyncio.sleep(0.05)
    assert Path(f"{marker}.cwd").read_text().strip() == str(app.resolve())
    assert "echo ezgd 9.9.9" in (app / "ezgd").read_text() or "9.9.9" in (app / "ezgd").read_text()
    assert (app / "_internal" / "lib.txt").read_text() == "ok"
    old = app.with_name(".EZGameDownloader.update-old")
    assert (old / "ezgd").read_text() == "#!/bin/sh\necho old\n"
    assert selfupdate.cleanup_previous(app) and not old.exists()


async def test_app_update_status_from_source_checkout(core, routes):
    st = await core.app_update_status()
    assert st["packaged"] is False and st["can_install"] is False
    from ezgd.core.selfupdate import DEFAULT_REPO, check
    assert st["repo"] == DEFAULT_REPO                      # works out of the box: the official repository
    routes.add(f"https://api.github.com/repos/{DEFAULT_REPO}", "{}", content_type="application/json")
    info = await core.check_app_update()                   # it has no release yet: "newest", not an error
    assert info["no_release"] and not info["newer"]
    routes.add("https://api.github.com/repos/o/missing/releases/latest", "", status=404)
    with pytest.raises(Exception, match="no published release"):
        await check(core.client, "o/missing")              # a repository that does not exist is still an error
    assert await core.set_update_repo("https://github.com/o/r") == "o/r"
    assert await core.set_update_repo(DEFAULT_REPO.upper()) == DEFAULT_REPO
    assert core.settings.settings.update_repo == ""
    with pytest.raises(Exception, match="runs from source"):
        await core.download_app_update()


# ------------------------------------------------------------------- controllers
class FakeDev:
    def __init__(self, path):
        self.path = path
        self.ranges = {0: (-32768, 32767), 1: (-32768, 32767)}
        self.stick = {0: 0, 1: 0}


def test_gamepad_buttons_dpad_stick_and_duplicates(qtbot):
    from ezgd.ui import gamepad as gp
    g = gp.Gamepads()
    got = []
    g.action.connect(got.append)
    a, b = FakeDev("/dev/input/event90"), FakeDev("/dev/input/event91")
    g._event(a, gp.EV_KEY, gp.BTN_SOUTH, 1)
    g._event(a, gp.EV_KEY, gp.BTN_SOUTH, 0)                     # release: nothing
    g._event(b, gp.EV_KEY, gp.BTN_SOUTH, 1)                     # the same press from Steam's virtual pad: dropped
    g._event(a, gp.EV_KEY, gp.BTN_EAST, 1)
    g._event(a, gp.EV_KEY, gp.BTN_WEST, 1)
    g._event(a, gp.EV_KEY, gp.BTN_TR, 1)
    g._event(a, gp.EV_ABS, gp.ABS_HAT0X, 1)
    g._event(a, gp.EV_ABS, gp.ABS_HAT0X, 0)
    g._event(a, gp.EV_ABS, gp.ABS_Y, -30000)                    # stick up
    g._event(a, gp.EV_ABS, gp.ABS_Y, -20000)                    # still up (hysteresis): nothing new
    g._event(a, gp.EV_ABS, gp.ABS_Y, 0)
    g._event(a, gp.EV_ABS, gp.ABS_Y, 30000)                     # down
    g._event(a, gp.EV_KEY, 0x2C, 1)                             # a keyboard key code: ignored
    assert got == ["a", "b", "x", "rb", "right", "up", "down"]
    g.stop()


@linux_only
def test_gamepad_reads_presses_from_a_device_node(qtbot, tmp_path, monkeypatch):
    """The real path: rescan() opens the node and a QSocketNotifier reads it. (Before 2026-10-13 the notifier's
    second argument replaced the device path, so no controller — DualSense, Xbox, anything — did anything.)"""
    import os
    from ezgd.ui import gamepad as gp
    node = tmp_path / "event77"
    os.mkfifo(node)
    monkeypatch.setattr(gp, "gamepad_info", lambda: [(str(node), "DualSense Wireless Controller", 0x054C, 0x0CE6,
                                                      1 << gp.BTN_TL2)])
    g = gp.Gamepads()
    got, families = [], []
    g.action.connect(got.append)
    g.family_changed.connect(families.append)
    g.rescan()
    assert str(node) in g.devices and g.family == "playstation" and families == ["playstation"]
    w = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
    try:
        ev = gp.EVENT.pack
        os.write(w, ev(0, 0, gp.EV_KEY, gp.BTN_SOUTH, 1) + ev(0, 0, gp.EV_KEY, gp.BTN_SOUTH, 0)
                 + ev(0, 0, gp.EV_ABS, gp.ABS_HAT0Y, 1) + ev(0, 0, gp.EV_ABS, gp.ABS_HAT0Y, 0)
                 + ev(0, 0, gp.EV_KEY, gp.BTN_TR2, 1) + ev(0, 0, gp.EV_KEY, gp.BTN_MODE, 1))
        qtbot.waitUntil(lambda: got == ["a", "down", "rt", "home"], timeout=3000)
    finally:
        g.stop()
        os.close(w)
        g.deleteLater()                                         # freed here, on the main thread
        qtbot.wait(50)


def test_gamepad_families_and_triggers(qtbot):
    from ezgd.ui import gamepad as gp
    assert gp.family_of(0x054C, 0x0CE6, "DualSense Wireless Controller") == "playstation"
    assert gp.family_of(0x045E, 0x0B12, "Xbox Wireless Controller") == "xbox"
    assert gp.family_of(0x28DE, 0x1205, "Steam Deck") == "steam"
    assert gp.family_of(0x057E, 0x2009, "Nintendo Switch Pro Controller") == "nintendo"
    assert gp.family_of(0x1234, 0x0001, "Some Pad") == "generic"
    g = gp.Gamepads()
    got, families = [], []
    g.action.connect(got.append)
    g.family_changed.connect(families.append)
    xbox = gp._Device("/dev/input/event80", "Xbox Wireless Controller", -1, 0x045E, 0x0B12, 0)
    xbox.ranges = {gp.ABS_Z: (0, 1023), gp.ABS_RZ: (0, 1023)}
    ps = gp._Device("/dev/input/event81", "DualSense Wireless Controller", -1, 0x054C, 0x0CE6, 1 << gp.BTN_TL2)
    virtual = gp._Device("/dev/input/event82", "Steam Virtual Gamepad", -1, 0x28DE, 0x11FF, 0)
    g.devices = {d.path: d for d in (xbox, ps, virtual)}
    g._event(xbox, gp.EV_ABS, gp.ABS_Z, 900)                    # left trigger pulled
    g._event(xbox, gp.EV_ABS, gp.ABS_Z, 700)                    # still held: nothing new
    g._event(xbox, gp.EV_ABS, gp.ABS_Z, 0)
    g._event(xbox, gp.EV_ABS, gp.ABS_RZ, 1000)
    assert got == ["lt", "rt"] and g.family == "xbox"
    g._event(ps, gp.EV_KEY, gp.BTN_EAST, 1)
    assert g.family == "playstation"
    g._event(ps, gp.EV_ABS, gp.ABS_Z, 255)                      # DualSense: triggers come as buttons, not axes
    assert got == ["lt", "rt", "b"]
    g._event(virtual, gp.EV_KEY, gp.BTN_NORTH, 1)               # Steam's mirror of the pad in use: keeps its look
    assert g.family == "playstation" and got[-1] == "x"
    g.devices = {}
    g.stop()


def test_gamepad_detection_from_sysfs(tmp_path):
    from ezgd.ui import gamepad as gp

    def dev(name, key, abs_):
        d = tmp_path / name / "device" / "capabilities"
        d.mkdir(parents=True)
        (d / "key").write_text(key)
        (d / "abs").write_text(abs_)
        return tmp_path / name
    pad = dev("event5", "7cdb000000000000 0 0 0 0", "3003f")           # BTN_SOUTH… and sticks
    keyboard = dev("event6", "ffffffffffffffff fffffffffffffffe", "0")  # claims every key, no axes
    mouse = dev("event7", "1f0000 0 0 0 0", "100000000")
    assert gp.is_gamepad(pad) and not gp.is_gamepad(keyboard) and not gp.is_gamepad(mouse)


# ------------------------------------------------------------------- stopping a game completely
@linux_only
async def test_stop_completely_kills_stubborn_games_and_their_prefix_processes(core, tmp_path):
    folder = tmp_path / "games" / "Stubborn"
    folder.mkdir(parents=True)
    e = core.library.import_folder("Stubborn", folder)
    prefix = core.library.prefix_dir(e.id)
    prefix.mkdir(parents=True)
    env = {**os.environ, "STEAM_COMPAT_DATA_PATH": str(prefix)}
    # the game ignores SIGTERM, and one of its helpers left the process group (like wineserver can)
    script = "trap '' TERM; setsid sh -c \"trap '' TERM; sleep 300\" & sleep 300"
    proc = subprocess.Popen(["sh", "-c", script], env=env, start_new_session=True)
    bystander = subprocess.Popen(["sleep", "300"])              # some other program: must survive
    log_path = tmp_path / "g.log"
    core.library._processes[e.id] = (proc, time.monotonic(), SimpleNamespace(post=[]), log_path)
    from ezgd.library.manager import _prefix_processes
    await wait_for(lambda: len(_prefix_processes(prefix)) >= 3)
    try:
        r = await asyncio.to_thread(core.library.stop_completely, e.id, 1.0)
        assert r == {"stopped": True, "left": []}
        assert proc.poll() is not None and _prefix_processes(prefix) == []
        assert bystander.poll() is None
        assert not core.library.is_running(e.id)
    finally:
        bystander.kill()
        for pid in _prefix_processes(prefix):
            os.kill(pid, signal.SIGKILL)


@linux_only
def test_stop_completely_when_nothing_runs(core):
    assert core.library.stop_completely(12345) == {"stopped": True, "left": []}


def test_raw_buttons_count_once_when_steam_mirrors_the_controller(qtbot):
    """A quick tap: the real pad's press and release, then Steam's virtual copy of both. The couch browser turns raw
    A into mouse clicks — the copy made a second click and file hosts started the download twice."""
    from ezgd.ui import gamepad as gp
    g = gp.Gamepads()
    raw, acts = [], []
    g.button.connect(lambda n, d: raw.append((n, d)))
    g.action.connect(acts.append)
    real, virtual = FakeDev("/dev/input/event90"), FakeDev("/dev/input/event91")
    g._event(real, gp.EV_KEY, gp.BTN_SOUTH, 1)
    g._event(real, gp.EV_KEY, gp.BTN_SOUTH, 0)
    g._event(virtual, gp.EV_KEY, gp.BTN_SOUTH, 1)               # Steam's copy, after the real button is up
    g._event(virtual, gp.EV_KEY, gp.BTN_SOUTH, 0)
    assert raw == [("a", True), ("a", False)] and acts == ["a"]
    raw.clear()
    g._event(real, gp.EV_KEY, gp.BTN_SOUTH, 1)                  # overlapping copy (the usual case)
    g._event(virtual, gp.EV_KEY, gp.BTN_SOUTH, 1)
    g._event(real, gp.EV_KEY, gp.BTN_SOUTH, 0)
    g._event(virtual, gp.EV_KEY, gp.BTN_SOUTH, 0)
    assert raw == [("a", True), ("a", False)]
    raw.clear()
    g._raw_owner["a"] = ("/dev/input/event90", 0.0, False)       # long after: the other device may press again
    g._event(virtual, gp.EV_KEY, gp.BTN_SOUTH, 1)
    g._event(virtual, gp.EV_KEY, gp.BTN_SOUTH, 0)
    assert raw == [("a", True), ("a", False)]
    g.stop()
