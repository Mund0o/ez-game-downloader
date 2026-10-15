"""What the compositor does with couch mode's window — written to the log for the first seconds after it opens.

Couch mode's size on Wayland is the compositor's decision (it answers a full-screen request with the size it wants,
in an ``xdg_toplevel.configure`` event). When the window ends up the wrong size on someone's PC, the log then says
which compositor it was and every size it sent, instead of leaving it to guesswork:

    couch display trace: compositor kwin_wayland (wayland)
    couch display trace +0.02s: compositor asks: Received xdg_toplevel.configure with QSize(2560, 1440) and …
    couch display trace +0.03s: window 2560x1440, state full screen
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import time

from PySide6.QtCore import QEvent, QLoggingCategory, QObject, QTimer, qInstallMessageHandler

log = logging.getLogger(__name__)
TRACE_SECONDS = 10.0


def compositor_name() -> str:
    """The program drawing the desktop: on Wayland the process at the other end of the display socket (asked of the
    kernel, SO_PEERCRED); on X11 the window manager's own name for itself."""
    disp = os.environ.get("WAYLAND_DISPLAY")
    if disp and hasattr(socket, "SO_PEERCRED"):
        path = disp if disp.startswith("/") else os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), disp)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(path)
                pid, _uid, _gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                    struct.calcsize("3i")))
            with open(f"/proc/{pid}/cmdline", "rb") as f:          # (comm would cut the name at 15 letters)
                exe = f.read().split(b"\0")[0].decode("utf-8", "replace")
            return os.path.basename(exe) or str(pid)
        except OSError:
            return "unknown (Wayland)"
    if os.environ.get("DISPLAY"):
        try:
            import re
            from ezgd.ui.steam_bpm import _xprop
            m = re.search(r"window id # (0x[0-9a-f]+)", _xprop("-root", "_NET_SUPPORTING_WM_CHECK"))
            if m:
                t = re.search(r'= "(.*)"', _xprop("-id", m.group(1), "_NET_WM_NAME"))
                if t:
                    return t.group(1)
        except Exception:
            pass
        return "unknown (X11)"
    return ""


class DisplayTrace(QObject):
    """Logs a window's sizes and states, and the compositor's configure events, for ``seconds`` after start()."""

    _active: "DisplayTrace | None" = None

    def __init__(self, window, seconds: float = TRACE_SECONDS):
        super().__init__(window)
        self.window = window
        self.seconds = seconds
        self.t0 = 0.0
        self._previous = None
        self._hooked = False                     # (Qt's Wayland messages are being read)
        self._last = None
        self._lines = 0

    def start(self) -> None:
        if DisplayTrace._active is not None:
            DisplayTrace._active.stop()
        DisplayTrace._active = self
        self.t0 = time.monotonic()
        from PySide6.QtWidgets import QApplication
        log.info("couch display trace: compositor %s (%s)", compositor_name() or "?", QApplication.platformName())
        self.window.installEventFilter(self)
        if QApplication.platformName().startswith("wayland"):
            self._previous = qInstallMessageHandler(self._message)
            self._hooked = True
            QLoggingCategory.setFilterRules("qt.qpa.wayland.debug=true")
        self._note()
        QTimer.singleShot(int(self.seconds * 1000), self, self.stop)

    def stop(self) -> None:
        if DisplayTrace._active is not self:
            return
        DisplayTrace._active = None
        try:
            self.window.removeEventFilter(self)
        except RuntimeError:
            pass
        if self._hooked:
            self._hooked = False
            QLoggingCategory.setFilterRules("qt.qpa.wayland.debug=false")
            qInstallMessageHandler(self._previous)
        self._previous = None

    def _at(self) -> str:
        return f"+{time.monotonic() - self.t0:.2f}s"

    def _message(self, mode, context, message) -> None:
        cat = getattr(context, "category", "") or ""
        if cat.startswith("qt.qpa.wayland"):
            if "configure" in message and ("Received" in message or "invalid" in message) and self._lines < 60:
                self._lines += 1
                log.info("couch display trace %s: compositor asks: %s", self._at(), message.strip())
            if "warning" not in str(mode).lower() and "critical" not in str(mode).lower():
                return                                   # (debug lines turned on only for this)
        if self._previous is not None:
            self._previous(mode, context, message)
        else:
            import sys
            sys.stderr.write(message + "\n")

    def _note(self) -> None:
        w = self.window
        state = ("minimized" if w.isMinimized() else "full screen" if w.isFullScreen() else
                 "maximized" if w.isMaximized() else "normal")
        sc = w.screen()
        now = (w.width(), w.height(), state, w.isVisible(), sc.name() if sc else "?")
        if now != self._last and self._lines < 60:
            self._last = now
            self._lines += 1
            log.info("couch display trace %s: window %dx%d, state %s%s, on %s (%dx%d, scale %.2f)", self._at(),
                     w.width(), w.height(), state, "" if w.isVisible() else ", hidden", now[4],
                     sc.geometry().width() if sc else 0, sc.geometry().height() if sc else 0,
                     w.devicePixelRatioF())

    def eventFilter(self, obj, ev) -> bool:                    # noqa: N802
        if ev.type() in (QEvent.Resize, QEvent.WindowStateChange, QEvent.Show, QEvent.Hide):
            self._note()
        return False
