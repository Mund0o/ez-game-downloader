"""The quick menu (guide button held while playing): suspending and resuming games for real, switching games,
couch mode, quitting, the game's own volume — only for games the app runs. Also couch mode's Game settings page and
the sound volume settings."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QSlider

from test_couch_ui import _open_couch
from test_features_v2 import core  # noqa: F401 — the core fixture
from test_ui import ui  # noqa: F401 — the shared main-window fixture

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process states from /proc")


def _state(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


def _install(lib, tmp_path, name: str):
    """A library game whose "game" is a shell with a child process (like a launcher and its game)."""
    folder = tmp_path / "games" / name
    folder.mkdir(parents=True)
    exe = folder / "game.sh"
    exe.write_text("#!/bin/sh\nsleep 300 &\nwait\n")
    exe.chmod(0o755)
    e = lib.import_folder(name, folder, None)
    lib.set_executable(e.id, str(exe))
    lib.mark_installed(e.id, str(folder), str(exe))
    return lib.get(e.id)


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------------------------------- the core
@linux_only
async def test_suspend_freezes_the_whole_game_and_resume_continues_it(core, tmp_path):  # noqa: F811
    lib = core.library
    e = _install(lib, tmp_path, "Frozen Game")
    lib.launch(e.id, confirmed=True)
    assert _wait(lambda: len(lib.game_processes(e.id)) >= 2)             # the shell and its "game"
    pids = lib.game_processes(e.id)
    assert os.getpid() not in pids
    time.sleep(0.3)
    lib.suspend(e.id)
    assert all(_state(p) == "T" for p in pids)                          # every process frozen
    assert lib.suspended_ids() == [e.id] and lib.is_running(e.id)
    assert (lib.running_dir / f"suspended-{e.id}.json").exists()       # remembered, should the app crash
    assert any(n == "game_state" and p.get("suspended") for n, p in core.events)
    time.sleep(1.2)
    lib.resume(e.id)
    assert all(_state(p) != "T" for p in pids)                          # running again, same processes
    assert not lib.suspended_ids() and not (lib.running_dir / f"suspended-{e.id}.json").exists()
    assert lib.session_seconds(e.id) < 1.2                               # frozen time is not play time
    lib.suspend(e.id)                                                    # quitting a frozen game works too
    r = lib.stop_completely(e.id, grace=3)
    assert r["stopped"] and not lib.is_running(e.id)
    assert all(not Path(f"/proc/{p}").exists() or _state(p) == "Z" for p in pids)


@linux_only
async def test_a_game_left_frozen_by_a_crashed_app_runs_again(core, tmp_path):  # noqa: F811
    lib = core.library
    p = subprocess.Popen(["sleep", "60"])
    try:
        os.kill(p.pid, signal.SIGSTOP)
        assert _wait(lambda: _state(p.pid) == "T")
        lib.running_dir.mkdir(parents=True, exist_ok=True)
        (lib.running_dir / "suspended-99.json").write_text(json.dumps({"pids": [p.pid]}))
        lib.thaw_leftovers()                                             # what a new start does
        assert _wait(lambda: _state(p.pid) != "T")
        assert not (lib.running_dir / "suspended-99.json").exists()
    finally:
        p.kill()
        p.wait()


@linux_only
async def test_game_volume_changes_only_the_games_own_sound(core, tmp_path, monkeypatch):  # noqa: F811
    lib = core.library
    e = _install(lib, tmp_path, "Loud Game")
    lib.launch(e.id, confirmed=True)
    assert _wait(lambda: len(lib.game_processes(e.id)) >= 2)
    game_pid = lib.game_processes(e.id)[-1]
    streams = [{"index": 11, "properties": {"application.process.id": str(game_pid)},
                "volume": {"front-left": {"value_percent": "70%"}, "front-right": {"value_percent": "70%"}}},
               {"index": 12, "properties": {"application.process.id": "1"},          # another program
                "volume": {"front-left": {"value_percent": "100%"}}}]
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "pactl":
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(streams), stderr="")
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pactl" if name == "pactl" else None)
    assert lib.game_volume(e.id) == 70
    assert lib.set_game_volume(e.id, 40) == 40
    assert ["pactl", "set-sink-input-volume", "11", "40%"] in calls
    assert not any(c[:3] == ["pactl", "set-sink-input-volume", "12"] for c in calls)
    monkeypatch.setattr(subprocess, "run", real_run)
    lib.stop_completely(e.id, grace=3)


# ---------------------------------------------------------------------------------------- the menu
def _running(win, core, tmp_path, qtbot, name):
    e = _install(core.library, tmp_path, name)
    win.bridge.run_sync(core.library_call("launch", e.id, True))
    win._reload_library()
    qtbot.waitUntil(lambda: e.id in win.pages["library"].running and e.id in win.pages["library"].entries,
                    timeout=5000)                         # (running comes from an event, the entries from a reload)
    return e


@linux_only
def test_quick_menu_is_only_for_games_the_app_runs(ui, qtbot, tmp_path):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch._home_held()                                    # nothing running (a game started in Steam itself is
    assert couch.quick is None                            # not the app's): no menu
    e = _running(win, core, tmp_path, qtbot, "Desk Game")          # started from the desktop: still the app's
    qtbot.waitUntil(lambda: e.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.isVisible(), timeout=5000)
    q = couch.quick
    assert q.game.id == e.id
    assert [r.key for r in q.rows][:3] == ["resume", "switch", "couch"] and q.rows[-1].key == "quit"
    q.grab()
    q.command("b")                                        # back to the game
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    qtbot.waitUntil(lambda: couch.isMinimized(), timeout=3000)
    couch._home_held()                                    # and again
    qtbot.waitUntil(lambda: q.isVisible(), timeout=3000)
    q.opened_at -= 2                                      # (a moment later — not the hold that opened it)
    couch._home_held()                                    # held once more: back to the game
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    win.bridge.run_sync(core.library_call("stop_completely", e.id, 3))
    couch.force_close()


@linux_only
def test_switching_games_suspends_the_one_being_played(ui, qtbot, tmp_path):
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "First Game")
    b = _install(core.library, tmp_path, "Second Game")
    win._reload_library()
    qtbot.waitUntil(lambda: b.id in win.pages["library"].entries, timeout=5000)
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running and len(couch.entries) == 2, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.command("down")                                     # Switch game
    q.command("a")
    assert q.view == "switch" and [e.id for e in q.grid] == [b.id]
    q.grab()
    q.command("a")                                        # the other one: "Play …?" right here in the menu
    assert q.view == "confirm" and q.target.id == b.id and q.isVisible()
    assert core.library.is_running(a.id) and not core.library.is_suspended(a.id)     # (nothing happens yet)
    q.grab()
    q.command("a")                                        # ▶ Play
    qtbot.waitUntil(lambda: core.library.is_suspended(a.id), timeout=5000)
    qtbot.waitUntil(lambda: core.library.is_running(b.id), timeout=10000)
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=10000)         # it is up: the menu gets out of the way
    assert couch.overlay is None                          # couch mode never came forward to ask
    assert a.id in couch.suspended and b.id in couch.launched
    win.bridge.run_sync(core.library_call("stop_completely", b.id, 3))
    # the suspended game waits in couch mode: A on it continues it
    tile = next(e for e in couch.entries if e.id == a.id)
    assert couch.state_text(tile) == "Suspended"
    couch.grab()
    couch.primary(tile)
    qtbot.waitUntil(lambda: not core.library.is_suspended(a.id), timeout=5000)
    qtbot.waitUntil(lambda: a.id not in couch.suspended, timeout=5000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_go_to_couch_mode_then_leaving_couch_mode_resumes_the_game(ui, qtbot, tmp_path):
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Couch Break")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "couch")
    q.command("a")
    qtbot.waitUntil(lambda: core.library.is_suspended(a.id), timeout=5000)
    qtbot.waitUntil(lambda: not q.isVisible() and couch.isVisible() and not couch.isMinimized(), timeout=5000)
    couch._home_held()                                    # held in couch mode: the menu lists the suspended game
    qtbot.waitUntil(lambda: q.isVisible() and q._shown >= 1.0, timeout=5000)
    assert q.game is None and any(r.key == f"game-{a.id}" for r in q.rows)
    q.command("b")                                        # (closed again: stays suspended)
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    assert core.library.is_suspended(a.id)
    couch.force_close()                                   # nothing may stay frozen without couch mode
    qtbot.waitUntil(lambda: not core.library.is_suspended(a.id), timeout=5000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


@linux_only
def test_quit_from_the_quick_menu_asks_twice(ui, qtbot, tmp_path):
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Quit Me")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "quit")
    q.command("a")
    assert q.isVisible() and q.rows[q.focus].label == "Press again to quit"
    assert core.library.is_running(a.id)
    q.command("a")
    qtbot.waitUntil(lambda: not core.library.is_running(a.id), timeout=20000)
    couch.force_close()


# ------------------------------------------------------------------------------ game settings, volume
def test_couch_game_settings_save_fps_limit_and_launch_options(ui, qtbot, tmp_path):
    win, core, _ = ui
    e = _install(core.library, tmp_path, "Tuned Game")
    win._reload_library()
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: len(couch.entries) == 1, timeout=5000)
    couch.open_game_settings(couch.entries[0])
    page = couch.current_page()
    qtbot.waitUntil(lambda: page.loaded, timeout=5000)
    couch.grab()
    assert not couch._paint_failed
    page.focus = next(k for k, it in enumerate(page.items) if it.label == "FPS limit")
    couch.command("right")
    qtbot.waitUntil(lambda: core.library.get(e.id).compat_config.fps_limit == 30, timeout=5000)
    from ezgd.library import fixes
    ctx = core.library._context(core.library.get(e.id))
    assert fixes.effects(ctx).env["DXVK_FRAME_RATE"] == "30"
    page.focus = next(k for k, it in enumerate(page.items) if it.label == "Launch options")
    couch.command("a")
    kb = couch.keyboard
    assert kb is not None and len(kb.rows) == 5                       # the symbols row for = and %
    kb.text = ""
    kb.press(len(kb.rows), 0)                                         # ⇧: capitals
    for ch in "mangohud":
        kb.type(ch.upper() if kb.shift else ch)
    kb.press(len(kb.rows), 0)
    for ch in "=1 %command% -dx11":
        kb.type(ch)
    couch.grab()
    kb.done()
    qtbot.waitUntil(lambda: core.library.get(e.id).launch_args == "MANGOHUD=1 %command% -dx11", timeout=5000)
    couch.command("b")
    assert couch.current_page() is not page
    couch.force_close()


def test_sound_volume_settings(ui, qtbot):
    win, core, _ = ui
    slider = win.pages["settings"].widgets["couch_sounds_volume"]
    assert isinstance(slider, QSlider) and isinstance(win.pages["settings"].widgets["couch_intro_volume"], QSlider)
    couch = _open_couch(win, qtbot)
    from ezgd.ui.couch import T_SETTINGS
    couch._switch_tab(T_SETTINGS, force=True)
    page = couch.current_page()
    page._build()
    page.focus = next(k for k, it in enumerate(page.items) if it.label == "Sound volume")
    assert page.items[page.focus].value == "15 %"
    couch.zone = "main"
    couch.command("right")
    assert couch.actions.settings["couch_sounds_volume"] == 20 and couch.sounds.played[-1] == "select"
    qtbot.waitUntil(lambda: core.settings.get("couch_sounds_volume") == 20, timeout=5000)
    couch.command("left")
    couch.command("left")
    assert couch.actions.settings["couch_sounds_volume"] == 10
    couch.force_close()


@linux_only
def test_a_tap_of_the_guide_button_opens_and_closes_the_quick_menu(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Tap Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)      # the game has the screen
    couch._pad("home")                                               # a tap (like the PS button on a PS5)
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.isVisible(), timeout=5000)
    q = couch.quick
    couch._home_held()                                               # the same press, held: not "close it again"
    assert q.isVisible()
    q.opened_at -= 2
    couch._pad("home")                                               # tapped again: back to the game
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_quick_menu_over_a_desktop_game_with_couch_mode_closed(ui, qtbot, tmp_path):
    """Started from the desktop, couch mode never opened: the guide button still brings the quick menu, with couch
    mode hidden behind it — the desktop stays as it was unless the menu asks for couch mode."""
    win, core, _ = ui
    win.show()
    a = _running(win, core, tmp_path, qtbot, "Desk Only")
    assert win.couch is None
    win._quick_menu_from_desktop()
    couch = win.couch
    assert couch is not None and couch.background and not couch.isVisible()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.isVisible(), timeout=5000)
    assert couch.quick.game.id == a.id and win.isVisible()
    couch.quick.command("b")                                         # back to the game
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)         # the hidden couch mode is gone again
    assert win.isVisible()
    win._quick_menu_from_desktop()                                   # again — this time: Go to couch mode
    couch = win.couch
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "couch")
    q.command("a")
    qtbot.waitUntil(lambda: couch.isVisible() and not couch.background, timeout=5000)
    qtbot.waitUntil(lambda: not win.isVisible(), timeout=5000)       # couch mode for real, as from the desktop
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


@linux_only
def test_quitting_a_desktop_game_from_the_quick_menu_never_opens_couch_mode(ui, qtbot, tmp_path):
    """Started from the desktop: the quick menu may be used, but leaving the game (Quit there, or the game ending)
    goes back to the desktop app — couch mode opened by itself before."""
    win, core, _ = ui
    win.show()
    a = _running(win, core, tmp_path, qtbot, "Desk Quit")
    win._quick_menu_from_desktop()
    couch = win.couch
    opened = []
    real = couch._promote
    couch._promote = lambda: (opened.append(1), real())
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "quit")
    q.command("a")                                                   # armed …
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "quit")
    q.command("a")                                                   # … and quit
    qtbot.waitUntil(lambda: a.id not in win.pages["library"].running, timeout=20000)
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)         # the hidden couch mode is gone …
    assert opened == [] and win.isVisible()                          # … and never opened for real


@linux_only
def test_game_settings_from_a_desktop_games_quick_menu_is_only_a_visit(ui, qtbot, tmp_path):
    """Game settings need couch mode's screens: shown for them, put away again on "Back to the game"."""
    win, core, _ = ui
    win.show()
    a = _running(win, core, tmp_path, qtbot, "Desk Settings")
    win._quick_menu_from_desktop()
    couch = win.couch
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "settings")
    q.command("a")
    qtbot.waitUntil(lambda: couch.isVisible() and bool(couch.stack), timeout=5000)
    assert couch._visiting
    couch.command("b")                                               # B: back to the game
    qtbot.waitUntil(lambda: not couch.isVisible(), timeout=5000)
    assert couch.background and not couch._visiting
    qtbot.waitUntil(lambda: win.couch is None and win.isVisible(), timeout=5000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


def test_a_game_that_ran_is_never_closed_when_started_again(ui, qtbot):
    """Leaving couch mode right after a game ended noted it as "still starting — close it when it appears", and
    starting it again a minute later closed it after two seconds."""
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch._launch_at[42] = __import__("time").monotonic()            # started from couch mode …
    couch.launched.add(42)
    couch.game_state({"id": 42, "running": True})                    # … it runs …
    assert 42 not in couch._launch_at
    couch.game_state({"id": 42, "running": False})                   # … and ends
    assert 42 not in couch._starting_ids() and not couch.pending_kill
    couch.force_close()
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)
    win._kill_on_start = {7: __import__("time").monotonic() + 90}   # really left while it was starting
    win.actions.launch_requested(7)                                  # the user starts it again: never closed
    assert 7 not in win._kill_on_start


@linux_only
def test_back_from_game_settings_goes_into_the_game(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Settings Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    entry = next(e for e in couch.entries if e.id == a.id) if any(e.id == a.id for e in couch.entries) else a
    went = []
    monkeypatch.setattr(couch, "go_to_game", lambda e: went.append(e.id))
    couch.open_game_settings(entry, back_to_game=True)              # (what the quick menu opens)
    page = couch.current_page()
    assert page.items[0].label == "▶  Back to the game"
    assert ("b", "Back to the game") in page.hints()
    couch.command("b")
    assert went == [a.id] and couch.current_page() is not page
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_the_ps_button_still_opens_the_menu_when_running_came_before_started(ui, qtbot, tmp_path, monkeypatch):
    """The game's "running" came before couch mode's "started": the launch flag stayed on for the whole game, every
    PS tap was ignored, and putting Steam's window away pulled couch mode over the game."""
    from ezgd.ui import steam_bpm, window_control
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Order Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    qtbot.wait(600)                                                    # (opening: activated, size checked)
    couch.running.discard(a.id)
    couch._launching = True                                        # Play pressed in couch mode …
    couch._launch_at[a.id] = __import__("time").monotonic()
    couch.game_state({"id": a.id, "running": True})                # … "running" arrives before "started"
    assert not couch._launching and a.id in couch.launched
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)    # the game has the screen
    monkeypatch.setattr(steam_bpm, "steam_running", lambda: True)
    monkeypatch.setattr(window_control, "hide_steam_windows", lambda only_active=True: True)
    pulled = []
    monkeypatch.setattr(couch.wc, "activate", lambda: pulled.append(1))
    couch._pad("home")                                             # a tap: the quick menu
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.isVisible(), timeout=5000)
    couch.quick.command("b")                                       # back to the game
    qtbot.waitUntil(lambda: not couch.quick.isVisible(), timeout=3000)
    couch._front = "game"
    couch._regain_screen()                                         # (after Steam was put away)
    assert not pulled                                              # couch mode never comes over the game
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


async def test_mangohud_is_ready_hidden_and_switches_live(core, tmp_path, monkeypatch):  # noqa: F811
    """MangoHud from the quick menu over the running game, not after a restart: every game starts with MangoHud
    loaded but hidden, reading its own config — switching rewrites it, and MangoHud reloads a changed config."""
    import shutil
    cfg = tmp_path / "xdg"
    (cfg / "MangoHud").mkdir(parents=True)
    (cfg / "MangoHud" / "MangoHud.conf").write_text("fps\ngpu_temp\nno_display\n")      # the user's own setup
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/mangohud" if n == "mangohud" else real_which(n))
    lib = core.library
    folder = tmp_path / "games" / "Hud Game"
    folder.mkdir(parents=True)
    exe = folder / "game.sh"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    e = lib.import_folder("Hud Game", folder, None)
    lib.set_executable(e.id, str(exe))
    plan = lib.launch_plan(e.id)
    conf = lib.mangohud_conf(e.id)
    assert plan.env["MANGOHUD"] == "1" and plan.env["MANGOHUD_CONFIGFILE"] == str(conf)
    assert conf.read_text().split() == ["fps", "gpu_temp", "no_display"]          # loaded, hidden
    lib.set_mangohud(e.id, True)                                                   # switched on: shown …
    assert conf.read_text().split() == ["fps", "gpu_temp"]
    assert "mangohud" in lib.get(e.id).compat_config.fixes                         # … and from the next start
    lib.set_mangohud(e.id, False)
    assert conf.read_text().split() == ["fps", "gpu_temp", "no_display"]
    core.settings.set("mangohud_live", False)                                      # switched off in Settings
    assert "MANGOHUD" not in lib.launch_plan(e.id).env


@linux_only
def test_mangohud_switch_in_the_quick_menu(ui, qtbot, tmp_path, monkeypatch):
    import shutil
    win, core, _ = ui
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/mangohud" if n == "mangohud" else real_which(n))
    a = _running(win, core, tmp_path, qtbot, "Hud Menu")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    row = next(r for r in q.rows if r.key == "mangohud")
    assert row.value == "Off" and row.extra == {"toggle": False}            # (drawn as an on / off switch)
    q.focus = q.rows.index(row)
    q.command("a")
    qtbot.waitUntil(lambda: "mangohud" in core.library.get(a.id).compat_config.fixes, timeout=5000)
    qtbot.waitUntil(lambda: next(r for r in q.rows if r.key == "mangohud").value == "On", timeout=5000)
    assert next(r for r in q.rows if r.key == "mangohud").extra == {"toggle": True}
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_the_wheel_moves_through_the_quick_menu(ui, qtbot, tmp_path):
    """A Steam controller's left trackpad is a scroll wheel in Steam's desktop layout: it moves the selection."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Wheel Menu")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick

    def wheel(dy):
        q.wheelEvent(QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, dy), Qt.NoButton,
                                 Qt.NoModifier, Qt.NoScrollPhase, False))
    start = q.focus
    wheel(-120)
    assert q.focus > start
    wheel(120)
    assert q.focus == start
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_a_steam_controller_tap_over_a_game_with_steams_overlay_is_steams(ui, qtbot, tmp_path, monkeypatch):
    """A Steam Controller (Steam has it; its Steam button comes from Steam's log): over a game with Steam's overlay a
    tap is Steam's (overlay, friends, invites) and only a hold opens the quick menu; without the overlay a tap opens
    the menu as before. The menu says so once."""
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Steam Pad Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)      # the game has the screen
    monkeypatch.setattr(couch.pads, "steam_as_keys", lambda: True)
    monkeypatch.setattr(couch.pads, "_steam_direct", lambda: False)
    overlay = {"on": True}
    monkeypatch.setattr(core.library, "has_steam_overlay", lambda gid: overlay["on"])
    seen = []
    couch.pads.action.connect(lambda n: seen.append((n, couch.pads.home_from_steam)))
    couch.pads._steam_guide(False)                                   # a tap Steam logged
    assert seen == [("home", True)] and couch.pads.home_from_steam is False
    qtbot.wait(300)
    assert couch.quick is None or not couch.quick.isVisible()         # Steam's overlay has it
    couch.actions.settings["steam_button_tip_seen"] = False
    couch.pads._steam_guide(True)                                    # held: the quick menu
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    assert q.rows[0].key == "h-tip" and "Steam's overlay" in q.rows[0].label and q.rows[q.focus].key == "resume"
    q.grab()
    assert couch.actions.settings["steam_button_tip_seen"] is True
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    couch._home_held()                                               # the tip only once
    qtbot.waitUntil(lambda: q.isVisible() and q._shown >= 1.0, timeout=5000)
    assert q.rows[0].key == "resume"
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    overlay["on"] = False                                            # no overlay in the game: a tap is the menu
    couch.pads._steam_guide(False)
    qtbot.waitUntil(lambda: q.isVisible(), timeout=5000)
    assert "tap opens Steam's overlay" in couch._starting_text()
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_with_our_overlay_a_steam_tap_is_the_quick_menu(ui, qtbot, tmp_path, monkeypatch):
    """Settings → "Our overlay instead of Steam's": a tap of the Steam button opens the quick menu even over a game
    that has Steam's overlay, and no tip about Steam's overlay is shown; the desktop's switch greys out Steam's."""
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Own Overlay Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)      # the game has the screen
    monkeypatch.setattr(couch.pads, "steam_as_keys", lambda: True)
    monkeypatch.setattr(couch.pads, "_steam_direct", lambda: False)
    monkeypatch.setattr(core.library, "has_steam_overlay", lambda gid: True)
    monkeypatch.setitem(couch.actions.settings, "own_overlay", True)
    monkeypatch.setitem(couch.actions.settings, "steam_button_tip_seen", False)
    from PySide6.QtGui import QColor, QImage
    from ezgd.platform import hidden
    shot = QImage(1600, 1600, QImage.Format_RGB32)
    shot.fill(QColor("red"))
    monkeypatch.setattr(hidden, "capture_screen", lambda name=None, timeout=1.0: shot)   # the game, just before
    asked = []                                                       # Steam's controller layout: left alone
    monkeypatch.setattr(core.library.input_layout, "_open", asked.append)
    couch.pads._steam_guide(False)                                   # a tap Steam logged
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    assert q.rows[0].key == "resume" and couch.actions.settings["steam_button_tip_seen"] is False
    assert "friends, chat and invites" in couch._starting_text()
    assert q._still is not None                    # a game that minimizes itself: its picture stays behind the menu
    img = q.grab().toImage()
    assert img.pixelColor(5, img.height() // 2).red() > 60          # (dimmed red at the far left, not the desktop)
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    assert q._still is None and asked == []          # (the game keeps its controller: see steaminput)
    page = win.pages["settings"]
    own, steam_ov = page.widgets["own_overlay"], page.widgets["steam_overlay"]
    page._loading = True                                             # (shown, not saved)
    own.setChecked(True)
    assert not steam_ov.isEnabled()
    assert page.widgets["launch_through_steam"].isEnabled()          # (the games are still added to Steam)
    own.setChecked(False)
    assert steam_ov.isEnabled()
    page._loading = False
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()
