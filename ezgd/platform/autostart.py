"""Start the app with the desktop session, in the tray (``--minimized``): downloads finish, the wishlist and game
updates are checked without opening the window.

Linux: an XDG autostart entry (``~/.config/autostart/ezgd.desktop`` — KDE, GNOME, Xfce … all read it).
Windows: the per-user ``Run`` key. The entry is rewritten at every start while the setting is on, so it keeps
pointing at the app after it was moved or updated."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ezgd.platform import IS_WINDOWS

log = logging.getLogger(__name__)

NAME = "EZGameDownloader"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def entry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "ezgd.desktop"


def command() -> list[str]:
    """How this app is started (the packaged program, or Python with ``-m ezgd``), in the tray."""
    from ezgd.library import steamrun
    exe, args = steamrun.runner()
    return [str(exe), *args, "--minimized"]


def desktop_entry(cmd: list[str]) -> str:
    # (double quotes, as the Desktop Entry Specification has them — shlex's single quotes are not part of it, and
    # the app's folder may have spaces in its name)
    from ezgd.platform.appentry import exec_line
    return ("[Desktop Entry]\nType=Application\nName=EZ Game Downloader\n"
            "Comment=Finishes downloads and checks for game updates in the tray\n"
            f"Exec={exec_line(cmd)}\nIcon=ezgd\nTerminal=false\n"
            "X-GNOME-Autostart-enabled=true\nX-KDE-autostart-after=panel\n")


def enabled() -> bool:
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, NAME)
                return True
        except OSError:
            return False
    return entry_path().is_file()


def apply(on: bool) -> None:
    """Add or remove the autostart entry (best effort: a failure is logged, never raised)."""
    from ezgd.library import steamrun
    if steamrun.HOME:
        # a portable run (--home) keeps to its folder: its entry would start the desktop's app with that folder's
        # data (a scratch copy's test runs once left the real app starting a copy, hidden in the tray)
        log.debug("portable run: the desktop's autostart entry is left alone")
        return
    try:
        if IS_WINDOWS:
            import subprocess
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(command()))
                else:
                    try:
                        winreg.DeleteValue(k, NAME)
                    except FileNotFoundError:
                        pass
            return
        path = entry_path()
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = desktop_entry(command())
            if not path.is_file() or path.read_text(encoding="utf-8", errors="replace") != text:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
                log.info("autostart entry written: %s", path)
        elif path.is_file():
            path.unlink()
            log.info("autostart entry removed")
    except OSError as exc:
        log.warning("autostart could not be %s: %s", "set up" if on else "removed", exc)
