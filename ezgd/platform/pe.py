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
BLOCK = 1024 * 1024                  # read in pieces: only the headers and the import tables are needed
MAX_BLOCKS = 64                      # (a 191 MB Unreal game keeps its import table 178 MB into the file)


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


class _File:
    """Random access to a (possibly huge) file, read in cached 1 MB blocks."""

    def __init__(self, fh):
        self.fh = fh
        self.blocks: dict[int, bytes] = {}
        self.size = fh.seek(0, 2)

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or off >= self.size:
            return b""
        out = bytearray()
        while n > 0 and off < self.size:
            i, inner = divmod(off, BLOCK)
            blk = self.blocks.get(i)
            if blk is None:
                if len(self.blocks) >= MAX_BLOCKS:
                    raise OSError("too many blocks")
                self.fh.seek(i * BLOCK)
                blk = self.blocks[i] = self.fh.read(BLOCK)
                if not blk:
                    break
            part = blk[inner:inner + n]
            if not part:
                break
            out += part
            off += len(part)
            n -= len(part)
        return bytes(out)

    def unpack(self, fmt: str, off: int) -> tuple:
        return struct.unpack(fmt, self.read(off, struct.calcsize(fmt)))

    def cstr(self, off: int | None, limit: int = 256) -> str | None:
        if off is None:
            return None
        raw = self.read(off, limit)
        end = raw.find(b"\0")
        if end == -1:
            return None
        try:
            return raw[:end].decode("ascii")
        except UnicodeDecodeError:
            return None


def read_pe(path: Path) -> PEInfo | None:
    """PE details, or None if the file is not a (readable) PE image."""
    try:
        with open(path, "rb") as fh:
            return _parse(_File(fh))
    except (OSError, struct.error):
        return None


def _parse(f: _File) -> PEInfo | None:
    head = f.read(0, 0x40)
    if len(head) < 0x40 or head[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if f.read(pe, 4) != b"PE\0\0":
        return None
    machine, nsec = f.unpack("<HH", pe + 4)
    opt_size = f.unpack("<H", pe + 20)[0]
    opt = pe + 24
    magic = f.unpack("<H", opt)[0]
    if magic == 0x10B:              # PE32
        dirs = opt + 96
    elif magic == 0x20B:            # PE32+
        dirs = opt + 112
    else:
        return None
    ndirs = f.unpack("<I", dirs - 4)[0]
    try:
        subsys = f.unpack("<HH", opt + 48)
        stamp = f.unpack("<I", pe + 8)[0]
    except struct.error:
        subsys, stamp = (0, 0), 0

    def directory(i: int) -> tuple[int, int]:
        if i >= ndirs:
            return 0, 0
        try:
            return f.unpack("<II", dirs + 8 * i)
        except struct.error:
            return 0, 0

    sec = opt + opt_size
    sections = []
    for i in range(min(nsec, 96)):
        try:
            vsize, va, rsize, raw = f.unpack("<IIII", sec + 40 * i + 8)
        except struct.error:
            break
        sections.append((va, vsize, raw, rsize))
    info = PEInfo(machine, subsystem_version=(int(subsys[0]), int(subsys[1])), timestamp=int(stamp))
    imp_rva, _ = directory(1)
    off = _rva_to_off(sections, imp_rva) if imp_rva else None
    seen = 0
    while off is not None and seen < 512:
        desc = f.read(off, 20)
        if len(desc) < 20 or not any(struct.unpack("<5I", desc)):
            break
        name = f.cstr(_rva_to_off(sections, struct.unpack_from("<I", desc, 12)[0]))
        if name:
            info.imports.append(name.lower())
        off += 20
        seen += 1
    dly_rva, _ = directory(13)
    off = _rva_to_off(sections, dly_rva) if dly_rva else None
    seen = 0
    while off is not None and seen < 512:
        desc = f.read(off, 32)
        if len(desc) < 32:
            break
        attrs, name_rva = struct.unpack_from("<II", desc)
        if not attrs and not name_rva:
            break
        name = f.cstr(_rva_to_off(sections, name_rva))
        if name:
            info.delay_imports.append(name.lower())
        off += 32
        seen += 1
    clr_rva, clr_size = directory(14)
    info.dotnet = bool(clr_rva and clr_size)
    return info
