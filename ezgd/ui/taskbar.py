"""Download progress outside the window: on the taskbar icon and in the tray.

* Taskbar (Linux): the "launcher entry" D-Bus signal KDE Plasma's task manager, Dock-to-Dock and others read — a
  progress bar over the app's icon and the number of running downloads, also while the window is hidden in the tray.
* Tray: a menu (open, couch mode, pause / resume all, quit) and a tooltip with the speed and time left.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QMenu

log = logging.getLogger(__name__)

APP_URI = "application://ezgd.desktop"
PATH = "/com/canonical/unity/launcherentry/ezgd"


def eta_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return "less than a minute"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


class LauncherEntry:
    """Progress and a count on the app's taskbar icon (best effort: nothing happens where nobody listens)."""

    def __init__(self):
        self._bus = None
        self._last: tuple | None = None
        if not sys.platform.startswith("linux"):
            return
        try:
            from PySide6.QtDBus import QDBusConnection
            bus = QDBusConnection.sessionBus()
            self._bus = bus if bus.isConnected() else None
        except Exception:
            self._bus = None

    def update(self, progress: float | None, count: int) -> None:
        if self._bus is None:
            return
        state = (None if progress is None else round(progress, 3), count)
        if state == self._last:
            return
        self._last = state
        try:
            from PySide6.QtDBus import QDBusMessage
            msg = QDBusMessage.createSignal(PATH, "com.canonical.Unity.LauncherEntry", "Update")
            props = {"progress": float(progress or 0.0), "progress-visible": progress is not None and count > 0,
                     "count": int(count), "count-visible": count > 0}
            msg.setArguments([APP_URI, props])
            self._bus.send(msg)
        except Exception:
            log.debug("taskbar progress could not be sent", exc_info=True)
            self._bus = None

    def clear(self) -> None:
        self.update(None, 0)


def tray_menu(window) -> QMenu:
    """The tray icon's menu. ``window`` is the main window."""
    m = QMenu(window)
    show = m.addAction("Open EZ Games")
    show.triggered.connect(window.show_from_tray)
    couch = m.addAction("Couch mode")
    couch.triggered.connect(lambda: (window.show_from_tray(), window.open_couch()))
    m.addSeparator()
    pause = m.addAction("Pause all downloads")
    pause.triggered.connect(lambda: window.bridge.call(window.bridge.core.pause_all()))
    resume = m.addAction("Resume all downloads")
    resume.triggered.connect(lambda: window.bridge.call(window.bridge.core.resume_all()))
    m.addSeparator()
    quit_ = m.addAction("Quit")
    quit_.triggered.connect(window.quit_from_tray)
    return m


def tray_tooltip(name: str, s: dict) -> str:
    n = int(s.get("active") or 0) + int(s.get("queued") or 0)
    if not n:
        return name
    from ezgd.core.fsutil import format_speed
    parts = [f"{n} download{'s' if n != 1 else ''}", f"↓ {format_speed(s.get('speed') or 0)}"]
    if s.get("progress") is not None:
        parts.append(f"{s['progress'] * 100:.0f} %")
    if s.get("eta"):
        parts.append(f"{eta_text(s['eta'])} left")
    return f"{name} — " + " · ".join(parts)
