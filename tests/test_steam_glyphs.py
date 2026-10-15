"""A Steam controller (Steam Deck, Steam Controller) shows its own buttons in couch mode — also while Steam turns it
into keyboard and mouse input outside games."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.ui import gamepad


def test_valve_controllers_are_found_on_the_hid_bus(tmp_path):
    (tmp_path / "0003:046D:C547.0001").mkdir()                       # a Logitech receiver
    assert not gamepad.steam_hardware(tmp_path)
    (tmp_path / "0003:28DE:1205.0009").mkdir()                       # a Steam Deck's controller
    assert gamepad.steam_hardware(tmp_path)
    assert not gamepad.steam_hardware(tmp_path / "missing")


def test_keys_from_a_steam_controller_keep_its_buttons(ui, qtbot, monkeypatch):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch.pads.steam_hw = True                                       # Steam has the controller (no gamepad node)
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
