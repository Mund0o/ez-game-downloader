"""Build minimal Windows PE files (with a chosen import table) and minidumps for tests."""

import struct


def build_pe(imports: list[str], bits: int = 64, dotnet: bool = False, subsystem: tuple[int, int] = (6, 0)) -> bytes:
    sect_rva, sect_raw = 0x1000, 0x400
    body = bytearray()
    desc_off = 0
    names_off = 20 * (len(imports) + 1)
    names = bytearray()
    name_rvas = []
    for n in imports:
        name_rvas.append(sect_rva + names_off + len(names))
        names += n.encode() + b"\0"
    for rva in name_rvas:                                   # IMAGE_IMPORT_DESCRIPTOR: only Name is used
        body += struct.pack("<IIIII", 1, 0, 0, rva, 1)
    body += b"\0" * 20
    body += names
    body += b"\0" * (-len(body) % 0x200)
    e_lfanew = 0x40
    opt_size = 240 if bits == 64 else 224
    hdr = bytearray(b"MZ" + b"\0" * 0x3A + struct.pack("<I", e_lfanew))
    hdr += b"PE\0\0"
    hdr += struct.pack("<HHIIIHH", 0x8664 if bits == 64 else 0x14C, 1, 0, 0, 0, opt_size, 0x22)
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x20B if bits == 64 else 0x10B)
    struct.pack_into("<HH", opt, 48, *subsystem)                 # MajorSubsystemVersion (5 = XP era)
    dirs = 112 if bits == 64 else 96
    struct.pack_into("<I", opt, dirs - 4, 16)
    struct.pack_into("<II", opt, dirs + 8, sect_rva + desc_off, 20 * (len(imports) + 1))
    if dotnet:
        struct.pack_into("<II", opt, dirs + 8 * 14, sect_rva, 72)
    hdr += opt
    hdr += struct.pack("<8sIIIIIIHHI", b".idata\0\0", len(body), sect_rva, len(body), sect_raw, 0, 0, 0, 0, 0)
    hdr += b"\0" * (sect_raw - len(hdr))
    return bytes(hdr + body)


def build_minidump(code: int, address: int, modules: list[tuple[int, int, str]], access: int | None = None) -> bytes:
    """MDMP with an exception stream and a module list."""
    out = bytearray(32)
    streams = []
    exc = bytearray(168)
    struct.pack_into("<IIIIQQI", exc, 0, 1, 0, code, 0, 0, address, 2 if access is not None else 0)
    if access is not None:
        struct.pack_into("<QQ", exc, 40, access, 0x1234)
    streams.append((6, bytes(exc)))
    names = bytearray()
    mlist = bytearray(struct.pack("<I", len(modules)))
    name_base = None
    entries = []
    for base, size, name in modules:
        entries.append((base, size, len(names)))
        raw = name.encode("utf-16-le")
        names += struct.pack("<I", len(raw)) + raw + b"\0\0"
    streams.append((4, None))
    # layout: header(32) | dir | exception | modulelist | names
    dir_rva = 32
    exc_rva = dir_rva + 12 * len(streams)
    ml_rva = exc_rva + len(exc)
    ml_size = 4 + 108 * len(modules)
    name_base = ml_rva + ml_size
    for base, size, noff in entries:
        m = bytearray(108)
        struct.pack_into("<QIIII", m, 0, base, size, 0, 0, name_base + noff)
        mlist += m
    struct.pack_into("<IIII", out, 0, 0x504D444D, 0xA793, len(streams), dir_rva)
    d = bytearray()
    d += struct.pack("<III", 6, len(exc), exc_rva)
    d += struct.pack("<III", 4, len(mlist), ml_rva)
    return bytes(out + d + exc + mlist + names)
