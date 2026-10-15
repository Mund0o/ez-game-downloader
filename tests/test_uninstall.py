"""Uninstall: everything the app put on disk for a game, never anything outside its folders or shared."""

import httpx
import pytest

from ezgd.catalog.models import Game
from ezgd.core.app_core import AppCore
from ezgd.platform import steam


@pytest.fixture
async def core(app_paths, tmp_path):
    c = AppCore(app_paths, transport=httpx.MockTransport(lambda r: httpx.Response(404)), enable_torrents=False)
    c.set_emitter(lambda *a: None)
    await c.start()
    c.settings.set("download_dir", str(tmp_path / "dl"))
    c.settings.set("install_dir", str(tmp_path / "games"))
    yield c
    await c.shutdown()


def _game_on_disk(core, tmp_path, title="Big Game"):
    dl, games = tmp_path / "dl", tmp_path / "games" / title
    (games / "bin").mkdir(parents=True)
    (games / "bin" / "game.exe").write_bytes(b"MZ" + b"\0" * 5000)
    dl.mkdir(exist_ok=True)
    parts = []
    for i in (1, 2, 3):
        p = dl / f"Big.Game-OFME.part{i}.rar"
        p.write_bytes(b"R" * (1000 if i < 3 else 400))
        parts.append(p)
    (dl / "Other.Game.part1.rar").write_bytes(b"other set")             # someone else's download
    g = Game("onlinefix", "1", title, "https://online-fix.me/games/x/1-big.html")
    core.games.save(g)
    e = core.library.import_folder(title, games, g.id)
    e.download_path = str(parts[0])
    core.library.repo.update(e)
    prefix = core.library.prefix_dir(e.id) / "pfx" / "drive_c" / "users" / "steamuser" / "Saves"
    prefix.mkdir(parents=True)
    (prefix / "save.dat").write_bytes(b"S" * 300)
    (core.paths.logs / f"game-{e.id}.log").write_text("log")
    return e, games, parts


async def test_uninstall_everything(core, tmp_path):
    e, games, parts = _game_on_disk(core, tmp_path)
    plan = await core.uninstall_plan(e.id)
    by = {i["key"]: i for i in plan["items"]}
    assert set(by) == {"files", "downloads", "prefix"}
    assert {r["path"] for r in by["downloads"]["paths"]} == {str(p) for p in parts}   # all parts, not others
    assert by["files"]["size"] >= 5002 and by["prefix"]["size"] >= 300 and all(i["default"] for i in by.values())
    r = await core.uninstall_game(e.id, ["files", "downloads", "prefix"])
    assert not games.exists() and not any(p.exists() for p in parts)
    assert not core.library.prefix_dir(e.id).exists()
    assert not (core.paths.logs / f"game-{e.id}.log").exists()
    assert (tmp_path / "dl" / "Other.Game.part1.rar").exists()
    assert core.library.repo.get(e.id) is None and r["freed"] >= 5002 + 2400 + 300 and not r["kept"]


async def test_uninstall_keeps_saves_when_unticked_and_never_touches_outside_or_shared(core, tmp_path):
    e, games, parts = _game_on_disk(core, tmp_path)
    outside = tmp_path / "elsewhere" / "Game"
    outside.mkdir(parents=True)
    (outside / "game.exe").write_bytes(b"MZ")
    e2 = core.library.import_folder("Outside Game", outside)
    shared = core.library.import_folder("Same Folder", games)          # a duplicate pointing at the same files
    plan = {i["key"]: i for i in (await core.uninstall_plan(e2.id))["items"]}
    assert not plan["files"]["paths"][0]["ok"] and "outside" in plan["files"]["paths"][0]["why"]
    r = await core.uninstall_game(e2.id, ["files"])
    assert outside.exists() and r["kept"] and core.library.repo.get(e2.id) is None
    plan = {i["key"]: i for i in (await core.uninstall_plan(e.id))["items"]}
    assert not plan["files"]["paths"][0]["ok"] and "another game" in plan["files"]["paths"][0]["why"]
    await core.uninstall_game(e.id, ["files", "downloads"])           # prefix (saves) not ticked
    assert games.exists()                                              # still used by "Same Folder"
    assert core.library.prefix_dir(e.id).exists() and not any(p.exists() for p in parts)
    assert core.library.repo.get(shared.id) is not None


async def test_uninstall_refuses_while_running(core, tmp_path, monkeypatch):
    e, games, _ = _game_on_disk(core, tmp_path)
    monkeypatch.setattr(core.library, "is_running", lambda entry_id: True)
    assert (await core.uninstall_plan(e.id))["running"] is True
    with pytest.raises(Exception, match="Stop the game"):
        await core.uninstall_game(e.id, ["files"])
    assert games.exists()


def test_remove_steam_shortcut(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "is_steam_running", lambda: False)      # Steam's files are only changed while closed
    root = tmp_path / "Steam"
    (root / "userdata" / "123" / "config").mkdir(parents=True)
    exe = tmp_path / "g" / "game.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    a = steam.add_shortcut(root, "123", name="Game A", exe=exe, start_dir=exe.parent)
    b = steam.add_shortcut(root, "123", name="Game B", exe=tmp_path / "g" / "b.exe", start_dir=exe.parent)
    assert steam.remove_shortcut(root, a) == ["123"]
    data = steam.vdf.binary_loads((root / "userdata" / "123" / "config" / "shortcuts.vdf").read_bytes())
    names = [v["AppName"] for v in data["shortcuts"].values()]
    assert names == ["Game B"] and b
    assert list((root / "userdata" / "123" / "config").glob("shortcuts.vdf.ezgd-backup-*"))
    assert steam.remove_shortcut(root, a) == []                       # already gone: nothing changes


async def test_failed_delete_keeps_the_game_and_says_why(core, tmp_path):
    import os
    from ezgd.core.app_core import UninstallIncomplete
    e, games, parts = _game_on_disk(core, tmp_path)
    locked = games / "bin"
    os.chmod(locked, 0o500)                                            # files inside cannot be removed
    try:
        with pytest.raises(UninstallIncomplete) as exc:
            await core.uninstall_game(e.id, ["files", "downloads"])
        assert exc.value.failed and "permission denied" in exc.value.failed[0]["why"]
        assert games.exists() and core.library.repo.get(e.id) is not None        # kept for a retry
        assert "Uninstall incomplete" in core.library.repo.get(e.id).notes
    finally:
        os.chmod(locked, 0o700)
    r = await core.uninstall_game(e.id, ["files", "downloads"])          # retry after fixing it
    assert not games.exists() and core.library.repo.get(e.id) is None and r["freed"] > 0


async def test_leftover_folders(core, tmp_path):
    e, games, parts = _game_on_disk(core, tmp_path)                     # in use: folder + all 3 parts
    old = tmp_path / "games" / "Removed Long Ago"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "game.exe").write_bytes(b"MZ" + b"\0" * 1000)
    core.created_paths.add(old, "install")                              # the app made it
    stray = tmp_path / "dl" / "Removed.Long.Ago-OFME.rar"              # older download, no record: by its name
    stray.write_bytes(b"R" * 500)
    named = tmp_path / "games" / "Peak Climber.v2.1"                    # a catalog title + version
    named.mkdir()
    core.games.save(Game("onlinefix", "77", "Peak Climber", "https://online-fix.me/games/x/77.html"))
    engine = tmp_path / "games" / "UnrealEngine-6"                      # the user's own program: never offered
    (engine / "Engine").mkdir(parents=True)
    (engine / "Engine" / "UE.exe").write_bytes(b"MZ")
    rows = {r["path"]: r for r in await core.find_leftovers()}
    assert rows[str(old)]["default"] is True and rows[str(old)]["why"] == "created by the app"
    assert rows[str(stray)]["default"] is False and "OFME" in rows[str(stray)]["why"]
    assert rows[str(named)]["default"] is False and "Peak Climber" in rows[str(named)]["why"]
    assert str(engine) not in rows and str(games) not in rows and not any(str(p) in rows for p in parts)
    r = await core.delete_leftovers([str(old), str(games)])             # the second one is in use: refused
    assert r["deleted"] == [str(old)] and not old.exists() and games.exists()
    assert r["failed"][0]["path"] == str(games)


async def test_a_never_started_game_uninstalls_with_save_backup_ticked(core, tmp_path):
    """No saves to back up (the game was never started — its prefix has no users folder): the uninstall goes ahead
    whether "back up my saves" is ticked or not (it used to refuse: "your saves could not be backed up")."""
    games = tmp_path / "games" / "Never Played"
    games.mkdir(parents=True)
    (games / "game.exe").write_bytes(b"MZ" + b"\0" * 100)
    e = core.library.import_folder("Never Played", games, None)
    core.library.prefix_dir(e.id).mkdir(parents=True)                  # made, but the game never ran
    plan = {i["key"] for i in (await core.uninstall_plan(e.id))["items"]}
    assert "prefix" in plan                                           # (what used to need the backup)
    r = await core.uninstall_game(e.id, sorted(plan), backup_saves=True)
    assert not games.exists() and core.library.repo.get(e.id) is None and not r["kept"]
