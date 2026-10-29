"""Steam Controller bugs found by going through the code (2026-10-28, after a friend's "the controller falls off a
cliff after the quick menu"). Each test failed before its fix."""

import sys
import time

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from test_quick_menu import _running
from test_ui import ui  # noqa: F401 — the shared main-window fixture

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process states from /proc")


def test_a_controller_that_goes_away_lets_go_of_what_it_held(qtbot):
    """Steam takes its virtual pad away whenever it switches a Steam controller's layout (on a friend's PC it came
    and went every few minutes). Held at that moment: the stick kept scrolling forever, the guide button opened the
    quick menu 0.8 s later by itself, and A stayed "down" for good — A's down/up from the pad Steam made next were
    dropped as copies (the couch browser's click)."""
    from ezgd.ui import gamepad as gp
    g = gp.Gamepads()
    g._home_timer.setInterval(150)
    got, held, buttons = [], [], []
    g.action.connect(got.append)
    g.home_held.connect(lambda: held.append(1))
    g.button.connect(lambda n, down: buttons.append((n, down)))
    old = gp._Device("/dev/input/event258", "Microsoft X-Box 360 pad 0", -1, 0x28DE, 0x11FF, 0)
    old.ranges = {gp.ABS_X: (-32768, 32767), gp.ABS_Y: (-32768, 32767)}
    g.devices[old.path] = old
    g._event(old, gp.EV_ABS, gp.ABS_Y, -32000)                    # the stick held up …
    g._event(old, gp.EV_KEY, gp.BTN_SOUTH, 1)                     # … A …
    g._event(old, gp.EV_KEY, gp.BTN_MODE, 1)                      # … and the guide button
    g._close(old.path)                                            # Steam took the pad away
    assert ("a", False) in buttons and ("home", False) in buttons  # (a held click in the browser is let go)
    got.clear()
    qtbot.wait(700)
    assert got == [] and held == []
    new = gp._Device("/dev/input/event259", "Microsoft X-Box 360 pad 0", -1, 0x28DE, 0x11FF, 0)
    g.devices[new.path] = new                                     # the pad Steam makes next
    buttons.clear()
    g._event(new, gp.EV_KEY, gp.BTN_SOUTH, 1)
    g._event(new, gp.EV_KEY, gp.BTN_SOUTH, 0)
    assert buttons == [("a", True), ("a", False)]
    g.stop()
    g.deleteLater()                                               # freed here, on the main thread
    qtbot.wait(50)


@linux_only
def test_a_desktop_game_gets_the_screen_back_however_long_its_processes_take(ui, qtbot, tmp_path, monkeypatch,
                                                                           caplog):
    """Back to a game started from the desktop: the request that gives it the screen belonged to the hidden couch
    mode, which closes 400 ms after the quick menu. When listing the game's processes took longer, the answer was
    dropped and the game never got the focus back — and Steam picks a Steam controller's layout by that focus."""
    from ezgd.library.manager import LibraryManager
    from ezgd.ui import couch as C
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Slow Answer")
    real = LibraryManager.game_processes

    def slow(self, entry_id):
        time.sleep(0.8)
        return real(self, entry_id)
    monkeypatch.setattr(LibraryManager, "game_processes", slow)
    given = []
    monkeypatch.setattr(C, "bring_game_forward", lambda pids: given.append(list(pids)) or True)
    focus = []
    monkeypatch.setattr(C, "_log_x_focus", focus.append)          # (never this computer's real X11 display)
    caplog.set_level("INFO", logger="ezgd.ui.couch")
    win._quick_menu_from_desktop()
    couch = win.couch
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    couch.quick.command("b")                                      # back to the game
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)      # the hidden couch mode went (400 ms)
    qtbot.waitUntil(lambda: bool(given), timeout=5000)
    assert given[0]                                               # the game's processes: its window comes forward
    qtbot.waitUntil(lambda: bool(focus), timeout=3000)
    assert "back to Slow Answer: asked KWin to bring the game's window forward" in caplog.text   # (evidence in the log)
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


@linux_only
def test_the_quick_menu_hides_the_pointer_while_the_controller_drives_it(ui, qtbot, tmp_path, monkeypatch):
    """Couch mode hides the mouse pointer while a controller is used; the quick menu never did — with a Steam
    controller on Steam's desktop layout (its right pad is a mouse) the pointer stayed on screen over the game."""
    from test_couch_ui import _open_couch
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Pointer Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)  # the game has the screen
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    assert q.cursor().shape() == Qt.BlankCursor                   # opened by the guide button
    pos = QPointF(5, 5)
    q.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, pos, q.mapToGlobal(pos), Qt.NoButton, Qt.NoButton,
                                 Qt.NoModifier))
    assert q.cursor().shape() == Qt.ArrowCursor                   # the mouse moved: it shows
    q.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
    assert q.cursor().shape() == Qt.BlankCursor                   # keys (a Steam controller's stick): hidden
    q.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, pos, q.mapToGlobal(pos), Qt.NoButton, Qt.NoButton,
                                 Qt.NoModifier))
    couch._pad("down")                                            # a controller read directly: hidden too
    assert q.cursor().shape() == Qt.BlankCursor
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_the_players_own_layout_is_found_however_far_back_steam_named_it(tmp_path, monkeypatch):
    """Which desktop layout Steam uses comes from the line Steam writes when it loads it. Only the last 256 KB of its
    logs were searched: once a long session had pushed that line further back, a player's own layout (a friend's
    Steam Controller has one) silently became Steam's default — and with it what every button means."""
    from ezgd.ui import steam_input as si
    root = tmp_path / "Steam"
    (root / "logs").mkdir(parents=True)
    (root / "controller_base").mkdir()
    gordon = 'controller_mappings { "version" "2" }'
    (root / "controller_base" / "desktop_steamcontroller_gordon.vdf").write_text(gordon)
    mine = tmp_path / "mine.vdf"
    mine.write_text(gordon)
    log = root / "logs" / "controller_ui.txt"
    log.write_text(f"[2026-10-28 09:00:00] Loaded Config for Local Selection Path for App ID 413080, Controller 0: "
                   f"{mine}\n" + "[2026-10-28 10:00:00] OnFocusWindowChanged to window type: desktop\n" * 20_000)
    assert log.stat().st_size > 1024 * 1024
    monkeypatch.setattr(si, "_log_cache", {})
    assert si.logged_desktop_configs(root) == {"0": mine}
    with open(log, "a") as fh:                                    # Steam goes on writing: only that is read again
        fh.write(f"[2026-10-28 11:00:00] Loaded Config for Local Selection Path for App ID 413080, Controller 0: "
                 f"{root / 'controller_base' / 'desktop_steamcontroller_gordon.vdf'}\n")
    assert si.logged_desktop_configs(root)["0"].name == "desktop_steamcontroller_gordon.vdf"


def test_keys_are_a_steam_controllers_buttons_only_while_steam_runs(qtbot):
    """A Steam Controller's receiver stays plugged in when Steam is closed — then no key comes from the controller,
    but the keyboard's Space, Tab and Page Down were still read as its B, ✕ and Manage."""
    from ezgd.ui import gamepad as gp
    from ezgd.ui import steam_input as si
    g = gp.Gamepads()
    g.steam_hw = True
    g.desktop.keys = dict(si.DEFAULT_KEYS["gordon"])
    g.steam_up = False                                            # Steam is closed
    assert g.steam_key(Qt.Key_Space) is None and not g.steam_as_keys()
    g.steam_up = True
    assert g.steam_key(Qt.Key_Space) == "b" and g.steam_as_keys()
    g.stop()
    g.deleteLater()
    qtbot.wait(50)


def test_a_held_dpad_repeats_once_not_twice(qtbot):
    """Buttons ignore the kernel's own key repeat (value 2); the d-pad took each one as a new press — on top of the
    app's own repeat."""
    from ezgd.ui import gamepad as gp
    g = gp.Gamepads()
    got = []
    g.action.connect(got.append)
    pad = gp._Device("/dev/input/event90", "Pad", -1, 0x045E, 0x028E, 0)
    g._event(pad, gp.EV_KEY, 0x221, 1)                            # BTN_DPAD_DOWN
    for _ in range(5):
        g._event(pad, gp.EV_KEY, 0x221, 2)                        # the kernel repeating it
    g._event(pad, gp.EV_KEY, 0x221, 0)
    assert got == ["down"]
    g.stop()
    g.deleteLater()
    qtbot.wait(50)


def test_flatpak_steam_is_seen_running(tmp_path, monkeypatch):
    """Flatpak's Steam keeps its pid file inside its own home (with a process id of its sandbox): Steam never counted
    as running, so the Steam button and Steam's keys were never read."""
    from ezgd.ui import steam_bpm
    monkeypatch.delenv("EZGD_STEAM_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(steam_bpm, "_seen", (0.0, False))
    alive = {"steam": True}
    monkeypatch.setattr(steam_bpm, "_steam_process", lambda proc="/proc": alive["steam"])
    assert not steam_bpm.steam_running()                          # no Steam installed this way: never looked for
    (tmp_path / ".var" / "app" / "com.valvesoftware.Steam").mkdir(parents=True)
    assert steam_bpm.steam_running()
    alive["steam"] = False
    monkeypatch.setattr(steam_bpm, "_seen", (0.0, True))
    assert not steam_bpm.steam_running()
    fake = tmp_path / "proc"
    (fake / "77").mkdir(parents=True)
    (fake / "77" / "comm").write_text("steam\n")
    monkeypatch.undo()
    assert steam_bpm._steam_process(str(fake))


@linux_only
def test_with_two_games_running_the_menu_is_for_the_one_started_last(ui, qtbot, tmp_path):
    """A quick menu made for a desktop start knows no "last game": it took the first or last of a set — in id order,
    so the game added to the library first lost even when it was the one just started."""
    from test_quick_menu import _install
    win, core, _ = ui
    win.settings["own_overlay"] = True                            # (the quick menu shows only with our overlay)
    older = _install(core.library, tmp_path, "Added First")
    newer = _install(core.library, tmp_path, "Added Second")
    assert older.id < newer.id
    for e in (newer, older):                                      # the older one is started last
        win.bridge.run_sync(core.library_call("launch", e.id, True))
        time.sleep(1.1)                                           # (last played is kept in seconds)
    win._reload_library()
    qtbot.waitUntil(lambda: {older.id, newer.id} <= set(win.pages["library"].running) and
                    {older.id, newer.id} <= set(win.pages["library"].entries), timeout=5000)
    win._quick_menu_from_desktop()
    couch = win.couch
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.isVisible(), timeout=5000)
    assert couch.quick.game.id == older.id
    couch.quick.command("b")
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)
    for e in (older, newer):
        win.bridge.run_sync(core.library_call("stop_completely", e.id, 3))


# --------------------------------------------------------------- the quick menu keeps the game's controller layout
def test_the_game_layout_steam_chose_is_read_from_its_log(tmp_path):
    """Steam's own decision for the focused window: a game it knows (``to game window type: AppID N``). Its own
    windows and keyboard flashing up with the Steam button are skipped; the desktop means no game layout now."""
    from ezgd.platform import steaminput
    root = tmp_path / "Steam"
    (root / "logs").mkdir(parents=True)
    log = root / "logs" / "controller_ui.txt"

    def say(*lines):
        with open(log, "a") as fh:
            fh.writelines(f"[2026-10-28 15:00:00] OnFocusWindowChanged {x}\n" for x in lines)
    assert steaminput.game_layout_now(root) is None                       # (no log yet)
    say("to window type: k_nGameIDControllerConfigs_Desktop, AppID 413080", "to game window type: AppID 480, 480")
    assert steaminput.game_layout_now(root) == 480
    say("to window type: k_nGameIDControllerConfigs_ClientUI, AppID 769")   # Steam's window, put away again
    assert steaminput.game_layout_now(root) == 480
    say("to game window type: AppID 2959482844, valor mortis")              # one of the app's Steam shortcuts
    assert steaminput.game_layout_now(root) == 2959482844
    say("to window type: k_nGameIDControllerConfigs_Desktop, AppID 413080")
    assert steaminput.game_layout_now(root) is None
    say("URL Forcing to window type: k_EWindowTypeGame, 480")             # forced (the menu's hold): still Steam's
    assert steaminput.game_layout_now(root) == 480                        # until the focus moves, also after /0
    say("URL Forcing to window type: k_EWindowTypeGame, 413080")          # a hand-back through the desktop
    assert steaminput.game_layout_now(root) is None
    base = tmp_path / "input"
    for name, ids in (("input7", ("054c", "0ce6")), ("input40", ("28de", "11ff"))):
        (base / name / "id").mkdir(parents=True)
        (base / name / "id" / "vendor").write_text(ids[0] + "\n")
        (base / name / "id" / "product").write_text(ids[1] + "\n")
    assert steaminput.virtual_pads(base) == ["input40"]


def test_the_menu_holds_the_games_layout_and_lets_it_go(tmp_path):
    """While the quick menu has the focus, Steam would give it the desktop layout and take the game's virtual pad
    away. The game's own layout is forced first (once Steam took it the menu shows), released after the menu went —
    ``/0`` while the game has the focus again keeps the game's layout until the focus moves on. Quit from the menu:
    handed back through the desktop layout, as for a game the app forced."""
    from test_steaminput import fake_steam, until
    from ezgd.platform import steaminput
    root, sent, opener = fake_steam(tmp_path)
    f = steaminput.Forced(tmp_path / "marker", opener, root=lambda: root)
    t1 = f.hold(5, 480)
    assert t1 and sent == ["steam://forceinputappid/480"] and f.active and f.held == (5, 480) and f.marker.is_file()
    t2 = f.hold(5, 480)                                   # opened again before the release: no second link
    assert t2 != t1 and sent == ["steam://forceinputappid/480"]
    assert not f.let_go(t1, running=True)                 # the first opening's release: ignored
    assert f.let_go(t2, running=True) and sent[-1] == "steam://forceinputappid/0"
    assert not f.active and not f.marker.is_file()
    sent.clear()
    t3 = f.hold(5, 480)
    assert f.end(5)                                       # quit from the menu: through the desktop layout
    assert until(lambda: sent[-1] == "steam://forceinputappid/0")
    assert sent == ["steam://forceinputappid/480", "steam://forceinputappid/413080", "steam://forceinputappid/0"]
    assert not f.let_go(t3, running=False)                # (handed back already)
    sent.clear()
    f.start(7)                                            # a game the app forces: nothing to add for the menu
    assert f.hold(7, 480) == 0 and sent == ["steam://forceinputappid/480"]
    assert f.end(7, desktop=False)


@linux_only
def test_the_quick_menu_waits_for_steam_and_releases_after(ui, qtbot, tmp_path, monkeypatch):
    """The menu takes the focus only once Steam keeps the game's layout (asked before, not raced against Steam's
    focus switch), and hands it back a moment after it went — also when its hidden couch mode is gone by then."""
    from ezgd.library.manager import LibraryManager
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Held Game")
    asked, released = [], []

    def hold(self, entry_id):
        time.sleep(0.4)                                   # (Steam taking its time)
        asked.append(entry_id)
        return 17
    monkeypatch.setattr(LibraryManager, "hold_input_layout", hold)
    monkeypatch.setattr(LibraryManager, "let_go_input_layout", lambda self, token: released.append(token) or True)
    win._quick_menu_from_desktop()
    couch = win.couch
    qtbot.waitUntil(lambda: couch.quick is not None, timeout=3000)
    q = couch.quick
    assert not q.isVisible()                              # waiting for Steam
    couch._home_held()                                    # the same press reported again meanwhile: once
    qtbot.waitUntil(lambda: q.isVisible(), timeout=3000)
    assert asked == [a.id]
    q.command("b")
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)     # the hidden couch mode went (400 ms)
    qtbot.waitUntil(lambda: released == [17], timeout=4000)
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


@linux_only
def test_pressed_again_right_after_closing_the_menu_still_opens(ui, qtbot, tmp_path, monkeypatch):
    """Found by the real test on Steam: the menu of a desktop start was closed and the guide button pressed again
    0.5 s later. While the new menu waited for Steam, the hidden couch mode closed itself (400 ms after the first
    menu: "no menu visible") and took the waiting menu with it — the press did nothing, and Steam's answer (the
    layout kept for it) was never handed back."""
    from ezgd.library.manager import LibraryManager
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Again Game")
    tokens = iter(range(1, 100))
    released = []

    def hold(self, entry_id):
        time.sleep(0.8)                                   # Steam answering more slowly than the 400 ms
        return next(tokens)
    monkeypatch.setattr(LibraryManager, "hold_input_layout", hold)
    monkeypatch.setattr(LibraryManager, "let_go_input_layout", lambda self, token: released.append(token) or True)
    win._quick_menu_by_key()
    couch = win.couch
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.isVisible(), timeout=4000)
    couch.quick.command("b")
    qtbot.waitUntil(lambda: not couch.quick.isVisible(), timeout=3000)
    qtbot.wait(100)
    win._quick_menu_by_key()                              # pressed again at once
    qtbot.waitUntil(lambda: win.couch is couch and couch.quick.isVisible(), timeout=4000)
    qtbot.wait(600)
    assert win.couch is couch and couch.quick.isVisible()       # it stayed (and so did its couch mode)
    couch.quick.command("b")
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)
    qtbot.waitUntil(lambda: sorted(released) == [1, 2], timeout=4000)
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


@linux_only
def test_a_menu_gone_before_steam_answered_hands_the_layout_back(ui, qtbot, tmp_path, monkeypatch):
    from ezgd.library.manager import LibraryManager
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Gone Game")
    released = []

    def hold(self, entry_id):
        time.sleep(0.6)
        return 9
    monkeypatch.setattr(LibraryManager, "hold_input_layout", hold)
    monkeypatch.setattr(LibraryManager, "let_go_input_layout", lambda self, token: released.append(token) or True)
    win._quick_menu_by_key()
    couch = win.couch
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick.opening, timeout=3000)
    couch.force_close()                                   # (the app quitting, say) while Steam is still switching
    qtbot.waitUntil(lambda: released == [9], timeout=4000)
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


# --------------------------------------------------------------- the quick menu keeps the game's pad to itself
class _Grabs:
    """EVIOCGRAB calls (the kernel's own test of them: tmp scratchpad grabprobe.py on Steam's real pad, 2026-10-28 —
    a read-only handle may take it, a second reader is then refused with EBUSY)."""

    def __init__(self, monkeypatch, gp, absinfo=None, keys=b""):
        self.calls = []
        real = gp.fcntl.ioctl

        def ioctl(fd, req, arg=0, *a):
            if req == gp.EVIOCGRAB:
                self.calls.append(arg)
                return 0
            if req == gp.EVIOCSCLOCKID:
                return 0
            if req == gp.EVIOCGKEY:
                return keys.ljust(gp.KEY_BYTES, b"\0")
            if absinfo is not None and req in absinfo:
                return gp.ABSINFO.pack(*absinfo[req])
            return real(fd, req, arg, *a)
        monkeypatch.setattr(gp.fcntl, "ioctl", ioctl)


def _pad(gp, fd=-1):
    dev = gp._Device("/dev/input/event90", "Microsoft X-Box 360 pad 0", fd, 0x28DE, 0x11FF, 0)
    dev.ranges.update({gp.ABS_X: (-32768, 32767), gp.ABS_Y: (-32768, 32767), gp.ABS_Z: (0, 255),
                       gp.ABS_RZ: (0, 255), gp.ABS_HAT0X: (-1, 1), gp.ABS_HAT0Y: (-1, 1)})
    return dev


def _feed(gp, g, dev, w, events, at=None):
    """Input events through the real read path (a pipe in place of the device)."""
    import os as _os
    t = time.monotonic() if at is None else at
    data = b"".join(gp.EVENT.pack(int(t), int((t % 1) * 1e6), typ, code, value) for typ, code, value in events)
    _os.write(w, data)
    g._read(dev.path)


def test_the_quick_menu_takes_the_games_pad_for_itself_and_gives_it_back(qtbot, monkeypatch):
    """The game reads the same virtual pad as the menu (Steam's layout for it is kept while the menu is open): a
    press in the menu — B to close it, A on Quit — reached the game too. While the menu is open the pad is read
    by the app alone; when it goes, the game has it back."""
    from ezgd.ui import gamepad as gp
    grabs = _Grabs(monkeypatch, gp)
    g = gp.Gamepads()
    dev = _pad(gp)
    g.devices[dev.path] = dev
    real = gp._Device("/dev/input/event91", "DualSense Wireless Controller", -1, 0x054C, 0x0CE6, 0)
    g.devices[real.path] = real
    g.hold_virtual(True)
    assert grabs.calls == [1] and dev.grabbed                     # only Steam's pad (the game reads the real one raw)
    g.hold_virtual(False)
    assert grabs.calls == [1, 0] and not dev.grabbed
    g.devices.clear()
    g.stop()
    g.deleteLater()
    qtbot.wait(50)


def test_a_pad_held_when_the_menu_opens_is_taken_once_let_go(qtbot, monkeypatch):
    """Taken while the stick is held, the game would never see it let go — the character kept running after the
    menu. It is taken at the first report that has everything at rest."""
    import os as _os
    from ezgd.ui import gamepad as gp
    grabs = _Grabs(monkeypatch, gp)
    g = gp.Gamepads()
    r, w = _os.pipe()
    _os.set_blocking(r, False)
    dev = _pad(gp, r)
    g.devices[dev.path] = dev
    _feed(gp, g, dev, w, [(gp.EV_ABS, gp.ABS_Y, -30000), (gp.EV_SYN, 0, 0)])       # running forward
    g.hold_virtual(True)
    assert grabs.calls == []
    _feed(gp, g, dev, w, [(gp.EV_ABS, gp.ABS_RZ, 200), (gp.EV_SYN, 0, 0)])         # … and a trigger
    _feed(gp, g, dev, w, [(gp.EV_ABS, gp.ABS_Y, 0), (gp.EV_SYN, 0, 0)])
    assert grabs.calls == []                                       # (the trigger is still held)
    _feed(gp, g, dev, w, [(gp.EV_ABS, gp.ABS_RZ, 0), (gp.EV_SYN, 0, 0)])
    assert grabs.calls == [1]                                      # everything let go: the menu's alone now
    g.hold_virtual(False)
    assert grabs.calls == [1, 0]
    g.devices.clear()
    _os.close(r)
    _os.close(w)
    g.stop()
    g.deleteLater()
    qtbot.wait(50)


def test_a_press_from_just_before_the_pad_was_taken_gives_it_back(qtbot, monkeypatch):
    """A press made a moment before the pad was taken reached the game, but its release would not: the pad goes
    back to the game until everything is let go again (event times, monotonic clock)."""
    import os as _os
    from ezgd.ui import gamepad as gp
    grabs = _Grabs(monkeypatch, gp)
    g = gp.Gamepads()
    r, w = _os.pipe()
    _os.set_blocking(r, False)
    dev = _pad(gp, r)
    g.devices[dev.path] = dev
    g.hold_virtual(True)
    assert grabs.calls == [1]
    _feed(gp, g, dev, w, [(gp.EV_KEY, gp.BTN_SOUTH, 1), (gp.EV_SYN, 0, 0)], at=dev.grabbed - 0.01)
    assert grabs.calls == [1, 0]                                   # the game saw A go down: it sees it go up
    _feed(gp, g, dev, w, [(gp.EV_KEY, gp.BTN_SOUTH, 0), (gp.EV_SYN, 0, 0)])
    assert grabs.calls == [1, 0, 1]
    _feed(gp, g, dev, w, [(gp.EV_KEY, gp.BTN_EAST, 1), (gp.EV_SYN, 0, 0)])         # taken: a press is the menu's
    assert grabs.calls == [1, 0, 1]
    g.devices.clear()
    _os.close(r)
    _os.close(w)
    g.stop()
    g.deleteLater()
    qtbot.wait(50)


def test_a_pad_opened_while_a_stick_is_held_knows_it(qtbot, monkeypatch):
    """The quick menu of a game started from the desktop opens the pads the moment it appears: a stick already
    held then (the player was running) counted as at rest, and the pad was taken under it."""
    from ezgd.ui import gamepad as gp
    _Grabs(monkeypatch, gp, absinfo={gp._eviocgabs(gp.ABS_X): (25000, -32768, 32767, 16, 128, 0),
                                     gp._eviocgabs(gp.ABS_Y): (0, -32768, 32767, 16, 128, 0)},
           keys=bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                       0, 0, 0, 0, 0, 0, 1]))                    # BTN_SOUTH (0x130) held
    dev = gp._Device("/dev/input/event92", "Microsoft X-Box 360 pad 0", -1, 0x28DE, 0x11FF, 0)
    assert dev.axes[gp.ABS_X] > 0.7 and gp.BTN_SOUTH in dev.down
    assert not gp.Gamepads._at_rest(dev)
    qtbot.wait(10)


@linux_only
def test_the_quick_menu_holds_the_pads_while_it_is_open(ui, qtbot, tmp_path, monkeypatch):
    from test_couch_ui import _open_couch
    from ezgd.ui.gamepad import Gamepads
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Held Pad Game")
    holds = []
    monkeypatch.setattr(Gamepads, "hold_virtual", lambda self, on: holds.append(on))
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)  # the game has the screen
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    assert holds == [True]
    couch.quick.command("b")
    qtbot.waitUntil(lambda: not couch.quick.isVisible(), timeout=3000)
    assert holds == [True, False]
    monkeypatch.undo()
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()
