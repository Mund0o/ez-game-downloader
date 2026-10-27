"""Shift+Tab over one of the app's games opens the quick menu — the key is taken only while that game has the focus
and has no Steam overlay (Shift+Tab is everyone's otherwise: reverse Tab, Steam's overlay). KWin is faked."""
import sys

import pytest

from ezgd.ui import hotkey


class FakeKWin:
    def __init__(self):
        self.loaded: dict[str, str] = {}
        self.calls: list[tuple] = []

    def load(self, key, template, what):
        name = f"script-{what}-{len(self.calls)}"
        self.loaded[name] = template
        self.calls.append(("load", what))
        return name

    def unload(self, key, name):
        self.loaded.pop(name, None)
        self.calls.append(("unload", name.split("-")[1]))


@pytest.fixture
def key(qtbot, monkeypatch):
    kwin = FakeKWin()
    answers = {}
    opened = []
    k = hotkey.QuickMenuKey(lambda pid, answer: answer(answers.get(pid, False)), lambda: opened.append(1))
    monkeypatch.setattr(k, "_load", lambda template, what: kwin.load(k, template, what))
    monkeypatch.setattr(k, "_unload", lambda name: kwin.unload(k, name))
    unregistered = []
    monkeypatch.setattr(k, "_call", lambda *a: unregistered.append(a))
    k._focus_script = kwin.load(k, hotkey.FOCUS_SCRIPT, "focus")      # (as start() would, with KWin there)
    yield k, kwin, answers, opened, unregistered
    k.deleteLater()


def test_the_key_is_ours_only_while_our_game_has_the_focus(key, qtbot):
    k, kwin, answers, opened, unregistered = key
    answers[4242] = True                                       # our game, no Steam overlay
    k.Focus(1000)                                              # a browser: Shift+Tab stays the browser's
    assert not any(t == hotkey.KEY_SCRIPT for t in kwin.loaded.values())
    k.Focus(4242)
    assert any(t == hotkey.KEY_SCRIPT for t in kwin.loaded.values())
    k.Open()
    qtbot.waitUntil(lambda: opened == [1], timeout=2000)
    k.Focus(1000)                                              # away again: unloaded and unregistered from KDE
    assert not any(t == hotkey.KEY_SCRIPT for t in kwin.loaded.values())
    assert any(a[3] == "unregister" and a[-1] == hotkey.ACTION for a in unregistered)
    k.Focus(4242)
    k.stop()                                                   # the last game ended: nothing left behind
    assert kwin.loaded == {} and not k.active


def test_an_answer_for_a_window_that_lost_the_focus_is_ignored(qtbot):
    pending = []
    k = hotkey.QuickMenuKey(lambda pid, answer: pending.append((pid, answer)), lambda: None)
    loads = []
    k._load = lambda template, what: loads.append(what) or what
    k._unload = lambda name: None
    k._call = lambda *a: None
    k._focus_script = "focus"
    k.Focus(4242)
    k.Focus(1000)                                              # the focus moved on before the core answered
    pending[0][1](True)
    assert "key" not in loads
    k.deleteLater()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="game processes from /proc")
def test_only_our_running_games_without_steams_overlay(core, tmp_path, monkeypatch):  # noqa: F811
    lib = core.library
    monkeypatch.setattr(lib, "running_ids", lambda: [7, 8])
    monkeypatch.setattr(lib, "game_processes", lambda eid: {7: [700, 701], 8: [800]}[eid])
    monkeypatch.setattr(lib, "has_steam_overlay", lambda eid: eid == 8)
    assert lib.quick_menu_key(701)                             # our game, no Steam overlay
    assert not lib.quick_menu_key(800)                         # Steam's overlay: Shift+Tab is Steam's
    assert not lib.quick_menu_key(999) and not lib.quick_menu_key(0)
    lib._suspended[7] = {"at": 0, "pids": [700]}
    assert not lib.quick_menu_key(701)                         # suspended: frozen, no menu over it
    lib._suspended.pop(7)


from test_release_fixes import core  # noqa: E402,F401


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="game processes from /proc")
def test_shift_tab_in_the_menu_closes_it_and_the_watch_follows_running_games(ui, qtbot, tmp_path, monkeypatch):  # noqa: F811
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from test_couch_ui import _open_couch
    from test_quick_menu import _running
    win, core, _ = ui
    states = []
    monkeypatch.setattr(hotkey.QuickMenuKey, "start", lambda self: states.append("on"))
    monkeypatch.setattr(hotkey.QuickMenuKey, "stop", lambda self: states.append("off"))
    a = _running(win, core, tmp_path, qtbot, "Keyboard Game")
    qtbot.waitUntil(lambda: "on" in states, timeout=5000)               # a game runs: the focus is followed
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    win._quick_menu_by_key()                                           # Shift+Tab over the game
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    import time
    q.opened_at = time.monotonic() - 1                                 # (not the press that opened it)
    q.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Backtab, Qt.ShiftModifier))
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)           # Shift+Tab again: back to the game
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()
    qtbot.waitUntil(lambda: states[-1] == "off", timeout=8000)         # no game: Shift+Tab is everyone's again


from test_ui import ui  # noqa: E402,F401
