"""Post-download processing.

Download, verification, extraction and installation are separate states:
a completed transfer becomes a library entry in ``downloaded`` state; an
optional extraction moves it to ``extracted``; only the user marks a game
``installed`` (after choosing its executable). Nothing is ever executed here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ezgd.core.fsutil import InsufficientSpaceError, is_within, safe_dirname
from ezgd.database.repositories import ExtractionRepository, GameRepository, LibraryRepository
from ezgd.downloads.models import DownloadTask
from ezgd.installation import archives as ar
from ezgd.installation import volumes as vol
from ezgd.library.models import LibraryEntry, LibraryStatus

log = logging.getLogger(__name__)

Emit = Callable[[str, Any], None]
_FIX_ONLY = re.compile(r"fix[ _-]?repair", re.I)


_INSTALLER_NAMES = {"setup.exe", "installer.exe", "install.exe"}
_REDIST_DIRS = {"_commonredist", "redist", "redistributables", "directx", "vcredist", "prereqs", "__installer"}


def suspicious_release_files(root: Path, max_depth: int = 2) -> list[str]:
    """Things OnlineFix says its releases never contain (installers, a renpy folder).

    From the warning on hosters.online-fix.me: "If the downloaded archive contains items such as a
    renpy folder, setup.exe, Installer.exe … it is likely not our release." Redistributable folders
    (DirectX, VC++ installers shipped with many games) are ignored.
    """
    found: list[str] = []
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base
        dirnames[:] = [d for d in dirnames if d.lower() not in _REDIST_DIRS]
        if depth >= max_depth:
            dirnames[:] = []
        for d in dirnames:
            if d.lower() == "renpy":
                found.append(str((Path(dirpath) / d).relative_to(root)) + "/")
        for f in filenames:
            if f.lower() in _INSTALLER_NAMES or f.lower().endswith(".msi"):
                found.append(str((Path(dirpath) / f).relative_to(root)))
    return sorted(found)


def choose_archives(found: list[Path]) -> tuple[list[Path], list[Path]]:
    """Split archives into (extract automatically, leave for the user).

    OnlineFix torrents contain the full game archive plus an optional
    "Fix Repair" archive that only applies to copies from elsewhere; it is
    not auto-extracted when a main archive exists.
    """
    main = [p for p in found if not any(_FIX_ONLY.search(part) for part in p.parts[-2:])]
    if not main:
        return found, []
    return main, [p for p in found if p not in main]


class PostProcessor:
    def __init__(self, library: LibraryRepository, extractions: ExtractionRepository, games: GameRepository,
                 settings, emit: Emit, created=None):
        self.library = library
        self.extractions = extractions
        self.games = games
        self.settings = settings
        self.emit = emit
        self._cancel: dict[int, threading.Event] = {}
        self._running: dict[int, asyncio.Task] = {}
        self._sets_extracting: set[tuple[int, str]] = set()     # multi-part sets being extracted
        self.created = created                                  # CreatedPathsRepository (what the app made)
        self.choose_executable = None        # LibraryManager.auto_executable: Play works right after extracting
        self.before_update = None            # LibraryManager.backup_saves: saves are zipped before an update
        self.resumable: list[dict] = []      # extractions the app was closed during (finished on the next start)

    # -------------------------------------------------------------- entries
    _FIX = re.compile(r"(fix[\s._-]*repair|fix files only|_fix_|hot[\s._-]?fix|\bupdate\b|\bpatch\b)", re.I)

    def updating_entry(self, game_id: str | None, title: str | None) -> LibraryEntry | None:
        """The library entry waiting for a new version of this game (the user pressed Update), if any."""
        from ezgd.catalog.normalize import dedup_key
        key = dedup_key(title) if title else None
        for e in self.library.all():
            if (e.release or {}).get("updating") and ((game_id and e.game_id == game_id) or
                                                      (key and e.dedup_key == key)):
                return e
        return None

    def update_dest(self, entry: LibraryEntry) -> Path | None:
        """Where an update goes: the folder the game's archive was extracted into (so it overwrites the game)."""
        from ezgd.library import updates
        rows = sorted((r for r in self.extractions.for_library(entry.id) if r.get("status") == "completed"),
                      key=lambda r: r["id"])
        for r in rows:
            if not updates.is_fix_archive(Path(r["archive_path"]).name):
                return Path(r["dest_dir"])
        return Path(entry.install_path) if entry.install_path else None

    def entry_for_download(self, task: DownloadTask, content: Path | None) -> LibraryEntry:
        entry = self.library.by_download(task.id) if task.id else None
        if entry is None:
            game = self.games.get(task.game_id) if task.game_id else None
            upd = self.updating_entry(task.game_id, game.title if game else task.title)
            if upd is not None:                 # the new version of a game in the Library goes over that game
                upd.download_id = task.id
                upd.download_path = str(content) if content else str(Path(task.dest_dir) / (task.filename or ""))
                self.library.update(upd)
                self.emit("library_changed", upd.id)
                return upd
        if entry is None and task.game_id and self._FIX.search(task.filename or task.title or ""):
            # a fix/update for a game already in the library joins that entry instead of becoming a second game
            entry = next((e for e in self.library.all() if e.game_id == task.game_id and e.install_path), None)
            if entry is not None:
                return entry
        game = self.games.get(task.game_id) if task.game_id else None
        if entry is None:
            entry = LibraryEntry(title=game.title if game else task.title, status=LibraryStatus.DOWNLOADED,
                                 game_id=task.game_id, source=task.source, download_id=task.id,
                                 cover_url=game.cover_url if game else None)
            entry.download_path = str(content) if content else str(Path(task.dest_dir) / (task.filename or ""))
            entry = self.library.insert(entry)
        else:
            entry.download_path = str(content) if content else entry.download_path
            if entry.status in (LibraryStatus.BROKEN,):
                entry.status = LibraryStatus.DOWNLOADED
            self.library.update(entry)
        self.emit("library_changed", entry.id)
        return entry

    def steam_base(self, entry: LibraryEntry) -> tuple[str, Path | None] | None:
        """For OnlineFix pages that only offer a fix: (the free Steam game it goes into, its folder if installed)."""
        game = self.games.get(entry.game_id) if entry.game_id else None
        name = game.extra.get("steam_base_game") if game else None
        if not name:
            return None
        from ezgd.platform import steam
        hit = steam.find_installed_game(steam.find_steam_root(self.settings.settings.steam_root or None), name)
        return name, (hit["folder"] if hit and hit["complete"] else None)

    def default_extract_dest(self, entry: LibraryEntry, archive: Path) -> Path:
        base = self.steam_base(entry)
        if base and base[1] is not None:
            return base[1]                            # the fix goes into the Steam game's own folder
        s = self.settings.settings
        if s.extract_into_install_dir:
            return Path(s.install_dir) / safe_dirname(entry.title)
        return archive.parent / safe_dirname(re.sub(r"(\.part\d+)?\.(rar|zip|7z|tar(\.\w+)?|\d{3})$", "",
                                                     archive.name, flags=re.I) or entry.title)

    # ------------------------------------------------------ multi-part sets
    def entry_for_set(self, game_id: str | None, title: str, set_key: str) -> LibraryEntry | None:
        """The library entry already collecting the parts of ``set_key`` for this game."""
        for e in self.library.all():
            if (e.game_id == game_id if game_id else e.title == title) and e.download_path:
                v = vol.volume_of(Path(e.download_path))
                if v is not None and v.set_key == set_key:
                    return e
        return None

    def expected_parts(self, game_id: str | None, set_key: str) -> int | None:
        game = self.games.get(game_id) if game_id else None
        names = [m.file_name or m.label.split("—")[-1].strip() for m in (game.download_methods or [])] if game else []
        return vol.expected_parts(names, set_key)

    async def add_volume(self, entry: LibraryEntry, part: Path, volume, password: str | None, *,
                         extract: bool = True, dest: Path | None = None) -> dict:
        """Record one finished part; extract the set from its first volume once every part is present."""
        status = vol.set_status(part.parent, volume.set_key, self.expected_parts(entry.game_id, volume.set_key))
        entry.download_path = str(part.parent / volume.first_name)
        key = (entry.id, volume.set_key)
        if not status.complete:
            entry.notes = f"Multi-part download: {status.summary}. Extraction starts when the last part is in."
            if entry.status not in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED):
                entry.status = LibraryStatus.DOWNLOADED
            self.library.update(entry)
            self.emit("library_changed", entry.id)
            return {"complete": False, "summary": status.summary, "extracting": False}
        done = entry.status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED) and \
            not (entry.release or {}).get("updating")
        if key in self._sets_extracting or done or entry.status is LibraryStatus.EXTRACTING:
            return {"complete": True, "summary": status.summary, "extracting": False}
        self._sets_extracting.add(key)
        entry.notes = f"Multi-part download: {status.summary}."
        self.library.update(entry)
        self.emit("library_changed", entry.id)
        if not extract:
            self._sets_extracting.discard(key)
            return {"complete": True, "summary": status.summary, "extracting": False}

        try:
            ok = await self.extract(entry, status.first, dest or self.default_extract_dest(entry, status.first),
                                    password, download_root=part.parent)
        finally:
            self._sets_extracting.discard(key)
        return {"complete": True, "summary": status.summary, "extracting": True, "extracted": ok}

    def _record(self, path: Path | None, kind: str, game_id: str | None) -> None:
        if self.created is not None and path is not None:
            try:
                self.created.add(path, kind, game_id)
            except Exception:                       # bookkeeping must never break a download
                log.exception("could not record %s", path)

    def _wait_for_steam_game(self, entry: LibraryEntry) -> bool:
        """A fix for a free Steam game that is not installed yet: keep it, explain, extract later."""
        base = self.steam_base(entry)
        if not base or base[1] is not None:
            return False
        entry.notes = (f"This is only the fix. Install “{base[0]}” in Steam (it is free) — the app puts the fix "
                       "into that game's folder by itself as soon as Steam has finished installing it.")
        entry.status = LibraryStatus.DOWNLOADED      # not playable before the fix is in (Play started the bare game)
        entry.setup = {"state": "waiting_steam", "base": base[0]}
        self.library.update(entry)
        self.emit("library_changed", entry.id)
        self.emit("notify", ("Install the game in Steam", f"{entry.title}: install “{base[0]}” in Steam (it is "
                                                          "free). The app adds the fix to it automatically when "
                                                          "Steam has finished."))
        return True

    def fix_archive_of(self, entry: LibraryEntry) -> Path | None:
        """The downloaded fix archive of a fix-only entry (the first volume of a set)."""
        if not entry.download_path:
            return None
        p = Path(entry.download_path)
        if p.is_file():
            return p if ar.detect_format(p) is not None and not ar.is_secondary_volume(p) else None
        found = ar.find_archives(p, 2) if p.is_dir() else []
        return found[0] if found else None

    @staticmethod
    def _has_onlinefix(folder: Path) -> bool:
        try:
            return any(True for n in ("OnlineFix64.dll", "OnlineFix.dll", "OnlineFix.ini")
                       for _ in folder.rglob(n))
        except OSError:
            return False

    def fixes_ready_for_steam(self) -> list[tuple[LibraryEntry, Path, Path]]:
        """Fix-only entries whose free Steam game is installed now but which do not have the fix in it yet:
        (entry, archive, Steam game folder). Covers entries waiting for Steam and ones left without the fix."""
        out = []
        for e in self.library.all():
            if e.status is LibraryStatus.EXTRACTING:
                continue
            waiting = (e.setup or {}).get("state") == "waiting_steam"
            if not waiting and e.source != "onlinefix":
                continue
            base = self.steam_base(e)
            if not base or base[1] is None:
                continue
            archive = self.fix_archive_of(e)
            if archive is None or self._has_onlinefix(base[1]):
                continue
            out.append((e, archive, base[1]))
        return out

    async def on_download_completed(self, task: DownloadTask, content: Path | None) -> LibraryEntry:
        self._record(content, "download", task.game_id)
        if content is not None and content.is_file():
            volume = vol.volume_of(content)
            if volume is not None:
                game = self.games.get(task.game_id) if task.game_id else None
                title = game.title if game else task.title
                entry = self.entry_for_set(task.game_id, title, volume.set_key) or \
                    self.entry_for_download(task, content)
                auto = task.auto_extract and self.settings.settings.auto_extract
                if auto and await asyncio.to_thread(self._wait_for_steam_game, entry):
                    auto = False
                dest = Path(task.extract_dest) if task.extract_dest else \
                    (self.update_dest(entry) if (entry.release or {}).get("updating") else None)
                await self.add_volume(entry, content, volume, task.archive_password, extract=auto, dest=dest)
                return entry
        entry = self.entry_for_download(task, content)
        if content is None or not content.exists():
            return entry
        if not (task.auto_extract and self.settings.settings.auto_extract):
            return entry
        if await asyncio.to_thread(self._wait_for_steam_game, entry):
            return entry
        found = await asyncio.to_thread(ar.find_archives, content)
        to_extract, left = choose_archives(found)
        if left:
            if to_extract and all(_FIX_ONLY.search(" ".join(a.parts[-2:])) for a in left):
                entry.notes = ("The Fix Repair archive was not needed — the game archive already contains the fix.")
            else:
                entry.notes = "Not extracted automatically: " + ", ".join(p.name for p in left)
            self.library.update(entry)
        if not to_extract:
            # not an archive (e.g. a pre-extracted folder) → the content itself is the game folder
            if content.is_dir():
                entry.install_path = str(content)
                entry.status = LibraryStatus.EXTRACTED
                installer = await asyncio.to_thread(self._mark_installer, entry)
                self.library.update(entry)
                self.emit("library_changed", entry.id)
                if installer:
                    self.emit("notify", ("Ready to install", f"{entry.title} is a repack: press “Install game” in "
                                                             f"your Library to run its installer."))
            return entry
        dest = Path(task.extract_dest) if task.extract_dest else \
            ((self.update_dest(entry) if (entry.release or {}).get("updating") else None)
             or self.default_extract_dest(entry, to_extract[0]))
        ok = True
        for archive in to_extract:
            ok = await self.extract(entry, archive, dest, task.archive_password, download_root=content)
            if not ok:
                break
        if ok and left and self.settings.settings.delete_archive_after_extract:
            # the torrent's "Fix Repair" is only for copies from elsewhere (the game archive has the fix): with
            # "delete the archive" on it goes too, so only the game is left (Diagnose can download it again)
            gone = [a for a in left if _FIX_ONLY.search(" ".join(a.parts[-2:]))]
            for a in gone:
                self._delete_archive(a, content if content.is_dir() else content.parent)
            if gone:
                entry = self.library.get(entry.id) or entry
                entry.notes = " ".join(filter(None, [re.sub(r"(Not extracted automatically: [^.]*|The Fix Repair "
                                                            r"archive was not needed[^.]*\.)", "",
                                                            entry.notes or "").strip(),
                                                     "The torrent's Fix Repair archive was deleted — the game archive "
                                                     "already contains the fix."])) or None
                self.library.update(entry)
        return entry

    # ------------------------------------------------------------ extraction
    async def extract(self, entry: LibraryEntry, archive: Path, dest: Path, password: str | None = None,
                      download_root: Path | None = None) -> bool:
        ext_id = self.extractions.create(entry.download_id, entry.id, str(archive), str(dest))
        cancel = threading.Event()
        self._cancel[ext_id] = cancel
        loop = asyncio.get_running_loop()
        prev_status = entry.status
        entry.status = LibraryStatus.EXTRACTING
        self.library.update(entry)
        self.extractions.update(ext_id, status="running")
        state = {"id": ext_id, "library_id": entry.id, "download_id": entry.download_id, "archive": archive.name,
                 "dest": str(dest), "status": "running", "progress": 0.0, "item": ""}
        self.emit("extraction", dict(state))
        last_db = [0.0]

        def progress(frac: float, item: str) -> None:
            state.update(progress=frac, item=item)
            loop.call_soon_threadsafe(self.emit, "extraction", dict(state))
            now = time.monotonic()
            if now - last_db[0] > 2:
                last_db[0] = now
                self.extractions.update(ext_id, progress=frac)

        rar_tool = self.settings.settings.unrar_tool or None
        if (entry.release or {}).get("updating") and self.before_update is not None:
            try:                               # an update overwrites the game: its saves are zipped first
                await asyncio.to_thread(self.before_update, entry.id, "before-update", True)
            except Exception:
                log.exception("save backup before updating %s failed", entry.title)
        if self._FIX.search(archive.name) and dest.is_dir() and any(dest.iterdir()):
            # a fix goes over the game (also from the browser / an import): keep what it replaces
            from ezgd.installation.integrity import backup_overwritten
            try:
                await asyncio.to_thread(backup_overwritten, archive, dest, password, rar_tool)
            except Exception:
                log.exception("could not back up the files %s replaces", archive.name)
        try:
            result = await asyncio.to_thread(ar.extract, archive, dest, password=password, progress=progress,
                                             cancel=cancel, rar_tool=rar_tool)
        except (ar.ArchiveError, InsufficientSpaceError, OSError) as exc:
            if isinstance(exc, ar.ExtractionCancelled) and getattr(self, "_shutting_down", False):
                log.info("extraction of %s stopped because the app is closing; it resumes next time", archive.name)
                return False
            msg = self._describe(exc, password)
            log.warning("extraction of %s failed: %s", archive.name, msg)
            self.extractions.update(ext_id, status="cancelled" if isinstance(exc, ar.ExtractionCancelled)
                                    else "failed", error=msg)
            entry.status = prev_status if prev_status is not LibraryStatus.EXTRACTING else LibraryStatus.DOWNLOADED
            self.library.update(entry)
            state.update(status="failed", error=msg)
            self.emit("extraction", dict(state))
            self.emit("library_changed", entry.id)
            if not isinstance(exc, ar.ExtractionCancelled):
                self.emit("notify", ("Extraction failed", f"{entry.title}: {msg}"))
            return False
        finally:
            self._cancel.pop(ext_id, None)
        self.extractions.update(ext_id, status="completed", progress=1.0, files_count=result.files,
                                bytes_total=result.bytes, nested_archives=result.nested_archives,
                                completed_at=time.time())
        entry.install_path = str(dest)
        entry.status = LibraryStatus.EXTRACTED
        self._record_release(entry, archive)
        notes = []
        if result.nested_archives:
            notes.append("Contains nested archives (not extracted automatically): "
                         + ", ".join(result.nested_archives[:5]))
        if result.skipped:
            notes.append(f"Skipped {len(result.skipped)} link/special entries for safety.")
        if entry.source == "onlinefix":
            odd = await asyncio.to_thread(suspicious_release_files, dest)
            if odd:
                notes.append("⚠ OnlineFix states its releases never contain installers or a renpy folder, but this "
                             "archive has: " + ", ".join(odd[:5]) + ". It may not be a genuine OnlineFix release — "
                             "do not run these files unless you trust them.")
                self.emit("notify", ("Check this download", f"{entry.title} contains files OnlineFix says its "
                                                            f"releases never include."))
        base = await asyncio.to_thread(self.steam_base, entry)
        if base and base[1] is not None and Path(base[1]) == Path(dest):
            # OnlineFix's own override set (the same line its users put into Steam's launch options)
            dlls = "OnlineFix64=n;SteamOverlay64=n;winmm=n,b;dnet=n;steam_api64=n;winhttp=n,b"
            entry.notes = (f"The fix is in the Steam game's folder ({dest}); replaced files are in .ezgd-backup-*. "
                           "Press Play here with Steam running. To start it from Steam instead, put this into the "
                           f"game's Properties → Launch options in Steam: WINEDLLOVERRIDES=\"{dlls}\" %command%")
            notes = [n for n in notes if "renpy" not in n]
        if notes:
            entry.notes = " ".join(filter(None, [entry.notes, *notes]))
        installer = await asyncio.to_thread(self._mark_installer, entry)
        self.library.update(entry)
        if self.settings.settings.delete_archive_after_extract:
            root = download_root if download_root is not None else self.app_folder_of(archive)
            if root is not None:
                self._delete_archive(archive, root)
        state.update(status="completed", progress=1.0, files=result.files)
        s = self.settings.settings
        if any(is_within(dest, r) for r in (s.install_dir, s.download_dir)):
            self._record(dest, "install", entry.game_id)   # never a Steam game's folder (not the app's to delete)
        chosen = None
        if (entry.setup or {}).get("state") == "waiting_steam":
            entry.setup = {}
            self.library.update(entry)
        if not installer and self.choose_executable:
            chosen = await asyncio.to_thread(self.choose_executable, entry.id)
        self.emit("extraction", dict(state))
        self.emit("library_changed", entry.id)
        if installer:
            self.emit("notify", ("Ready to install", f"{entry.title} is a repack: press “Install game” in your "
                                                     f"Library to run its installer."))
        elif chosen:
            self.emit("notify", ("Ready to play", f"{entry.title} is installed — press Play in your Library. "
                                                  f"(It starts {chosen}; change that with “Choose executable” "
                                                  "if it is not the game.)"))
        else:
            self.emit("notify", ("Extraction finished", f"{entry.title} is ready — choose its executable in Library."))
        return True

    def _record_release(self, entry: LibraryEntry, archive: Path) -> None:
        """Remember which release is installed now (update checks compare against it). Fix archives do not count."""
        from ezgd.library import updates
        if updates.is_fix_archive(archive.name):
            return
        game = self.games.get(entry.game_id) if entry.game_id else None
        rel = updates.installed_release([archive.name], game.version if game else None)
        prev = entry.release or {}
        new = {k: v for k, v in prev.items() if k in ("checked_at", "latest", "page_updated_at")}
        new.update(rel, installed_at=time.time())
        if prev.get("updating"):
            new["updated_from"] = prev.get("version")
            new["updated_at"] = time.time()
        if new.get("version"):
            new["status"] = updates.status(new["version"], new.get("latest") or new["version"])
        entry.release = new

    @staticmethod
    def _mark_installer(entry: LibraryEntry) -> bool:
        """Repacks (setup.exe + data files) must be installed before they can be played: remember that."""
        from ezgd.library import repack
        rep = repack.detect(entry.install_path)
        if rep is None or (rep.kind == "inno" and entry.source != "fitgirl" and repack._has_game_exe(rep.root)):
            return False
        entry.setup = {"state": "pending", "root": str(rep.root), "kind": rep.kind}
        return True

    @staticmethod
    def _describe(exc: BaseException, password: str | None) -> str:
        if isinstance(exc, ar.PasswordRequiredError):
            return ("The archive is password-protected" + (" and the published password did not work."
                                                           if password else "; enter the password and retry."))
        if isinstance(exc, OSError) and not isinstance(exc, InsufficientSpaceError):
            return f"File system error: {exc.strerror or exc}"
        return str(exc)

    def app_folder_of(self, path: Path) -> Path | None:
        """The app's download / installation folder ``path`` is in (archives elsewhere are never deleted)."""
        s = self.settings.settings
        for r in (s.install_dir, s.download_dir):
            if r and is_within(path, r):
                return Path(path).parent
        return None

    def _prune_empty(self, folder: Path) -> None:
        """Remove folders the deleted archives left empty (e.g. "Fix Repair", the torrent's folder) — never the
        app's own download / installation folders themselves."""
        s = self.settings.settings
        stops = {Path(s.install_dir).resolve(), Path(s.download_dir).resolve(), Path.home().resolve()}
        d = Path(folder)
        for _ in range(4):
            try:
                if d.resolve() in stops or not any(is_within(d, r) for r in (s.install_dir, s.download_dir)):
                    return
                if any(d.iterdir()):
                    return
                d.rmdir()
            except OSError:
                return
            d = d.parent

    def _delete_archive(self, archive: Path, download_root: Path) -> list[Path]:
        """Delete the extracted archive and the other volumes of the same set — nothing else, and only inside the
        folder the download came from. (Matching by name prefix also deleted unrelated archives next to it, e.g.
        ``Hades.II.Soundtrack.zip`` after extracting ``Hades.II.zip``.)"""
        root = download_root if download_root.is_dir() else download_root.parent
        targets = {archive}
        v = vol.volume_of(archive)
        try:
            siblings = list(archive.parent.iterdir())
        except OSError:
            siblings = []
        if v is not None:
            targets |= {p for p in siblings if (pv := vol.volume_of(p)) is not None and pv.set_key == v.set_key}
        if archive.suffix.lower() == ".zip":          # split zip: Game.z01, Game.z02 … belong to Game.zip
            targets |= {p for p in siblings if re.fullmatch(re.escape(archive.stem) + r"\.z\d{2,3}", p.name, re.I)}
        deleted = []
        for p in sorted(targets):
            if p.is_file() and is_within(p, root):
                try:
                    p.unlink()
                    deleted.append(p)
                except OSError as exc:
                    log.warning("could not delete archive %s: %s", p, exc)
        if deleted:
            log.info("deleted %d archive file(s) after extracting: %s", len(deleted), ", ".join(x.name for x in deleted))
            self._prune_empty(archive.parent)
        return deleted

    def stop_all(self) -> None:
        """The app is closing: stop every running extraction. Their rows stay "running", so ``recover`` marks them
        interrupted at the next start and the core finishes them."""
        self._shutting_down = True
        for ev in list(self._cancel.values()):
            ev.set()

    def cancel(self, ext_id: int) -> bool:
        ev = self._cancel.get(ext_id)
        if ev:
            ev.set()
            return True
        return False

    INTERRUPTED = "Interrupted (the app was closed); retry it."

    def recover(self) -> int:
        """Called at start-up: interrupted extractions are marked failed and staging dirs removed. Those whose
        archive and game are still there are remembered in ``resumable`` — the core finishes them by itself."""
        n = 0
        self.resumable = []
        for row in self.extractions.interrupted():
            self.extractions.update(row["id"], status="failed", error=self.INTERRUPTED)
            ar.cleanup_stale_staging(Path(row["dest_dir"]))
            e = self.library.get(row["library_id"]) if row.get("library_id") else None
            if e and e.status is LibraryStatus.EXTRACTING:
                e.status = LibraryStatus.DOWNLOADED
                self.library.update(e)
            if e is not None and Path(row["archive_path"]).is_file():
                self.resumable.append({"entry_id": e.id, "title": e.title, "archive": row["archive_path"],
                                       "dest": row["dest_dir"]})
            n += 1
        return n

    def interrupted_install(self, entry_id: int) -> dict | None:
        """The newest extraction of this game if it was cut short and can be done again (archive still there)."""
        rows = sorted(self.extractions.for_library(entry_id), key=lambda r: r["id"])
        if not rows:
            return None
        last = rows[-1]
        if last.get("status") != "failed" or last.get("error") != self.INTERRUPTED:
            return None
        if not Path(last["archive_path"]).is_file():
            return None
        return {"entry_id": entry_id, "archive": last["archive_path"], "dest": last["dest_dir"],
                "name": Path(last["archive_path"]).name}

    def running_count(self) -> int:
        return len(self._cancel)
