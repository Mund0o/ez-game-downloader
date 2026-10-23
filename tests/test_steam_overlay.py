"""Starting games through Steam so the Steam overlay works: shortcuts to play mode, play mode itself, tracking."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from ezgd.library import steamrun
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.platform import steam, vdf

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native script games are a Linux test")


@pytest.fixture
def library(app_paths, tmp_path):
    """The app's own database file — the one play mode opens."""
    from ezgd.core.settings import SettingsStore
    from ezgd.database.db import Database
    from ezgd.database.repositories import GameRepository, LibraryRepository
    from ezgd.library.manager import LibraryManager
    db = Database(app_paths.database, backup_dir=app_paths.backups).open()
    settings = SettingsStore(db)
    settings.set("install_dir", str(tmp_path / "games"))
    settings.set("steam_root", str(tmp_path / "no-steam"))         # the tests that need Steam set up a fake one
    events = []
    m = LibraryManager(LibraryRepository(db), GameRepository(db), settings, app_paths,
                       lambda n, p: events.append((n, p)))
    m.events = events
    m._watch_steam = lambda *a: None             # the safety-net thread is tested on its own (below)
    yield m
    db.close()


def script_game(library, tmp_path, body: str, title: str = "Script Game") -> LibraryEntry:
    folder = tmp_path / "games" / title
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / "run.sh"
    exe.write_text("#!/bin/sh\n" + body + "\n")
    exe.chmod(0o755)
    return library.repo.insert(LibraryEntry(title=title, status=LibraryStatus.INSTALLED, install_path=str(folder),
                                            executable=str(exe), executable_trusted=True))


def fake_steam(tmp_path, monkeypatch, running=False) -> Path:
    root = tmp_path / "Steam"
    (root / "userdata" / "1234" / "config").mkdir(parents=True)
    (root / "steamapps").mkdir()
    for arch in ("ubuntu12_32", "ubuntu12_64"):
        (root / arch).mkdir()
        (root / arch / "gameoverlayrenderer.so").write_bytes(b"\x7fELF")
    monkeypatch.setattr(steam, "is_steam_running", lambda: running)
    monkeypatch.setattr(steam, "steam_command", lambda: ["/usr/bin/true"])
    return root


def test_shortcuts_for_play_mode(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    a = script_game(library, tmp_path, "true", "Game A")
    b = script_game(library, tmp_path, "true", "Game B")
    st = library.steam_overlay_status(a.id)
    assert st["use"] and not st["ready"] and not st["steam_running"]
    r = library.prepare_steam_shortcuts()
    assert r == {"games": 2, "users": 1, "restarted": False, "skipped": []}
    assert library.steam_overlay_status(a.id)["ready"] and library.steam_overlay_status(b.id)["ready"]
    shortcuts = steam.shortcuts_of(root, "1234")
    exe, _ = steamrun.runner()
    assert {s["AppName"] for s in shortcuts} == {"Game A", "Game B"}
    assert all(s["Exe"] == f'"{exe}"' and s["LaunchOptions"].endswith(f"--play {i}") and s["AllowOverlay"] == 1
               for s, i in zip(sorted(shortcuts, key=lambda s: s["AppName"]), (a.id, b.id)))
    assert library.prepare_steam_shortcuts()["games"] == 2 and len(steam.shortcuts_of(root, "1234")) == 2  # no dupes
    opened = []
    monkeypatch.setattr(steam, "open_steam_url", opened.append)
    library.launch_via_steam(a.id)
    assert opened == [f"steam://rungameid/{steam.shortcut_rungameid(library.get(a.id).steam_shortcut_appid)}"]
    library.settings.set("launch_through_steam", False)
    assert library.steam_overlay_status(a.id)["use"] is False


def test_shortcuts_need_steam_closed(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    script_game(library, tmp_path, "true")
    with pytest.raises(Exception, match="has to be closed"):
        library.prepare_steam_shortcuts(restart_steam=False)
    assert steam.shortcuts_of(root, "1234") == []                     # nothing written while Steam runs
    def shutdown(timeout=40.0):
        monkeypatch.setattr(steam, "is_steam_running", lambda: False)      # Steam closed for real
        return True
    monkeypatch.setattr(steam, "shutdown_steam", shutdown)
    monkeypatch.setattr(threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    assert library.prepare_steam_shortcuts(restart_steam=True)["restarted"] is True
    assert len(steam.shortcuts_of(root, "1234")) == 1


def test_play_mode_runs_the_game_and_reports_it(library, tmp_path, monkeypatch, app_paths):
    e = script_game(library, tmp_path, 'echo "steam app id: ${SteamAppId:-none}"; sleep 2; exit 3')
    monkeypatch.setenv("SteamAppId", "0")                              # Steam sets it for shortcuts
    monkeypatch.setenv("SteamOverlayGameId", "12345")
    out = {}
    t = threading.Thread(target=lambda: out.update(code=steamrun.play(app_paths, e.id)))
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and e.id not in library.running_ids():
        time.sleep(0.05)
    assert library.is_running(e.id)                                    # the app sees it (and can stop it)
    library.reap()
    assert ("game_state", {"id": e.id, "running": True, "steam": True}) in library.events
    t.join(20)
    assert out["code"] == 3 and not library.is_running(e.id)
    library.reap()
    assert any(n == "game_state" and p["id"] == e.id and p["running"] is False for n, p in library.events)
    e = library.get(e.id)
    assert e.play_count == 1 and e.playtime_seconds >= 1
    text = (app_paths.logs / f"game-{e.id}.log").read_text()
    assert "through Steam" in text and "exited with code 3" in text and "steam app id: none" in text


def test_stop_a_game_started_through_steam(library, tmp_path, app_paths):
    e = script_game(library, tmp_path, "sleep 30")
    out = {}
    t = threading.Thread(target=lambda: out.update(code=steamrun.play(app_paths, e.id)))
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not library.is_running(e.id):
        time.sleep(0.05)
    started = time.time()
    library.stop(e.id)
    t.join(15)
    assert not t.is_alive() and time.time() - started < 10 and not library.is_running(e.id)


def test_stale_running_file_is_ignored(library, app_paths):
    d = app_paths.data / "running"
    d.mkdir(parents=True, exist_ok=True)
    steamrun.marker(d, 99).write_text('{"runner": 999999999, "game": 999999999, "started": 0}')
    assert not library.is_running(99) and not steamrun.marker(d, 99).exists()


def test_command_line_play_mode(tmp_path):
    """`ezgd --play ID` is what the Steam shortcut runs: it must work without the app's window."""
    import subprocess
    from ezgd.database.db import Database
    from ezgd.database.repositories import LibraryRepository
    from ezgd.platform.paths import resolve_paths
    home = tmp_path / "home"
    paths = resolve_paths(home)
    paths.ensure()
    db = Database(paths.database, backup_dir=paths.backups).open()
    exe = tmp_path / "g.sh"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    e = LibraryRepository(db).insert(LibraryEntry(title="CLI Game", status=LibraryStatus.INSTALLED,
                                                  executable=str(exe), executable_trusted=True))
    db.close()
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    r = subprocess.run([sys.executable, "-m", "ezgd", "--home", str(home), "--play", str(e.id)], env=env,
                       capture_output=True, timeout=60, cwd=str(Path(__file__).resolve().parents[1]))
    assert r.returncode == 0, r.stderr.decode()[-2000:]
    assert "exited with code 0" in (paths.logs / f"game-{e.id}.log").read_text()
    _ = vdf


def test_overlay_without_shortcut_or_restart(library, tmp_path, monkeypatch, app_paths):
    """Default: while Steam runs, Play loads Steam's own overlay into the game (no shortcut, no Steam restart)."""
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    e = script_game(library, tmp_path, 'echo "preload=$LD_PRELOAD vk=$ENABLE_VK_LAYER_VALVE_steam_overlay_1"')
    assert library.settings.settings.steam_overlay and library.settings.settings.launch_through_steam
    assert library.overlay_available() is None
    plan = library.launch(e.id, confirmed=True)
    assert "Steam overlay (friends list only)" in plan.description     # Steam does not know the game yet
    assert "SteamGameId" not in plan.env
    deadline = time.time() + 10
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.05)
    library.reap()
    out = (app_paths.logs / f"game-{e.id}.log").read_text()
    assert f"{root}/ubuntu12_32/gameoverlayrenderer.so:{root}/ubuntu12_64/gameoverlayrenderer.so" in out
    assert "vk=1" in out
    assert steam.shortcuts_of(root, "1234") == []                     # Steam's files untouched
    assert "Steam overlay" in library.preview(e.id)["description"]
    # Steam not running → started without it, and the Runs-with line says why
    monkeypatch.setattr(steam, "is_steam_running", lambda: False)
    assert library.overlay_available() == "start Steam first"
    assert "start Steam first" in library.preview(e.id)["description"]
    plan = library.launch(e.id, confirmed=True)
    assert "Steam overlay" not in plan.description and "LD_PRELOAD" not in plan.env
    library.settings.set("steam_overlay", False)
    assert library.overlay_available() == "switched off"


def test_a_steam_controller_keeps_steams_overlay_and_the_app_sees_it(library, tmp_path, monkeypatch):
    """0.5.3 left the overlay out while a Valve controller was connected — that also stopped in-game invites (a
    game's Invite button opens Steam's overlay). The overlay is loaded again: a tap of the Steam button over the game
    is Steam's, a hold the quick menu. has_steam_overlay tells which games have it."""
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", False)
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    e = script_game(library, tmp_path, "sleep 20")
    hid = tmp_path / "hid"
    hid.mkdir()
    real = steam.valve_controllers
    monkeypatch.setattr(steam, "valve_controllers", lambda base=None: real(hid))
    (hid / "0005:28DE:1106.0002").mkdir()                            # a Steam Controller (Bluetooth)
    assert library.overlay_available() is None
    assert "Steam Controller" not in library.steam_overlay_status(e.id)["reason"]
    plan = library.launch(e.id, confirmed=True)
    assert "Steam overlay (friends list only)" in plan.description and "gameoverlayrenderer" in plan.env["LD_PRELOAD"]
    try:
        assert library.has_steam_overlay(e.id)
        library._launch_info.pop(e.id, None)                         # (as if Steam had started it: from /proc)
        deadline = time.time() + 5
        while not library.has_steam_overlay(e.id) and time.time() < deadline:
            time.sleep(0.05)
        assert library.has_steam_overlay(e.id)
    finally:
        library.stop_completely(e.id, 3)
    library.settings.set("steam_overlay", False)
    e2 = script_game(library, tmp_path, "sleep 20", "No Overlay")
    library.launch(e2.id, confirmed=True)
    try:
        time.sleep(0.3)
        assert not library.has_steam_overlay(e2.id)
    finally:
        library.stop_completely(e2.id, 3)


def test_our_overlay_instead_of_steams(library, tmp_path, monkeypatch):
    """Settings → "Our overlay instead of Steam's": games start without Steam's overlay and not through Steam — the
    quick menu is the overlay (friends, chat, invites, carried out by Steam). They are still added to Steam (its
    library), but Play starts them here."""
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    e = script_game(library, tmp_path, "true")
    assert library.overlay_available() is None
    library.settings.set("own_overlay", True)
    assert "own overlay" in library.overlay_available()
    st = library.steam_overlay_status(e.id)
    assert st.get("add_only") is True                     # into Steam's library, started here
    plan = library.launch(e.id, confirmed=True)
    assert "Steam overlay" not in plan.description and "LD_PRELOAD" not in plan.env
    assert "own overlay" in library.preview(e.id)["description"]
    deadline = time.time() + 10
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.05)
    library.reap()


def test_overlay_env_keeps_other_preloads(tmp_path):
    root = tmp_path / "Steam"
    for arch in ("ubuntu12_32", "ubuntu12_64"):
        (root / arch).mkdir(parents=True)
        (root / arch / "gameoverlayrenderer.so").write_bytes(b"\x7fELF")
    env = steam.overlay_env(root, "/usr/lib/libgamemodeauto.so.0 /old/gameoverlayrenderer.so")
    assert env["LD_PRELOAD"].split(":") == ["/usr/lib/libgamemodeauto.so.0",
                                            f"{root}/ubuntu12_32/gameoverlayrenderer.so",
                                            f"{root}/ubuntu12_64/gameoverlayrenderer.so"]
    assert steam.overlay_env(tmp_path / "nosteam") is None


def test_overlay_can_be_switched_off_per_game(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    e = script_game(library, tmp_path, "true")
    assert any(f["key"] == "no_steam_overlay" for f in library.fixes_for(e.id))
    library.enable_fix(e.id, "no_steam_overlay")
    plan = library.launch(e.id, confirmed=True)
    assert "Steam overlay" not in plan.description and "LD_PRELOAD" not in plan.env
    assert "off for this game" in library.preview(e.id)["description"]


def test_running_file_of_another_program_is_ignored(library, app_paths):
    import subprocess
    other = subprocess.Popen(["sleep", "30"])          # alive, but not play mode (a reused process number)
    try:
        d = app_paths.data / "running"
        d.mkdir(parents=True, exist_ok=True)
        steamrun.marker(d, 98).write_text(f'{{"runner": {other.pid}, "game": {other.pid}, "started": 0}}')
        assert not library.is_running(98) and not steamrun.marker(d, 98).exists()
    finally:
        other.kill()


def test_quick_overlay_names_the_game_when_steam_knows_it(library, tmp_path, monkeypatch):
    """With the game's shortcut in Steam, the injected overlay gets its game id → the full overlay, not just friends."""
    root = fake_steam(tmp_path, monkeypatch, running=False)
    library.settings.set("steam_root", str(root))
    e = script_game(library, tmp_path, "true")
    library.prepare_steam_shortcuts()
    monkeypatch.setattr(steam, "is_steam_running", lambda: True)
    plan = library.launch(e.id, confirmed=True)
    gid = str(steam.shortcut_rungameid(library.get(e.id).steam_shortcut_appid))
    assert plan.env["SteamGameId"] == gid and plan.env["SteamOverlayGameId"] == gid
    assert plan.description.endswith("Steam overlay (Shift+Tab)")





# a shortcut list Steam itself wrote on this computer, if there is one
STEAM_WRITTEN = next(iter(sorted((Path.home() / ".local/share/Steam/userdata").glob("*/config/shortcuts.vdf"))),
                     Path("/nonexistent"))


def steam_file(root: Path) -> Path:
    return root / "userdata" / "1234" / "config" / "shortcuts.vdf"


def test_shortcuts_are_written_exactly_like_steams_own(library, tmp_path, monkeypatch):
    """2026-10-11: Steam broke after games were added to it. Entries now match what Steam writes itself (no icon
    path, no tags, unquoted start folder, same fields), and the user's own shortcuts stay byte-identical."""
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    own = vdf.binary_dumps({"shortcuts": {"0": steam._native_entry(123, "My Game", Path("/g/my.exe"), Path("/g"), "")}})
    steam_file(root).write_bytes(own)
    script_game(library, tmp_path, "true", "Game A")
    library.prepare_steam_shortcuts()
    entries = steam.shortcuts_of(root, "1234")
    assert vdf.binary_dumps({"x": entries[0]}) == vdf.binary_dumps({"x": vdf.binary_loads(own)["shortcuts"]["0"]})
    ours = entries[1]
    assert list(ours) == ["appid", "AppName", "Exe", "StartDir", "icon", "ShortcutPath", "LaunchOptions", "IsHidden",
                          "AllowDesktopConfig", "AllowOverlay", "OpenVR", "Devkit", "DevkitGameID",
                          "DevkitOverrideAppID", "LastPlayTime", "FlatpakAppID", "sortas", "tags"]
    assert ours["icon"] == "" and ours["tags"] == {} and ours["StartDir"].endswith("/") and '"' not in ours["StartDir"]
    assert list(steam_file(root).parent.glob("shortcuts.vdf.ezgd-backup-*"))                 # backup kept
    if Path(STEAM_WRITTEN).exists():                     # same fields as a shortcut Steam wrote on this computer
        real = vdf.binary_loads(Path(STEAM_WRITTEN).read_bytes())["shortcuts"]["0"]
        assert list(real) == list(ours)
        assert {k: type(v).__name__ for k, v in real.items()} == {k: type(v).__name__ for k, v in ours.items()}


def test_steam_files_never_written_while_steam_runs_or_unverified(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    with pytest.raises(steam.SteamFileError):
        steam.add_shortcut(root, "1234", name="X", exe=Path("/x"), start_dir=Path("/"))
    assert not steam_file(root).exists()
    monkeypatch.setattr(steam, "is_steam_running", lambda: False)
    steam_file(root).write_bytes(b"\x00not a shortcut list")            # unreadable: left alone
    with pytest.raises(steam.SteamFileError, match="left alone"):
        steam.add_shortcut(root, "1234", name="X", exe=Path("/x"), start_dir=Path("/"))
    assert steam_file(root).read_bytes() == b"\x00not a shortcut list"
    assert not hasattr(library, "sync_steam_shortcuts")               # nothing is ever written in the background


def test_onlinefix_games_get_the_overlay_without_steams_game_ids(library, tmp_path, monkeypatch, app_paths):
    """OnlineFix asks for a "Steamworks activation" when it sees the game ids Steam sets for a shortcut: those are
    hidden from it in play mode, while the overlay keeps its own id and preload."""
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    of = script_game(library, tmp_path, 'echo "id=${SteamGameId:-none} app=${SteamAppId:-none} '
                                       'ov=${SteamOverlayGameId:-none} pre=${LD_PRELOAD:-none}"', "OF Game")
    (Path(of.install_path) / "OnlineFix.ini").write_text("[Main]\nRealAppId=2868840\n")
    assert library.talks_to_steam_itself(library.get(of.id))
    assert library.steam_overlay_status(of.id)["use"]
    library.settings.set("steam_overlay", False)
    for k, v in {"SteamGameId": "999", "SteamAppId": "0", "SteamOverlayGameId": "999",
                 "LD_PRELOAD": "/s/ubuntu12_64/gameoverlayrenderer.so"}.items():
        monkeypatch.setenv(k, v)
    assert steamrun.play(app_paths, of.id) == 0
    out = (app_paths.logs / f"game-{of.id}.log").read_text()
    assert "id=none app=none ov=999 pre=/s/ubuntu12_64/gameoverlayrenderer.so" in out
    # with OnlineFix's FakeAppId known, that app becomes the game id: the full overlay, and what OnlineFix expects
    (Path(of.install_path) / "OnlineFix.ini").write_text("[Main]\nRealAppId=2868840\nFakeAppId=480\n#Language=x\n")
    assert library.onlinefix_app_id(library.get(of.id)) == 480
    for k, v in {"SteamGameId": "999", "SteamAppId": "0", "SteamOverlayGameId": "999"}.items():
        monkeypatch.setenv(k, v)
    assert steamrun.play(app_paths, of.id) == 0
    out = (app_paths.logs / f"game-{of.id}.log").read_text()
    assert "id=480 app=none ov=480 pre=/s/ubuntu12_64/gameoverlayrenderer.so" in out


def test_quick_overlay_of_onlinefix_games_uses_its_app_id(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    library.settings.set("steam_overlay", True)
    monkeypatch.setattr(library, "overlay_available", lambda: None)
    monkeypatch.setattr(steam, "overlay_env", lambda r, pre="": {"LD_PRELOAD": "/s/gameoverlayrenderer.so"})
    of = script_game(library, tmp_path, "exit 0", "OF Quick")
    (Path(of.install_path) / "OnlineFix.ini").write_text("[Main]\nFakeAppId=480\n")
    plan = library.launch_plan(of.id)
    library._add_overlay(plan, library.get(of.id))
    assert plan.env["SteamGameId"] == "480" == plan.env["SteamOverlayGameId"] and "SteamAppId" not in plan.env
    assert "Shift+Tab" in plan.description


def test_a_working_proton_is_kept_and_overlay_quick_exit_falls_back(library, tmp_path, monkeypatch):
    from ezgd.library.models import LibraryEntry, LibraryStatus
    e = library.repo.insert(LibraryEntry(title="Win Game", status=LibraryStatus.INSTALLED, executable="/g/Game.exe"))
    library.remember_working_proton(e, "Proton-GE Latest", 30)
    assert e.proton_version is None                                   # too short to count as "worked"
    library.remember_working_proton(e, "Proton-GE Latest", 600)
    assert e.proton_version == "Proton-GE Latest"
    library.repo.update(e)
    other = library.repo.insert(LibraryEntry(title="Other", status=LibraryStatus.INSTALLED, executable="/g/O.exe"))
    monkeypatch.setattr(library, "protons", lambda refresh=False: [type("P", (), {"name": "Proton-CachyOS"})()])
    assert library.pin_current_protons() == 1                       # before a new GE is installed
    assert library.get(other.id).proton_version == "Proton-CachyOS"
    assert library.get(e.id).proton_version == "Proton-GE Latest"   # its own choice untouched
    # a game that quits within seconds with the overlay loaded starts without it next time
    root = fake_steam(tmp_path, monkeypatch, running=True)
    library.settings.set("steam_root", str(root))
    g = script_game(library, tmp_path, "exit 1", "Quitter")
    plan = library.launch(g.id, confirmed=True)
    assert "Steam overlay" in plan.description
    deadline = time.time() + 10
    while library.is_running(g.id) and time.time() < deadline:
        time.sleep(0.05)
    library.reap()
    assert "no_steam_overlay" in library.get(g.id).compat_config.fixes
    assert any(n == "notify" and "overlay switched off" in p[0] for n, p in library.events)


def test_safety_net_puts_steams_list_back(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    original = vdf.binary_dumps({"shortcuts": {"0": steam._native_entry(5, "Mine", Path("/m"), Path("/"), "")}})
    steam_file(root).write_bytes(original)
    script_game(library, tmp_path, "true")
    from ezgd.library.manager import LibraryManager
    monkeypatch.setattr(library, "STEAM_START_SECONDS", 0.3)
    started = []
    monkeypatch.setattr(threading, "Thread", lambda target, args, **kw: started.append(args) or
                        type("T", (), {"start": lambda self: None})())
    library.prepare_steam_shortcuts()
    assert steam_file(root).read_bytes() != original
    LibraryManager._watch_steam(library, *started[0])  # Steam never came up after the change
    assert steam_file(root).read_bytes() == original and library.settings.settings.launch_through_steam is False
    assert any(n == "notify" and p[0] == "Steam was put back" for n, p in library.events)


def test_remove_the_apps_games_from_steam(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    steam_file(root).write_bytes(vdf.binary_dumps({"shortcuts": {"0": steam._native_entry(5, "Mine", Path("/m"),
                                                                                             Path("/"), "")}}))
    script_game(library, tmp_path, "true", "A")
    script_game(library, tmp_path, "true", "B")
    library.prepare_steam_shortcuts()
    assert len(steam.shortcuts_of(root, "1234")) == 3
    assert library.remove_steam_shortcuts() == 2
    assert [s["AppName"] for s in steam.shortcuts_of(root, "1234")] == ["Mine"]


def test_old_crash_prone_shortcuts_are_never_written_and_get_removed(library, tmp_path, monkeypatch):
    """2026-10-12: Steam's Library crashed (memstd.cpp fatal assert) on app shortcuts with the game's .exe as icon."""
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    mine = steam._native_entry(5, "Mine", Path("/m"), Path("/"), "")
    bad = {**steam._native_entry(7, "PEAK", Path("/a/ezgd"), Path("/a"), "--play 1"),
           "icon": "/games/PEAK/PEAK.exe", "tags": {"0": "EZ Game Downloader"}}   # what the old version wrote
    with pytest.raises(steam.SteamFileError, match="icon"):
        steam.write_shortcuts(steam_file(root), lambda sc: sc.update({"0": bad}) or True)
    assert not steam_file(root).exists()                              # refused before anything was written
    steam_file(root).write_bytes(vdf.binary_dumps({"shortcuts": {"0": mine, "1": bad}}))
    monkeypatch.setattr(steam, "is_steam_running", lambda: True)
    assert "Remove the app's games from Steam" in library.check_steam_shortcuts()   # Steam open: only a message
    assert steam.risky_app_shortcuts(root) == 1
    monkeypatch.setattr(steam, "is_steam_running", lambda: False)
    assert "Removed 1" in library.check_steam_shortcuts()
    left = steam.shortcuts_of(root, "1234")
    assert [v["AppName"] for v in left] == ["Mine"]
    assert vdf.binary_dumps({"x": left[0]}) == vdf.binary_dumps({"x": mine})
    assert library.check_steam_shortcuts() is None


def test_onlinefix_games_say_they_need_steam_running(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch, running=False)
    library.settings.set("steam_root", str(root))
    monkeypatch.setattr(steam, "steam_command", lambda: ["true"])
    of = script_game(library, tmp_path, "exit 0", "OF Needs Steam")
    (Path(of.install_path) / "OnlineFix.ini").write_text("[Main]\nFakeAppId=480\n")
    plain = script_game(library, tmp_path, "exit 0", "Plain Game")
    st = library.steam_overlay_status(of.id)
    assert st["needs_steam"] is True and st["steam_running"] is False
    assert library.steam_overlay_status(plain.id)["needs_steam"] is False


def test_onlinefix_as_the_genuine_app_gets_steams_own_ids(library, tmp_path, monkeypatch):
    """Little Nightmares III's Friend's Pass: OnlineFix runs as the real app (FakeAppId == RealAppId), and the game
    quits at once (exit code 53) unless it gets the Steam ids Steam itself would pass."""
    of = script_game(library, tmp_path, "exit 0", "LN3 Like")
    deep = Path(of.install_path) / "Engine" / "Binaries" / "ThirdParty" / "Steamworks" / "Steamv157" / "Win64"
    deep.mkdir(parents=True)
    (deep / "OnlineFix.ini").write_text("[Main]\nRealAppId=1392860\nFakeAppId=1392860\n")
    (deep / "OnlineFix64.dll").write_bytes(b"MZ")
    e = library.get(of.id)
    assert library.onlinefix_ids(e) == (1392860, 1392860)
    env = library.launch_plan(of.id).env
    assert env["SteamAppId"] == env["SteamGameId"] == "1392860"
    (deep / "OnlineFix.ini").write_text("[Main]\nRealAppId=2868840\nFakeAppId=480\n")   # Spacewar: not exported
    assert "SteamAppId" not in library.launch_plan(of.id).env


def test_overlay_off_for_a_game_also_means_not_through_steam(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    g = script_game(library, tmp_path, "exit 0", "No Overlay Game")
    assert library.steam_overlay_status(g.id)["use"]
    library.enable_fix(g.id, "no_steam_overlay")
    st = library.steam_overlay_status(g.id)
    assert not st["use"] and "off for this game" in st["reason"]


# ------------------------------------------------------------- never the same game twice in Steam
def _put_shortcuts(root, entries: list[dict]) -> None:
    path = root / "userdata" / "1234" / "config" / "shortcuts.vdf"
    path.write_bytes(vdf.binary_dumps({"shortcuts": {str(i): e for i, e in enumerate(entries)}}))


def _user_shortcut(name: str, exe: Path, opts: str = "") -> dict:
    return steam._native_entry(steam.shortcut_appid(f'"{exe}"', name), name, exe, exe.parent, opts)


def test_a_game_already_in_steam_is_not_added_twice(library, tmp_path, monkeypatch):
    """2026-10-12 on the user's PC: Steam had "PEAK.exe" (added outside the app) and the app added "PEAK" too."""
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    peak = script_game(library, tmp_path, "true", "PEAK")
    other = script_game(library, tmp_path, "true", "Other Game")
    named = script_game(library, tmp_path, "true", "Hades II")
    mine = _user_shortcut("PEAK.exe", Path(peak.executable), 'WINEDLLOVERRIDES="winmm=n,b" %command%')
    by_name = _user_shortcut("Hades II.exe", tmp_path / "elsewhere" / "Hades2.exe")     # same name, other folder
    unrelated = _user_shortcut("Some Emulator", tmp_path / "emu" / "emu.AppImage")
    _put_shortcuts(root, [mine, by_name, unrelated])
    before = {k: vdf.binary_dumps({"x": v}) for k, v in enumerate([mine, by_name, unrelated])}
    st = library.steam_overlay_status(peak.id)
    assert st["use"] is False and st["in_steam_as"] == "PEAK.exe"           # Play starts it directly then
    assert library.steam_overlay_status(other.id)["use"] is True
    r = library.prepare_steam_shortcuts()
    assert r["games"] == 1 and {x["title"] for x in r["skipped"]} == {"PEAK", "Hades II"}
    names = [v["AppName"] for v in steam.shortcuts_of(root, "1234")]
    assert sorted(names) == sorted(["PEAK.exe", "Hades II.exe", "Some Emulator", "Other Game"])
    kept = [vdf.binary_dumps({"x": v}) for v in steam.shortcuts_of(root, "1234") if v["AppName"] != "Other Game"]
    assert sorted(kept) == sorted(before.values())                          # the user's own: untouched
    with pytest.raises(Exception, match="already has this game as “PEAK.exe”"):
        library.add_to_steam(peak.id)
    assert library.steam_overlay_status(named.id)["in_steam_as"] == "Hades II.exe"


def test_the_apps_own_shortcut_is_updated_not_duplicated(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    a = script_game(library, tmp_path, "true", "Game A")
    old_app = tmp_path / "old place" / "ezgd"                              # the app was somewhere else back then
    opts = steamrun.launch_options(a.id)
    twice = [_user_shortcut("Game A", old_app, opts), _user_shortcut("Game A", old_app, opts)]
    _put_shortcuts(root, twice)
    assert library.prepare_steam_shortcuts()["games"] == 1
    sc = steam.shortcuts_of(root, "1234")
    exe, _ = steamrun.runner()
    assert len(sc) == 1 and sc[0]["Exe"] == f'"{exe}"' and sc[0]["LaunchOptions"] == opts
    assert library.steam_overlay_status(a.id)["ready"]


def test_two_library_copies_of_a_game_get_one_steam_entry(library, tmp_path, monkeypatch):
    root = fake_steam(tmp_path, monkeypatch)
    library.settings.set("steam_root", str(root))
    library.settings.set("launch_through_steam", True)
    first = script_game(library, tmp_path, "true", "Twin Game")
    second = library.repo.insert(LibraryEntry(title="Twin Game", status=LibraryStatus.INSTALLED,
                                              install_path=str(tmp_path / "games" / "Twin Game 2"),
                                              executable=first.executable, executable_trusted=True))
    r = library.prepare_steam_shortcuts()
    assert r["games"] == 1 and r["skipped"][0]["by_app"] is True
    assert [v["AppName"] for v in steam.shortcuts_of(root, "1234")] == ["Twin Game"]
    assert library.steam_overlay_status(second.id)["use"] is False
    # the first copy is removed from the Library: its leftover shortcut is reused for the second, not doubled
    library.repo.delete(first.id)
    assert library.prepare_steam_shortcuts()["games"] == 1
    sc = steam.shortcuts_of(root, "1234")
    assert len(sc) == 1 and sc[0]["LaunchOptions"].endswith(f"--play {second.id}")


def test_our_overlay_adds_the_game_to_steam_then_plays_it_here():
    """With our overlay on, Play adds the game to Steam's library (while Steam is closed), starts Steam — it must
    come up to keep the list, and the overlay and the controller's game layout need it — then the game directly."""
    from types import SimpleNamespace
    from ezgd.ui.actions import Actions
    calls, shown = [], []

    class Core:
        def library_call(self, name, *args):
            return (name, args)

    class Bridge:
        def call(self, coro, ok=None, err=None, owner=None):
            calls.append(coro)
            ok({"games": 1, "restarted": False, "skipped": []} if coro[0] == "prepare_steam_shortcuts" else True)
    fake = SimpleNamespace(bridge=Bridge(), core=Core(), _told_in_steam=set(), settings={},
                           toasts=SimpleNamespace(show=lambda *a: shown.append(a[0])), fail=lambda *a: None)
    entry = SimpleNamespace(id=7, title="Dark Souls III")
    started = []
    Actions._add_then_play(fake, None, entry, {"use": True, "add_only": True, "ready": False,
                                               "steam_running": False}, False, lambda note="": started.append(1))
    assert calls == [("prepare_steam_shortcuts", (False,)), ("start_steam", ())] and started == [1]
    calls.clear()
    Actions._add_then_play(fake, None, entry, {"use": True, "add_only": True, "ready": True}, False,
                           lambda note="": started.append(2))
    assert calls == [] and started == [1, 2]                     # already in Steam: straight to the game
    Actions._add_then_play(fake, None, entry, {"use": True, "add_only": True, "ready": False,
                                               "steam_running": True}, True, lambda note="": started.append(3))
    assert calls == [] and started[-1] == 3                      # couch mode never restarts Steam
