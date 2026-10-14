"""Save-game backups for games that run in their own Proton prefix (Linux).

Windows games keep saves and settings in the prefix's user folders (``AppData``, ``Documents``, ``Saved Games``)
and, for most cracked releases, in ``users/Public/Documents`` (OnlineFix, RUNE, CODEX …). A backup is a zip of
those folders without caches (shader/DXVK caches, logs, crash dumps), so it stays small:

* automatically after a play session, before an update, an uninstall, a prefix rebuild or a restore;
* by hand from the Library ("Back up now").

Backups are stored per game *title* (not per library entry), so a game that is uninstalled and installed again
finds its old saves. Restoring never deletes anything: it writes the backed-up files over the current ones, after
backing up the current state first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

MANIFEST = "ezgd-save-manifest.json"
# top-level folders inside <prefix>/pfx/drive_c/users that hold saves
ROOTS = ("steamuser", "Public")
# folders (any depth, case-insensitive) that are caches or logs, never saves
SKIP_DIRS = {"temp", "tmp", "cache", "caches", "shader_cache", "shadercache", "shaders_cache", "gpucache",
             "code cache", "d3dscache", "dxvk", "vkd3d", "vulkan", "nvidia", "nvidia corporation", "amd", "logs",
             "log", "crashes", "crashdumps", "crashreports", "sentry", "webcache", "inetcache",
             "packages", "downloads", "music", "pictures", "videos", "templates", "desktop", "favorites", "links",
             "contacts", "searches", "start menu", "mesa_shader_cache", "pipelinecache", "htmlcache"}
# Windows' own folders directly under AppData/Roaming|Local|LocalLow — never game data
SKIP_APPDATA = {"microsoft", "temp", "packages", "programs", "connecteddevicesplatform", "comms", "d3dscache",
                "crashdumps", "nvidia", "nvidia corporation", "openal"}
SKIP_SUFFIXES = (".log", ".dmp", ".mdmp", ".dxvk-cache", ".dxvk.bin", ".dxvk.lut", ".tmp", ".pdb", ".etl",
                 ".shadercache", ".pipelinecache")
MAX_FILE = 512 * 1024 * 1024                  # a single "save" bigger than this is not a save
AUTO_MAX_BYTES = 768 * 1024 * 1024            # automatic backups skip games whose saves are larger
KEEP = {"auto": 10, "safety": 10, "manual": 20}
SAFETY_REASONS = ("before-update", "before-uninstall", "before-restore", "before-reset", "before-fix")
REASON_TEXT = {"auto": "After playing", "manual": "Backed up by you", "before-update": "Before an update",
               "before-uninstall": "Before uninstalling", "before-restore": "Before restoring a backup",
               "before-reset": "Before rebuilding the prefix", "before-fix": "Before a fix changed the prefix"}


class SaveBackupError(Exception):
    pass


@dataclass
class SaveBackup:
    path: Path
    created: float
    reason: str
    files: int
    bytes: int
    title: str = ""
    entry_id: int | None = None
    fingerprint: str = ""
    folders: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"path": str(self.path), "created": self.created, "reason": self.reason,
                "reason_text": REASON_TEXT.get(self.reason, self.reason), "files": self.files, "bytes": self.bytes,
                "title": self.title, "entry_id": self.entry_id, "folders": self.folders,
                "size": self.path.stat().st_size if self.path.exists() else 0}


def key_for(title: str) -> str:
    """Folder name for a game's backups — the same for every install of that title."""
    from ezgd.catalog.normalize import dedup_key
    k = re.sub(r"[^a-z0-9]+", "-", dedup_key(title).lower()).strip("-")
    return k[:80] or "game"


def users_dir(prefix: Path) -> Path:
    """``<prefix>/pfx/drive_c/users`` (Steam/Proton layout) or ``<prefix>/drive_c/users`` (umu-launcher)."""
    prefix = Path(prefix)
    if not (prefix / "pfx").is_dir() and (prefix / "drive_c").is_dir():
        return prefix / "drive_c" / "users"
    return prefix / "pfx" / "drive_c" / "users"


def _skip_dir(rel: PurePosixPath, name: str) -> bool:
    low = name.lower()
    if low in SKIP_DIRS:
        return True
    parts = [p.lower() for p in rel.parts]
    # <root>/AppData/<Roaming|Local|LocalLow>/<name>: Windows' own folders
    if len(parts) == 3 and parts[1] == "appdata" and low in SKIP_APPDATA:
        return True
    return False


def collect(prefix: Path) -> list[tuple[Path, str, int, float]]:
    """(file, name in the backup, size, mtime) for every save/setting file in the prefix — symlinks are never
    followed (Wine's "My Documents" etc. point back into the same tree or out of it)."""
    base = users_dir(prefix)
    out: list[tuple[Path, str, int, float]] = []
    for root in ROOTS:
        top = base / root
        if not top.is_dir() or top.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            d = Path(dirpath)
            rel = PurePosixPath(d.relative_to(base).as_posix())
            dirnames[:] = [n for n in dirnames if not (d / n).is_symlink() and not _skip_dir(rel, n)]
            for fn in filenames:
                f = d / fn
                if fn.lower().endswith(SKIP_SUFFIXES) or f.is_symlink():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_size > MAX_FILE:
                    continue
                out.append((f, (rel / fn).as_posix(), st.st_size, st.st_mtime))
    out.sort(key=lambda x: x[1])
    return out


def fingerprint(files: list[tuple[Path, str, int, float]]) -> str:
    h = hashlib.sha1()
    for _f, name, size, mtime in files:
        h.update(f"{name}\0{size}\0{int(mtime)}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _folders(files: list[tuple[Path, str, int, float]]) -> list[str]:
    """The game folders a backup covers, for display ("AppData/Roaming/SlayTheSpire2", "Documents/OnlineFix/…")."""
    seen: list[str] = []
    for _f, name, _s, _m in files:
        parts = name.split("/")
        if len(parts) >= 4 and parts[1] == "AppData":
            k = "/".join(parts[1:4])
        elif len(parts) >= 4 and parts[1] == "Documents" and parts[2] in ("OnlineFix", "Steam", "My Games"):
            k = "/".join(parts[1:4])
        else:
            k = "/".join(parts[1:3]) if len(parts) >= 3 else parts[-1]
        if k not in seen:
            seen.append(k)
    return seen[:12]


def list_backups(folder: Path) -> list[SaveBackup]:
    out = []
    for z in sorted(Path(folder).glob("*.zip")) if Path(folder).is_dir() else []:
        try:
            with zipfile.ZipFile(z) as zf:
                m = json.loads(zf.read(MANIFEST))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        out.append(SaveBackup(path=z, created=float(m.get("created") or z.stat().st_mtime),
                              reason=str(m.get("reason") or "manual"), files=int(m.get("files") or 0),
                              bytes=int(m.get("bytes") or 0), title=str(m.get("title") or ""),
                              entry_id=m.get("entry_id"), fingerprint=str(m.get("fingerprint") or ""),
                              folders=list(m.get("folders") or [])))
    out.sort(key=lambda b: b.created, reverse=True)
    return out


def _klass(reason: str) -> str:
    return "auto" if reason == "auto" else "manual" if reason == "manual" else "safety"


def prune(folder: Path) -> int:
    removed = 0
    count: dict[str, int] = {}
    for b in list_backups(folder):                       # newest first
        k = _klass(b.reason)
        count[k] = count.get(k, 0) + 1
        if count[k] > KEEP[k]:
            try:
                b.path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def backup(prefix: Path, folder: Path, *, title: str, reason: str, entry_id: int | None = None,
           max_bytes: int | None = None, force: bool = False) -> SaveBackup | None:
    """Zip the prefix's saves into ``folder``. Returns None when there is nothing to save, nothing changed since the
    newest backup (unless ``force``), or the saves are larger than ``max_bytes``."""
    files = collect(prefix)
    if not files:
        return None
    total = sum(f[2] for f in files)
    if max_bytes is not None and total > max_bytes:
        log.info("saves of %s are %d bytes — larger than %d, not backed up automatically", title, total, max_bytes)
        return None
    fp = fingerprint(files)
    folder = Path(folder)
    if not force:
        newest = next(iter(list_backups(folder)), None)
        if newest is not None and newest.fingerprint == fp:
            return None
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    name = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{reason}.zip"
    target = folder / name
    n = 1
    while target.exists():
        n += 1
        target = folder / f"{name[:-4]}-{n}.zip"
    tmp = target.with_suffix(".zip.part")
    written = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f, arc, _size, _mtime in files:
                try:
                    zf.write(f, arc)
                    written += 1
                except OSError as exc:              # a file the game is writing right now: skip it
                    log.info("save backup of %s: skipped %s (%s)", title, arc, exc)
            manifest = {"title": title, "entry_id": entry_id, "reason": reason, "created": now, "files": written,
                        "bytes": total, "fingerprint": fp, "folders": _folders(files), "format": 1}
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1))
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SaveBackupError(f"Could not write the save backup: {exc.strerror or exc}") from exc
    prune(folder)
    log.info("saves of %s backed up (%s): %d files, %d bytes → %s", title, reason, written, total, target.name)
    return SaveBackup(path=target, created=now, reason=reason, files=written, bytes=total, title=title,
                      entry_id=entry_id, fingerprint=fp, folders=manifest["folders"])


def _safe_member(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or not p.parts or ".." in p.parts or p.parts[0] not in ROOTS or name.endswith("/"):
        return None
    if any(":" in part for part in p.parts):
        return None
    return p


def restore(zip_path: Path, prefix: Path) -> int:
    """Write the backed-up files into the prefix (over the current ones). Returns the number of files written."""
    base = users_dir(prefix)
    if not base.is_dir():
        raise SaveBackupError("The game's prefix does not exist yet — start the game once (to its main menu), quit "
                              "it, then restore.")
    real_base = Path(os.path.realpath(base))
    n = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.filename == MANIFEST or info.is_dir():
                    continue
                rel = _safe_member(info.filename)
                if rel is None:
                    log.warning("save restore: refused member %r", info.filename)
                    continue
                target = base.joinpath(*rel.parts)
                # the target must stay inside the prefix's users folder, also through symlinked folders
                parent = Path(os.path.realpath(target.parent)) if target.parent.exists() else None
                if parent is not None and not (parent == real_base or real_base in parent.parents):
                    log.warning("save restore: %s leaves the prefix, skipped", info.filename)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    target.unlink()
                tmp = target.with_name(target.name + ".ezgd-restore")
                with zf.open(info) as src, open(tmp, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                os.replace(tmp, target)
                ts = time.mktime(info.date_time + (0, 0, -1))
                os.utime(target, (ts, ts))
                n += 1
    except zipfile.BadZipFile as exc:
        raise SaveBackupError(f"The backup file is damaged: {exc}") from exc
    except OSError as exc:
        raise SaveBackupError(f"Could not restore: {exc.strerror or exc}") from exc
    log.info("restored %d save file(s) from %s", n, Path(zip_path).name)
    return n
