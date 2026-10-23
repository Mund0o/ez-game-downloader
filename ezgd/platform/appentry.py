"""The app's launcher entry on Linux (``~/.local/share/applications/ezgd.desktop``), so it can be pinned to the
taskbar / dock and keeps its icon there while it is closed.

The window says it is "ezgd" (``QGuiApplication.setDesktopFileName``); the desktop finds the launcher of that name
for the icon and the command of a pinned entry. Run straight from its folder (no install.sh), the app had none: a
pinned entry had no icon once the app closed, and could not start it. Written at every start (the app may have
moved or been updated), with the icon the window itself shows — drawn in the accent colour — so pinned, open and
closed all look the same. A launcher someone else installed (install.sh, a distribution package) is left alone.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ezgd.platform import IS_LINUX

log = logging.getLogger(__name__)

APP_ID = "ezgd"
MANAGED = "X-EZGD-Managed=true"


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def entry_path() -> Path:
    return data_home() / "applications" / f"{APP_ID}.desktop"


def exec_quote(arg: str) -> str:
    """One argument for a desktop entry's Exec key (Desktop Entry Specification: double quotes, with ``"`` ``` ` ```
    ``$`` ``\\`` escaped inside them; ``%`` doubled). Single quotes are not part of the specification."""
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    out = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{out}"'


def exec_line(cmd: list[str]) -> str:
    return " ".join(exec_quote(c) for c in cmd)


def desktop_entry(cmd: list[str], icon: str) -> str:
    return ("[Desktop Entry]\nType=Application\nName=EZ Game Downloader\n"
            "Comment=Game catalog, torrent/HTTP download manager and library\n"
            f"Exec={exec_line(cmd)}\nIcon={icon}\nTerminal=false\n"
            "Categories=Game;\n"                         # (one main category: listed once in the menu)
            f"StartupWMClass={APP_ID}\nStartupNotify=true\n{MANAGED}\n")


def _ours_or_missing(path: Path) -> bool:
    """May the app write this launcher? Yes when there is none, or it is the app's own, or one whose program is
    gone (an old copy of the app that was deleted)."""
    if not path.is_file():
        return True
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return False
    if MANAGED in text:
        return True
    import shlex
    import shutil
    line = next((ln[5:] for ln in text.splitlines() if ln.startswith("Exec=")), "")
    try:
        prog = shlex.split(line)[0] if line else ""
    except ValueError:
        return False
    return bool(prog) and not (Path(prog).is_file() or shutil.which(prog))


def register(icon_png: bytes, accent: str, cmd: list[str] | None = None) -> bool:
    """Write the launcher and its icon (``icon_png``: the window's icon, drawn in ``accent``). True when written
    or already right; best effort — a failure is logged, never raised."""
    if not IS_LINUX:
        return False
    from ezgd.library import steamrun
    if steamrun.HOME:                       # a portable run (--home) keeps to its folder (see autostart.apply)
        log.debug("portable run: the desktop's launcher entry is left alone")
        return False
    try:
        path = entry_path()
        if not _ours_or_missing(path):
            log.debug("launcher entry %s belongs to an installation — left alone", path)
            return False
        if cmd is None:
            from ezgd.library import steamrun
            exe, args = steamrun.runner()
            cmd = [str(exe), *args]
        icons = data_home() / "icons"
        icon = icons / f"{APP_ID}-{accent or 'app'}.png"       # (a new name per colour: icon caches never mix)
        icons.mkdir(parents=True, exist_ok=True)
        if not icon.is_file() or icon.read_bytes() != icon_png:
            _write(icon, icon_png)
        for old in icons.glob(f"{APP_ID}-*.png"):
            if old != icon:
                old.unlink(missing_ok=True)
        text = desktop_entry(cmd, str(icon))
        if not path.is_file() or path.read_text("utf-8", "replace") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, text.encode("utf-8"))
            log.info("launcher entry written (the app can be pinned to the taskbar): %s", path)
        return True
    except OSError as exc:
        log.warning("launcher entry could not be written: %s", exc)
        return False


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
