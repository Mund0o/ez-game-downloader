"""The monitors' real resolution (their current mode in real pixels), asked of the desktop itself.

Qt's own numbers can be off with fractional scaling: at 150 % some setups report a scale of 2 (Qt's screens only
know whole numbers there, or the desktop draws everything at 2× and shrinks it), so a 3840×2160 monitor showed up as
5120×2880. The desktop knows the truth:

* KDE Plasma — ``kscreen-doctor -j`` (Wayland and X11),
* GNOME — Mutter's DisplayConfig over D-Bus (``gdbus``),
* other X11 desktops — ``xrandr`` (the mode marked current).

The answers are cached and refreshed in the background (at start and when screens change), so nothing here ever
holds up the window. Unknown → None, and callers fall back to Qt's numbers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger(__name__)

_modes: dict[str, tuple[int, int]] = {}      # output name → (width, height) in real pixels
_source = ""
_at = 0.0
_busy = threading.Lock()
MAX_AGE = 30.0


def _run(cmd: list[str]) -> str:
    try:
        from ezgd.platform.hostenv import host_env
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3, env=host_env()).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def from_kscreen(text: str) -> dict[str, tuple[int, int]]:
    out = {}
    try:
        data = json.loads(text)
    except ValueError:
        return out
    for o in data.get("outputs") or []:
        if not o.get("enabled", True):
            continue
        cur = o.get("currentModeId")
        mode = next((m for m in o.get("modes") or [] if str(m.get("id")) == str(cur)), None)
        size = (mode or {}).get("size") or o.get("size") or {}
        w, h = int(size.get("width") or 0), int(size.get("height") or 0)
        if o.get("name") and w > 0 and h > 0:
            rot = int(o.get("rotation") or 1)
            out[str(o["name"])] = (h, w) if rot in (2, 8) else (w, h)        # turned 90° / 270°
    return out


def from_mutter(text: str) -> dict[str, tuple[int, int]]:
    """GetCurrentState's printed reply: each monitor's connector and the mode marked 'is-current'."""
    out = {}
    starts = list(re.finditer(r"\(\('(?P<conn>[^']+)', '[^']*', '[^']*', '[^']*'\), \[", text))
    for k, m in enumerate(starts):
        part = text[m.end():starts[k + 1].start() if k + 1 < len(starts) else len(text)]
        for mode in re.finditer(r"\('[^']*', (\d+), (\d+), [\d.]+, [\d.]+, \[[^\]]*\], \{([^}]*)\}\)", part):
            if "'is-current': <true>" in mode.group(3):
                out[m.group("conn")] = (int(mode.group(1)), int(mode.group(2)))
                break
    return out


def from_xrandr(text: str) -> dict[str, tuple[int, int]]:
    out, name = {}, None
    for line in text.splitlines():
        head = re.match(r"(\S+) connected", line)
        if head:
            name = head.group(1)
            continue
        if not line.startswith((" ", "\t")):
            name = None
            continue
        mode = re.match(r"\s+(\d+)x(\d+)\S*\s+.*\*", line)
        if name and mode and name not in out:
            out[name] = (int(mode.group(1)), int(mode.group(2)))
    return out


def query() -> tuple[dict[str, tuple[int, int]], str]:
    """Ask the desktop (blocking: up to a few seconds — use refresh() from the UI)."""
    if os.name == "nt":
        return {}, ""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if shutil.which("kscreen-doctor") and ("KDE" in desktop or not desktop):
        modes = from_kscreen(_run(["kscreen-doctor", "-j"]))
        if modes:
            return modes, "kscreen-doctor"
    if shutil.which("gdbus") and ("GNOME" in desktop or "UNITY" in desktop or "BUDGIE" in desktop):
        modes = from_mutter(_run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
                                  "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method",
                                  "org.gnome.Mutter.DisplayConfig.GetCurrentState"]))
        if modes:
            return modes, "mutter"
    if os.environ.get("XDG_SESSION_TYPE") == "x11" and shutil.which("xrandr"):
        modes = from_xrandr(_run(["xrandr", "--current"]))
        if modes:
            return modes, "xrandr"
    return {}, ""


def refresh(wait: bool = False) -> None:
    """Ask again in the background (or now, with ``wait``)."""
    def work() -> None:
        global _modes, _source, _at
        if not _busy.acquire(blocking=False):
            return
        try:
            modes, source = query()
            if modes != _modes or not _at:
                log.info("monitors (%s): %s", source or "unknown", ", ".join(f"{k} {w}x{h}" for k, (w, h) in
                                                                            sorted(modes.items())) or "none found")
            _modes, _source, _at = modes, source, time.monotonic()
        finally:
            _busy.release()
    if wait:
        work()
    else:
        threading.Thread(target=work, daemon=True, name="displays").start()


def watch(app) -> None:
    """Keep the answers fresh: now, and whenever a screen is added, removed or changes."""
    refresh()

    def hook(screen) -> None:
        screen.geometryChanged.connect(lambda _g: refresh())
    for sc in app.screens():
        hook(sc)
    app.screenAdded.connect(lambda sc: (hook(sc), refresh()))
    app.screenRemoved.connect(lambda _sc: refresh())


def native_size(screen) -> tuple[int, int] | None:
    """``screen``'s current mode in real pixels, if the desktop said (None: not known yet / not available)."""
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() in ("offscreen", "minimal") and not os.environ.get("EZGD_DISPLAYS"):
        return None                                      # Qt's test screens are not the desktop's monitors
    if time.monotonic() - _at > MAX_AGE:
        refresh()
    if not _modes or screen is None:
        return None
    size = _modes.get(screen.name())
    if size is None and len(_modes) == 1:
        if len(QGuiApplication.screens()) == 1:          # one monitor: names may differ (DP-1 vs DP1)
            size = next(iter(_modes.values()))
    return size
