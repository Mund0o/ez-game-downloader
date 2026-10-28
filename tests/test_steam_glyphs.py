"""A Steam controller (Steam Deck, Steam Controller) shows its own buttons in couch mode — also while Steam turns it
into keyboard and mouse input outside games."""

import re

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.ui import gamepad, steam_input as si

# Steam's two formats, shaped like Steam's own default desktop layouts (controller_base/desktop_*.vdf)
GORDON_V2 = """
"controller_mappings"
{
	"version"		"2"
	"title"		"Desktop Configuration"
	"group" { "id" "0" "mode" "four_buttons" "bindings" { "button_a" "key_press ENTER" "button_B" "key_press SPACE"
		"button_X" "key_press PAGE_UP" "button_y" "key_press PAGE_DOWN" } }
	"group" { "id" "1" "mode" "dpad" "bindings" { "dpad_north" "key_press UP_ARROW" "click" "controller_action SHOW_KEYBOARD" } }
	"group" { "id" "3" "mode" "absolute_mouse" "bindings" { "click" "key_press LEFT_SHIFT" } }
	"group" { "id" "4" "mode" "trigger" "bindings" { "click" "mouse_button RIGHT" } }
	"group" { "id" "5" "mode" "trigger" "bindings" { "click" "mouse_button LEFT" } }
	"group" { "id" "6" "mode" "scrollwheel" "bindings" { "scroll_clockwise" "mouse_wheel SCROLL_DOWN" } }
	"preset" { "id" "0" "name" "Default"
		"group_source_bindings" { "0" "button_diamond active" "1" "joystick active" "6" "left_trackpad active"
			"3" "right_trackpad active" "4" "left_trigger active" "5" "right_trigger active" }
		"switch_bindings" { "bindings" { "left_bumper" "key_press LEFT_CONTROL" "right_bumper" "key_press LEFT_ALT"
			"button_escape" "key_press ESCAPE" "button_menu" "key_press TAB" } } }
}
"""


def _press(key):
    return '{ "activators" { "Full_Press" { "bindings" { "binding" "%s" } } } }' % key


NEPTUNE_V3 = """
"controller_mappings"
{
	"version" "3"
	"group" { "id" "0" "mode" "four_buttons" "inputs" {
		"button_a" %s "button_b" %s "button_x" %s "button_y" %s } }
	"group" { "id" "4" "mode" "trigger" "inputs" { "edge" %s } }
	"group" { "id" "5" "mode" "trigger" "inputs" { "edge" %s } }
	"group" { "id" "14" "mode" "absolute_mouse" "inputs" { "click" %s } }
	"group" { "id" "26" "mode" "scrollwheel" "inputs" { "scroll_clockwise" %s } }
	"group" { "id" "27" "mode" "dpad" "inputs" { "dpad_north" %s } }
	"group" { "id" "7" "mode" "switches" "inputs" { "button_escape" %s "button_menu" %s "left_bumper" %s
		"right_bumper" %s "button_back_right" %s } }
	"preset" { "id" "0" "name" "Default" "group_source_bindings" { "7" "switch active" "0" "button_diamond active"
		"26" "left_trackpad active" "14" "right_trackpad active" "4" "left_trigger active" "5" "right_trigger active"
		"27" "dpad active" "99" "joystick inactive" } }
}
""" % tuple(_press(k) for k in (
    "key_press RETURN", "key_press ESCAPE", "controller_action SHOW_KEYBOARD", "key_press SPACE", "mouse_button RIGHT",
    "mouse_button LEFT", "mouse_button LEFT", "mouse_wheel SCROLL_DOWN", "key_press UP_ARROW", "key_press ESCAPE",
    "key_press TAB", "key_press LEFT_CONTROL", "key_press LEFT_ALT", "key_press PAGE_DOWN"))


def _use(couch, text, kind):
    """Couch mode reads this configuration as the Steam controller's (what DesktopKeys does with Steam's files)."""
    couch.pads.steam_hw = couch.pads.steam_up = True                 # (and Steam runs)
    couch.pads.desktop.keys = si.normalise(si.keys_of(text))
    couch.pads.desktop.labels = si.labels_of(text, kind)
    couch.pads.desktop.browser = si.browser_hints(text, couch.pads.desktop.labels)


def test_valve_controllers_are_found_on_the_hid_bus(tmp_path):
    (tmp_path / "0003:046D:C547.0001").mkdir()                       # a Logitech receiver
    assert not gamepad.steam_hardware(tmp_path)
    (tmp_path / "0003:28DE:1205.0009").mkdir()                       # a Steam Deck's controller
    assert gamepad.steam_hardware(tmp_path)
    assert not gamepad.steam_hardware(tmp_path / "missing")


def test_keys_from_a_steam_controller_keep_its_buttons(ui, qtbot, monkeypatch):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch.pads.steam_hw = couch.pads.steam_up = True                 # Steam has the controller (no gamepad node)
    couch.pad_family = ""
    couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
    assert couch.pad_family == "steam"                               # A / B / L1 / R1, not Enter / Esc / Tab
    couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
    assert couch.pad_family == "steam"
    couch.pads.steam_hw = False                                      # a real keyboard
    couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Left, Qt.NoModifier))
    assert couch.pad_family == ""
    couch.force_close()


def test_the_store_keyboard_types_with_a_controller_that_sends_keys(ui, qtbot):
    """A friend's Steam controller: the search keyboard closed at the first press and typed nothing (Steam sends
    A as Enter and B as Esc)."""
    from ezgd.ui.couch import T_STORE
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch._switch_tab(T_STORE, force=True)
    couch.pages[T_STORE].open_keyboard()
    kb = couch.keyboard
    kb.row, kb.col = 1, 0                                            # Q

    def key(k):
        couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))
    key(Qt.Key_Return)                                               # A: types Q
    key(Qt.Key_Right)
    key(Qt.Key_Return)                                               # W
    assert couch.keyboard is kb and kb.text == "qw"
    key(Qt.Key_Escape)                                               # B: deletes one letter
    assert couch.keyboard is kb and kb.text == "q"
    # a real keyboard: letters, then Enter searches
    for ch in "uake":
        couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, ch))
    key(Qt.Key_Return)
    assert couch.keyboard is None and couch.pages[T_STORE].query == "quake"
    couch.force_close()


def test_the_clock_is_12_hour():
    import time
    from ezgd.ui.couch import clock_text
    t = time.mktime((2026, 10, 15, 15, 7, 0, 0, 0, -1))
    assert clock_text(t) == "3:07 PM"
    assert clock_text(time.mktime((2026, 10, 15, 0, 5, 0, 0, 0, -1))) == "12:05 AM"
    assert clock_text(time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1))) == "12:00 PM"


def test_big_picture_is_recognised_from_xprop(monkeypatch):
    from ezgd.ui import steam_bpm
    answers = {("-root", "_NET_ACTIVE_WINDOW"): "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x4a00012\n",
               ("-id", "0x4a00012", "_NET_WM_NAME", "WM_NAME"):
                   '_NET_WM_NAME(UTF8_STRING) = "Steam Big Picture Mode"\nWM_NAME(STRING) = "Steam Big Picture Mode"\n'}
    monkeypatch.setattr(steam_bpm, "_xprop", lambda *a: answers.get(a, ""))
    assert steam_bpm.big_picture_in_front()
    answers[("-id", "0x4a00012", "_NET_WM_NAME", "WM_NAME")] = 'WM_NAME(STRING) = "Steam"\n'
    assert not steam_bpm.big_picture_in_front()                     # Steam's normal window: left alone
    answers[("-root", "_NET_ACTIVE_WINDOW")] = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"
    assert steam_bpm.active_x_title() == ""


def test_couch_closes_big_picture_and_goes_home(ui, qtbot, monkeypatch):
    from ezgd.ui import couch as C, steam_bpm
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch._switch_tab(C.T_STORE, force=True)
    closed = []
    monkeypatch.setattr(steam_bpm, "steam_running", lambda: True)
    monkeypatch.setattr(steam_bpm, "big_picture_in_front", lambda: True)
    monkeypatch.setattr(steam_bpm, "close_big_picture", lambda: closed.append(1) or True)
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)     # Big Picture took the focus
    couch._check_big_picture()
    assert closed == [1] and couch.tab == C.T_HOME
    couch._launching = True                                         # a game is starting (Steam may show things)
    couch._check_big_picture()
    assert closed == [1]
    couch._launching = False
    monkeypatch.undo()
    couch.force_close()


def test_steam_controller_buttons_through_steams_desktop_layout(ui, qtbot):
    """Steam sends a Steam controller as keys (its default Desktop Configuration). The original Steam Controller:
    A Enter, B Space, X Page Up, Y Page Down, bumpers Ctrl / Alt — Y must open Manage, B go back, the bumpers switch
    tabs (they did nothing, B selected and Y jumped a page)."""
    from ezgd.ui import couch as C
    win, core, _ = ui
    folder = core.paths.data.parent / "games" / "Pad Game"
    folder.mkdir(parents=True)
    (folder / "Game.exe").write_bytes(b"MZ" + b"\0" * 100)
    e = win.bridge.run_sync(core.library_call("import_folder", "Pad Game", str(folder)))
    win._reload_library()
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: any(x.id == e.id for x in couch.entries), timeout=5000)
    _use(couch, GORDON_V2, "gordon")

    def press(k):
        couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))
    couch._switch_tab(C.T_LIBRARY, force=True)
    couch.zone = "main"
    press(Qt.Key_PageDown)                                           # Y: Manage
    assert couch.overlay is not None and couch.overlay.tag == "details" and couch.pad_family == "steam"
    press(Qt.Key_Space)                                              # B: back (closes it)
    assert couch.overlay is None
    tab = couch.tab
    press(Qt.Key_Alt)                                                # right bumper: next tab
    assert couch.tab == (tab + 1) % len(C.TABS)
    press(Qt.Key_Control)                                            # left bumper: back again
    assert couch.tab == tab
    _use(couch, NEPTUNE_V3, "neptune")                               # Steam Deck: Y = Space
    couch.zone = "main"
    press(Qt.Key_Space)
    assert couch.overlay is not None and couch.overlay.tag == "details"
    press(Qt.Key_Escape)                                             # B
    assert couch.overlay is None
    # on the search keyboard: A types, B (Space on the original controller) deletes instead of typing a space
    _use(couch, GORDON_V2, "gordon")
    couch._switch_tab(C.T_STORE, force=True)
    couch.pages[C.T_STORE].open_keyboard()
    kb = couch.keyboard
    kb.row, kb.col = 1, 0
    press(Qt.Key_Return)
    press(Qt.Key_Return)
    press(Qt.Key_Space)
    assert couch.keyboard is kb and kb.text == "q"
    couch.pads.steam_hw = False
    couch.force_close()


def test_steam_controller_models():
    assert si.kind_of({0x1102}) == "gordon" and si.kind_of({0x1142}) == "gordon"
    assert si.kind_of({0x1106}) == "gordon"                          # the original one over Bluetooth
    assert si.kind_of({0x1205}) == "neptune" and si.kind_of({0x1302}) == ""


def test_steams_virtual_pads_are_not_steam_hardware(tmp_path):
    (tmp_path / "0003:28DE:11FF.0004").mkdir()                       # Steam Input's virtual pads
    (tmp_path / "0003:28DE:11FC.0005").mkdir()
    assert gamepad.steam_products(tmp_path) == set()
    (tmp_path / "0005:28DE:1106.0006").mkdir()                       # a Steam Controller over Bluetooth
    assert gamepad.steam_products(tmp_path) == {0x1106}


def test_steams_desktop_layouts_are_read_not_assumed():
    """What each button sends comes from Steam's configuration file (both of Steam's formats)."""
    g = si.keys_of(GORDON_V2)
    assert {g["ENTER"], g["SPACE"], g["PAGE_UP"], g["PAGE_DOWN"]} == {"a", "b", "x"} and g["SPACE"] == "b"
    assert g["LEFT_CONTROL"] == "lb" and g["LEFT_ALT"] == "rb" and g["TAB"] == "start" and g["ESCAPE"] == "b"
    assert "LEFT_SHIFT" not in g                                     # the right pad's click: no couch meaning
    n = si.keys_of(NEPTUNE_V3)
    assert n["RETURN"] == "a" and n["ESCAPE"] == "b" and n["SPACE"] == "x" and "PAGE_DOWN" not in n   # (a grip)
    assert "lt" not in n.values() and "rt" not in n.values()        # the triggers are mouse clicks
    assert si.labels_of(NEPTUNE_V3, "neptune")["x"] == "Y"           # X opens Steam's keyboard: Manage is on Y
    assert si.labels_of(GORDON_V2, "gordon")["lb"] == "LB" and si.labels_of(NEPTUNE_V3, "neptune")["lb"] == "L1"
    assert si.browser_hints(GORDON_V2, si.LABELS["gordon"]) == [
        ("R-PAD", "Pointer"), ("RT", "Click"), ("L-PAD", "Scroll"), ("STICK click", "Keyboard"), ("◀", "Close")]
    assert si.browser_hints(NEPTUNE_V3, si.LABELS["neptune"]) == [
        ("R-PAD", "Pointer"), ("R2", "Click"), ("L-PAD", "Scroll"), ("X", "Keyboard"), ("B", "Close")]
    my = NEPTUNE_V3.replace("key_press RETURN", "key_press E")       # changed in Steam's settings: A sends E
    assert si.keys_of(my)["E"] == "a" and "RETURN" not in si.keys_of(my)


# a layout the user saved in Steam (Steam Controller Configs/<id>/config/413080/*.vdf): Steam's configurator writes a
# label after every binding — "key_press LEFT_CONTROL, Left Control, , "
SAVED_V3 = re.sub(r'"((?:key_press|mouse_button|mouse_wheel|controller_action) [^"]+)"',
                  lambda m: '"%s, %s, , "' % (m.group(1), m.group(1).split()[-1].title()), NEPTUNE_V3)


def test_layouts_saved_in_steam_are_read_too():
    """A friend's Steam controller under his own saved desktop layout: the bumpers did not switch tabs and Y opened
    the Play box (Space, unrecognised, fell back to "press") — every key had come out as "LEFT_CONTROL,"."""
    assert "key_press LEFT_CONTROL, Left_Control, , " in SAVED_V3
    assert si.keys_of(SAVED_V3) == si.keys_of(NEPTUNE_V3)
    k = si.keys_of(SAVED_V3)
    assert k["LEFT_CONTROL"] == "lb" and k["LEFT_ALT"] == "rb" and k["SPACE"] == "x" and k["RETURN"] == "a"
    assert si.labels_of(SAVED_V3, "neptune") == si.labels_of(NEPTUNE_V3, "neptune")
    assert si.browser_hints(SAVED_V3, si.LABELS["neptune"]) == si.browser_hints(NEPTUNE_V3, si.LABELS["neptune"])


def test_a_saved_layout_drives_couch_mode(ui, qtbot):
    from ezgd.ui import couch as C
    win, core, _ = ui
    folder = core.paths.data.parent / "games" / "Saved Pad Game"
    folder.mkdir(parents=True)
    (folder / "Game.exe").write_bytes(b"MZ" + b"\0" * 100)
    e = win.bridge.run_sync(core.library_call("import_folder", "Saved Pad Game", str(folder)))
    win._reload_library()
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: any(x.id == e.id for x in couch.entries), timeout=5000)
    _use(couch, SAVED_V3, "neptune")

    def press(k):
        couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))
    couch._switch_tab(C.T_LIBRARY, force=True)
    couch.zone = "main"
    press(Qt.Key_Space)                                              # Y: Manage, not the Play box
    assert couch.overlay is not None and couch.overlay.tag == "details"
    press(Qt.Key_Escape)
    assert couch.overlay is None
    tab = couch.tab
    press(Qt.Key_Alt)                                                # right bumper
    assert couch.tab == (tab + 1) % len(C.TABS)
    press(Qt.Key_Control)                                            # left bumper
    assert couch.tab == tab
    couch.pads.steam_hw = False
    couch.force_close()


def test_the_configuration_steam_logged_is_the_one_used(tmp_path, monkeypatch):
    root = tmp_path / "Steam"
    (root / "controller_base").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "controller_base" / "desktop_steamcontroller_gordon.vdf").write_text(GORDON_V2)
    (root / "controller_base" / "empty.vdf").write_text('"controller_mappings" { }')
    mine = tmp_path / "mine.vdf"
    mine.write_text(GORDON_V2.replace("key_press ENTER", "key_press E"))
    monkeypatch.setattr(si, "steam_root", lambda: root)
    dk = si.DesktopKeys()
    dk.update({0x1142})                                              # nothing logged: Steam's default file
    assert dk.keys["ENTER"] == "a" and dk.source.endswith("desktop_steamcontroller_gordon.vdf")
    (root / "logs" / "controller_ui.txt").write_text(
        "[2026-10-15 09:00:00] Loaded Config for Local Override Path for App ID 413080, Controller 1: "
        f"{root}//controller_base/empty.vdf\n"
        "[2026-10-15 09:00:01] Loaded Config for Local Selection Path for App ID 413080, Controller 0: "
        f"{mine}\n"
        "[2026-10-15 09:00:02] Loaded Config for Local Selection Path for App ID 443510, Controller 0: "
        "controller_base/chord_ps4.vdf\n")
    dk.update({0x1142})
    assert dk.keys["E"] == "a" and "ENTER" not in dk.keys and dk.source == str(mine)
    monkeypatch.setattr(si, "steam_root", lambda: None)              # no Steam folder: Steam's defaults, built in
    dk.update({0x1205})
    assert dk.keys["SPACE"] == "x" and dk.source.startswith("built-in")


def test_the_steam_button_is_read_from_steams_log(tmp_path, qtbot):
    log = tmp_path / "logs" / "controller_ui.txt"
    log.parent.mkdir()
    log.write_text("[2026-10-15 08:00:00] Guide button sent to JS\n")    # (before: not a press now)
    w = si.GuideWatch(root=tmp_path)
    taps, holds = [], []
    w.tapped.connect(lambda: taps.append(1))
    w.held.connect(lambda: holds.append(1))
    w.start()
    assert not taps
    with open(log, "a") as f:
        f.write("[2026-10-15 09:00:00] Loaded Config for Local Selection Path for App ID 443510, Controller 0: x\n"
                "[2026-10-15 09:00:00] Guide button sent to JS\n[2026-10-15 09:00:01] Guide button skipped due to")
    w.poll()
    assert taps == [1] and not holds                                 # (half a line waits)
    with open(log, "a") as f:
        f.write(" length\n")
    w.poll()
    assert holds == [1]
    log.write_text("[2026-10-15 10:00:00] Guide button sent to JS\n")    # Steam started its log again
    w.poll()
    assert taps == [1, 1]
    w.stop()


def test_a_steam_button_logged_is_home_unless_the_controller_said_so(qtbot, monkeypatch):
    import time
    pads = gamepad.Gamepads()
    got = []
    pads.action.connect(got.append)
    pads.home_held.connect(lambda: got.append("held"))
    pads.steam_hw = True
    pads._steam_guide(False)
    pads._steam_guide(True)
    assert got == ["home", "held"] and pads.family == "steam"
    pads._home_seen = time.monotonic()                               # a DualSense's PS button, read directly
    pads._steam_guide(False)
    assert got == ["home", "held"]                                   # the same press: not twice
    pads.stop()


def test_prompts_only_for_buttons_that_send_something(ui, qtbot):
    """Steam's layouts make the triggers mouse clicks: no "RT Filter" prompt the controller cannot follow; the bar
    names the Steam controller, not the keyboard."""
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    _use(couch, NEPTUNE_V3, "neptune")
    couch.pad_family = "steam"
    hints = [("a", "Open"), ("x", "Search"), ("lt", ""), ("rt", "Filter"), ("lb", ""), ("rb", "Tabs")]
    assert couch.usable_hints(hints) == [("a", "Open"), ("x", "Search"), ("lb", ""), ("rb", "Tabs")]
    assert couch._pad_labels()["lb"] == "L1" and couch._pad_labels()["x"] == "Y"
    _use(couch, GORDON_V2, "gordon")
    assert couch._pad_labels()["lb"] == "LB" and couch._pad_labels()["start"] == "▶"
    couch.pads.steam_hw = False
    couch.pad_family = "xbox"
    assert couch.usable_hints(hints) == hints                       # any other controller: all of them
    couch.force_close()


def test_steam_is_put_away_after_the_guide_button(ui, qtbot, monkeypatch):
    """Steam (Steam Input on the controller) answers the guide button too and brings its window up over couch mode:
    it is minimized again right after, and couch mode keeps the screen."""
    from ezgd.ui import steam_bpm, window_control
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    hidden, activated = [], []
    monkeypatch.setattr(steam_bpm, "steam_running", lambda: True)
    monkeypatch.setattr(window_control, "hide_steam_windows", lambda only_active=True: hidden.append(only_active)
                        or True)
    monkeypatch.setattr(couch.wc, "activate", lambda: activated.append(1))
    couch._pad("home")                                               # a tap of the PS button in couch mode
    qtbot.waitUntil(lambda: len(hidden) >= 2, timeout=3000)
    assert hidden == [True, True] and activated                      # only Steam's active window; couch is back
    monkeypatch.undo()
    couch.force_close()


def test_big_picture_is_closed_and_steam_stays_in_the_back(qtbot, monkeypatch):
    """Big Picture is closed (Steam's own URL), and Steam's desktop window that appears then is put away."""
    import subprocess
    from ezgd.ui import steam_bpm, window_control
    ran, hidden = [], []
    monkeypatch.setattr(steam_bpm.shutil, "which", lambda name: "/usr/bin/steam" if name == "steam" else None)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(window_control, "hide_steam_windows", lambda only_active=True: hidden.append(only_active)
                        or True)
    assert steam_bpm.close_big_picture()
    assert ran == [["/usr/bin/steam", "steam://close/bigpicture"]]
    qtbot.waitUntil(lambda: len(hidden) == 3, timeout=5000)
    assert hidden == [False, False, False]                           # every Steam window minimized, not just one


def test_the_ps_button_arms_an_instant_steam_guard(ui, qtbot, monkeypatch):
    """With KWin, a script put away Steam's window the moment it appears (no waiting on timers)."""
    from ezgd.ui import steam_bpm, window_control
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    armed, polled = [], []
    monkeypatch.setattr(steam_bpm, "steam_running", lambda: True)
    monkeypatch.setattr(window_control, "guard_steam", lambda ms=3000: armed.append(ms) or True)
    monkeypatch.setattr(window_control, "hide_steam_windows", lambda only_active=True: polled.append(1) or True)
    couch._pad("home")
    qtbot.wait(1500)
    assert armed == [3000] and not polled                           # the guard, not the slower timed checks
    script = window_control.STEAM_GUARD_SCRIPT % {"ms": 3000}
    assert "windowActivated" in script and "closeWindow" in script and "minimized = true" in script
    monkeypatch.undo()
    couch.force_close()


def test_a_steam_controller_read_directly_without_steam(qtbot):
    """Without Steam the kernel (hid-steam) gives the controller to the app: its left trackpad's touch position comes
    on the d-pad axes (±32767) and its click as d-pad buttons — a thumb resting on the pad must not scroll couch
    mode; clicking it does."""
    pads = gamepad.Gamepads()
    got = []
    pads.action.connect(got.append)
    sc = gamepad._Device("/dev/input/event90", "Steam Controller", -1, 0x28DE, 0x1142,
                         1 << gamepad.BTN_TL2 | 1 << gamepad.BTN_SOUTH)
    sc.ranges.update({gamepad.ABS_HAT0X: (-32767, 32767), gamepad.ABS_HAT0Y: (-32767, 32767),
                      gamepad.ABS_X: (-32767, 32767), gamepad.ABS_Y: (-32767, 32767)})
    pads.devices = {sc.path: sc}
    for x in (-20000, -25000, 12000, 0):
        pads._event(sc, gamepad.EV_ABS, gamepad.ABS_HAT0X, x)         # the thumb moves on the pad
    assert got == []
    pads._event(sc, gamepad.EV_KEY, 0x222, 1)                          # BTN_DPAD_LEFT: the pad clicked on the left
    pads._event(sc, gamepad.EV_KEY, 0x222, 0)
    pads._event(sc, gamepad.EV_KEY, 0x134, 1)                          # BTN_Y (= BTN_WEST): Manage
    pads._event(sc, gamepad.EV_KEY, gamepad.BTN_MODE, 1)               # the Steam button
    pads._event(sc, gamepad.EV_KEY, gamepad.BTN_TL2, 1)                # a trigger pulled all the way
    assert got == ["left", "x", "home", "lt"] and pads.family == "steam"
    assert not pads.steam_as_keys()                                    # (read directly: no keys from Steam)
    pads.stop()


def test_steams_virtual_pad_coming_and_going_is_not_announced(ui, qtbot, monkeypatch):
    """A friend's Steam Controller: couch mode said "Controller connected — Microsoft X-Box 360 pad 0" over and over
    (Steam's virtual pad for it, taken away and made again). A real new controller is still announced."""
    from types import SimpleNamespace
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    shown = []
    monkeypatch.setattr(couch.toasts, "show", lambda title, text="", *a, **k: shown.append(text))
    couch._opened = True
    couch._known_pads = set()
    pads = couch.pads
    monkeypatch.setattr(pads, "steam_hw", True)
    xbox = "Microsoft X-Box 360 pad 0"
    monkeypatch.setattr(pads, "devices", {"/dev/input/event258": SimpleNamespace(name=xbox, virtual=False)})
    couch._pads_changed([xbox])                                      # Steam's pad for its controller (Microsoft ids)
    couch._pads_changed([])
    couch._pads_changed([xbox])
    assert shown == []
    monkeypatch.setattr(pads, "steam_hw", False)
    monkeypatch.setattr(pads, "devices", {"/dev/input/event30": SimpleNamespace(name="Steam Virtual Gamepad",
                                                                               virtual=True)})
    couch._pads_changed(["Steam Virtual Gamepad"])                   # 28DE:11FF
    assert shown == []
    pad = "Wireless Controller"
    monkeypatch.setattr(pads, "devices", {"/dev/input/event40": SimpleNamespace(name=pad, virtual=False)})
    couch._pads_changed([pad])                                       # a real controller: news
    assert shown == [pad]
    couch._pads_changed([])
    couch._pads_changed([pad])                                       # back a moment later (Bluetooth): quiet
    assert shown == [pad]
    couch._pad_gone[pad] -= couch.PAD_QUIET_S + 1
    couch._pads_changed([])
    couch._pad_gone[pad] -= couch.PAD_QUIET_S + 1
    couch._pads_changed([pad])                                       # back after a long while: news again
    assert shown == [pad, pad]
    monkeypatch.undo()
    couch.force_close()
