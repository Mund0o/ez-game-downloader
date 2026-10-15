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


GAME_SCRIPT = """
(function () {
    var pids = %(pids)s, best = null;
    var list = workspace.windowList ? workspace.windowList() : workspace.clientList();
    for (var i = 0; i < list.length; i++) {
        var w = list[i];
        if (pids.indexOf(w.pid) === -1 || !w.normalWindow) continue;
        if (best === null || w.fullScreen || (w.width * w.height > best.width * best.height && !best.fullScreen))
            best = w;
    }
    if (best !== null) {
        if (best.minimized) best.minimized = false;
        if (workspace.activeWindow !== undefined) workspace.activeWindow = best; else workspace.activeClient = best;
    }
})();
"""


STEAM_HIDE_SCRIPT = """
(function () {
    var list = workspace.windowList ? workspace.windowList() : workspace.clientList();
    var active = workspace.activeWindow !== undefined ? workspace.activeWindow : workspace.activeClient;
    for (var i = 0; i < list.length; i++) {
        var w = list[i], cls = String(w.resourceClass).toLowerCase(), name = String(w.resourceName).toLowerCase();
        var steam = cls === "steam" || cls === "steamwebhelper" || name === "steamwebhelper";
        if (steam && w.normalWindow && !w.minimized && (w === active || %(all)s)) w.minimized = true;
    }
})();
"""


STEAM_GUARD_SCRIPT = """
(function () {
    var t0 = Date.now();
    var last = workspace.activeWindow !== undefined ? workspace.activeWindow : workspace.activeClient;
    function isSteam(w) {
        if (!w) return false;
        var c = String(w.resourceClass).toLowerCase(), n = String(w.resourceName).toLowerCase();
        return c === "steam" || c === "steamwebhelper" || n === "steamwebhelper";
    }
    function activate(w) {
        if (workspace.activeWindow !== undefined) workspace.activeWindow = w; else workspace.activeClient = w;
    }
    function handle(w) {
        if (Date.now() - t0 > %(ms)d || !w) return;
        if (!isSteam(w)) { last = w; return; }
        if (String(w.caption).toLowerCase().indexOf("big picture") !== -1) w.closeWindow();   // Big Picture: closed
        else w.minimized = true;                                                            // Steam: to the back
        if (last && !isSteam(last) && !last.minimized) activate(last);
    }
    var sig = workspace.windowActivated !== undefined ? workspace.windowActivated : workspace.clientActivated;
    sig.connect(handle);
    if (isSteam(last)) { var was = last; last = null; handle(was); }
})();
"""


def guard_steam(ms: int = 3000) -> bool:
    """The guide button was pressed: for the next ``ms``, the moment Steam answers it with a window of its own
    (Steam Input), KWin puts it away — Big Picture closed, Steam's window minimized — and gives the screen straight
    back to the window that had it (the quick menu, couch mode or the game). Instant (KWin's own activation event, no
    polling). Only Steam's client windows; never a game. False without KWin."""
    if not WindowControl._kwin_available():
        return False
    return _run_kwin(STEAM_GUARD_SCRIPT % {"ms": int(ms)}, "guard against Steam's window", keep_ms=ms + 500)


def hide_steam_windows(only_active: bool = True) -> bool:
    """Steam answered the guide button by bringing its own window up (Steam Input on the controller): put that window
    away again. Only Steam's own client windows (class steam / steamwebhelper) — never a game started through Steam
    (its class is steam_app_…). False without KWin."""
    if not WindowControl._kwin_available():
        return False
    return _run_kwin(STEAM_HIDE_SCRIPT % {"all": "false" if only_active else "true"}, "hide Steam's window")


def bring_game_forward(pids: list[int]) -> bool:
    """Give the screen back to a game: its biggest (or full-screen) window comes to the front with the focus.
    Only windows of the game's own processes are touched. False when the compositor cannot do it (then the game is
    simply what is under the app's windows)."""
    if not pids or not WindowControl._kwin_available():
        return False
    import json
    return _run_kwin(GAME_SCRIPT % {"pids": json.dumps(sorted(int(p) for p in pids))}, "game")


def real_scale(screen, first=None) -> float:
    """How many real pixels one of Qt's pixels is on ``screen``. With fractional scaling (125 %, 150 %) Qt's screen
    may say 2 while it really is 1.5: the desktop's own answer comes first (see displays.py), then a window that is
    on that screen, then Qt's screen."""
    from PySide6.QtWidgets import QApplication
    from ezgd.ui import displays
    native = displays.native_size(screen)
    g = screen.geometry()
    if native and g.width() > 0 and g.height() > 0:
        landscape_q, landscape_n = g.width() >= g.height(), native[0] >= native[1]
        nw = native[0] if landscape_q == landscape_n else native[1]
        return max(0.5, nw / g.width())
    widgets = ([first] if first is not None else []) + [w for w in QApplication.topLevelWidgets() if w is not first]
    for w in widgets:
        try:
            if w.isVisible() and not w.isMinimized() and w.screen() is screen:
                return max(1.0, float(w.devicePixelRatioF()))
        except RuntimeError:
            continue
    return max(1.0, float(screen.devicePixelRatio()))


def real_size(screen) -> tuple[int, int]:
    """The screen's resolution in real pixels (what people know as their resolution)."""
    from ezgd.ui import displays
    native = displays.native_size(screen)
    g = screen.geometry()
    if native:
        return native if (native[0] >= native[1]) == (g.width() >= g.height()) else (native[1], native[0])
    k = real_scale(screen)
    return round(g.width() * k), round(g.height() * k)


RELEASE_SCRIPT = """
(function () {
    var pid = %(pid)d;
    var list = workspace.windowList ? workspace.windowList() : workspace.clientList();
    for (var i = 0; i < list.length; i++) if (list[i].pid === pid) list[i].keepAbove = false;
})();
"""


def release_all() -> None:
    """Couch mode has closed: none of the app's windows may stay above other windows (the desktop window, the
    in-app browser, …) — "keep above" is for couch mode only."""
    from PySide6.QtGui import QGuiApplication
    for w in QGuiApplication.topLevelWindows():
        try:
            if w.flags() & Qt.WindowStaysOnTopHint:
                w.setFlag(Qt.WindowStaysOnTopHint, False)
        except RuntimeError:
            continue
    if WindowControl._kwin_available():
        _run_kwin(RELEASE_SCRIPT % {"pid": os.getpid()}, "release keep-above")


class WindowControl:
    def __init__(self, widget, marker: str):
        self.widget = widget
        self.marker = marker                      # a unique part of the window title
        self.kwin = self._kwin_available()
        self._above: bool | None = None

    @staticmethod
    def _kwin_available() -> bool:
        if os.name == "nt" or QGuiApplication.platformName() not in ("wayland", "xcb"):
            return False                                   # (KWin takes these scripts on Wayland and on X11)
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

    def place(self, x: int, y: int, w: int, h: int) -> None:
        """A borderless window at exactly this spot (Wayland apps cannot position their own windows)."""
        if self.kwin:
            self._kwin(f"w.noBorder = true; w.frameGeometry = {{x: {x}, y: {y}, width: {w}, height: {h}}};")

    def fill_screen(self, screen: str = "") -> None:
        """A borderless window covering the whole monitor, measured by KWin itself (Qt's idea of a monitor's size
        can be wrong with fractional scaling)."""
        if self.kwin:
            self._kwin(self._find_output(screen) +
                       "var a = workspace.clientArea(KWin.FullScreenArea, o); w.noBorder = true; "
                       "w.frameGeometry = {x: a.x, y: a.y, width: a.width, height: a.height};")

    def force_fullscreen(self, screen: str = "") -> None:
        """Full screen that did not fill the monitor (some setups keep the window's old size): KWin makes it full
        screen on that monitor and sizes it to the monitor itself."""
        if self.kwin:
            self._kwin(self._find_output(screen) +
                       "var a = workspace.clientArea(KWin.FullScreenArea, o); w.fullScreen = false; "
                       "w.frameGeometry = {x: a.x, y: a.y, width: a.width, height: a.height}; w.fullScreen = true;")

    def place_pixels(self, pw: int, ph: int, screen: str = "") -> None:
        """A borderless window of pw×ph *real* pixels in the monitor's top-left corner (KWin knows the monitor's
        true scale: 1.5 at 150 %, where Qt may say 2)."""
        if self.kwin:
            self._kwin(self._find_output(screen) +
                       "var g = o.geometry, k = o.devicePixelRatio || 1; "
                       f"var cw = Math.min(g.width, Math.round({int(pw)} / k)), "
                       f"ch = Math.min(g.height, Math.round({int(ph)} / k)); "
                       "w.noBorder = true; w.frameGeometry = {x: g.x, y: g.y, width: cw, height: ch};")

    @staticmethod
    def _find_output(screen: str) -> str:
        """JavaScript that sets ``o`` to the monitor named ``screen`` (else the one the window is on)."""
        import json
        return ("var o = w.output; var outs = workspace.screens || []; "
                f"for (var j = 0; j < outs.length; j++) if (outs[j].name === {json.dumps(screen)}) o = outs[j]; ")

    def forget(self) -> None:
        """The window closes: nothing to undo (KWin forgets a closed window's state)."""
        self._above = None

    # --------------------------------------------------------------- kwin
    def _kwin(self, body: str) -> None:
        import json
        script = KWIN_SCRIPT % {"pid": os.getpid(), "marker": json.dumps(self.marker), "body": body}
        _run_kwin(script, body)


_kwin_n = 0


def _run_kwin(script: str, what: str, keep_ms: int = 0) -> bool:
    """Load, run and unload a one-shot KWin script (over D-Bus, never waiting more than 1.5 s per call).
    ``keep_ms``: the script keeps listening (to KWin's signals) that long before it is unloaded."""
    global _kwin_n
    _kwin_n += 1
    name = f"ezgd-couch-{os.getpid()}-{_kwin_n}"
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
        if keep_ms > 0:
            from PySide6.QtCore import QTimer

            def unload() -> None:
                try:
                    call("/Scripting", "org.kde.kwin.Scripting", "unloadScript", name)
                except Exception:
                    pass
            QTimer.singleShot(keep_ms, unload)
        else:
            call("/Scripting", "org.kde.kwin.Scripting", "unloadScript", name)
        log.debug("kwin: %s (%.0f ms)", what, (time.monotonic() - t) * 1000)
        return True
    except Exception:
        log.info("KWin did not take the window request", exc_info=True)
        return False
    finally:
        try:
            path.unlink()
        except OSError:
            pass
