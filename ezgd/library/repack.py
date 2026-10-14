"""Installer releases (FitGirl and other repacks): the game has to be installed before it can be played.

A repack is ``setup.exe`` plus compressed data files (``fg-01.bin`` …). Downloading it gives an installer, not a
game folder: setup.exe has to unpack and install the game, and only then is there a game executable for Play.

The app never starts an installer by itself. The user presses “Install game” and confirms; setup.exe then runs
(through Proton in the game's own prefix on Linux) with Inno Setup's ``/DIR=`` preset to a folder in the app's
installation folder, which the user can still change in the installer. When the installer exits, the installed
game is found — the preset folder, the installer's uninstall record in the prefix's registry, or a new folder
on the prefix's C: drive — and becomes the game's install folder. Everything here only reads files.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_SETUP = re.compile(r"^setup(?:[_-][\w.\-]+)?\.exe$", re.I)
_DATA = re.compile(r"\.(bin|doi|dat\d*|esd)$", re.I)
_SKIP_DIRS = {"_redist", "redist", "_commonredist", "md5", "__installer", "directx", "vcredist"}
_UNINSTALL_KEY = re.compile(r"^\[(?:Software\\\\(?:Wow6432Node\\\\)?)Microsoft\\\\Windows\\\\CurrentVersion\\\\"
                            r"Uninstall\\\\([^\]]+)\]\s*(\d+)?", re.I | re.M)
_REG_VALUE = re.compile(r'^"([^"]+)"="((?:[^"\\]|\\.)*)"\s*$', re.M)


@dataclass
class Repack:
    setup: Path                     # the installer to run
    root: Path                      # folder holding setup.exe and its data files
    kind: str                       # fitgirl | dodi | gog | inno
    data_files: list[str] = field(default_factory=list)
    data_bytes: int = 0
    md5_file: Path | None = None    # FitGirl's MD5/fitgirl-bins.md5 (checked before installing, optional)
    optional: list[str] = field(default_factory=list)   # fg-optional-*/fg-selective-* parts present

    @property
    def label(self) -> str:
        return {"fitgirl": "FitGirl repack", "dodi": "DODI repack", "gog": "GOG offline installer"}.get(
            self.kind, "game installer")

    def to_dict(self) -> dict:
        return {"setup": str(self.setup), "root": str(self.root), "kind": self.kind, "label": self.label,
                "data_files": len(self.data_files), "data_bytes": self.data_bytes,
                "md5": str(self.md5_file) if self.md5_file else None, "optional": self.optional}


def _setup_in(folder: Path) -> Repack | None:
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        return None
    setups = sorted((p for p in files if _SETUP.match(p.name)), key=lambda p: (p.name.lower() != "setup.exe", p.name))
    if not setups:
        return None
    data = [p for p in files if _DATA.search(p.name) and not p.name.lower().endswith(".exe")]
    names = [p.name.lower() for p in data]
    if not data:
        return None                     # a lone setup.exe is a redistributable or a tool, not a repack
    if any(n.startswith("fg-") for n in names) or (folder / "MD5").is_dir() and \
            any(f.name.lower().startswith("fitgirl") for f in (folder / "MD5").glob("*")):
        kind = "fitgirl"
    elif any(n.endswith(".doi") for n in names) or "dodi" in folder.name.lower():
        kind = "dodi"
    elif setups[0].name.lower().startswith("setup_") and any(n.startswith("setup_") for n in names):
        kind = "gog"
    else:
        kind = "inno"
    md5 = next((p for p in sorted((folder / "MD5").glob("*.md5"))), None) if (folder / "MD5").is_dir() else None
    size = 0
    for p in data:
        try:
            size += p.stat().st_size
        except OSError:
            pass
    return Repack(setups[0], folder, kind, sorted(p.name for p in data), size, md5,
                  sorted(n for n in (p.name for p in data) if re.match(r"fg-(optional|selective)", n, re.I)))


def detect(root: Path | str | None, max_depth: int = 2) -> Repack | None:
    """The repack in ``root`` (or up to ``max_depth`` folders below it), if it is one."""
    if not root:
        return None
    root = Path(root)
    if root.is_file():
        root = root.parent
    if not root.is_dir():
        return None
    found = _setup_in(root)
    if found:
        return found
    base = len(root.parts)
    for dirpath, dirnames, _files in os.walk(root):
        depth = len(Path(dirpath).parts) - base
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
        for d in dirnames:
            found = _setup_in(Path(dirpath) / d)
            if found:
                return found
    return None


def windows_path(path: Path) -> str:
    """How Windows programs under Wine/Proton see a Linux path (drive Z: is the Linux root)."""
    if os.name == "nt":
        return str(path)
    return "Z:" + str(Path(path)).replace("/", "\\")


def unix_path(win: str, pfx: Path) -> Path | None:
    """A Windows path from inside a prefix (``C:\\Games\\X``) as a Linux path."""
    m = re.match(r"^\s*([A-Za-z]):[\\/]*(.*?)[\\/]*\s*$", win or "")
    if not m:
        return None
    letter, rest = m.group(1).lower(), m.group(2)
    dev = pfx / "dosdevices" / f"{letter}:"
    if dev.exists():
        base = Path(os.path.realpath(dev))
    elif letter == "c":
        base = pfx / "drive_c"
    elif letter == "z":
        base = Path("/")
    else:
        return None
    parts = [p for p in re.split(r"[\\/]+", rest) if p and p not in (".", "..")]
    return base.joinpath(*parts)


def _reg_unescape(s: str) -> str:
    s = re.sub(r"\\x([0-9a-fA-F]{1,4})", lambda m: chr(int(m.group(1), 16)), s)
    return s.replace('\\"', '"').replace("\\\\", "\\")


def uninstall_records(pfx: Path) -> list[dict]:
    """Programs installed into a Wine prefix, from its registry files: name, location, time written."""
    out = []
    for reg in ("system.reg", "user.reg"):
        try:
            text = (pfx / reg).read_text("utf-8", "replace")
        except OSError:
            continue
        keys = list(_UNINSTALL_KEY.finditer(text))
        for i, k in enumerate(keys):
            end = keys[i + 1].start() if i + 1 < len(keys) else len(text)
            nxt = text.find("\n[", k.end())
            block = text[k.end():min(end, nxt if nxt >= 0 else end)]
            vals = {m.group(1): _reg_unescape(m.group(2)) for m in _REG_VALUE.finditer(block)}
            loc = vals.get("Inno Setup: App Path") or vals.get("InstallLocation")
            if not loc:
                continue
            out.append({"key": _reg_unescape(k.group(1)), "name": vals.get("DisplayName") or k.group(1),
                        "location": loc, "time": int(k.group(2)) if k.group(2) else 0,
                        "uninstaller": vals.get("UninstallString")})
    return out


def _has_game_exe(folder: Path) -> bool:
    from ezgd.library.executables import _NOT_GAME, find_executables
    try:
        return any(not _NOT_GAME.search(p.stem) for p in find_executables(folder, max_depth=4, limit=60)
                   if p.suffix.lower() == ".exe")
    except OSError:
        return False


def _title_score(name: str, title: str) -> int:
    from ezgd.catalog.normalize import tokens
    return len(set(tokens(name)) & set(tokens(title)))


def find_installed(target: Path, pfx: Path | None, started: float, title: str,
                   exclude: Path | None = None) -> Path | None:
    """Where the installer put the game: the preset folder, its uninstall record, or a new folder on C:."""
    def ok(p: Path | None) -> bool:
        if p is None or not p.is_dir():
            return False
        if exclude is not None:
            try:
                rp, rx = p.resolve(), exclude.resolve()
                if rp == rx or rx in rp.parents:
                    return False              # the repack folder itself is not the installed game
            except OSError:
                return False
        return _has_game_exe(p)

    if ok(target):
        return target
    if pfx is not None and pfx.is_dir():
        recs = [r for r in uninstall_records(pfx) if not r["time"] or r["time"] >= started - 120]
        recs.sort(key=lambda r: (_title_score(r["name"], title), r["time"]), reverse=True)
        for r in recs:
            p = unix_path(r["location"], pfx)
            if ok(p):
                return p
        roots = [pfx / "drive_c" / d for d in ("Games", "Program Files", "Program Files (x86)", "GOG Games")]
    else:
        roots = []
    roots.append(target.parent)
    fresh = []
    for r in roots:
        try:
            children = [c for c in r.iterdir() if c.is_dir()]
        except OSError:
            continue
        for c in children:
            try:
                if c.stat().st_mtime >= started - 5 and ok(c):
                    fresh.append(c)
            except OSError:
                continue
    fresh.sort(key=lambda c: (_title_score(c.name, title), c.stat().st_mtime), reverse=True)
    return fresh[0] if fresh else None


def install_target(install_dir: Path, title: str, repack_root: Path) -> Path:
    """The folder offered to the installer: <install dir>/<title>, never the repack's own folder."""
    import re as _re
    from ezgd.core.fsutil import safe_dirname
    # "Heroes' Vow: Three Kingdoms" → "Heroes' Vow - Three Kingdoms" (not "Heroes' Vow_ Three Kingdoms")
    name = safe_dirname(_re.sub(r"\s*[:|/\\]\s*", " - ", title).strip(" -")) or "Game"
    t = Path(install_dir) / name
    try:
        same = t.resolve() == repack_root.resolve() or t.resolve() in repack_root.resolve().parents
    except OSError:
        same = False
    if same or (t.exists() and any(t.iterdir()) and not _has_game_exe(t)):
        t = Path(install_dir) / f"{name} (installed)"
    return t


def verify_md5(rep: Repack, progress=None, cancel=None) -> dict:
    """Check the repack's data files against FitGirl's MD5 list (what its “Verify BIN files” script does)."""
    if rep.md5_file is None:
        return {"available": False, "checked": 0, "bad": [], "missing": []}
    try:
        lines = rep.md5_file.read_text("utf-8", "replace").splitlines()
    except OSError:
        return {"available": False, "checked": 0, "bad": [], "missing": []}
    entries = []
    for ln in lines:
        m = re.match(r"^([0-9a-fA-F]{32})\s+\*?(.+?)\s*$", ln)
        if m and not ln.startswith(";"):
            rel = m.group(2).replace("\\", "/").lstrip("./")
            entries.append((m.group(1).lower(), rel))
    total = 0
    for _h, rel in entries:
        p = rep.root / rel
        total += p.stat().st_size if p.is_file() else 0
    done, bad, missing = 0, [], []
    for h, rel in entries:
        p = rep.root / rel
        if not p.is_file() or ".." in Path(rel).parts:
            missing.append(rel)
            continue
        md5 = hashlib.md5(usedforsecurity=False)
        with open(p, "rb") as fh:
            while chunk := fh.read(4 * 1024 * 1024):
                if cancel is not None and cancel.is_set():
                    return {"available": True, "checked": 0, "bad": [], "missing": [], "cancelled": True}
                md5.update(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total, Path(rel).name)
        if md5.hexdigest() != h:
            bad.append(rel)
    return {"available": True, "checked": len(entries), "bad": bad, "missing": missing}
