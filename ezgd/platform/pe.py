"""Minimal reader for Windows PE files (EXE/DLL): machine, imported DLLs (normal + delay-load), .NET header.

Only reads the headers and import tables; never executes anything. Used by the debugging assistant to predict
"DLL not found" problems before a game is started.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MACHINE_I386 = 0x14C
MACHINE_AMD64 = 0x8664
MAX_READ = 64 * 1024 * 1024          # headers + import tables live well inside the first part of a file


@dataclass
class PEInfo:
    machine: int
    imports: list[str] = field(default_factory=list)      # lower-case DLL names
    delay_imports: list[str] = field(default_factory=list)
    dotnet: bool = False
    subsystem_version: tuple[int, int] = (0, 0)     # the oldest Windows it was built for (5.1 = XP, 6.1 = 7 …)
    timestamp: int = 0                              # link time (often real; 0 or random when stripped)

    @property
    def is32(self) -> bool:
        return self.machine == MACHINE_I386

    @property
    def bits(self) -> int:
        return 32 if self.is32 else 64


def _rva_to_off(sections, rva: int) -> int | None:
    for va, vsize, raw, rsize in sections:
        if va <= rva < va + max(vsize, rsize):
            off = raw + (rva - va)
            return off
    return None


def _cstr(data: bytes, off: int | None, limit: int = 256) -> str | None:
    if off is None or off < 0 or off >= len(data):
        return None
    end = data.find(b"\0", off, off + limit)
    if end == -1:
        return None
    try:
        return data[off:end].decode("ascii")
    except UnicodeDecodeError:
        return None


def read_pe(path: Path) -> PEInfo | None:
    """PE details, or None if the file is not a (readable) PE image."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_READ)
    except OSError:
        return None
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        return None
    machine, nsec = struct.unpack_from("<HH", data, pe + 4)
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x10B:              # PE32
        dirs = opt + 96
    elif magic == 0x20B:            # PE32+
        dirs = opt + 112
    else:
        return None
    ndirs = struct.unpack_from("<I", data, dirs - 4)[0]
    try:
        subsys = struct.unpack_from("<HH", data, opt + 48)
        stamp = struct.unpack_from("<I", data, pe + 8)[0]
    except struct.error:
        subsys, stamp = (0, 0), 0

    def directory(i: int) -> tuple[int, int]:
        if i >= ndirs or dirs + 8 * i + 8 > len(data):
            return 0, 0
        return struct.unpack_from("<II", data, dirs + 8 * i)

    sec = opt + opt_size
    sections = []
    for i in range(min(nsec, 96)):
        o = sec + 40 * i
        if o + 40 > len(data):
            break
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, o + 8)
        sections.append((va, vsize, raw, rsize))
    info = PEInfo(machine, subsystem_version=(int(subsys[0]), int(subsys[1])), timestamp=int(stamp))
    imp_rva, _ = directory(1)
    off = _rva_to_off(sections, imp_rva) if imp_rva else None
    seen = 0
    while off is not None and off + 20 <= len(data) and seen < 512:
        name_rva = struct.unpack_from("<I", data, off + 12)[0]
        if not any(struct.unpack_from("<5I", data, off)):
            break
        name = _cstr(data, _rva_to_off(sections, name_rva))
        if name:
            info.imports.append(name.lower())
        off += 20
        seen += 1
    dly_rva, _ = directory(13)
    off = _rva_to_off(sections, dly_rva) if dly_rva else None
    seen = 0
    while off is not None and off + 32 <= len(data) and seen < 512:
        attrs, name_rva = struct.unpack_from("<II", data, off)
        if not attrs and not name_rva:
            break
        name = _cstr(data, _rva_to_off(sections, name_rva))
        if name:
            info.delay_imports.append(name.lower())
        off += 32
        seen += 1
    clr_rva, clr_size = directory(14)
    info.dotnet = bool(clr_rva and clr_size)
    return info
