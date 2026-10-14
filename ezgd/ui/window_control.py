"""Keep couch mode in front: "keep above" other windows and bring it back to the front.

Wayland has no standard way for an app to stay on top or take the focus back, so on KDE Plasma this asks KWin
itself, through its scripting interface (a tiny script that only touches *this app's* window, found by its process
id and title, then unloads). Elsewhere (X11, Windows) Qt's stay-on-top flag and activation do the same.

Everything here is best effort: if the compositor does not answer, couch mode simply works as a normal full-screen
window.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

log = logging.getLogger(__name__)

KWIN_SCRIPT = """
(function () {
    var pid = %(pid)d, marker = %(marker)s;
    var list = workspace.windowList ? workspace.windowList() : workspace.clientList();
    for (var i = 0; i < list.length; i++) {
        var w = list[i];
        if (w.pid !== pid || String(w.caption).indexOf(marker) === -1) continue;
        %(body)s
    }
})();
"""


class WindowControl:
    def __init__(self, widget, marker: str):
        self.widget = widget
        self.marker = marker                      # a unique part of the window title
        self.kwin = self._kwin_available()
        self._above: bool | None = None
        self._n = 0

    @staticmethod
    def _kwin_available() -> bool:
        if os.name == "nt" or QGuiApplication.platformName() != "wayland":
            return False
        if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            return False
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusInterface
            return QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                                  QDBusConnection.sessionBus()).isValid()
        except Exception:
            return False

    # ---------------------------------------------------------------- api
    def keep_above(self, on: bool) -> None:
        """Stay above other windows (Steam's pop-ups, launchers…); a full-screen game still comes in front."""
        if self._above == on:
            return
        self._above = on
        if self.kwin:
            self._kwin(f"w.keepAbove = {'true' if on else 'false'};")
        else:
            handle = self.widget.windowHandle()
            if handle is not None:
                handle.setFlag(Qt.WindowStaysOnTopHint, on)

    def activate(self) -> None:
        """Bring the window to the front and give it the keyboard (also when Wayland would refuse the app)."""
        if self.kwin:
            self._kwin("if (workspace.activeWindow !== undefined) workspace.activeWindow = w; "
                       "else workspace.activeClient = w;")
        else:
            self.widget.raise_()
            self.widget.activateWindow()

    def forget(self) -> None:
        """The window closes: nothing to undo (KWin forgets a closed window's state)."""
        self._above = None

    # --------------------------------------------------------------- kwin
    def _kwin(self, body: str) -> None:
        import json
        script = KWIN_SCRIPT % {"pid": os.getpid(), "marker": json.dumps(self.marker), "body": body}
        self._n += 1
        name = f"ezgd-couch-{os.getpid()}-{self._n}"
        folder = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        path = folder / f"{name}.js"
        try:
            from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage
            path.write_text(script, "utf-8")
            bus = QDBusConnection.sessionBus()

            def call(obj: str, iface: str, method: str, *args):
                msg = QDBusMessage.createMethodCall("org.kde.KWin", obj, iface, method)
                if args:
                    msg.setArguments(list(args))
                return bus.call(msg, QDBus.Block, 1500)      # never hang the app on the compositor
            t = time.monotonic()
            r = call("/Scripting", "org.kde.kwin.Scripting", "loadScript", str(path), name)
            sid = r.arguments()[0] if r.arguments() else -1
            if isinstance(sid, int) and sid >= 0:
                call(f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run")
            call("/Scripting", "org.kde.kwin.Scripting", "unloadScript", name)
            log.debug("kwin: %s (%.0f ms)", body, (time.monotonic() - t) * 1000)
        except Exception:
            log.info("KWin did not take the window request", exc_info=True)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
