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
    qtbot.waitUntil(lambda: e.id in win.pages["library"].running, timeout=5000)
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
    q.command("a")                                        # play the other one
    qtbot.waitUntil(lambda: core.library.is_suspended(a.id), timeout=5000)
    qtbot.waitUntil(lambda: couch.overlay is not None and couch.overlay.title.startswith(("Play", "Starting")),
                    timeout=5000)                         # couch mode starts it ("Play …?" first, as always)
    assert a.id in couch.suspended and not q.isVisible()
    couch._close_overlay()
    couch._launching = False
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
    couch._home_held()                                    # only suspended games and couch mode in front: nothing
    assert not q.isVisible()
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
