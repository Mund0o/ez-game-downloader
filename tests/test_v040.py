"""0.4.0: Linux ratings, quit by holding the guide button, synced save backups, autostart, download hours and the
"while playing" limit, Library organising, automatic game updates, install-space estimates."""

import gzip
import json
import sys
import time
from pathlib import Path

import pytest

from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from test_features_v2 import FakeSite, core, make_saves, wait_for, zip_bytes  # noqa: F401 — fixtures/helpers

linux_only = pytest.mark.skipif(sys.platform == "win32", reason="Proton prefixes / XDG autostart")


# ------------------------------------------------------------------- Linux ratings
def _index(path: Path, games: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"source": "test", "reports": 1, "games": games}, fh)
    return path


def test_rating_levels():
    from ezgd.library import protondb
    assert protondb.rating({"r": 100, "ok": 92})["level"] == "great"
    assert protondb.rating({"r": 100, "ok": 70})["level"] == "good"
    assert protondb.rating({"r": 100, "ok": 50})["level"] == "mixed"
    assert protondb.rating({"r": 100, "ok": 10})["level"] == "bad"
    r = protondb.rating({"r": 2, "ok": 2})
    assert r["level"] == "few" and "not enough" in r["text"]
    assert protondb.rating(None) is None and protondb.rating({"r": 0}) is None
    assert protondb.rating({"r": 758, "ok": 695}, 1145360)["label"] == "Linux 92%"


async def test_linux_ratings_by_app_id_and_by_title(core):  # noqa: F811
    _index(core._protondb_index(), {"1145360": {"t": "Hades", "r": 100, "ok": 95},
                                    "553850": {"t": "HELLDIVERS 2", "r": 10, "ok": 4}})
    a, b, c = await core.linux_ratings([(1145360, "whatever"), (None, "Helldivers 2"), (None, "Unknown Game")])
    assert a["level"] == "great" and a["app"] == 1145360
    assert b["app"] == 553850 and b["level"] == "mixed"           # found by its title
    assert c is None


async def test_linux_ratings_without_the_index_are_unknown(core):  # noqa: F811
    assert await core.linux_ratings([(1, "x")]) == [None]


# ------------------------------------------------------------------- guide button held
def test_holding_the_guide_button_asks_once(qtbot):
    from ezgd.ui import gamepad as gp
    from test_features_v2 import FakeDev
    g = gp.Gamepads()
    g._home_timer.setInterval(150)
    pad = FakeDev("/dev/input/event90")
    with qtbot.waitSignal(g.home_held, timeout=2000):
        g._event(pad, gp.EV_KEY, gp.BTN_MODE, 1)
    g._event(pad, gp.EV_KEY, gp.BTN_MODE, 0)
    fired = []
    g.home_held.connect(lambda: fired.append(1))
    g._event(pad, gp.EV_KEY, gp.BTN_MODE, 1)                     # a short press: nothing
    g._event(pad, gp.EV_KEY, gp.BTN_MODE, 0)
    qtbot.wait(300)
    assert fired == []
    g.stop()


# ------------------------------------------------------------------- synced save backups
@linux_only
async def test_save_backups_go_to_the_synced_folder(core, tmp_path):  # noqa: F811
    folder = tmp_path / "games" / "Hollow"
    folder.mkdir(parents=True)
    e = core.library.import_folder("Hollow", folder, None)
    make_saves(core.library.prefix_dir(e.id))
    local = core.library.backup_saves(e.id, "manual")
    assert local and str(core.paths.save_backups) in local["path"]
    sync = tmp_path / "Sync"
    sync.mkdir()
    core.settings.set("save_sync_dir", str(sync))
    make_saves_extra = core.library.prefix_dir(e.id) / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / \
        "Roaming" / "Hollow" / "save2.dat"
    make_saves_extra.write_bytes(b"more")
    b = core.library.backup_saves(e.id, "manual")
    assert b["path"].startswith(str(sync / "EZ Game saves"))
    listed = core.library.save_backups(e.id)
    assert listed["synced"] and len(listed["backups"]) == 2           # the older one was copied over too
    assert all(x["path"].startswith(str(sync)) for x in listed["backups"])
    core.library.restore_save_backup(e.id, listed["backups"][-1]["path"])      # a synced backup restores
    core.settings.set("save_sync_dir", str(tmp_path / "unplugged-drive"))
    assert str(core.paths.save_backups) in str(core.library.save_folder(e))    # not there: kept on this PC


# ------------------------------------------------------------------- autostart
@linux_only
def test_autostart_entry(tmp_path, monkeypatch):
    from ezgd.platform import autostart
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    autostart.apply(True)
    p = tmp_path / "cfg" / "autostart" / "ezgd.desktop"
    text = p.read_text()
    assert "--minimized" in text and text.startswith("[Desktop Entry]") and autostart.enabled()
    autostart.apply(False)
    assert not p.exists() and not autostart.enabled()


# ------------------------------------------------------------------- download hours / while playing
async def test_download_hours_and_playing_limit(core):  # noqa: F811
    m = core.downloads
    core.settings.set("download_hours", True)
    core.settings.set("download_from", "01:00")
    core.settings.set("download_to", "08:00")
    at = lambda h, mi=0: time.struct_time((2026, 10, 14, h, mi, 0, 1, 287, -1))   # noqa: E731
    assert m.outside_hours(at(12)) and not m.outside_hours(at(3)) and m.outside_hours(at(8))
    core.settings.set("download_from", "22:00")                      # across midnight
    assert not m.outside_hours(at(23, 30)) and not m.outside_hours(at(6)) and m.outside_hours(at(12))
    core.settings.set("download_hours", False)
    assert not m.outside_hours(at(12))
    rates = []
    m.http.bucket.set_rate = rates.append
    core.settings.set("http_speed_limit_kbps", 5000)
    core.settings.set("play_speed_limit_kbps", 1000)
    m.is_playing = lambda: True
    m._check_playing()
    assert rates[-1] == 1000 * 1024                                   # the lower "while playing" limit
    m.is_playing = lambda: False
    m._check_playing()
    assert rates[-1] == 5000 * 1024                                   # back to the user's own


async def test_downloads_wait_outside_the_hours(core, tmp_path, monkeypatch):  # noqa: F811
    from ezgd.downloads.models import DownloadStatus
    m = core.downloads
    monkeypatch.setattr(m, "outside_hours", lambda now=None: True)
    snap = await m.add_http("https://example.com/game.zip", title="Game", dest_dir=str(tmp_path / "dl"))
    m._schedule()
    t = m.tasks[snap["id"]]
    assert t.status is DownloadStatus.QUEUED                          # nothing starts
    t.status = DownloadStatus.DOWNLOADING
    m._apply_hours()
    assert t.status is DownloadStatus.QUEUED and "download hours" in t.status_detail
    assert m.summary()["waits_until"] == core.settings.settings.download_from


def test_time_settings_are_checked():
    from ezgd.core.settings import Settings
    assert Settings.coerce("download_from", "1.30") == "01:30"
    with pytest.raises(ValueError):
        Settings.coerce("download_to", "24:00")


# ------------------------------------------------------------------- organising
async def test_favourites_hidden_collections(core, tmp_path):  # noqa: F811
    from ezgd.ui.pages.library import sort_key
    ids = []
    for name in ("Alpha", "Beta", "Gamma"):
        f = tmp_path / "games" / name
        f.mkdir(parents=True)
        ids.append(core.library.import_folder(name, f, None).id)
    core.library.organize(ids[2], favorite=True, collections=["Co-op", " co-op ", "Racing  games"])
    core.library.organize(ids[1], hidden=True)
    g = core.library.get(ids[2])
    assert g.favorite and g.collections == ["Co-op", "Racing games"]
    assert core.library.get(ids[1]).hidden
    assert core.library.collections() == ["Co-op", "Racing games"]
    order = sorted(core.library.all(), key=lambda e: sort_key(e, "title"))
    assert [e.title for e in order] == ["Gamma", "Alpha", "Beta"]     # favourites first
    core.library.organize(ids[2], favorite=False, collections=[])
    assert core.library.get(ids[2]).organize == {}
    assert core.db.query_one("PRAGMA user_version")[0] >= 6


# ------------------------------------------------------------------- automatic game updates
class UpdatingSite(FakeSite):
    def __init__(self, fetcher, methods):
        super().__init__(fetcher, "upd")
        self.methods = methods

    async def details(self, game):
        game = await super().details(game)
        game.download_methods = list(self.methods)
        return game


async def _installed(core, tmp_path, site):  # noqa: F811
    core.registry.add_adapter(site)
    core.games.save(Game("upd", "1", "Speedy", "https://upd.example/speedy"))
    v1 = tmp_path / "dl" / "Speedy.v1.0.0.zip"
    v1.parent.mkdir(parents=True, exist_ok=True)
    v1.write_bytes(zip_bytes({"Speedy/Speedy.exe": b"MZ1"}))
    r = await core.import_downloaded_file("upd:1", str(v1))
    eid = r["entry_id"]
    # extracted and its version known (the update check skips a game that is still being installed)
    await wait_for(lambda: any(x["status"] == "completed" for x in core.extraction_repo.for_library(eid))
                   and (core.library.get(eid).release or {}).get("version")
                   and core.library.get(eid).status.value in ("installed", "extracted"))
    return eid


async def test_automatic_update_downloads_the_new_version(core, tmp_path, monkeypatch):  # noqa: F811
    torrent = DownloadMethod(MethodKind.TORRENT, "Torrent", "https://upd.example/t.torrent", supported=True)
    browser = DownloadMethod(MethodKind.EXTERNAL, "Gofile", "https://gofile.io/x")
    site = UpdatingSite(core.fetcher, [browser, torrent])
    eid = await _installed(core, tmp_path, site)
    started = []

    async def prepare(game_id, idx):
        return {"token": f"t{idx}", "size": 100}

    async def start(token, dest, **kw):
        started.append((token, dest, kw))
        return {"id": 99}
    monkeypatch.setattr(core, "prepare_download", prepare)
    monkeypatch.setattr(core, "start_prepared", start)
    core.settings.set("auto_update_games", True)
    site.version = "1.1.0"
    await core.check_game_updates(eid, manual=True)
    assert started and started[0][0] == "t1"                        # the in-app torrent, not the browser link
    assert core.library.get(eid).release.get("updating")
    assert any(n == "notify" and p[0].startswith("Updating") for n, p in core.events)
    started.clear()
    await core.check_game_updates(eid, manual=True)                  # the same version: not again
    assert not started


async def test_automatic_update_leaves_browser_only_games_to_the_user(core, tmp_path):  # noqa: F811
    site = UpdatingSite(core.fetcher, [DownloadMethod(MethodKind.EXTERNAL, "Gofile", "https://gofile.io/x")])
    eid = await _installed(core, tmp_path, site)
    core.settings.set("auto_update_games", True)
    site.version = "2.0.0"
    await core.check_game_updates(eid, manual=True)
    assert not core.library.get(eid).release.get("updating")
    assert any(n == "notify" and "need a browser" in p[1] for n, p in core.events)


# ------------------------------------------------------------------- install space
def test_install_estimate():
    from ezgd.core.fsutil import install_estimate
    g = Game("x", "1", "G", "https://x/1")
    assert install_estimate(g, 10_000) == {"install_size": 11_500, "install_size_guessed": True}
    g.extra["installer"] = True
    assert install_estimate(g, 10_000)["install_size"] == 25_000
    g.extra["installed_size"] = "35 GB"
    est = install_estimate(g, 10_000)
    assert est["install_size_guessed"] is False and est["install_size"] > 30 * 1024 ** 3
    assert install_estimate(Game("x", "2", "H", "https://x/2"), None)["install_size"] is None
