"""Minimal Windows minidump (``.dmp``) reader: the exception and the module it happened in.

Games, engines (Unreal ``UE4Minidump.dmp``, Unity ``crash.dmp``) and some cracks/clients write minidumps when
they crash. Under Proton these are regular files in the game folder or the prefix, so the debugging assistant can
tell *where* a game crashed even without a diagnostic launch.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

SIGNATURE = 0x504D444D            # "MDMP"
MODULE_LIST = 4
EXCEPTION = 6
SYSTEM_INFO = 7


@dataclass
class DumpModule:
    base: int
    size: int
    name: str


@dataclass
class Minidump:
    path: Path
    mtime: float
    code: int | None = None
    address: int | None = None
    access: int | None = None           # access violation: 0 read, 1 write, 8 execute
    target: int | None = None           # the address that was accessed
    modules: list[DumpModule] = field(default_factory=list)

    @property
    def module(self) -> DumpModule | None:
        if self.address is None:
            return None
        for m in self.modules:
            if m.base <= self.address < m.base + m.size:
                return m
        return None

    @property
    def offset(self) -> int | None:
        m = self.module
        return None if m is None or self.address is None else self.address - m.base


def _string(data: bytes, rva: int) -> str:
    if rva + 4 > len(data):
        return ""
    n = struct.unpack_from("<I", data, rva)[0]
    raw = data[rva + 4: rva + 4 + min(n, 1024)]
    return raw.decode("utf-16-le", "replace")


def read_minidump(path: Path) -> Minidump | None:
    """The exception and module list of a minidump. The file is mapped, not read: Sentry writes full-memory dumps
    (Expedition 33's was 945 MB) and only a few kilobytes of it are needed."""
    import mmap
    try:
        mtime = path.stat().st_mtime
        with open(path, "rb") as fh:
            if os.fstat(fh.fileno()).st_size < 32:
                return None
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
                return _parse(path, mtime, data)
    except (OSError, ValueError):
        return None


def _parse(path: Path, mtime: float, data) -> Minidump | None:
    if len(data) < 32 or struct.unpack_from("<I", data, 0)[0] != SIGNATURE:
        return None
    nstreams, dir_rva = struct.unpack_from("<II", data, 8)
    d = Minidump(path, mtime)
    for i in range(min(nstreams, 256)):
        o = dir_rva + 12 * i
        if o + 12 > len(data):
            break
        stype, size, rva = struct.unpack_from("<III", data, o)
        if stype == EXCEPTION and rva + 168 <= len(data):
            # thread id, alignment, then MINIDUMP_EXCEPTION
            code, _flags, _rec, addr, nparams = struct.unpack_from("<IIQQI", data, rva + 8)
            d.code, d.address = code, addr
            if code == 0xC0000005 and nparams >= 2:
                d.access, d.target = struct.unpack_from("<QQ", data, rva + 8 + 32)
        elif stype == MODULE_LIST and rva + 4 <= len(data):
            n = struct.unpack_from("<I", data, rva)[0]
            for k in range(min(n, 2048)):
                mo = rva + 4 + 108 * k
                if mo + 108 > len(data):
                    break
                base, msize = struct.unpack_from("<QI", data, mo)
                name_rva = struct.unpack_from("<I", data, mo + 20)[0]
                name = _string(data, name_rva).replace("\\", "/").rsplit("/", 1)[-1]
                d.modules.append(DumpModule(base, msize, name))
    return d


def _walk(root: Path, depth: int, since: float, out: list[Path], budget: list[int]) -> None:
    """*.dmp / *.mdmp below ``root`` (``depth`` levels), newer than ``since`` — a bounded walk (a game folder can
    hold 100 000 files)."""
    import os
    base = len(root.parts)
    try:
        for d, dirs, files in os.walk(root):
            budget[0] -= len(files) + len(dirs)
            if budget[0] < 0:
                return
            if len(Path(d).parts) - base >= depth:
                dirs[:] = []
            dirs[:] = [x for x in dirs if x.lower() not in ("paks", "movies", "content", "shadercache", "shaders",
                                                            "steamapps", "windows", "system32", "syswow64")]
            for f in files:
                if f.lower().endswith((".dmp", ".mdmp")):
                    p = Path(d) / f
                    try:
                        if p.stat().st_mtime >= since:
                            out.append(p)
                    except OSError:
                        continue
    except OSError:
        return


def find_dumps(game_dir: Path, prefix_dir: Path, since: float = 0.0) -> list[Path]:
    """Minidumps the game may have written, newest first (only files newer than ``since``): in its folder (Unreal's
    Saved/Crashes, dumps next to the exe), and where crash handlers keep them in the prefix — Unreal and Unity under
    AppData, Sentry (.sentry-native), Crashpad (Crashpad/reports), Windows' own CrashDumps, Temp, Documents."""
    users = prefix_dir / "pfx" / "drive_c" / "users" / "steamuser"
    found: list[Path] = []
    budget = [250_000]
    for root, depth in ((game_dir, 5), (users / "AppData" / "Local", 6), (users / "AppData" / "LocalLow", 6),
                        (users / "AppData" / "Roaming", 5), (users / "Documents", 5), (users / "Temp", 3),
                        (prefix_dir / "pfx" / "drive_c" / "ProgramData", 4)):
        if root.is_dir():
            _walk(root, depth, since, found, budget)
    files = list(dict.fromkeys(found))
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:10]
