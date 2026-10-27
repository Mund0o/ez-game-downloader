"""Shift+Tab over one of the app's games opens the quick menu (keyboard and mouse players).

While a game has the focus the app's windows get no keys, so the key is a KWin global shortcut — but only while it
is wanted: Shift+Tab is everyone's (reverse Tab in every app, Steam's overlay in Steam's games). So, while one of the
app's games runs (KDE Plasma only):

* a KWin script reports each window that takes the focus (its process) to the app over D-Bus;
* when that window belongs to one of the app's games without Steam's overlay (LibraryManager.quick_menu_key),
  a second script registers Shift+Tab, whose action calls the app back; when the focus goes elsewhere, it is
  unloaded and the shortcut unregistered from KDE's shortcut list (nothing stays in kglobalshortcutsrc).

Checked on KWin 6.8 (2026-10-27): a real Shift+Tab press reached the app; after unloading + ``unregister`` the
shortcut was gone and Shift+Tab was everyone's again.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import ClassInfo, QObject, QTimer, Slot

log = logging.getLogger(__name__)

PATH = "/EzgdQuickMenuKey"
IFACE = "org.ezgd.QuickMenuKey"
ACTION = "ezgd-quick-menu"
KEY = "Shift+Tab"

FOCUS_SCRIPT = """
function report(w) { callDBus(%(svc)s, "%(path)s", "%(iface)s", "Focus", w ? w.pid : 0); }
workspace.windowActivated.connect(report);
report(workspace.activeWindow);
"""

KEY_SCRIPT = """
registerShortcut("%(action)s", "EZ Game Downloader: quick menu over the game", "%(key)s", function() {
    callDBus(%(svc)s, "%(path)s", "%(iface)s", "Open");
});
"""


@ClassInfo({"D-Bus Interface": IFACE})
class QuickMenuKey(QObject):
    """``wanted(pid, answer)``: asks (asynchronously) whether Shift+Tab is the quick menu's in that process's window;
    ``on_open``: the quick menu over the game."""

    def __init__(self, wanted: Callable[[int, Callable[[bool], None]], None], on_open: Callable[[], None],
                 parent=None):
        super().__init__(parent)
        self._wanted = wanted
        self._on_open = on_open
        self._focus_script = ""
        self._key_script = ""
        self._registered = False
        self._focus_pid = 0
        self._n = 0

    # ----------------------------------------------------------- D-Bus (called by the KWin scripts)
    @Slot(int)
    def Focus(self, pid: int) -> None:                         # noqa: N802
        self._focus_pid = int(pid)

        def answer(yes: bool, pid=int(pid)) -> None:
            if pid != self._focus_pid or not self._focus_script:
                return                                         # (the focus moved on meanwhile, or stopped)
            self._arm() if yes else self._disarm()
        self._wanted(int(pid), answer)

    @Slot()
    def Open(self) -> None:                                    # noqa: N802
        log.info("Shift+Tab over the game: quick menu")
        QTimer.singleShot(0, self._on_open)

    # ----------------------------------------------------------- on / off
    @property
    def active(self) -> bool:
        return bool(self._focus_script)

    def start(self) -> None:
        """A game of the app runs: start following the focus (no-op without KWin, or when already on)."""
        if self._focus_script or os.environ.get("EZGD_QUICK_KEY", "1") == "0":
            return                                             # (tests: never shortcuts in the real desktop)
        from ezgd.ui.window_control import WindowControl
        if not WindowControl._kwin_available():
            return
        try:
            from PySide6.QtDBus import QDBusConnection
            bus = QDBusConnection.sessionBus()
            if not self._registered:
                self._registered = bus.registerObject(PATH, self, QDBusConnection.ExportAllSlots)
            if not self._registered:
                log.info("Shift+Tab quick menu: the D-Bus object could not be registered")
                return
            self._focus_script = self._load(FOCUS_SCRIPT, "focus")
        except Exception:
            log.info("Shift+Tab quick menu: not available", exc_info=True)

    def stop(self) -> None:
        """No game of the app runs (or the app quits): Shift+Tab is everyone's again."""
        self._disarm()
        if self._focus_script:
            self._unload(self._focus_script)
            self._focus_script = ""
        if self._registered:
            try:
                from PySide6.QtDBus import QDBusConnection
                QDBusConnection.sessionBus().unregisterObject(PATH)
            except Exception:
                pass
            self._registered = False

    def _arm(self) -> None:
        if not self._key_script:
            self._key_script = self._load(KEY_SCRIPT, "key")
            log.debug("Shift+Tab quick menu: on (a game of the app has the focus)")

    def _disarm(self) -> None:
        if not self._key_script:
            return
        self._unload(self._key_script)
        self._key_script = ""
        try:                                                   # (an unloaded script's shortcut stays registered)
            self._call("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel", "unregister", "kwin", ACTION)
        except Exception:
            pass
        log.debug("Shift+Tab quick menu: off")

    # ----------------------------------------------------------- KWin
    def _load(self, template: str, what: str) -> str:
        import json
        from PySide6.QtDBus import QDBusConnection
        self._n += 1
        name = f"ezgd-quickkey-{os.getpid()}-{what}-{self._n}"
        folder = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        path = folder / f"{name}.js"
        body = template % {"svc": json.dumps(QDBusConnection.sessionBus().baseService()), "path": PATH,
                           "iface": IFACE, "action": ACTION, "key": KEY}
        path.write_text(body, "utf-8")
        try:
            r = self._call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript", str(path), name)
            sid = r.arguments()[0] if r.arguments() else -1
            if not isinstance(sid, int) or sid < 0:
                return ""
            self._call("org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run")
            return name
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def _unload(self, name: str) -> None:
        try:
            self._call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", name)
        except Exception:
            pass

    @staticmethod
    def _call(service: str, obj: str, iface: str, method: str, *args):
        from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage
        msg = QDBusMessage.createMethodCall(service, obj, iface, method)
        if args:
            msg.setArguments(list(args))
        return QDBusConnection.sessionBus().call(msg, QDBus.Block, 1500)    # never hang the app on the compositor
