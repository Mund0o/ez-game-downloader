"""Steam's Big Picture popping up over couch mode.

While Steam runs, it owns Steam controllers (and others with Steam Input): the Steam / PS / Xbox button opens Big
Picture, and the app never sees the press. When that happens while couch mode is in front (no game starting or
being played), couch mode asks Steam to close Big Picture again (``steam://close/bigpicture`` — Steam's own URL; Steam
itself keeps running) and takes the screen back: the button then just means "Home".

Steam is an X11 program (Xwayland on Wayland), so the active Steam window is found with ``xprop``.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

from ezgd.platform.hostenv import host_env

log = logging.getLogger(__name__)

TITLES = ("Steam Big Picture Mode", "Big Picture")


def _xprop(*args: str) -> str:
    if not shutil.which("xprop"):
        return ""
    try:
        return subprocess.run(["xprop", *args], capture_output=True, text=True, timeout=1.5, env=host_env()).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def active_x_title() -> str:
    """The title of the active X11 window ("" when a Wayland window, or nothing, is active)."""
    m = re.search(r"window id # (0x[0-9a-f]+)", _xprop("-root", "_NET_ACTIVE_WINDOW"))
    if not m or int(m.group(1), 16) == 0:
        return ""
    t = re.search(r'(?:_NET_WM_NAME|WM_NAME)\([^)]*\) = "(.*)"', _xprop("-id", m.group(1), "_NET_WM_NAME", "WM_NAME"))
    return t.group(1) if t else ""


_seen = (0.0, False)                                    # (when, answer) of the last look at the processes


def steam_running() -> bool:
    """Is Steam running? Its pid file (~/.steam/steam.pid) — no process listing needed. Flatpak's and Snap's Steam
    keep that file inside their own home (Flatpak's even holds a process id of its own sandbox), so with those
    installed the processes are looked at instead (named "steam", at most every 3 seconds): the Steam button and
    Steam's keys were never read there."""
    import os
    import time
    from pathlib import Path
    forced = os.environ.get("EZGD_STEAM_ROOT")
    if forced and not Path(forced).is_dir():
        return False                                    # (the tests: never this computer's real Steam)
    if os.name == "nt":
        return False
    home = Path.home()
    try:
        pid = int((home / ".steam" / "steam.pid").read_text().strip())
        if pid > 0 and Path(f"/proc/{pid}").exists():
            return True
    except (OSError, ValueError):
        pass
    if forced or not any((home / d).is_dir() for d in (".var/app/com.valvesoftware.Steam", "snap/steam")):
        return False
    global _seen
    now = time.monotonic()
    if now - _seen[0] >= 3.0:
        _seen = (now, _steam_process())
    return _seen[1]


def _steam_process(proc: str = "/proc") -> bool:
    import os
    for name in os.listdir(proc):
        if name.isdigit():
            try:
                with open(f"{proc}/{name}/comm", encoding="utf-8") as f:
                    if f.read().strip() == "steam":
                        return True
            except OSError:
                continue
    return False


def big_picture_in_front() -> bool:
    title = active_x_title()
    return any(t.lower() in title.lower() for t in TITLES)


def close_big_picture() -> bool:
    """Close Big Picture (Steam's own ``steam://close/bigpicture``; Steam keeps running, it is never restarted) and
    keep Steam in the background: leaving Big Picture makes Steam show its desktop window, which is minimized again
    as it appears."""
    from ezgd.platform.steam import steam_command
    from ezgd.ui.window_control import hide_steam_windows
    steam = steam_command()                               # (Flatpak's Steam too: "flatpak run …")
    if not steam:
        return hide_steam_windows(only_active=True)
    try:
        subprocess.Popen(steam + ["steam://close/bigpicture"], env=host_env(), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return hide_steam_windows(only_active=True)
    log.info("couch mode: Steam's Big Picture came up over couch mode (Steam button) — closed it, Steam stays back")
    from PySide6.QtCore import QTimer
    for ms in (600, 1400, 2800):                          # (Steam's desktop window shows up a moment later)
        QTimer.singleShot(ms, lambda: hide_steam_windows(only_active=False))
    return True
