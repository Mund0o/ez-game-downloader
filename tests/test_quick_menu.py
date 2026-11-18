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
    """A game of the app running — with our overlay on: the quick menu shows only then (the user's rule,
    2026-10-28; with it off, Steam's overlay is the one over the game)."""
    win.settings["own_overlay"] = True
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


def test_with_our_overlay_off_the_quick_menu_never_shows(ui, qtbot, tmp_path, monkeypatch):
    """The user's rule (2026-10-28): with "Our overlay instead of Steam's" off, Steam's overlay is the one over the
    game and the quick menu does not show at all — not for a tap or a hold of the Steam / PS button (read from the
    controller or from Steam's log), not for suspended games, and Steam's own windows are left to Steam over a game.
    (It used to: a hold, or any press of a pad the app reads itself.)"""
    from ezgd.ui import window_control
    win, core, _ = ui
    assert not win.settings.get("own_overlay", False)                # (the default)
    a = _running(win, core, tmp_path, qtbot, "Steam Overlay Game")
    win.settings["own_overlay"] = False
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)      # the game has the screen
    guarded = []
    monkeypatch.setattr(window_control, "guard_steam", lambda ms=3000: guarded.append(ms) or True)

    def no_menu():
        qtbot.wait(300)
        return couch.quick is None or not couch.quick.isVisible()
    monkeypatch.setattr(couch.pads, "steam_as_keys", lambda: True)  # a Steam Controller: its button from Steam's log
    monkeypatch.setattr(couch.pads, "_steam_direct", lambda: False)
    couch.pads._steam_guide(False)                                   # a tap
    assert no_menu()
    couch.pads._steam_guide(True)                                    # a hold
    assert no_menu()
    couch._pad("home")                                               # a DualSense's PS button, read directly
    couch.pads.home_held.emit()                                      # … and held
    assert no_menu() and guarded == []                               # (Steam's windows: Steam's)
    couch.suspended.add(a.id)                                        # only a suspended game
    couch._home_held()
    assert no_menu()
    couch.suspended.discard(a.id)
    assert "quick menu" not in couch._starting_text()
    couch._bpm_tick()                                                # Steam's Big Picture over the game: Steam's
    assert not couch._bpm_timer.isActive()
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_over_a_desktop_start_the_guide_button_listens_only_with_our_overlay(ui, qtbot, tmp_path, monkeypatch):
    """From the desktop window (couch mode closed) the guide button, Shift+Tab and Steam's Big Picture are only
    watched with our overlay on; switching it on or off while a game runs takes effect at once."""
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Desktop Steam Overlay Game")
    keys = []
    monkeypatch.setattr(win, "_watch_quick_key", lambda playing: keys.append(playing))
    win.settings["own_overlay"] = False
    win._watch_quick_button()
    assert win._quick_pads is None and keys[-1] is False and not win._bpm_timer.isActive()
    win._quick_menu_from_desktop()
    win._quick_menu_by_key()
    qtbot.wait(300)
    assert win.couch is None                                         # no quick menu (nor its hidden couch mode)
    win._settings_loaded({**win.settings, "own_overlay": True}, "own_overlay")     # switched on
    assert win._quick_pads is not None and keys[-1] is True
    win._quick_menu_from_desktop()
    qtbot.waitUntil(lambda: win.couch is not None and win.couch.quick is not None and win.couch.quick.isVisible(),
                    timeout=5000)
    win.couch.quick.command("b")
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)
    win._settings_loaded({**win.settings, "own_overlay": False}, "own_overlay")    # and off again
    assert win._quick_pads is None and keys[-1] is False
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


def test_with_our_overlay_a_steam_tap_is_the_quick_menu(ui, qtbot, tmp_path, monkeypatch):
    """Settings → "Our overlay instead of Steam's": a tap of the Steam button opens the quick menu even over a game
    that has Steam's overlay (kept in the game for its FPS counter, so Steam's own switch stays usable)."""
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Own Overlay Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)      # the game has the screen
    monkeypatch.setattr(couch.pads, "steam_as_keys", lambda: True)
    monkeypatch.setattr(couch.pads, "_steam_direct", lambda: False)
    monkeypatch.setattr(core.library, "has_steam_overlay", lambda gid: True)
    monkeypatch.setitem(couch.actions.settings, "own_overlay", True)
    from PySide6.QtGui import QColor, QImage
    from ezgd.platform import hidden
    from ezgd.ui import couch_quick
    monkeypatch.setattr(couch_quick, "_MINIMIZERS", set())          # (games seen minimizing: this test's own)
    shot = QImage(1600, 1600, QImage.Format_RGB32)
    shot.fill(QColor("red"))
    monkeypatch.setattr(hidden, "capture_screen", lambda name=None, timeout=1.0: shot)   # the game, just before
    asked = []                                                       # Steam's controller layout: left alone
    monkeypatch.setattr(core.library.input_layout, "_open", asked.append)
    couch.pads._steam_guide(False)                                   # a tap Steam logged
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    assert q.rows[0].key == "resume"
    assert "friends, chat and invites" in couch._starting_text()
    # the game stays live behind the menu — no picture of it laid over the screen (it looked frozen, 2026-11-05)
    assert q._still is None and not q._game_hidden()
    left = q.grab().toImage()
    assert left.pixelColor(5, left.height() // 2).alpha() < 200     # (the shade only: the game shows through)
    q._watch_token = 99
    q._game_window(99, 0)                         # KWin: the game minimized itself as the menu took the focus
    assert q._game_hidden() and a.id in couch_quick._MINIMIZERS
    left = q.grab().toImage()
    assert left.pixelColor(5, left.height() // 2).alpha() == 255    # its stand-in, never the desktop
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    assert q._still is None and asked == []          # (the game keeps its controller: see steaminput)
    assert q._watch_token == 0
    q.opened_at -= 5
    couch.pads._steam_guide(False)                   # again: a game seen minimizing gets its picture from frame one
    qtbot.waitUntil(lambda: q.isVisible() and q._shown >= 1.0, timeout=5000)
    assert q._still is not None and q._game_hidden()
    img = q.grab().toImage()
    assert img.pixelColor(5, img.height() // 2).red() > 60          # (dimmed red at the far left, not the desktop)
    q._watch_token = 100
    q._game_window(100, 1)                           # KWin: this time it stayed — the live game once the grace is over
    q.opened_at -= 5
    assert not q._game_hidden()
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    page = win.pages["settings"]
    own, steam_ov = page.widgets["own_overlay"], page.widgets["steam_overlay"]
    page._loading = True                                             # (shown, not saved)
    own.setChecked(True)
    assert steam_ov.isEnabled()                                      # (Steam's overlay still loads with ours)
    assert page.widgets["launch_through_steam"].isEnabled()          # (the games are still added to Steam)
    own.setChecked(False)
    assert steam_ov.isEnabled()
    page._loading = False
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_steams_overlay_from_the_quick_menu_and_its_steam_button(ui, qtbot, tmp_path, monkeypatch):
    """A friend lost Steam's overlay with our overlay on (2026-10-29): the quick menu opens it over the game ("Steam
    overlay"), and while it is open the Steam button is Steam's — it closes it — so the menu does not open over it."""
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Overlay Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    busy, shown = [True], []

    async def steam_overlay_busy(entry_id):
        return busy[0]

    async def steam_overlay_show(entry_id):
        shown.append(entry_id)
        return "opened"
    monkeypatch.setattr(core, "steam_overlay_busy", steam_overlay_busy)
    monkeypatch.setattr(core, "steam_overlay_show", steam_overlay_show)
    couch._home_held()                                               # Steam's overlay is up: the press is Steam's
    qtbot.wait(400)
    assert couch.quick is None or not couch.quick.isVisible()
    busy[0] = False
    couch._home_held()                                               # it is gone: the quick menu
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.isVisible(), timeout=5000)
    q = couch.quick
    row = next(r for r in q.rows if r.key == "steamoverlay")
    row.action()                                                     # "Steam overlay": back to the game, then Steam's
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    qtbot.waitUntil(lambda: shown == [a.id], timeout=3000)
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_the_core_says_when_the_steam_button_is_steams(monkeypatch):
    """steam_overlay_busy: Steam's overlay open in that game, or the Steam button handed to it to close it just now —
    asked of Steam only for a game with Steam's overlay, with our overlay on."""
    import asyncio
    import types
    from ezgd.core.app_core import AppCore
    from ezgd.platform.steambridge import BridgeError
    answers, asked = [], []

    class FakeBridge:
        async def overlay_state(self, ids):
            asked.append(list(ids))
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
    core = types.SimpleNamespace(settings=types.SimpleNamespace(settings=types.SimpleNamespace(own_overlay=True)),
                                 _overlay_ids={3: {"480"}}, _bridge=lambda: FakeBridge())
    busy = lambda eid: asyncio.run(AppCore.steam_overlay_busy.__get__(core)(eid))   # noqa: E731
    answers[:] = [{"open": True, "passed": -1}, {"open": False, "passed": 300}, {"open": False, "passed": 9000},
                  {"open": False, "passed": -1}, BridgeError("no Steam")]
    assert [busy(3) for _ in range(5)] == [True, True, False, False, False]
    assert busy(4) is False and len(asked) == 5                      # no overlay known in that game: not asked
    core.settings.settings.own_overlay = False
    assert busy(3) is False and len(asked) == 5


# ------------------------------------------------------------------- keyboard, mouse and wheel (2026-11-05)
def _wheel_event(dy=0, pixels=0, phase=None):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    return QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, pixels), QPoint(0, dy), Qt.NoButton,
                       Qt.NoModifier, Qt.NoScrollPhase if phase is None else phase, False)


def test_one_notch_of_the_wheel_is_one_step():
    """A high-resolution wheel sends one notch in parts (8 × 15): it moved the quick menu 3-5 rows per notch (the
    user, 2026-11-05). A whole notch is one step — and a desktop that scrolls faster (one bigger event) still one."""
    from PySide6.QtCore import Qt
    from ezgd.ui.wheel_steps import WheelSteps
    now = [100.0]
    w = WheelSteps(clock=lambda: now[0])
    parts = [w.steps(_wheel_event(-15)) for _ in range(8)]              # one notch towards the player, in parts
    assert parts == [0] * 7 + [-1]
    assert [w.steps(_wheel_event(-15)) for _ in range(16)].count(-1) == 2   # two more notches: two steps
    assert w.steps(_wheel_event(120)) == 1                              # an ordinary wheel: one per notch
    assert w.steps(_wheel_event(360)) == 1                              # KDE's scroll speed 3: still one
    assert w.steps(_wheel_event(-60)) == 0                              # half a notch back …
    now[0] += 1.0
    assert w.steps(_wheel_event(-60)) == 0                              # … and the other half much later: a new turn
    assert w.steps(_wheel_event(-60)) == -1
    assert w.steps(_wheel_event(-90)) == 0 and w.steps(_wheel_event(60)) == 0   # turned back: nothing left over
    assert w.steps(_wheel_event(60)) == 1
    # a touchpad: its fingers' travel in pixels; its coasting after they left is not the player's
    assert [w.steps(_wheel_event(-10, -10, Qt.ScrollUpdate)) for _ in range(5)] == [0, 0, 0, 0, -1]
    assert w.steps(_wheel_event(-400, -400, Qt.ScrollMomentum)) == 0


def test_the_wheel_moves_the_quick_menu_and_couch_mode_once_per_notch(ui, qtbot, tmp_path):
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Notch Menu")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    start = q.focus
    for _ in range(8):
        q.wheelEvent(_wheel_event(-15))                                 # one notch of a high-resolution wheel
    assert q.focus == start + 1
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    moves = []
    real = couch.command
    couch.command = lambda name: (moves.append(name), real(name))
    couch.showNormal()
    for _ in range(8):
        couch.wheelEvent(_wheel_event(-15))                             # couch mode itself: once as well
    assert len(moves) == 1
    couch.command = real
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_a_key_still_held_as_the_menu_closes_is_let_go_before_the_game_gets_it(ui, qtbot, tmp_path, monkeypatch):
    """Esc closes the menu; KWin hands a newly focused window the keys being held, so the game saw a fresh Esc (its
    own pause menu) when the menu hid before the key was let go. The menu waits for it (not forever)."""
    from PySide6.QtCore import Qt
    from ezgd.ui import couch_quick
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Held Key")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    monkeypatch.setattr(q, "isActiveWindow", lambda: True)
    went = []
    monkeypatch.setattr(couch, "go_to_game", lambda e: went.append(e.id))
    qtbot.keyPress(q, Qt.Key_Escape)                                    # pressed … and held
    qtbot.waitUntil(lambda: q._release_wait, timeout=3000)              # slid out: waiting for the key
    qtbot.wait(300)
    assert q.isVisible() and went == []                                 # the game does not have the focus yet
    qtbot.keyRelease(q, Qt.Key_Escape)
    qtbot.waitUntil(lambda: not q.isVisible() and went == [a.id], timeout=2000)
    couch._home_held()                                                  # held and never let go: not forever
    qtbot.waitUntil(lambda: q.isVisible() and q._shown >= 1.0, timeout=5000)
    monkeypatch.setattr(couch_quick, "RELEASE_WAIT", 300)
    qtbot.keyPress(q, Qt.Key_Escape)
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    q._keys_down.clear()
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_keyboard_and_mouse_in_the_quick_menu(ui, qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Keys Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch.pad_family = "xbox"                                           # (a controller was used last)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    keys = [r.key for r in q.rows]
    qtbot.keyClick(q, Qt.Key_End)
    assert q.rows[q.focus].key == "quit" and couch.pad_family == ""     # the keyboard's keys as the prompts now
    assert couch._pad_labels()["a"] == "Enter"
    qtbot.keyClick(q, Qt.Key_Home)
    assert q.rows[q.focus].key == "resume"
    qtbot.keyClick(q, Qt.Key_PageDown)
    assert q.focus == min(len(keys) - 1, 5)
    qtbot.keyClick(q, Qt.Key_Home)
    qtbot.keyClick(q, Qt.Key_Tab)
    assert q.rows[q.focus].key == "switch"

    def click(pos, double=False, button=Qt.LeftButton):
        kind = QEvent.MouseButtonDblClick if double else QEvent.MouseButtonPress
        ev = QMouseEvent(kind, QPointF(pos), QPointF(pos), button, button, Qt.NoModifier)
        (q.mouseDoubleClickEvent if double else q.mousePressEvent)(ev)
        q.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(pos), QPointF(pos), button,
                                        Qt.NoButton, Qt.NoModifier))
    # the game's volume: its ◂ ▸ are buttons for the mouse
    q._got("volume", 70)
    row = next(k for k, r in enumerate(q.rows) if r.key == "volume")
    q.focus = row                                                       # (in view on a small screen)
    q.grab()
    dec = next(r for r, kind, k in q._hits if kind == "dec" and k == row)
    inc = next(r for r, kind, k in q._hits if kind == "inc" and k == row)
    click(dec.center())
    assert q.volume == 60 and q.focus == row
    click(inc.center())
    click(inc.center())
    assert q.volume == 80
    # a double click is one click: Quit only arms (it asks again)
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "quit")
    q.grab()
    quit_row = next(r for r, kind, k in q._hits if kind == "row" and q.rows[k].key == "quit")
    click(quit_row.center())
    click(quit_row.center(), double=True)
    assert q.isVisible() and q.rows[q.focus].label == "Press again to quit"
    assert core.library.is_running(a.id)
    # a click on the game beside the panel (not only on its left half): back to the game
    x = q._panel_rect(1.0).left() - 20
    assert x > q.width() * 0.5
    click(QPointF(x, q.height() / 2))
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_shift_tab_shows_the_keyboards_keys_and_steam_is_not_guarded(ui, qtbot, tmp_path, monkeypatch):
    """Opened with Shift+Tab from the desktop: the prompts are the keyboard's (Enter / Esc), even with a controller
    connected; and Steam never sees that key, so its windows are not watched for an answer to it."""
    win, core, _ = ui
    win.show()
    a = _running(win, core, tmp_path, qtbot, "Shift Tab Game")
    guarded = []
    from ezgd.ui import couch as couch_mod
    monkeypatch.setattr(couch_mod._Couch, "guide_pressed", lambda self: guarded.append(1))
    win._quick_menu_by_key()
    couch = win.couch
    qtbot.waitUntil(lambda: couch is not None and couch.quick is not None and couch.quick._shown >= 1.0,
                    timeout=5000)
    from PySide6.QtCore import Qt
    assert couch.pad_family == "" and guarded == []
    assert couch.quick.cursor().shape() == Qt.ArrowCursor                # Shift+Tab: mouse pointer shown right away
    couch.quick.command("b")
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)
    win._quick_menu_from_desktop()                                       # the guide button: Steam's windows watched
    qtbot.waitUntil(lambda: win.couch is not None and win.couch.quick is not None and win.couch.quick.isVisible(),
                    timeout=5000)
    assert guarded == [1] and win.couch.quick.cursor().shape() == Qt.BlankCursor
    win.couch.quick.command("b")
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


@linux_only
def test_the_game_grid_is_laid_out_once_as_the_panel_widens(ui, qtbot, tmp_path):
    """Switch game widens the panel: its tiles were laid out (and their pictures scaled) for every width the panel
    went through. Now they are where they end up from the first frame, uncovered as the panel widens."""
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Wide One")
    for n in range(3):
        _install(core.library, tmp_path, f"Other {n}")
    win._reload_library()
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running and len(couch.entries) == 4, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.open_switch()
    q._wide_anim.stop()

    def tiles(wide):
        q._wide = wide
        q.grab()
        return [(r.x(), r.y(), r.width(), r.height()) for r, kind, _k in q._hits if kind == "tile"]
    final = tiles(1.0)
    assert len(final) == 3
    assert tiles(0.3) == final
    q.close_switch()
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_kwin_reports_when_the_game_window_goes():
    """The script that watches the game's window while the quick menu is over it, run against a stand-in for KWin:
    the game's biggest (full-screen) window among its processes, reported at once and on every change."""
    import json
    from PySide6.QtQml import QJSEngine
    from ezgd.ui import window_control as wc
    eng = QJSEngine()
    eng.evaluate("""
        var sent = [];
        function callDBus() { sent.push(Array.prototype.slice.call(arguments)); }
        function signal() { return {fns: [], connect: function (f) { this.fns.push(f); },
                                    emit: function (a) { for (var i = 0; i < this.fns.length; i++) this.fns[i](a); }}; }
        function win(pid, normal, full, w, h) { return {pid: pid, normalWindow: normal, fullScreen: full, width: w,
                                                        height: h, minimized: false, minimizedChanged: signal()}; }
        var other = win(7, true, false, 800, 600), helper = win(43, false, false, 10, 10);
        var launcher = win(41, true, false, 640, 480), game = win(42, true, true, 3840, 2160);
        var workspace = {windowList: function () { return [other, launcher, helper, game]; }, windowRemoved: signal()};
    """)

    def run(pids):
        r = eng.evaluate(wc.GAME_WATCH_SCRIPT % {"pids": json.dumps(pids), "token": 5, "svc": '":1.9"',
                                                 "path": json.dumps(wc.WATCH_PATH), "iface": json.dumps(wc.WATCH_IFACE)})
        assert not r.isError(), r.toString()

    def sent():
        return json.loads(eng.evaluate("JSON.stringify(sent)").toString())
    run([41, 42, 43])
    assert sent() == [[":1.9", wc.WATCH_PATH, wc.WATCH_IFACE, "GameWindow", 5, 1]]
    eng.evaluate("launcher.minimized = true; launcher.minimizedChanged.emit();")      # not the game's window
    eng.evaluate("game.minimized = true; game.minimizedChanged.emit();")
    eng.evaluate("game.minimized = false; game.minimizedChanged.emit();")
    eng.evaluate("workspace.windowRemoved.emit(other); workspace.windowRemoved.emit(game);")
    assert [m[-1] for m in sent()] == [1, 0, 1, 0]
    eng.evaluate("sent = []")
    run([99])                                                            # none of its windows
    assert [m[-1] for m in sent()] == [-1]
    assert wc.watch_game_window([42], 1, lambda *_a: None) is None       # (never from the tests: no KWin here)


def test_couch_sounds_can_open_their_stream_later(qtbot, monkeypatch):
    """The hidden couch mode of a desktop start opens its sound stream after the quick menu slid in (opening it
    held up the slide's frames)."""
    import time
    from ezgd.ui import couch_sounds
    opened = []
    monkeypatch.setattr(couch_sounds, "_multimedia", lambda: object())
    monkeypatch.setattr(couch_sounds.CouchSounds, "_open_stream", lambda self, mm: opened.append(time.monotonic())
                        or True)
    t = time.monotonic()
    s = couch_sounds.CouchSounds(None, lambda: True)
    s.defer(500)
    qtbot.wait(300)
    assert opened == []
    qtbot.waitUntil(lambda: bool(opened), timeout=2000)
    assert opened[0] - t >= 0.45
    s.stop()


def test_waiting_for_pictures_redraws_only_the_panel_and_not_forever(ui, qtbot, tmp_path):
    """A game with no picture at all kept the menu "waiting for its art": the whole 4K screen redrawn 8 times a second
    for as long as the menu was open. Now only the panel is looked at again, and for the first 15 s."""
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "No Art Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.grab()
    assert q._pending_art                                               # (no picture for this game)
    asked = []
    real = q.update
    q.update = lambda *args: (asked.append(args), real(*args))
    q._art_tick()
    assert asked and asked[-1] and asked[-1][0].left() > 0              # the panel's area, not the whole screen
    asked.clear()
    q.opened_at -= 16
    q._art_tick()
    assert asked == []                                                  # given up after 15 s
    q.update = real
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_volume_slider_drag_wheel_and_mute_in_the_quick_menu(ui, qtbot, tmp_path):
    """Mouse & keyboard volume controls: draggable slider (snapping near 100 %), wheel ±5 % over the slider, and
    mute / unmute on the speaker icon or M."""
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QWheelEvent
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Slider Vol Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held(by_key=True)
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.resize(1920, 1080)
    q._got("volume", 75)
    row = next(k for k, r in enumerate(q.rows) if r.key == "volume")
    q.focus = row
    q.grab()
    assert q._vol_track is not None
    tr = q._vol_track

    def mouse(kind, pt, btn=Qt.LeftButton, buttons=Qt.NoButton):
        ev = QMouseEvent(kind, QPointF(pt), QPointF(pt), btn, buttons, Qt.NoModifier)
        if kind == QEvent.MouseButtonPress:
            q.mousePressEvent(ev)
        elif kind == QEvent.MouseMove:
            q.mouseMoveEvent(ev)
        else:
            q.mouseReleaseEvent(ev)

    # click & drag along the slider; near 100 % snaps cleanly to 100 %
    p30 = QPointF(tr.left() + tr.width() * (30 / 150), tr.center().y())
    mouse(QEvent.MouseButtonPress, p30, Qt.LeftButton, Qt.LeftButton)
    assert q.volume == 30
    p99 = QPointF(tr.left() + tr.width() * (99 / 150), tr.center().y() + 25)    # (drifting vertically keeps dragging)
    mouse(QEvent.MouseMove, p99, Qt.NoButton, Qt.LeftButton)
    assert q.volume == 100
    mouse(QEvent.MouseButtonRelease, p99, Qt.LeftButton, Qt.NoButton)

    # mouse wheel directly over the slider steps volume by 5 % without moving the row focus
    q.grab()
    bar_hit = next(r for r, kind, k in q._hits if kind == "vol_bar" and k == row)
    mouse(QEvent.MouseMove, bar_hit.center())
    q.wheelEvent(QWheelEvent(bar_hit.center(), bar_hit.center(), QPoint(), QPoint(0, 120), Qt.NoButton,
                             Qt.NoModifier, Qt.NoScrollPhase, False))
    assert q.volume == 105 and q.focus == row

    # M key and speaker icon mute & restore the previous level
    qtbot.keyClick(q, Qt.Key_M)
    assert q.volume == 0 and next(r for r in q.rows if r.key == "volume").value == "Muted"
    q.grab()
    mute_hit = next(r for r, kind, k in q._hits if kind == "mute" and k == row)
    mouse(QEvent.MouseButtonPress, mute_hit.center(), Qt.LeftButton, Qt.LeftButton)
    mouse(QEvent.MouseButtonRelease, mute_hit.center(), Qt.LeftButton, Qt.NoButton)
    assert q.volume == 105

    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_header_buttons_grid_navigation_chat_scroll_and_direct_typing(ui, qtbot, tmp_path, monkeypatch):
    """Mouse & keyboard across subviews: ← Back and ✕ Close in the header, clickable footer prompts, Tab wrapping &
    row-wise wheel in a multi-row game grid (without misclick-closing on tile gaps), scrollable long chats, direct
    typing / Ctrl+V paste in chat, '/' search & one-click ✕ clear in Friends, and clicking outside Keyboard."""
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication
    from ezgd.platform import steamfriends as sf
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Mouse Hub")
    for n in range(5):                                                   # 5 other games → 2 rows in Switch game (4 + 1)
        _install(core.library, tmp_path, f"Grid {n}")
    win._reload_library()

    bob_sid = sf.to_steamid64(202)
    bob_acct = 202
    many_friends = [sf.Friend(sf.to_steamid64(200 + i), f"Friend {i:02d}", "online").as_dict() for i in range(18)]
    many_friends[2]["name"] = "Bob"
    many_friends[2]["steamid"] = bob_sid

    async def friends(force=False):
        return {"me": "1", "live": True, "private": False, "error": "", "at": 0, "friends": many_friends}

    async def chats():
        return {"self": {"accountid": 1, "appid": 480, "lobby": "77", "connect": "", "game": ""},
                "chats": [{"accountid": bob_acct, "name": "Bob", "unread": 0, "time": 1, "last": "hi",
                           "last_from": bob_acct, "invite": None}]}

    history_msgs = [{"mine": i % 2 == 0, "time": i + 1, "text": f"line {i}", "invite": None} for i in range(25)]
    sent = []

    async def history(acct, limit=40):
        return list(history_msgs)

    async def send(acct, text):
        sent.append((acct, text))
    monkeypatch.setattr(core, "steam_friends", friends)
    monkeypatch.setattr(core, "steam_chats", chats)
    monkeypatch.setattr(core, "steam_chat_history", history)
    monkeypatch.setattr(core, "steam_chat_send", send)

    couch = _open_couch(win, qtbot)
    monkeypatch.setitem(couch.actions.settings, "own_overlay", True)
    qtbot.waitUntil(lambda: a.id in couch.running and len(couch.entries) == 6, timeout=5000)
    couch.pad_family = ""
    couch._home_held(by_key=True)
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.resize(1600, 900)
    qtbot.waitUntil(lambda: q.friends is not None and q.chats is not None, timeout=3000)

    def click(pt, button=Qt.LeftButton):
        q.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(pt), QPointF(pt), Qt.NoButton, Qt.NoButton,
                                     Qt.NoModifier))
        q.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(pt), QPointF(pt), button, button,
                                      Qt.NoModifier))
        q.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(pt), QPointF(pt), button, Qt.NoButton,
                                        Qt.NoModifier))

    # stray middle / forward mouse buttons never trigger actions
    q.grab()
    r0 = next(r for r, kind, k in q._hits if kind == "row" and k == 0)
    click(r0.center(), button=Qt.MiddleButton)
    assert q.isVisible()

    # Switch game: Tab wraps across rows, mouse wheel steps a full row, gap click stays in Switch, header ← goes back
    q.open_switch()
    q._wide = 1.0
    q._wide_anim.stop()
    q.grab()
    for _ in range(4):
        qtbot.keyClick(q, Qt.Key_Tab)
    assert q.grid_focus == 4                                             # reached row 1 instead of stopping at col 3
    qtbot.keyClick(q, Qt.Key_Tab)
    assert q.grid_focus == 0                                             # wrapped back to tile 0
    q.pointer(True)
    q.wheelEvent(QWheelEvent(QPointF(1200, 500), QPointF(1200, 500), QPoint(), QPoint(0, -120), Qt.NoButton,
                             Qt.NoModifier, Qt.NoScrollPhase, False))
    assert q.grid_focus == 4                                             # one wheel notch moved down a full row
    t0 = next(r for r, kind, k in q._hits if kind == "tile" and k == 0)
    t1 = next(r for r, kind, k in q._hits if kind == "tile" and k == 1)
    click(QPointF((t0.right() + t1.left()) / 2, t0.center().y()))        # gap between tiles: stays in Switch game
    assert q.view == "switch"
    back_btn = next(r for r, kind, _k in q._hits if kind == "back")
    click(back_btn.center())
    assert q.view == "main"

    # Friends: scrollbar on long list, '/' opens empty search (not '/'), outside click closes Keyboard, ✕ clears search
    q.open_friends()
    q.grab()
    assert any(kind == "scrollbar" for _r, kind, _k in q._hits)
    assert "/" in q._hint_actions                                        # '/' prompt instead of 'I'
    q.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Slash, Qt.NoModifier, "/"))
    assert q.keyboard is not None and q.keyboard.text == ""
    q.grab()
    click(QPointF(20, 20))                                               # outside Keyboard: cancels it, menu stays
    assert q.keyboard is None and q.view == "friends"
    q._found("Bob")
    q.focus = 0
    q.grab()
    clr = next(r for r, kind, _k in q._hits if kind == "clear")
    click(clr.center())
    assert q.friend_query == "" and len([r for r in q.rows if r.kind == "friend"]) == 18

    # Chat: scrollable 25-message history with Write pinned, direct typing + Enter sends, Ctrl+V pastes, header ✕ closes
    q.open_chat(many_friends[2])
    qtbot.waitUntil(lambda: len(q.chat_msgs) == 25, timeout=3000)
    q.grab()
    assert any(kind == "scrollbar" for _r, kind, _k in q._hits)
    assert any(kind == "row" and q.rows[k].key == "write" for _r, kind, k in q._hits)
    q.wheelEvent(QWheelEvent(QPointF(1300, 400), QPointF(1300, 400), QPoint(), QPoint(0, 120), Qt.NoButton,
                             Qt.NoModifier, Qt.NoScrollPhase, False))
    assert q._chat_top is not None                                       # scrolled up into earlier messages
    qtbot.keyClick(q, Qt.Key_Home)
    assert q._chat_top == 0
    qtbot.keyClick(q, Qt.Key_End)
    assert q._chat_top is None
    q.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_H, Qt.NoModifier, "h"))
    q.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_I, Qt.NoModifier, "i"))
    assert q.keyboard is not None and q.keyboard.text == "hi"
    qtbot.keyClick(q, Qt.Key_Return)                                     # real keyboard Enter sends immediately
    qtbot.waitUntil(lambda: (bob_acct, "hi") in sent, timeout=3000)
    assert q.keyboard is None
    QApplication.clipboard().setText("lobby 99")
    q.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier, ""))
    assert q.keyboard is not None and q.keyboard.text == "lobby 99"
    q.grab()
    kb_x = next(r for r, kind, _k in q._hits if kind == "kb_close")
    click(kb_x.center())
    assert q.keyboard is None and q.view == "chat"

    # Top-right ✕ closes the quick menu straight back to the game from any subview
    q.grab()
    close_x = next(r for r, kind, _k in q._hits if kind == "close")
    click(close_x.center())
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)

    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()
