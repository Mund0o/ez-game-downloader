"""Safe archive extraction (zip, 7z, rar, tar.*, single-file gz/bz2/xz).

Every archive is treated as hostile:

* member names are validated *before* anything is written: absolute paths,
  drive letters, ``..`` components, NUL bytes and alternate data streams are
  rejected, as is anything that would resolve outside the destination;
* symlinks, hard links and device nodes are never extracted;
* the archive is unpacked into a private staging directory inside the
  destination and only moved into place after the whole archive succeeded,
  so a failed or cancelled extraction leaves the destination untouched;
* after extraction the staging tree is re-checked (no links, nothing outside);
* free space is checked against the declared uncompressed size;
* nothing extracted is ever executed.
"""

from __future__ import annotations

import bz2
import contextlib
import gzip
import logging
import lzma
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import time
try:                                   # zip with Zstandard (method 93, e.g. AnkerGames): Python 3.14's zipfile,
    from backports.zstd import zipfile  # backported (ships with py7zr) — the stdlib one before 3.14 cannot read it
except ImportError:                    # pragma: no cover - Python ≥ 3.14 or no backport
    import zipfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from ezgd.core.fsutil import dir_size, ensure_free_space
from ezgd.platform import IS_WINDOWS

log = logging.getLogger(__name__)

STAGING_PREFIX = ".ezgd-extract-"


class ArchiveFormat(StrEnum):
    ZIP = "zip"
    SEVENZ = "7z"
    RAR = "rar"
    TAR = "tar"
    GZIP = "gz"
    BZIP2 = "bz2"
    XZ = "xz"


class ArchiveError(Exception):
    pass


class UnsafeArchiveError(ArchiveError):
    pass


class CorruptArchiveError(ArchiveError):
    pass


class PasswordRequiredError(ArchiveError):
    pass


class MissingToolError(ArchiveError):
    pass


class ExtractionCancelled(ArchiveError):
    pass


@dataclass
class Member:
    name: str
    size: int | None
    is_dir: bool = False
    is_link: bool = False
    is_special: bool = False


@dataclass
class ExtractResult:
    dest: Path
    files: int
    bytes: int
    skipped: list[str] = field(default_factory=list)
    nested_archives: list[str] = field(default_factory=list)


ProgressCb = Callable[[float, str], None]       # (fraction 0..1, current item)

_MAGIC = [
    (b"PK\x03\x04", ArchiveFormat.ZIP), (b"PK\x05\x06", ArchiveFormat.ZIP),
    (b"7z\xbc\xaf\x27\x1c", ArchiveFormat.SEVENZ),
    (b"Rar!\x1a\x07", ArchiveFormat.RAR),
    (b"\x1f\x8b", ArchiveFormat.GZIP), (b"BZh", ArchiveFormat.BZIP2),
    (b"\xfd7zXZ\x00", ArchiveFormat.XZ),
]
_EXT = {".zip": ArchiveFormat.ZIP, ".7z": ArchiveFormat.SEVENZ, ".rar": ArchiveFormat.RAR,
        ".tar": ArchiveFormat.TAR, ".tgz": ArchiveFormat.TAR, ".tbz2": ArchiveFormat.TAR,
        ".txz": ArchiveFormat.TAR, ".gz": ArchiveFormat.GZIP, ".bz2": ArchiveFormat.BZIP2,
        ".xz": ArchiveFormat.XZ}
_RAR_PART = re.compile(r"\.part(\d+)\.rar$", re.I)
_RAR_OLD_VOL = re.compile(r"\.r\d{2,3}$", re.I)
_7Z_VOL = re.compile(r"\.7z\.(\d{3})$", re.I)
_ZIP_VOL = re.compile(r"\.z\d{2}$", re.I)


@contextlib.contextmanager
def _volumes(path: Path):
    """A split archive (``Game.7z.001``, ``Game.zip.001`` …) read as one continuous file; other paths as-is."""
    if re.search(r"\.\d{3}$", path.name) and path.name.endswith(".001"):
        import multivolumefile
        with multivolumefile.open(str(path)[:-4], mode="rb") as fh:
            yield fh
    else:
        yield path


def detect_format(path: Path) -> ArchiveFormat | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            if fmt in (ArchiveFormat.GZIP, ArchiveFormat.BZIP2, ArchiveFormat.XZ) and _is_tar_compressed(path):
                return ArchiveFormat.TAR
            return fmt
    if len(head) >= 262 and head[257:262] == b"ustar":
        return ArchiveFormat.TAR
    if _7Z_VOL.search(path.name) and path.name.lower().endswith(".001"):
        return ArchiveFormat.SEVENZ
    return None


def _is_tar_compressed(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(s) for s in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"))


def is_secondary_volume(path: Path) -> bool:
    """True for volumes other than the first of a multi-part set (they are opened via the first)."""
    name = path.name
    m = _RAR_PART.search(name)
    if m:
        return int(m.group(1)) != 1
    if _RAR_OLD_VOL.search(name) or _ZIP_VOL.search(name):
        return True
    m = _7Z_VOL.search(name)
    if m:
        return int(m.group(1)) != 1
    return False


def find_archives(root: Path, max_depth: int = 2) -> list[Path]:
    """Archives under ``root`` (or ``root`` itself), first volumes only."""
    if root.is_file():
        return [root] if detect_format(root) and not is_secondary_volume(root) else []
    out = []
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if not d.startswith(STAGING_PREFIX)]
        if depth >= max_depth:
            dirnames[:] = []
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if is_secondary_volume(p) or fn.endswith(".part"):
                continue
            if detect_format(p):
                out.append(p)
    return out


# ----------------------------------------------------------------- name safety
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)


def safe_member_path(dest: Path, name: str) -> Path:
    """Map an archive member name to a path inside ``dest`` or raise UnsafeArchiveError."""
    if not name or "\x00" in name:
        raise UnsafeArchiveError(f"invalid member name {name!r}")
    norm = name.replace("\\", "/")
    if norm.startswith("/") or PureWindowsPath(name).drive or re.match(r"^[A-Za-z]:", norm):
        raise UnsafeArchiveError(f"absolute path in archive: {name}")
    parts = [p for p in PurePosixPath(norm).parts if p not in ("", ".")]
    if not parts:
        raise UnsafeArchiveError(f"empty member name {name!r}")
    for p in parts:
        if p == "..":
            raise UnsafeArchiveError(f"path traversal in archive: {name}")
        if ":" in p:
            raise UnsafeArchiveError(f"illegal ':' in archive member: {name}")
        if IS_WINDOWS and _WIN_RESERVED.match(p):
            raise UnsafeArchiveError(f"reserved Windows name in archive: {name}")
    target = dest.joinpath(*parts)
    root = os.path.realpath(dest)
    resolved = os.path.realpath(target)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise UnsafeArchiveError(f"member escapes destination: {name}")
    return target


def _check_members(dest: Path, members: list[Member]) -> list[str]:
    """Validate all names up front. Returns names of links/specials that will be skipped."""
    skipped = []
    for m in members:
        safe_member_path(dest, m.name)
        if m.is_link or m.is_special:
            skipped.append(m.name)
    return skipped


# ------------------------------------------------------------------- listing
def list_members(path: Path, fmt: ArchiveFormat, password: str | None = None,
                 tool: str | None = None) -> list[Member]:
    try:
        if fmt is ArchiveFormat.ZIP:
            with _volumes(path) as src, zipfile.ZipFile(src) as zf:
                out = []
                for zi in zf.infolist():
                    kind = _zip_kind(zi)
                    out.append(Member(zi.filename, zi.file_size, zi.is_dir(),
                                      is_link=kind == "link", is_special=kind == "special"))
                return out
        if fmt is ArchiveFormat.TAR:
            with tarfile.open(path) as tf:
                return [Member(ti.name, ti.size, ti.isdir(), ti.issym() or ti.islnk(),
                               not (ti.isreg() or ti.isdir() or ti.issym() or ti.islnk()))
                        for ti in tf.getmembers()]
        if fmt is ArchiveFormat.SEVENZ:
            import py7zr
            with _volumes(path) as src, py7zr.SevenZipFile(src, password=password) as zf:
                if zf.needs_password() and not password:
                    raise PasswordRequiredError("This 7z archive is password-protected.")
                return [Member(e.filename, e.uncompressed, e.is_directory, bool(e.is_symlink))
                        for e in zf.list()]
        if fmt is ArchiveFormat.RAR:
            return _list_rar(path, password, tool)
        if fmt in (ArchiveFormat.GZIP, ArchiveFormat.BZIP2, ArchiveFormat.XZ):
            return [Member(_single_name(path), None)]
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        raise CorruptArchiveError(f"The archive is incomplete or corrupted: {exc}") from exc
    except ArchiveError:
        raise
    except Exception as exc:
        if type(exc).__module__.startswith("py7zr"):
            raise CorruptArchiveError(f"The 7z archive is incomplete or corrupted: {exc}") from exc
        raise
    raise ArchiveError(f"unsupported format {fmt}")


def _zip_kind(zi: zipfile.ZipInfo) -> str:
    """'file', 'link' or 'special' from the Unix mode bits (often absent: plain permissions only)."""
    fmt = stat.S_IFMT((zi.external_attr >> 16) & 0xFFFF)
    if fmt == stat.S_IFLNK:
        return "link"
    if fmt in (0, stat.S_IFREG, stat.S_IFDIR):
        return "file"
    return "special"


def _single_name(path: Path) -> str:
    name = path.name
    for suf in (".gz", ".bz2", ".xz"):
        if name.lower().endswith(suf):
            return name[: -len(suf)] or "file"
    return name + ".out"


# ----------------------------------------------------------------------- RAR
def bundled_7zip() -> str | None:
    """7-Zip shipped with the app (``tools/7zzs`` next to the program; build assets when run from source)."""
    import sys
    here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else \
        Path(__file__).resolve().parents[2] / "packaging" / "build-assets"
    for cand in (here / "tools" / ("7z.exe" if IS_WINDOWS else "7zzs"),
                 here / "tools" / "linux" / "7zzs"):
        if cand.is_file() and (IS_WINDOWS or os.access(cand, os.X_OK)):
            return str(cand)
    return None


def find_rar_tool(explicit: str | None = None) -> tuple[str, str] | None:
    """Return (kind, path) for an external RAR extractor: unrar, 7-Zip (installed or bundled) or bsdtar.

    bsdtar comes last: it cannot open password-protected RAR archives (all OnlineFix archives)."""
    candidates: list[tuple[str, str]] = []
    if explicit:
        base = Path(explicit).name.lower()
        kind = "unrar" if "unrar" in base else "7z" if base.startswith("7z") else "bsdtar" if "bsdtar" in base else "unrar"
        candidates.append((kind, explicit))
    for kind, names in (("unrar", ["unrar", "UnRAR.exe"]), ("7z", ["7z", "7zz", "7z.exe"]),
                        ("bsdtar", ["bsdtar"])):
        for n in names:
            p = shutil.which(n)
            if p:
                candidates.append((kind, p))
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            for kind, rel in (("unrar", r"WinRAR\UnRAR.exe"), ("7z", r"7-Zip\7z.exe")):
                p = Path(base) / rel
                if p.exists():
                    candidates.append((kind, str(p)))
    bundled = bundled_7zip()
    if bundled:                                   # before bsdtar (which cannot decrypt RAR)
        i = next((n for n, c in enumerate(candidates) if c[0] == "bsdtar"), len(candidates))
        candidates.insert(i, ("7z", bundled))
    for kind, p in candidates:
        if Path(p).exists() or shutil.which(p):
            return kind, p
    return None


def _run(cmd: list[str], timeout: float | None = 120) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags,
                          stdin=subprocess.DEVNULL)


def _rar_error(out: str, code: int) -> ArchiveError:
    low = out.lower()
    if "encryption is not supported" in low:      # bsdtar
        return MissingToolError("This RAR archive is password-protected, which bsdtar cannot open. Install UnRAR "
                                "or 7-Zip (Settings → System check), or use the app build that includes 7-Zip.")
    if "password" in low and ("incorrect" in low or "wrong" in low or "enter" in low or "encrypted" in low):
        return PasswordRequiredError("The RAR archive needs a password (or the password is wrong).")
    if "cannot find volume" in low or "next volume" in low or "unexpected end of archive" in low:
        return CorruptArchiveError("A volume of this multi-part archive is missing or incomplete.")
    if "crc failed" in low or "checksum error" in low or "corrupt" in low or "data error" in low:
        return CorruptArchiveError("The RAR archive is corrupted (checksum error).")
    return ArchiveError(f"RAR tool failed (exit code {code}): {out.strip()[-300:]}")


def _list_rar(path: Path, password: str | None, tool: str | None) -> list[Member]:
    found = find_rar_tool(tool)
    if not found:
        raise MissingToolError("Extracting RAR archives needs UnRAR or 7-Zip. Install one "
                               "(Linux: package 'unrar' or '7zip'; Windows: 7-Zip or WinRAR) "
                               "or set its path in Settings → Extraction.")
    kind, exe = found
    pw = password or "-"
    if kind == "unrar":
        res = _run([exe, "lt", "-v", f"-p{pw}", "--", str(path)])
        text = res.stdout.decode("utf-8", "replace")
        if res.returncode not in (0,):
            raise _rar_error(text + res.stderr.decode("utf-8", "replace"), res.returncode)
        return _parse_unrar_technical(text)
    if kind == "7z":
        res = _run([exe, "l", "-slt", "-ba", f"-p{password or ''}", str(path)])
        text = res.stdout.decode("utf-8", "replace")
        if res.returncode != 0:
            raise _rar_error(text + res.stderr.decode("utf-8", "replace"), res.returncode)
        return _parse_7z_slt(text)
    res = _run([exe, "-tvf", str(path)])
    if res.returncode != 0:
        raise _rar_error(res.stderr.decode("utf-8", "replace"), res.returncode)
    out = []
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 8)
        if len(parts) == 9:
            out.append(Member(parts[8], int(parts[4]) if parts[4].isdigit() else None,
                              line.startswith("d"), line.startswith("l"),
                              not line[:1] in ("-", "d", "l")))
    return out


def _parse_unrar_technical(text: str) -> list[Member]:
    members, cur = [], {}
    for line in text.splitlines() + [""]:
        s = line.strip()
        if not s:
            if "name" in cur and cur.get("type", "").lower() == "service":
                pass                       # RAR service headers (comment, quick-open data) are not files
            elif "name" in cur:
                t = cur.get("type", "file").lower()
                members.append(Member(cur["name"], int(cur["size"]) if cur.get("size", "").isdigit() else None,
                                      is_dir=t == "directory", is_link="link" in t or "symlink" in t,
                                      is_special=t not in ("file", "directory") and "link" not in t))
            cur = {}
            continue
        k, sep, v = s.partition(": ")
        if not sep:
            continue
        k = k.strip().lower()
        if k == "name":
            cur["name"] = v
        elif k == "type":
            cur["type"] = v
        elif k == "size":
            cur["size"] = v.strip()
    return members


def _parse_7z_slt(text: str) -> list[Member]:
    members, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if "Path" in cur:
                attrs = cur.get("Attributes", "")
                members.append(Member(cur["Path"], int(cur["Size"]) if cur.get("Size", "").isdigit() else None,
                                      is_dir=cur.get("Folder") == "+" or attrs.startswith("D"),
                                      is_link=cur.get("Symbolic Link", "") not in ("", "-") or " l" in attrs.lower()))
            cur = {}
            continue
        k, sep, v = line.partition(" = ")
        if sep:
            cur[k.strip()] = v
    return members


# ------------------------------------------------------------------- extract
class _Progress:
    def __init__(self, total: int | None, cb: ProgressCb | None, cancel: threading.Event | None):
        self.total = total or 0
        self.done = 0
        self.cb = cb
        self.cancel = cancel
        self._last = 0.0

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise ExtractionCancelled("Extraction cancelled")

    def add(self, n: int, item: str = "") -> None:
        self.done += n
        self.report(item)

    def report(self, item: str = "", force: bool = False) -> None:
        self.check()
        now = time.monotonic()
        if self.cb and (force or now - self._last > 0.2):
            self._last = now
            frac = min(1.0, self.done / self.total) if self.total else 0.0
            self.cb(frac, item)


def extract(archive: Path, dest: Path, *, password: str | None = None, progress: ProgressCb | None = None,
            cancel: threading.Event | None = None, rar_tool: str | None = None,
            overwrite: bool = True, only: set[str] | None = None) -> ExtractResult:
    """Extract ``archive`` into ``dest`` safely. ``only``: member names to (re-)extract — used to repair a game."""
    archive = Path(archive)
    dest = Path(dest)
    fmt = detect_format(archive)
    if fmt is None:
        raise ArchiveError(f"{archive.name} is not a supported archive")
    dest.mkdir(parents=True, exist_ok=True)
    members = list_members(archive, fmt, password, rar_tool)
    skipped = _check_members(dest, members)
    if only is not None:
        only = {_norm(n) for n in only}
        members = [m for m in members if _norm(m.name) in only]
    declared = sum(m.size or 0 for m in members if not m.is_dir)
    ensure_free_space(dest, declared or archive.stat().st_size)
    staging = Path(os.path.realpath(dest)) / f"{STAGING_PREFIX}{os.getpid()}-{int(time.time() * 1000)}"
    staging.mkdir()
    prog = _Progress(declared or None, progress, cancel)
    try:
        if fmt is ArchiveFormat.ZIP:
            _extract_zip(archive, staging, password, prog, only)
        elif fmt is ArchiveFormat.TAR:
            _extract_tar(archive, staging, prog, only)
        elif fmt is ArchiveFormat.SEVENZ:
            _extract_7z(archive, staging, password, prog, {m.name for m in members if m.is_link}, only)
        elif fmt is ArchiveFormat.RAR:
            _extract_rar(archive, staging, password, prog, rar_tool,
                         sorted(m.name for m in members if not m.is_dir) if only is not None else None)
        else:
            _extract_single(archive, staging / _single_name(archive), fmt, prog)
        prog.check()                       # cancelled while the extractor finished: nothing is moved in
        _verify_tree(staging)
        files, total = _move_into(staging, dest, overwrite, only)
        prog.done = prog.total
        prog.report("done", force=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    nested = [str(p.relative_to(dest)) for p in find_archives(dest, max_depth=2)
              if p.resolve() != archive.resolve()]
    return ExtractResult(dest, files, total, skipped, nested)


def _norm(name: str) -> str:
    """Member name in one canonical form: forward slashes, no leading "./" or "/"."""
    n = name.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n.strip("/")


def seven_zip() -> str | None:
    """The 7-Zip program: the one shipped with the app first, else one installed on the system."""
    exe = bundled_7zip()
    if exe:
        return exe
    for name in ("7zz", "7z", "7za"):
        found = shutil.which(name)
        if found:
            return found
    return None


class _ToolCannotOpen(ArchiveError):
    """7-Zip could not handle this archive (old 7-Zip, unusual method): the built-in extractor tries instead."""


def _seven_zip_error(out: str, code: int, what: str) -> ArchiveError:
    low = out.lower()
    if "wrong password" in low or "enter password" in low or "password is incorrect" in low:
        return PasswordRequiredError(f"The {what} archive needs a password (or the password is wrong).")
    if "unexpected end of archive" in low or "missing volume" in low:
        return CorruptArchiveError(f"The {what} archive is incomplete — a part may be missing or still downloading.")
    if "crc failed" in low or "data error" in low or "headers error" in low:
        return CorruptArchiveError(f"The {what} archive is corrupted (checksum error).")
    if "unsupported method" in low or "cannot open the file as archive" in low or "can not open the file" in low:
        return _ToolCannotOpen(out.strip()[-300:])
    if "not enough space" in low or "no space left" in low:
        return ArchiveError("The disk is full — free some space and install the game again.")
    return _ToolCannotOpen(f"7-Zip failed (exit code {code}): {out.strip()[-300:]}")


def _extract_with_7zip(exe: str, archive: Path, staging: Path, password: str | None, prog: _Progress,
                       names: list[str] | None, what: str) -> None:
    """Extract with 7-Zip in its own process: fast (C, every core) and it never holds Python's lock, so the app
    stays responsive however big the game is. Encrypted zips are ~40× faster than Python's zipfile."""
    files = names if names is not None and len(names) <= 400 else []    # long lists: extract all, move some
    cmd = [exe, "x", "-y", "-bso0", "-bsp0", "-snl-", f"-p{password or ''}", f"-o{staging}", str(archive), *files]
    flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=flags)
    output: list[bytes] = []
    reader = threading.Thread(target=lambda: output.append(proc.stdout.read() if proc.stdout else b""),
                              daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            time.sleep(0.4)
            prog.done = dir_size(staging)
            prog.report()
    except ExtractionCancelled:
        proc.kill()
        proc.wait()
        raise
    reader.join(timeout=5)
    if proc.returncode not in (0, 1):            # 1 = warnings only (e.g. a file could not get its timestamp)
        raise _seven_zip_error(b"".join(output).decode("utf-8", "replace"), proc.returncode, what)


def _restart_staging(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)


def _extract_zip(archive: Path, staging: Path, password: str | None, prog: _Progress,
                 only: set[str] | None = None) -> None:
    exe = seven_zip()
    if exe:
        names = None
        if only is not None:
            with _volumes(archive) as src, zipfile.ZipFile(src) as zf:
                names = [zi.filename for zi in zf.infolist() if _norm(zi.filename) in only]
        try:
            _extract_with_7zip(exe, archive, staging, password, prog, names, "zip")
            return
        except _ToolCannotOpen as exc:
            log.info("7-Zip could not extract %s (%s); using the built-in extractor", archive.name, exc)
            _restart_staging(staging)
            prog.done = 0
    _extract_zip_builtin(archive, staging, password, prog, only)


def _extract_zip_builtin(archive: Path, staging: Path, password: str | None, prog: _Progress,
                         only: set[str] | None = None) -> None:
    try:
        with _volumes(archive) as src, zipfile.ZipFile(src) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            for zi in zf.infolist():
                prog.check()
                mode = (zi.external_attr >> 16) & 0xFFFF
                if _zip_kind(zi) != "file":
                    continue
                if only is not None and _norm(zi.filename) not in only:
                    continue
                target = safe_member_path(staging, zi.filename)
                if zi.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(zi) as src, open(target, "wb") as dst:
                        while True:
                            block = src.read(1024 * 1024)
                            if not block:
                                break
                            dst.write(block)
                            prog.add(len(block), zi.filename)
                except RuntimeError as exc:
                    if "password" in str(exc).lower() or "encrypted" in str(exc).lower():
                        raise PasswordRequiredError("This zip archive is password-protected.") from exc
                    raise
                except NotImplementedError as exc:
                    raise ArchiveError(f"Unsupported zip compression/encryption ({exc}); "
                                       f"extract it with 7-Zip.") from exc
                if mode & 0o111 and not IS_WINDOWS:
                    os.chmod(target, 0o755)
    except zipfile.BadZipFile as exc:
        raise CorruptArchiveError(f"The zip archive is incomplete or corrupted: {exc}") from exc
    except (zipfile.LargeZipFile, EOFError) as exc:
        raise CorruptArchiveError(f"The zip archive is truncated: {exc}") from exc
    except OSError as exc:
        if "Bad CRC" in str(exc) or "CRC" in str(exc):
            raise CorruptArchiveError("The zip archive is corrupted (CRC mismatch).") from exc
        raise


def _extract_tar(archive: Path, staging: Path, prog: _Progress, only: set[str] | None = None) -> None:
    try:
        with tarfile.open(archive) as tf:
            for ti in tf:
                prog.check()
                if not (ti.isreg() or ti.isdir()):
                    continue                       # links/devices are never extracted
                if only is not None and _norm(ti.name) not in only:
                    continue
                safe_member_path(staging, ti.name)
                tf.extract(ti, staging, filter="data")
                prog.add(ti.size, ti.name)
    except tarfile.FilterError as exc:
        raise UnsafeArchiveError(f"Unsafe tar member: {exc}") from exc
    except (tarfile.TarError, EOFError) as exc:
        raise CorruptArchiveError(f"The tar archive is incomplete or corrupted: {exc}") from exc


def _extract_7z(archive: Path, staging: Path, password: str | None, prog: _Progress, links: set[str],
                only: set[str] | None = None) -> None:
    exe = seven_zip()
    if exe:
        names = None
        if only is not None:
            import py7zr
            with _volumes(archive) as src, py7zr.SevenZipFile(src, password=password) as zf:
                names = [n for n in zf.getnames() if _norm(n) in only]
        try:
            _extract_with_7zip(exe, archive, staging, password, prog, names, "7z")
            return
        except _ToolCannotOpen as exc:
            log.info("7-Zip could not extract %s (%s); using the built-in extractor", archive.name, exc)
            _restart_staging(staging)
            prog.done = 0
    _extract_7z_builtin(archive, staging, password, prog, links, only)


def _extract_7z_builtin(archive: Path, staging: Path, password: str | None, prog: _Progress, links: set[str],
                        only: set[str] | None = None) -> None:
    import py7zr
    from py7zr.callbacks import ExtractCallback

    class CB(ExtractCallback):
        def report_start_preparation(self): pass
        def report_start(self, processing_file_path, processing_bytes): pass
        def report_update(self, decompressed_bytes):
            try:
                prog.done += int(decompressed_bytes)
            except (TypeError, ValueError):
                pass
        def report_end(self, processing_file_path, wrote_bytes): pass
        def report_warning(self, message): log.warning("7z: %s", message)
        def report_postprocess(self): pass

    stop = threading.Event()

    def ticker():
        while not stop.wait(0.3):
            try:
                prog.report()
            except ExtractionCancelled:
                return

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    try:
        with _volumes(archive) as src, py7zr.SevenZipFile(src, password=password) as zf:
            names = [n for n in zf.getnames() if n not in links and (only is None or _norm(n) in only)]
            zf.extract(path=staging, targets=names, callback=CB())
    except py7zr.exceptions.PasswordRequired as exc:
        raise PasswordRequiredError("This 7z archive is password-protected.") from exc
    except (py7zr.exceptions.Bad7zFile, py7zr.exceptions.CrcError, EOFError, lzma.LZMAError) as exc:
        raise CorruptArchiveError(f"The 7z archive is incomplete or corrupted: {exc}") from exc
    finally:
        stop.set()
        t.join(timeout=2)
    prog.check()


def _extract_rar(archive: Path, staging: Path, password: str | None, prog: _Progress, tool: str | None,
                 names: list[str] | None = None) -> None:
    found = find_rar_tool(tool)
    if not found:
        raise MissingToolError("Extracting RAR archives needs UnRAR or 7-Zip (see Settings → Extraction).")
    kind, exe = found
    pw = password or "-"
    if kind == "unrar":
        # -ol-: do not create symlinks; -o+: overwrite inside staging; -y: assume yes
        files = names if names is not None and len(names) <= 400 else []   # long lists: extract all, move some
        cmd = [exe, "x", "-y", "-o+", "-ol-", "-idq", f"-p{pw}", "--", str(archive), *files, str(staging) + os.sep]
    elif kind == "7z":
        files = names if names is not None and len(names) <= 400 else []
        cmd = [exe, "x", "-y", "-snl-", f"-p{password or ''}", f"-o{staging}", str(archive), *files]
    else:
        cmd = [exe, "-xf", str(archive), "-C", str(staging), "--no-same-owner"]
    flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=flags)
    output: list[bytes] = []
    reader = threading.Thread(target=lambda: output.append(proc.stdout.read() if proc.stdout else b""),
                              daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            time.sleep(0.4)
            prog.done = dir_size(staging)
            prog.report()
    except ExtractionCancelled:
        proc.kill()
        proc.wait()
        raise
    reader.join(timeout=5)
    if proc.returncode != 0:
        raise _rar_error(b"".join(output).decode("utf-8", "replace"), proc.returncode)


def _extract_single(src: Path, target: Path, fmt: ArchiveFormat, prog: _Progress) -> None:
    opener = {ArchiveFormat.GZIP: gzip.open, ArchiveFormat.BZIP2: bz2.open, ArchiveFormat.XZ: lzma.open}[fmt]
    try:
        with opener(src, "rb") as fin, open(target, "wb") as fout:
            while True:
                block = fin.read(1024 * 1024)
                if not block:
                    break
                fout.write(block)
                prog.add(len(block), target.name)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise CorruptArchiveError(f"The compressed file is incomplete or corrupted: {exc}") from exc


def _verify_tree(staging: Path) -> None:
    """Post-extraction audit: nothing may be a link or point outside staging."""
    root = os.path.realpath(staging)
    for dirpath, dirnames, filenames in os.walk(staging):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                os.unlink(p)
                log.warning("removed symlink produced by extractor: %s", name)
                continue
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                os.unlink(p)
                continue
            if not os.path.realpath(p).startswith(root + os.sep):
                raise UnsafeArchiveError(f"extracted item escaped the destination: {name}")


def _move_into(staging: Path, dest: Path, overwrite: bool, only: set[str] | None = None) -> tuple[int, int]:
    files = total = 0
    for dirpath, _dirs, filenames in os.walk(staging):
        rel = Path(dirpath).relative_to(staging)
        if only is None:
            (dest / rel).mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            src = Path(dirpath) / fn
            if only is not None and _norm((rel / fn).as_posix()) not in only:
                continue                          # repair: only the files that were asked for
            (dest / rel).mkdir(parents=True, exist_ok=True)
            dst = dest / rel / fn
            if dst.exists():
                if not overwrite:
                    raise ArchiveError(f"{dst} already exists")
                if dst.is_dir():
                    raise ArchiveError(f"cannot overwrite folder {dst} with a file")
            size = src.stat().st_size
            os.replace(src, dst)
            files += 1
            total += size
    return files, total


def cleanup_stale_staging(dest_root: Path) -> int:
    """Remove staging folders left behind by a crash (only our own prefix)."""
    removed = 0
    if not dest_root.exists():
        return 0
    for dirpath, dirnames, _ in os.walk(dest_root):
        for d in list(dirnames):
            if d.startswith(STAGING_PREFIX):
                shutil.rmtree(Path(dirpath) / d, ignore_errors=True)
                dirnames.remove(d)
                removed += 1
        if len(Path(dirpath).parts) - len(dest_root.parts) >= 2:
            dirnames[:] = []
    return removed
