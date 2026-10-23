"""Test starts nobody sees: on KDE Plasma, a test game's windows are moved beyond every monitor.

The debugging assistant starts games to test fixes. A KWin script, loaded before the test starts, takes every
Proton game window that appears afterwards (``steam_app_<id>``, ``steam_proton``): it makes it transparent first,
keeps it off the taskbar, Alt+Tab and the pager, below everything, out of full screen — then moves it beyond every
monitor and only there makes it opaque again, so KWin still draws it (the game keeps running at full speed: Expedition
33 reached its menu as fast as when shown) and its picture can be taken. If the game moves itself back (going full
screen again), it is made transparent at once and moved away again. If it is activated, the window that was active
before is activated again: keyboard and mouse stay with the user.

The script reports each window it hides in the user journal (``print`` → kwin_wayland); the app reads that back to
know the window's id, checks the process belongs to the test (a Steam game the user starts at the same moment is put
back), mutes its sound and takes its picture with KWin's ScreenShot2 ``CaptureWindow`` (through ``busctl``, which
can hand KWin the pipe the picture is written to).

Nothing to install: KWin, journald, busctl (systemd) and pactl are part of the desktop. Elsewhere tests are shown
as before.
"""

from __future__ import annotations

import json
import logging
import os
import select
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SCRIPT = r"""
const marker = "%(marker)s";
const names = %(names)s;
const before = new Set(workspace.windowList().map(w => w.internalId.toString()));
const revealed = new Set();
let previous = workspace.activeWindow;
const busy = new Set();
function ours(w) {
    if (!w || before.has(w.internalId.toString()) || revealed.has(w.internalId.toString())) return false;
    const rc = (w.resourceClass || "").toLowerCase();
    return rc.startsWith("steam_app_") || rc === "steam_proton" || names.indexOf(rc) >= 0;
}
function onScreen(g) {
    for (const o of workspace.screens) {
        const a = o.geometry;
        if (g.x < a.x + a.width && g.x + g.width > a.x && g.y < a.y + a.height && g.y + g.height > a.y) return true;
    }
    return false;
}
function place(w) {
    const id = w.internalId.toString();
    if (Math.abs(w.opacity - 0.999) < 0.0005) revealed.add(id);     // put back by the app: left alone
    if (busy.has(id) || revealed.has(id)) return;
    busy.add(id);
    w.opacity = 0.0;
    w.skipTaskbar = true; w.skipSwitcher = true; w.skipPager = true; w.keepAbove = false; w.keepBelow = true;
    if (w.fullScreen) w.fullScreen = false;
    const g = w.frameGeometry;
    if (onScreen(g)) w.frameGeometry = {x: 60000, y: 60000, width: g.width, height: g.height};
    const away = !onScreen(w.frameGeometry);
    w.opacity = away ? 1.0 : 0.0;
    print(marker + " " + w.pid + " " + id + " " + (w.resourceClass || "-") + " " + (away ? "away" : "transparent"));
    busy.delete(id);
}
function hide(w) {
    if (!ours(w)) return;
    place(w);
    w.frameGeometryChanged.connect(function () { if (!revealed.has(w.internalId.toString())) place(w); });
    w.fullScreenChanged.connect(function () { if (!revealed.has(w.internalId.toString())) place(w); });
}
workspace.windowAdded.connect(hide);
workspace.windowActivated.connect(function (w) {
    if (ours(w)) { if (previous && previous !== w) workspace.activeWindow = previous; }
    else if (w) previous = w;
});
"""

_REVEAL = r"""
for (const w of workspace.windowList()) {
    if (w.internalId.toString() === "%(id)s") {
        w.opacity = 0.999;                              // (the hiding script's sign to leave it alone)
        w.skipTaskbar = false; w.skipSwitcher = false; w.skipPager = false; w.keepBelow = false;
        const a = workspace.clientArea(KWin.PlacementArea, workspace.activeScreen, workspace.currentDesktop);
        w.frameGeometry = {x: a.x + 40, y: a.y + 40, width: w.frameGeometry.width, height: w.frameGeometry.height};
    }
}
"""

_available: bool | None = None


def _bus(*args: str, timeout: float = 5) -> str | None:
    try:
        r = subprocess.run(["busctl", "--user", "call", "org.kde.KWin", *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def available() -> bool:
    """Can tests run hidden here? (KDE Plasma with KWin scripting and window pictures; checked once.)"""
    global _available
    if os.environ.get("EZGD_HIDDEN") == "0":
        return False
    if _available is None:
        _available = bool(
            "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and shutil.which("busctl")
            and shutil.which("journalctl")
            and _bus("/org/kde/KWin/ScreenShot2", "org.freedesktop.DBus.Properties", "Get", "ss",
                     "org.kde.KWin.ScreenShot2", "Version") is not None
            and _bus("/Scripting", "org.freedesktop.DBus.Introspectable", "Introspect") is not None)
    return _available


def _load(script: str, name: str) -> bool:
    fd, path = tempfile.mkstemp(prefix="ezgd-kwin-", suffix=".js")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        _bus("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", name)
        out = _bus("/Scripting", "org.kde.kwin.Scripting", "loadScript", "ss", path, name)
        if not out:
            return False
        sid = out.split()[-1]
        return _bus(f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run") is not None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class HiddenSession:
    """One hidden test: start() before the game is started, stop() after it ended."""

    def __init__(self, names: list[str] | None = None):
        self.marker = "EZGD-HIDE-" + secrets.token_hex(4)
        self.name = "ezgd-" + self.marker.lower()
        self.names = [n.lower() for n in names or []]
        self.since = ""
        self.active = False
        self._seen: dict[str, dict] = {}
        self._revealed: set[str] = set()
        self._muted: set[int] = set()

    def start(self) -> bool:
        self.since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
        self.active = _load(_SCRIPT % {"marker": self.marker, "names": json.dumps(self.names)}, self.name)
        if not self.active:
            log.info("hidden test: the KWin script did not load — the test is shown")
        return self.active

    def windows(self) -> list[dict]:
        """The windows hidden so far: {"pid", "id", "class", "away"}."""
        if not self.active:
            return []
        try:
            out = subprocess.run(["journalctl", "--user", "--since", self.since, "-o", "cat", "--no-pager"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return list(self._seen.values())
        for ln in out.splitlines():
            if not ln.startswith(self.marker + " "):
                continue
            parts = ln.split()
            if len(parts) >= 5:
                try:
                    self._seen[parts[2]] = {"pid": int(parts[1]), "id": parts[2], "class": parts[3],
                                            "away": parts[4] == "away"}
                except ValueError:
                    continue
        return [w for w in self._seen.values() if w["id"] not in self._revealed]

    def check(self, ours: set[int]) -> list[dict]:
        """Put back any hidden window that is not the test's (a game the user started at the same moment);
        returns the test's own windows."""
        mine = []
        for w in self.windows():
            if ours and w["pid"] > 0 and w["pid"] not in ours:
                self.reveal(w["id"])
                log.info("hidden test: window of process %s is not the test's — shown again", w["pid"])
                continue
            mine.append(w)
        return mine

    def reveal(self, window_id: str) -> None:
        self._revealed.add(window_id)
        _load(_REVEAL % {"id": window_id.replace('"', "")}, self.name + "-reveal")
        _bus("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", self.name + "-reveal")

    def mute(self, pids: set[int]) -> None:
        """No sound from the test (its streams found by process, muted — nothing else on the computer)."""
        if not pids or not shutil.which("pactl"):
            return
        try:
            from ezgd.platform.hostenv import host_env
            out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True, text=True,
                                 timeout=3, env=host_env()).stdout
            streams = json.loads(out or "[]")
        except (OSError, ValueError, subprocess.SubprocessError):
            return
        for st in streams:
            try:
                pid = int((st.get("properties") or {}).get("application.process.id") or 0)
            except ValueError:
                continue
            if pid in pids and st.get("index") not in self._muted:
                subprocess.run(["pactl", "set-sink-input-mute", str(st["index"]), "1"], capture_output=True,
                               timeout=3, env=host_env())
                self._muted.add(st.get("index"))

    def stop(self) -> None:
        if self.active:
            _bus("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", self.name)
            self.active = False


def capture(window_id: str, timeout: float = 8.0):
    """The picture of one window (a QImage), drawn by KWin even though it is off-screen; None when not possible."""
    return _screenshot("CaptureWindow", "sa{sv}h", [window_id], timeout)


def capture_screen(name: str | None = None, timeout: float = 1.0):
    """What a monitor shows right now (a QImage; ``name``: its connector, e.g. "eDP-1" — else the monitor with the
    active window); None when KWin cannot (another desktop, or it refused)."""
    if os.environ.get("EZGD_HIDDEN", "1") == "0" or not shutil.which("busctl") or \
            "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return None                              # (tests: never the real screen)
    img = _screenshot("CaptureScreen", "sa{sv}h", [name], timeout) if name else None
    return img if img is not None else _screenshot("CaptureActiveScreen", "a{sv}h", [], timeout)


def _screenshot(method: str, signature: str, args: list[str], timeout: float):
    """One KWin ScreenShot2 call (through busctl: PySide cannot read its a{sv} answer) → a QImage, or None."""
    from PySide6.QtGui import QImage
    r, w = os.pipe()
    try:
        p = subprocess.Popen(["busctl", "--user", "--json=short", "call", "org.kde.KWin", "/org/kde/KWin/ScreenShot2",
                              "org.kde.KWin.ScreenShot2", method, signature, *args, "1",
                              "native-resolution", "b", "true", str(w)], pass_fds=(w,), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    except OSError:
        os.close(r)
        os.close(w)
        return None
    os.close(w)
    data = bytearray()
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            if select.select([r], [], [], 0.5)[0]:
                chunk = os.read(r, 1 << 20)
                if not chunk:
                    break
                data += chunk
        out, _err = p.communicate(timeout=5)
    except (OSError, subprocess.SubprocessError):
        p.kill()
        return None
    finally:
        os.close(r)
    if p.returncode != 0 or not data:
        return None
    try:
        meta = {k: v["data"] for k, v in json.loads(out)["data"][0].items()}
        img = QImage(bytes(data), int(meta["width"]), int(meta["height"]), int(meta["stride"]),
                     QImage.Format(int(meta["format"])))
        return img.copy()
    except (ValueError, KeyError, IndexError, TypeError):
        return None
