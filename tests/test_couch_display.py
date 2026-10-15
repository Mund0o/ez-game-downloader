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
