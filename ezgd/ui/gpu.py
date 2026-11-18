"""GPU acceleration (Settings → GPU acceleration): the parts that must be chosen before the QApplication exists.

Couch mode reads the setting itself each time it opens (see couch.make_couch). The in-app browser can only be
switched when the app starts, so this runs first and reads the setting straight from the
database (read-only: the core has not opened it yet).

If a start with GPU acceleration does not get as far as showing the window (a driver crash), the next start
leaves it out and says so — the switch can never lock anyone out of the app."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Windows / opt-in browser GPU flags (EZGD_BROWSER_GPU=1 on Linux).
CHROMIUM_FLAGS = ("--enable-features=VaapiVideoDecoder",)
# On Linux, Qt WebEngine 6.12 fails GBM on Wayland/hybrid laptops ("GBM is not supported with the current
# configuration. Fallback to Vulkan rendering in Chromium."), falls back to Vulkan while QQuickWidget runs OpenGL RHI
# on the main thread, and recreates MainWindow's native Wayland QWindow mid-exec() — crashing with SIGSEGV
# (EZGD-SEGV-F117EA). Software Qt Quick + Chromium CPU rasterization renders the download pop-up cleanly over wl_shm
# while leaving Couch Mode's hardware OpenGL window (CouchGLWindow) untouched.
LINUX_BROWSER_FLAGS = ("--disable-gpu", "--disable-gpu-compositing", "--disable-features=Vulkan")
MARKER = "gpu-start"                  # in the data folder while a GPU start is under way


def configure_browser(env=os.environ, platform: str | None = None) -> None:
    """Configure Qt Quick / Qt WebEngine flags before QApplication starts so the Linux pop-up browser never hits the
    broken Wayland GBM → Vulkan → QQuickWidget RHI path."""
    plat = sys.platform if platform is None else platform
    if not plat.startswith("linux") or env.get("EZGD_BROWSER_GPU") == "1":
        return
    env.setdefault("QT_QUICK_BACKEND", "software")
    env.setdefault("QTWEBENGINE_FORCE_USE_GBM", "0")
    flags = [f for f in env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
             if not f.startswith("--enable-features=Vaapi")]
    for f in LINUX_BROWSER_FLAGS:
        if f not in flags:
            flags.append(f)
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)


def stored_setting(database: Path) -> bool:
    """The saved "gpu_acceleration" value (False when unset or unreadable)."""
    if not database.exists():
        return False
    try:
        db = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            row = db.execute("SELECT value FROM settings WHERE key = 'gpu_acceleration'").fetchone()
        finally:
            db.close()
        return bool(row) and json.loads(row[0]) is True
    except (sqlite3.Error, ValueError):
        return False


def apply_at_startup(data: Path, database: Path, env=os.environ) -> str:
    """Set Qt's and Chromium's GPU switches when the setting is on. Returns what happened: "off", "on", or
    "skipped" (the last GPU start did not finish)."""
    if not stored_setting(database):
        return "off"
    marker = data / MARKER
    if marker.exists():
        try:
            marker.unlink()
        except OSError:
            pass
        log.warning("GPU acceleration: the last start with it did not finish; starting without it this time")
        return "skipped"
    try:
        marker.write_text(str(os.getpid()))
    except OSError:
        pass
    # (Not QT_WIDGETS_RHI for every desktop window: those are painted by the CPU anyway, and composed through
    # OpenGL on Wayland a menu's first frame could show one item blown up across the whole menu until the mouse
    # moved. Couch mode's OpenGL window and the browser use the GPU without it.)
    if not sys.platform.startswith("linux") or env.get("EZGD_BROWSER_GPU") == "1":
        flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + [f for f in CHROMIUM_FLAGS if f not in flags])
        log.info("GPU acceleration on: couch mode and the in-app browser use the graphics card")
    else:
        configure_browser(env)
        log.info("GPU acceleration on: couch mode uses OpenGL; in-app browser uses software rendering on Linux")
    return "on"


def started(data: Path) -> None:
    """The window is up: the GPU start worked."""
    try:
        (data / MARKER).unlink(missing_ok=True)
    except OSError:
        pass
