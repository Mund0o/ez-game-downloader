"""Multi-part archives: recognise the parts of a set and decide when the set is complete.

Sites split big games into ``Game.part1.rar … Game.part7.rar`` (also ``part01``), ``Game.7z.001 …``, ``Game.zip.001``,
``Game.001`` or the old RAR style ``Game.rar`` + ``Game.r00, .r01 …``. Each part is downloaded separately, so
extraction must wait until every part is present; extracting starts from the first volume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_PART_RAR = re.compile(r"^(?P<base>.+)\.part(?P<n>\d{1,4})\.rar$", re.I)
_SPLIT = re.compile(r"^(?P<base>.+?)(?P<ext>\.(?:7z|zip|rar|tar))?\.(?P<n>\d{3})$", re.I)
_OLD_RAR = re.compile(r"^(?P<base>.+)\.(?:rar|r(?P<n>\d{2,3}))$", re.I)


@dataclass(frozen=True)
class Volume:
    set_key: str          # identifies the set (lower-case base name + style)
    index: int            # 1-based position in the set
    first_name: str       # file name of the first volume (where extraction starts)


def volume_info(name: str) -> Volume | None:
    """Volume details for a file name, or None if it is not part of a multi-part set."""
    name = Path(name).name
    m = _PART_RAR.match(name)
    if m:
        width = len(m.group("n"))
        return Volume(f"{m.group('base').lower()}|partrar", int(m.group("n")),
                      f"{m.group('base')}.part{1:0{width}d}.rar")
    m = _SPLIT.match(name)
    if m:
        ext = m.group("ext") or ""
        return Volume(f"{m.group('base').lower()}{ext.lower()}|split", int(m.group("n")),
                      f"{m.group('base')}{ext}.001")
    m = _OLD_RAR.match(name)
    if m and m.group("n") is not None:        # .r00 is the 2nd volume, the .rar itself is the 1st
        return Volume(f"{m.group('base').lower()}|oldrar", int(m.group("n")) + 2, f"{m.group('base')}.rar")
    return None


def old_rar_head(name: str, siblings: list[str]) -> Volume | None:
    """``Game.rar`` is the first volume of an old-style set only when ``Game.r00`` exists next to it."""
    m = _OLD_RAR.match(Path(name).name)
    if not m or m.group("n") is not None:
        return None
    base = m.group("base").lower()
    if any(re.fullmatch(re.escape(base) + r"\.r\d{2,3}", s, re.I) for s in siblings):
        return Volume(f"{base}|oldrar", 1, Path(name).name)
    return None


def volume_of(path: Path) -> Volume | None:
    v = volume_info(path.name)
    if v is None and path.suffix.lower() == ".rar":
        try:
            v = old_rar_head(path.name, [p.name for p in path.parent.iterdir()])
        except OSError:
            v = None
    return v


@dataclass
class SetStatus:
    set_key: str
    first: Path | None        # first volume, if present
    have: list[int]
    expected: int | None      # from the game page, when it lists the parts
    complete: bool
    missing: list[int]

    @property
    def summary(self) -> str:
        total = self.expected or (max(self.have) if self.have else 0)
        if self.complete:
            return f"all {len(self.have)} parts downloaded"
        if self.expected:
            return f"{len(self.have)} of {self.expected} parts downloaded"
        return f"{len(self.have)} part(s) downloaded" + (f" (missing part {', '.join(map(str, self.missing))})"
                                                       if self.missing else f", waiting for more after part {total}")


def set_status(folder: Path, set_key: str, expected: int | None = None) -> SetStatus:
    """Which parts of ``set_key`` are present in ``folder`` (finished files only; ``.part`` downloads are
    still in progress) and whether the set is complete.

    Without a known part count a set counts as complete when the parts are contiguous from 1 and the last one
    is smaller than the first (volumes are fixed-size except the last).
    """
    parts: dict[int, Path] = {}
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        files = []
    names = [p.name for p in files]
    for p in files:
        v = volume_info(p.name) or (old_rar_head(p.name, names) if p.suffix.lower() == ".rar" else None)
        if v is not None and v.set_key == set_key:
            parts[v.index] = p
    have = sorted(parts)
    top = expected or (max(have) if have else 0)
    missing = [i for i in range(1, top + 1) if i not in parts]
    first = parts.get(1)
    if expected:
        complete = not missing and len(have) >= expected
    else:
        last = parts.get(max(have)) if have else None
        try:
            smaller = bool(first and last and len(have) > 1 and last.stat().st_size < first.stat().st_size)
        except OSError:
            smaller = False
        complete = bool(have) and not missing and smaller
    return SetStatus(set_key, first, have, expected, complete, missing)


def expected_parts(file_names: list[str], set_key: str) -> int | None:
    """Number of parts a game page lists for ``set_key`` (from its download links' file names)."""
    idx = {v.index for v in (volume_info(n) for n in file_names if n) if v is not None and v.set_key == set_key}
    if not idx:
        return None
    if set_key.endswith("|oldrar"):
        idx.add(1)                      # the .rar head has no number of its own
    return max(idx)
