"""Minimal Windows minidump (``.dmp``) reader: the exception and the module it happened in.

Games, engines (Unreal ``UE4Minidump.dmp``, Unity ``crash.dmp``) and some cracks/clients write minidumps when
they crash. Under Proton these are regular files in the game folder or the prefix, so the debugging assistant can
tell *where* a game crashed even without a diagnostic launch.
"""

from __future__ import annotations

import glob
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
    try:
        data = path.read_bytes()[:256 * 1024 * 1024]
        mtime = path.stat().st_mtime
    except OSError:
        return None
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


def find_dumps(game_dir: Path, prefix_dir: Path, since: float = 0.0) -> list[Path]:
    """Minidumps the game may have written, newest first (only files newer than ``since``)."""
    users = prefix_dir / "pfx" / "drive_c" / "users" / "steamuser"
    pats = [str(game_dir / "*.dmp"), str(game_dir / "*" / "*.dmp"), str(game_dir / "**" / "Saved" / "Crashes" /
            "*" / "*.dmp"), str(users / "AppData" / "Local" / "CrashDumps" / "*.dmp"),
            str(users / "AppData" / "Local" / "*" / "Saved" / "Crashes" / "*" / "*.dmp"),
            str(users / "AppData" / "LocalLow" / "*" / "*" / "Crashes" / "*" / "*.dmp")]
    found = set()
    for p in pats:
        found.update(glob.glob(p, recursive=True))
    files = [Path(f) for f in found if Path(f).is_file()]
    files = [f for f in files if f.stat().st_mtime >= since]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:10]
