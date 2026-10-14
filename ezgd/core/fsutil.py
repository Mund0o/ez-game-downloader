"""File-system helpers that treat every externally supplied name as untrusted."""

from __future__ import annotations

import errno
import os
import re
import shutil
import unicodedata
from pathlib import Path

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
MAX_NAME_BYTES = 200


class InsufficientSpaceError(OSError):
    def __init__(self, path: Path, needed: int, free: int):
        super().__init__(errno.ENOSPC, f"Not enough free space in {path}: need {format_bytes(needed)}, "
                                       f"{format_bytes(free)} available")
        self.needed = needed
        self.free = free


def safe_filename(name: str | None, fallback: str = "download") -> str:
    """Normalise a remote-supplied file name into a single safe path component."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]           # drop any directory part
    name = _BAD_CHARS.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name or name in {".", ".."}:
        name = fallback
    stem, dot, ext = name.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    encoded = name.encode("utf-8")
    if len(encoded) > MAX_NAME_BYTES:
        root, ext = os.path.splitext(name)
        ext = ext[:16]
        budget = MAX_NAME_BYTES - len(ext.encode("utf-8"))
        root = root.encode("utf-8")[:budget].decode("utf-8", "ignore")
        name = root + ext
    return name


def safe_dirname(name: str | None, fallback: str = "game") -> str:
    return safe_filename(name, fallback).rstrip(". ") or fallback


def is_within(path: Path | str, root: Path | str) -> bool:
    """True if ``path`` resolves to ``root`` or somewhere beneath it."""
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def unique_path(path: Path) -> Path:
    """Return ``path`` or ``name (n).ext`` if it already exists."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 10_000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(path)


def free_space(path: Path) -> int:
    """Free bytes on the volume holding ``path`` (walks up to an existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def ensure_free_space(path: Path, needed: int, margin: int = 64 * 1024 * 1024) -> None:
    if needed <= 0:
        return
    free = free_space(path)
    if free < needed + margin:
        raise InsufficientSpaceError(Path(path), needed, free)


def dir_size(path: Path) -> int:
    """Bytes used by a folder (recursively) or a single file."""
    if os.path.isfile(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def format_bytes(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_speed(bps: float | int | None) -> str:
    if not bps:
        return "–"
    return f"{format_bytes(bps)}/s"


def format_eta(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "–"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400 * 2:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


_SIZE_RE = re.compile(r"([\d.,]+)\s*(KB|MB|GB|TB|КБ|МБ|ГБ|ТБ|B)\b", re.I)
_SIZE_MULT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4,
              "КБ": 1024, "МБ": 1024**2, "ГБ": 1024**3, "ТБ": 1024**4}


def parse_size(text: str | None) -> int | None:
    """Parse human sizes like '10.3 GB' (as displayed by sources; approximate)."""
    if not text:
        return None
    m = _SIZE_RE.search(text)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return int(value * _SIZE_MULT[m.group(2).upper()])
