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
from pathlib import Path

log = logging.getLogger(__name__)

# the in-app browser (Chromium): draw pages and decode video on the graphics card, also on drivers Chromium is
# unsure about
CHROMIUM_FLAGS = ("--enable-gpu-rasterization", "--enable-zero-copy", "--ignore-gpu-blocklist",
                  "--enable-features=VaapiVideoDecoder,VaapiVideoDecodeLinuxGL")
MARKER = "gpu-start"                  # in the data folder while a GPU start is under way


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
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + [f for f in CHROMIUM_FLAGS if f not in flags])
    log.info("GPU acceleration on: couch mode and the in-app browser use the graphics card")
    return "on"


def started(data: Path) -> None:
    """The window is up: the GPU start worked."""
    try:
        (data / MARKER).unlink(missing_ok=True)
    except OSError:
        pass
