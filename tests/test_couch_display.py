"""Couch mode's size on a friend's PC: it came up full screen for a moment, then shrank to a small window.

Tested on KWin (2026-10-15): on Wayland, showNormal(); setGeometry(); showFullScreen() in one go leaves full screen and
never asks for it again (Qt sends only what differs from the compositor's last answer) — the window ends up at its
minimum size, 800×500. Couch mode did exactly that to "fix" a full-screen size it thought was too small.
"""

import logging
import os
import socket

from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.ui import couch as C


def _calls(couch, monkeypatch, *names):
    got = []
    for n in names:
        monkeypatch.setattr(couch, n, lambda *a, n=n: got.append(n))
    return got


def test_wayland_full_screen_is_never_left_to_fix_its_size(ui, qtbot, monkeypatch):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    monkeypatch.setattr(C, "on_wayland", lambda: True)
    monkeypatch.setattr(couch.wc, "kwin", False)
    monkeypatch.setattr(couch, "isFullScreen", lambda: True)
    monkeypatch.setattr(couch, "width", lambda: 800)                # "too small" for the screen
    monkeypatch.setattr(couch, "height", lambda: 500)
    got = _calls(couch, monkeypatch, "showNormal", "setGeometry", "showFullScreen", "hide")
    couch._size_tries = 0
    couch._fit_screen()
    assert got == [] and couch._size_tries == 2                     # left as the compositor made it (and logged)
    monkeypatch.setattr(C, "on_wayland", lambda: False)             # X11: the window may set its own size
    couch._size_tries = 0
    couch._fit_screen()
    assert got == ["showNormal", "setGeometry", "showFullScreen"]
    monkeypatch.undo()
    couch.force_close()


def test_a_borderless_window_kwin_left_small_is_sized_once_it_is_mapped(ui, qtbot, monkeypatch):
    """A friend's borderless couch mode came up 1811×1075 (the desktop window's size): the KWin request made right
    after show() ran before KWin had the window. The check after opening asks KWin again — at most twice."""
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    monkeypatch.setitem(couch.actions.settings, "couch_window", "borderless")
    monkeypatch.setattr(couch.wc, "kwin", True)
    monkeypatch.setattr(couch, "isFullScreen", lambda: False)
    asked = []
    monkeypatch.setattr(couch.wc, "fill_screen", lambda name="": asked.append(("fill", name)))
    monkeypatch.setattr(couch.wc, "place_pixels", lambda w, h, name="": asked.append(("pixels", w, h)))
    got = _calls(couch, monkeypatch, "place_window", "showFullScreen")
    screen, want, mode = couch.display_target()
    assert mode == "borderless"
    monkeypatch.setattr(couch, "width", lambda: int(want.width() * 0.7))
    monkeypatch.setattr(couch, "height", lambda: int(want.height() * 0.75))
    couch._size_tries = 0
    for _ in range(4):
        couch._fit_screen()
    assert asked == [("fill", screen.name())] * 2 and got == []      # asked twice, never left for full screen
    monkeypatch.setitem(couch.actions.settings, "couch_window", "custom")    # a custom size: KWin places the pixels
    monkeypatch.setitem(couch.actions.settings, "couch_resolution", "1280x720")
    monkeypatch.setattr(couch, "width", lambda: 100)
    monkeypatch.setattr(couch, "height", lambda: 100)
    asked.clear()
    couch._size_tries = 0
    couch._fit_screen()
    assert asked == [("pixels", 1280, 720)]
    asked.clear()
    monkeypatch.setitem(couch.actions.settings, "couch_window", "borderless")  # the right size: nothing to do
    monkeypatch.setattr(couch, "width", lambda: want.width())
    monkeypatch.setattr(couch, "height", lambda: want.height())
    couch._size_tries = 0
    couch._fit_screen()
    assert asked == []
    monkeypatch.undo()
    couch.force_close()


def test_a_shrunk_full_screen_is_left_alone_and_said_in_the_log(ui, qtbot, monkeypatch, caplog):
    """A friend's couch mode came up full screen, then the desktop window's size, twice (2026-10-29: a window-memory
    KWin script handing it the desktop window's place). The app used to fill it again each time — choppy, and a
    fight. Now couch mode has an app id of its own (test below), and a full screen the compositor made smaller is
    left as it is — only logged."""
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    monkeypatch.setattr(C, "on_wayland", lambda: True)
    monkeypatch.setattr(couch.wc, "kwin", True)
    monkeypatch.setattr(couch, "isFullScreen", lambda: True)
    asked = []
    monkeypatch.setattr(couch.wc, "force_fullscreen", lambda name="": asked.append(name))
    got = _calls(couch, monkeypatch, "showNormal", "setGeometry", "showFullScreen", "hide", "place_window")
    g = couch.screen().geometry()
    monkeypatch.setattr(couch, "width", lambda: int(g.width() * 0.72))
    monkeypatch.setattr(couch, "height", lambda: int(g.height() * 0.81))
    couch._size_tries = 0
    with caplog.at_level(logging.WARNING):
        couch._fit_screen()
        couch._fit_screen()
    assert asked == [] and got == []                                # nothing fights the compositor
    assert sum("resized it (left as it is)" in r.message for r in caplog.records) == 1
    assert not hasattr(couch, "_shrunk") and not hasattr(couch, "_refill")
    monkeypatch.undo()
    couch.force_close()


def test_couch_windows_are_their_own_app_to_the_desktop(ui, qtbot, monkeypatch):
    """Window-memory tools hand an app's last window place to the app's next window: couch mode, its quick menu and
    its browser each have an app id of their own (with the monitor in it), the desktop window keeps "ezgd"."""
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtGui import QGuiApplication
    from ezgd.ui import window_control
    from ezgd.ui.couch_quick import QuickMenu
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    monkeypatch.setattr(window_control, "_wayland", lambda: True)

    class Seen(QObject):
        def __init__(self):
            super().__init__()
            self.ids = []

        def eventFilter(self, obj, ev):                              # noqa: N802
            if ev.type() == QEvent.Show:
                self.ids.append(QGuiApplication.desktopFileName())  # (what Qt reads as the window is made)
            return False
    before = QGuiApplication.desktopFileName()
    seen = Seen()
    couch.installEventFilter(seen)
    couch.hide()
    couch.showFullScreen()
    def app_id(role, screen):                                       # (a test screen may have no name)
        return f"ezgd-{role}" + (f"-{screen.name()}" if screen.name() else "")
    assert seen.ids == [app_id("couch", couch.screen())]
    assert QGuiApplication.desktopFileName() == before              # only for that moment
    q = QuickMenu(couch)
    q.installEventFilter(seen)
    q.resize(400, 300)
    q.show()
    assert seen.ids[-1] == app_id("quick-menu", q.screen())
    q.hide()
    main = Seen()
    win.installEventFilter(main)
    win.hide()
    win.show()
    assert main.ids == [before]                                     # the desktop window: the app's own id
    win.hide()
    from ezgd.ui import couch_pages
    from ezgd.ui.dialogs import browser
    got = {}
    monkeypatch.setattr(browser, "AVAILABLE", True)
    monkeypatch.setattr(browser, "open_browser_download", lambda *a, **k: got.update(k))
    game = next(iter(couch.entries), None) or type("G", (), {"source": "steamrip", "title": "X", "url": ""})()
    couch_pages.open_browser(couch, game, type("M", (), {"label": "Site", "url": "https://x.test"})(), "https://x.test")
    assert got.get("app_id_role") == "couch-browser"
    q.deleteLater()
    monkeypatch.undo()
    couch.force_close()


def test_closing_couch_mode_minimized_un_minimizes_it_unseen_first(ui, qtbot, monkeypatch):
    """A window-memory tool saves a window's state as it closes: couch mode closed while minimized (the app quitting
    during a game) would come back minimized next time. KWin un-minimizes it first, transparent."""
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    asked = []
    monkeypatch.setattr(couch.wc, "unminimize_unseen", lambda: asked.append(1))
    monkeypatch.setattr(couch, "isMinimized", lambda: True)
    couch.force_close()
    assert asked == [1]
    couch2 = _open_couch(win, qtbot)
    asked.clear()
    monkeypatch.setattr(couch2.wc, "unminimize_unseen", lambda: asked.append(1))
    couch2.force_close()
    assert asked == []                                              # not minimized: nothing to do


def test_the_kwin_scripts_switched_on_are_listed(tmp_path, monkeypatch):
    from ezgd.ui.display_trace import kwin_scripts
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "kwinrc").write_text("[Plugins]\nblurEnabled=true\nrememberwindowpositionsEnabled=true\n"
                                             "krohnkiteEnabled=false\n\n[Other]\nfooEnabled=true\n")
    (tmp_path / "data" / "kwin-wayland" / "scripts" / "rememberwindowpositions").mkdir(parents=True)
    (tmp_path / "data" / "kwin-wayland" / "scripts" / "krohnkite").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "none"))
    assert kwin_scripts() == ["rememberwindowpositions"]           # (blur is an effect; krohnkite is off)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing"))
    assert kwin_scripts() == []

def test_moving_to_another_screen_on_wayland_reopens_instead_of_leaving_full_screen(ui, qtbot, monkeypatch):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    monkeypatch.setattr(C, "on_wayland", lambda: True)
    for kwin in (True, False):
        monkeypatch.setattr(couch.wc, "kwin", kwin)
        monkeypatch.setattr(couch.wc, "force_fullscreen", lambda *a: None)
        monkeypatch.setattr(couch, "screen", lambda: object())       # on some other screen than the chosen one
        monkeypatch.setattr(couch, "isFullScreen", lambda: True)
        got = _calls(couch, monkeypatch, "showNormal", "hide", "setScreen", "showFullScreen")
        couch.place_window(couch, couch.wc)
        assert got == ["hide", "setScreen", "showFullScreen"], (kwin, got)
    monkeypatch.undo()
    couch.force_close()


def test_the_log_names_the_compositor(tmp_path, monkeypatch):
    """The program at the other end of the Wayland socket (here: this test itself, listening on a fake one)."""
    from ezgd.ui.display_trace import compositor_name
    path = tmp_path / "wayland-test"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    monkeypatch.setenv("WAYLAND_DISPLAY", str(path))
    try:
        name = compositor_name()
    finally:
        srv.close()
    with open(f"/proc/{os.getpid()}/cmdline", "rb") as f:
        assert name == os.path.basename(f.read().split(b"\0")[0].decode())


def test_the_display_trace_records_the_window(qtbot):
    from PySide6.QtWidgets import QWidget
    from ezgd.ui.display_trace import DisplayTrace
    w = QWidget()
    w.setMinimumSize(800, 500)
    qtbot.addWidget(w)
    handler = logging.Handler()
    lines = []
    handler.emit = lambda r: lines.append(r.getMessage())
    logging.getLogger("ezgd.ui.display_trace").addHandler(handler)
    try:
        t = DisplayTrace(w, seconds=5)
        t.start()
        w.show()
        w.resize(900, 600)
        t.stop()
    finally:
        logging.getLogger("ezgd.ui.display_trace").removeHandler(handler)
    assert lines[0].startswith("couch display trace: compositor")
    assert any("window 900x600" in x for x in lines)


def test_the_desktop_window_comes_back_from_couch_mode_as_it_was(ui, qtbot, monkeypatch):
    """A friend's KDE gave the desktop window couch mode's full-screen size when couch mode closed (no title bar,
    over the taskbar). It is put back: out of full screen, its old size (or maximized again)."""
    from ezgd.ui import window_control
    win, core, _ = ui
    win.showNormal()
    win.resize(1100, 720)
    qtbot.wait(50)
    couch = _open_couch(win, qtbot)
    assert win._before_couch[0].width() == win.width() and win._before_couch[1] is False
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible() and win.couch is None, timeout=5000)
    asked = []
    monkeypatch.setattr(window_control, "restore_desktop_window", lambda *a: asked.append(a) or True)
    win._before_couch = (win._before_couch or (win.size(), False, False))
    size = win._before_couch[0]
    monkeypatch.setattr(win, "isFullScreen", lambda: True)            # the window manager kept it full screen
    calls = _calls(win, monkeypatch, "showNormal", "showMaximized")
    win._check_after_couch(False)
    assert calls == ["showNormal"] and asked == [(win.windowTitle(), size.width(), size.height(), False)]
    monkeypatch.setattr(win, "isFullScreen", lambda: False)           # as it was: nothing is done
    asked.clear()
    calls.clear()
    win._check_after_couch(True)
    assert asked == [] and calls == [] and win._before_couch is None
    win._before_couch = (size, False, False)                         # 28 px taller (KWin kept "no border"): back
    monkeypatch.setattr(win, "height", lambda: size.height() + 28)
    win._check_after_couch(False)
    assert asked == [(win.windowTitle(), size.width(), size.height(), False)]
    monkeypatch.setattr(win, "height", lambda: size.height() + 1)    # (a pixel of rounding: left alone)
    asked.clear()
    calls.clear()
    win._check_after_couch(False)
    assert asked == [] and calls == []
    win._before_couch = (size, True, False)                          # it was maximized: maximized again
    monkeypatch.setattr(win, "isFullScreen", lambda: True)
    win._check_after_couch(True)
    assert calls == ["showNormal", "showMaximized"] and asked[-1][-1] is True
    monkeypatch.undo()


def test_borderless_is_full_screen_on_wayland(ui, qtbot, monkeypatch):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    monkeypatch.setitem(couch.actions.settings, "couch_window", "borderless")
    assert couch.display_target()[2] == "borderless"                   # (X11: a borderless window of its own)
    monkeypatch.setattr(C, "on_wayland", lambda: True)
    assert couch.display_target()[2] == "fullscreen"
    monkeypatch.undo()
    couch.force_close()


def test_the_frame_pump_runs_faster_when_the_gpu_draws():
    """Frames go out at a whole fraction of the screen's rate: at most 75 Hz when the CPU draws, at most 144 Hz when
    the GPU does (a 240 Hz screen: 120 frames — half the wait between a press and the screen)."""
    pace = C.CouchWindow.pace_ms
    assert pace(240, False) == 17 and pace(240, True) == 8              # 60 / 120 Hz
    assert pace(144, True) == 7 and pace(144, False) == 14              # 144 / 72 Hz
    assert pace(160, True) == 12 and pace(60, True) == 17 and pace(0, True) == 17


def test_the_app_id_names_the_monitor_and_only_when_shown(monkeypatch):
    """On another monitor it is another app: a window-memory tool never moves couch mode to the monitor it was on."""
    from PySide6.QtGui import QGuiApplication
    from ezgd.ui import window_control
    monkeypatch.setattr(window_control, "_wayland", lambda: True)
    seen = []

    class Base:
        def setVisible(self, visible):                              # noqa: N802 (what Qt reads as it is made)
            seen.append((visible, QGuiApplication.desktopFileName()))

    class Win(window_control.OwnAppId, Base):
        app_id_role = "couch"
        name = "HDMI-A-1"

        def screen(self):
            return type("S", (), {"name": lambda _s, n=self.name: n})()
    before = QGuiApplication.desktopFileName()
    w = Win()
    w.setVisible(True)
    w.name = "DP 2/x"
    w.setVisible(True)
    w.setVisible(False)
    assert seen == [(True, "ezgd-couch-HDMI-A-1"), (True, "ezgd-couch-DP-2-x"), (False, before)]
    monkeypatch.setattr(window_control, "_wayland", lambda: False)  # X11: one WM_CLASS for the app, nothing to do
    w.setVisible(True)
    assert seen[-1] == (True, before) and QGuiApplication.desktopFileName() == before
