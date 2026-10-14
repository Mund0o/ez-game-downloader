"""Release integrity: compare an installed game with the archive(s) it was extracted from, and repair it.

Every archive stores its file list with sizes. Files that are missing, empty or of the wrong size after
extraction (incomplete download, a crashed extraction, something deleted later) are found without needing any
checksum from the site, and re-extracted from the same archive — only those files.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ezgd.installation import archives as ar

log = logging.getLogger(__name__)

# files games and players change on purpose (settings, saves, logs): a different size is not damage
_MUTABLE = re.compile(r"\.(ini|cfg|conf|config|log|txt|json|xml|sav|save|dat\.bak|bak|tmp|lock|db|sqlite|"
                      r"cache|cfgx|vdf)$", re.I)


@dataclass
class Issue:
    rel: str               # path inside the game folder
    kind: str              # "missing" | "damaged"
    expected: int | None
    actual: int | None
    archive: str
    dest: str


@dataclass
class IntegrityReport:
    checked: int = 0
    issues: list[Issue] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)     # archives that no longer exist
    errors: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[Issue]:
        return [i for i in self.issues if i.kind == "missing"]

    @property
    def damaged(self) -> list[Issue]:
        return [i for i in self.issues if i.kind == "damaged"]

    def to_dict(self) -> dict:
        return {"checked": self.checked, "missing": len(self.missing), "damaged": len(self.damaged),
                "examples": [f"{i.rel} ({'missing' if i.kind == 'missing' else f'{i.actual} of {i.expected} bytes'})"
                             for i in self.issues[:6]],
                "archives": self.archives, "unverifiable": self.unverifiable, "errors": self.errors,
                "repairable": sum(1 for i in self.issues if Path(i.archive).exists())}


def check(records: list[dict], password: str | None = None, rar_tool: str | None = None) -> IntegrityReport:
    """``records``: completed extractions of this game ({"archive", "dest"}), oldest first."""
    rep = IntegrityReport()
    expected: dict[tuple[str, str], tuple[int | None, str]] = {}
    for r in records:
        archive, dest = Path(r["archive"]), Path(r["dest"])
        if not archive.exists():
            rep.unverifiable.append(archive.name)
            continue
        fmt = ar.detect_format(archive)
        if fmt is None:
            continue
        try:
            members = ar.list_members(archive, fmt, password, rar_tool)
        except ar.ArchiveError as exc:
            rep.errors.append(f"{archive.name}: {exc}")
            continue
        rep.archives.append(archive.name)
        for m in members:
            if m.is_dir or m.is_link or m.is_special:
                continue
            rel = ar._norm(m.name)
            if not rel:
                continue
            expected[(str(dest), rel)] = (m.size, str(archive))    # a later archive (e.g. a fix) wins
    for (dest, rel), (size, archive) in expected.items():
        rep.checked += 1
        try:
            p = ar.safe_member_path(Path(dest), rel)
        except ar.ArchiveError:
            continue
        if not p.exists():
            if Path(str(p) + ".old").exists() or any(p.parent.glob(p.name + ".*.old")):
                continue                     # renamed on purpose by the release's own launch script
            rep.issues.append(Issue(rel, "missing", size, None, archive, dest))
            continue
        if size is None or _MUTABLE.search(p.name):
            continue
        actual = p.stat().st_size
        if actual != size:
            rep.issues.append(Issue(rel, "damaged", size, actual, archive, dest))
    return rep


def repair(rep: IntegrityReport, password: str | None = None, rar_tool: str | None = None,
           progress=None) -> dict:
    """Re-extract only the missing/damaged files, from the archive each one came from."""
    by_src: dict[tuple[str, str], set[str]] = {}
    skipped = []
    for i in rep.issues:
        if Path(i.archive).exists():
            by_src.setdefault((i.archive, i.dest), set()).add(i.rel)
        else:
            skipped.append(i.rel)
    repaired, failed = 0, []
    for (archive, dest), names in by_src.items():
        try:
            res = ar.extract(Path(archive), Path(dest), password=password, rar_tool=rar_tool, only=names,
                             progress=progress, overwrite=True)
            repaired += res.files
        except ar.ArchiveError as exc:
            failed.append(f"{Path(archive).name}: {exc}")
    log.info("integrity repair: %d file(s) restored, %d failed archive(s), %d without archive",
             repaired, len(failed), len(skipped))
    return {"repaired": repaired, "failed": failed, "no_archive": len(skipped)}


def backup_dir_for(dest: Path) -> Path:
    import time
    return Path(dest) / f".ezgd-backup-{time.strftime('%Y%m%d-%H%M%S')}"


def backup_overwritten(archive: Path, dest: Path, password: str | None = None,
                       rar_tool: str | None = None) -> Path | None:
    """Copy the installed files an archive is about to replace into ``dest/.ezgd-backup-<time>/`` (same layout)."""
    import shutil
    fmt = ar.detect_format(archive)
    if fmt is None:
        return None
    try:
        members = ar.list_members(archive, fmt, password, rar_tool)
    except ar.ArchiveError:
        return None
    bdir = backup_dir_for(dest)
    n = 0
    for m in members:
        if m.is_dir or m.is_link or m.is_special:
            continue
        rel = ar._norm(m.name)
        try:
            src = ar.safe_member_path(Path(dest), rel)
        except ar.ArchiveError:
            continue
        if src.is_file():
            t = bdir / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, t)
            n += 1
    return bdir if n else None


def copy_tree_with_backup(src: Path, dst: Path) -> tuple[int, Path | None]:
    """Copy a release's Crack/Fix/Update folder over ``dst``; replaced files go to ``dst/.ezgd-backup-<time>/``."""
    import shutil
    bdir = backup_dir_for(dst)
    copied = backed = 0
    for f in Path(src).rglob("*"):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(src)
        t = Path(dst) / rel
        if t.exists():
            b = bdir / rel
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(t, b)
            backed += 1
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
        copied += 1
    return copied, (bdir if backed else None)
