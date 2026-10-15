"""Reading what is on the screen while the debugging assistant tests a game — for errors Wine knows nothing about.

Wine logs the text of real Windows message boxes (ezgd.library.errorbox), but many games draw their own error
screens (Unreal's "Fatal error" window, a launcher's "Failed to initialise…", a black screen with "Your GPU is not
supported"). During a diagnostic launch or a test run, the game's window is photographed now and then
(KDE's Spectacle, grim on wlroots desktops, ImageMagick on X11) and its text read with Tesseract. Lines that sound
like an error are written into the game's log (``ezgd-screen: …``), where the assistant reads them like any other
evidence.

Only while a test the user started is running, and only when the game — not this app — has the focus. The pictures
are deleted right away; nothing leaves the computer.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "ezgd-screen: "
ERROR_WORDS = re.compile(
    r"\b(error|errors|failed|failure|fatal|crash(ed)?|exception|cannot|can't|could ?n[o']t|unable|not supported|"
    r"unsupported|missing|not found|invalid|denied|corrupt(ed)?|requires?|required|out of memory|license|"
    r"activation|not responding|access violation|assertion|D3D|DirectX|Vulkan|driver|GPU)\b", re.I)

# words that mean the game has stopped on an error (settings screens also say "GPU" or "DirectX": not enough)
STRONG = re.compile(r"\b(error|failed|failure|fatal|crash(ed)?|exception|unable|cannot|can't|could ?n[o']t|"
                    r"not supported|unsupported|access violation|out of memory)\b", re.I)

_app_focused = threading.Event()          # set by the window layer while one of the app's own windows is active


def set_app_focused(on: bool) -> None:
    (_app_focused.set if on else _app_focused.clear)()


def available() -> bool:
    return bool(shutil.which("tesseract")) and _capture_command(Path("x.png")) is not None


def _capture_command(out: Path) -> list[str] | None:
    """The active window (or the screen) as a PNG, without any prompt or sound."""
    if shutil.which("spectacle") and "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return ["spectacle", "-b", "-n", "-a", "-o", str(out)]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("grim"):
        return ["grim", str(out)]
    if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY") and shutil.which("import"):
        return ["import", "-window", "root", str(out)]
    return None


def game_window_active() -> bool:
    """Is the window in front a Windows program's (Wine / Proton)? Checked through XWayland — Proton games are X11
    windows. Anything else in front (a browser, an editor — once it photographed a chat window, 2026-10-14) is never
    photographed; without a way to check, nothing is."""
    if not shutil.which("xprop") or not os.environ.get("DISPLAY"):
        return False
    try:
        r = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True, text=True, timeout=5)
        m = re.search(r"window id # (0x[0-9a-f]+)", r.stdout)
        if not m or int(m.group(1), 16) == 0:
            return False
        r = subprocess.run(["xprop", "-id", m.group(1), "_NET_WM_PID", "WM_CLASS"], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    pid = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", r.stdout)
    if pid:
        try:
            cmd = Path(f"/proc/{pid.group(1)}/cmdline").read_bytes().decode("utf-8", "replace").lower()
        except OSError:
            cmd = ""
        if ".exe" in cmd:
            return True
    return bool(re.search(r'WM_CLASS\(STRING\) = .*("steam_proton"|\.exe")', r.stdout, re.I))


def read_screen(timeout: float = 20.0) -> str | None:
    """The text on the game's window, or None when it cannot be read (no tools, the app has the focus, or the
    window in front is not the game's)."""
    if _app_focused.is_set() or not shutil.which("tesseract") or not game_window_active():
        return None
    fd, name = tempfile.mkstemp(prefix="ezgd-screen-", suffix=".png")
    os.close(fd)
    out = Path(name)
    try:
        cmd = _capture_command(out)
        if cmd is None:
            return None
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 100:
            return None
        r = subprocess.run(["tesseract", str(out), "stdout", "--psm", "3"], capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def error_lines(text: str | None, limit: int = 6) -> list[str]:
    """The lines of recognised text that sound like an error (OCR noise left out)."""
    out: list[str] = []
    for ln in (text or "").splitlines():
        ln = " ".join(ln.split())
        letters = sum(ch.isalpha() for ch in ln)
        if len(ln) < 8 or letters < len(ln) * 0.6 or not ERROR_WORDS.search(ln):
            continue
        if ln not in out:
            out.append(ln[:240])
            if len(out) >= limit:
                break
    return out


def look(log_path: Path) -> list[str]:
    """Photograph the game's window, write error-like text into its log; returns those lines."""
    lines = error_lines(read_screen())
    if lines:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("".join(PREFIX + ln + "\n" for ln in lines))
        except OSError:
            pass
        log.info("the game's screen shows: %s", " | ".join(lines))
    return lines


def stopped_on_error(lines: list[str]) -> bool:
    return any(STRONG.search(ln) for ln in lines)


def from_log(text: str, limit: int = 8) -> list[str]:
    """What earlier looks found (the ``ezgd-screen:`` lines of a run's log)."""
    out: list[str] = []
    for ln in (text or "").splitlines():
        if ln.startswith(PREFIX):
            v = ln[len(PREFIX):].strip()
            if v and v not in out:
                out.append(v)
                if len(out) >= limit:
                    break
    return out
