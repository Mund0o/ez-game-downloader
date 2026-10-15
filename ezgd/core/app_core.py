"""Application core: wires every subsystem together and exposes the async API used by the UI.

All methods run on the core asyncio loop (see :mod:`ezgd.ui.bridge`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ezgd.catalog.models import DownloadMethod, Game
from ezgd.catalog.normalize import dedup_key
from ezgd.core.fsutil import dir_size, install_estimate, is_within, safe_dirname
from ezgd.core.logging_setup import set_level
from ezgd.core.settings import SettingsStore, download_folder
from ezgd.database.db import Database
from ezgd.database.repositories import (CreatedPathsRepository, CustomSourceRepository, DownloadRepository,
                                        ExtractionRepository, GameRepository, HttpCacheRepository, LibraryRepository,
                                        SourceRepository, WishlistRepository)
from ezgd.downloads.http_engine import HttpEngine, file_checksum
from ezgd.downloads.manager import DownloadCommandError, DownloadManager
from ezgd.downloads.models import DownloadStatus
from ezgd.downloads.torrent_engine import TorrentEngine, TorrentError, inspect_torrent
from ezgd.installation.archives import detect_format, find_archives
from ezgd.installation.volumes import set_status as vol_set_status, volume_of
from ezgd.platform.steam import remove_shortcut as steam_remove_shortcut
from ezgd.installation.pipeline import PostProcessor, choose_archives
from ezgd.library.manager import LibraryManager
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.net import adblock, filterlists
from ezgd.net.client import SafeHttpClient
from ezgd.net.ratelimit import HostRateLimiter, TokenBucket
from ezgd.platform.paths import AppPaths
from ezgd.sources import ADAPTER_CLASSES
from ezgd.sources.base import ManualActionRequired, SourceError
from ezgd.sources.cache import CachedFetcher
from ezgd.sources.custom import SiteRejected, WordPressSiteAdapter, check_site
from ezgd.sources.registry import SourceRegistry
from ezgd.sources.reporting import write_report

log = logging.getLogger(__name__)


class ChecksumMismatchError(SourceError):
    """An imported file does not match the checksum its source publishes."""


class UninstallIncomplete(SourceError):
    """Some selected files could not be deleted; the library entry was kept so the user can retry."""

    def __init__(self, title: str, failed: list[dict], kept: list[dict], deleted: list[str], freed: int):
        self.title, self.failed, self.kept, self.deleted, self.freed = title, failed, kept, deleted, freed
        super().__init__(f"{title} was not fully uninstalled: " +
                         "; ".join(f"{f['path']} — {f['why']}" for f in failed))

Emit = Callable[[str, Any], None]
MAX_COVER_BYTES = 5 * 1024 * 1024


@dataclass
class PreparedDownload:
    token: str
    game: Game
    method: DownloadMethod
    kind: str                        # torrent | http
    name: str
    size: int | None
    checksum: str | None
    files: list[dict] = field(default_factory=list)
    torrent_bytes: bytes | None = None
    magnet: str | None = None
    url: str | None = None
    referer: str | None = None
    archive_password: str | None = None
    created: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {"token": self.token, "kind": self.kind, "name": self.name, "size": self.size,
                "checksum": self.checksum, "files": self.files, "title": self.game.title,
                "source": self.game.source, "method": self.method.label,
                "has_password": bool(self.archive_password), "password": self.archive_password,
                **install_estimate(self.game, self.size)}


class AppCore:
    def __init__(self, paths: AppPaths, *, allow_private_network: bool = False,
                 transport=None, enable_torrents: bool = True, torrent_overrides: dict | None = None):
        self.paths = paths
        self.allow_private = allow_private_network
        self._transport = transport
        self._enable_torrents = enable_torrents
        self._torrent_overrides = torrent_overrides or {}
        self._emit: Emit = lambda name, payload: None
        self._prepared: dict[str, PreparedDownload] = {}
        self.db: Database | None = None
        self.started = False

    def set_emitter(self, emit: Emit) -> None:
        self._emit = emit

    def emit(self, name: str, payload: Any = None) -> None:
        try:
            self._emit(name, payload)
        except Exception:
            log.exception("event emitter failed for %s", name)

    # ------------------------------------------------------------ lifecycle
    def open_database(self) -> Database:
        self.paths.ensure()
        self.db = Database(self.paths.database, backup_dir=self.paths.backups).open()
        return self.db

    async def start(self) -> None:
        if self.db is None:
            self.open_database()
        db = self.db
        assert db is not None
        self.settings = SettingsStore(db)
        s = self.settings.settings
        set_level(s.log_level)
        self.games = GameRepository(db)
        self.http_cache = HttpCacheRepository(db)
        self.source_repo = SourceRepository(db)
        self.download_repo = DownloadRepository(db)
        self.extraction_repo = ExtractionRepository(db)
        self.library_repo = LibraryRepository(db)
        self.created_paths = CreatedPathsRepository(db)
        self.custom_sources = CustomSourceRepository(db)
        self.wishlist_repo = WishlistRepository(db)

        self.rate_limiter = HostRateLimiter(min_interval=1.0, per_host=2,
                                            overrides={"pixeldrain.com": 0.5})
        self.client = SafeHttpClient(timeout=s.http_timeout_seconds, allow_private=self.allow_private,
                                     rate_limiter=self.rate_limiter, transport=self._transport)
        self.fetcher = CachedFetcher(self.client, self.http_cache, default_ttl=s.cache_ttl_minutes * 60)
        adapters = [cls(self.fetcher) for cls in ADAPTER_CLASSES.values()]
        for row in self.custom_sources.all():               # sites the user added (see ezgd.sources.custom)
            adapters.append(WordPressSiteAdapter(self.fetcher, row["id"], row["name"], row["base_url"]))
        self.registry = SourceRegistry(adapters, self.games, self.source_repo, s.sources_enabled)
        self.registry.hide_withdrawn = s.hide_withdrawn_games
        self.registry.hide_adult = s.hide_adult_games
        self._verify_task: asyncio.Task | None = None
        self._import_tasks: set[asyncio.Task] = set()

        self.bucket = TokenBucket(s.http_speed_limit_kbps * 1024)
        self.http_engine = HttpEngine(self.client, self.bucket, max_retries=s.http_max_retries)
        self.torrents: TorrentEngine | None = None
        self.torrent_error: str | None = None
        if self._enable_torrents:
            cfg = {"listen_port": s.torrent_listen_port, "max_connections": s.torrent_max_connections,
                   "download_limit_kbps": s.torrent_download_limit_kbps,
                   "upload_limit_kbps": s.torrent_upload_limit_kbps, "enable_dht": s.torrent_enable_dht,
                   "enable_lsd": s.torrent_enable_lsd, "enable_upnp": s.torrent_enable_upnp,
                   "stall_minutes": s.torrent_stall_minutes, **self._torrent_overrides}
            try:
                self.torrents = TorrentEngine(cfg, self.paths.torrents)
            except Exception as exc:     # e.g. broken libtorrent install: keep the rest working
                self.torrent_error = f"Torrent engine unavailable: {exc}"
                log.exception("torrent engine failed to start")

        self.library = LibraryManager(self.library_repo, self.games, self.settings, self.paths, self.emit)
        self.post = PostProcessor(self.library_repo, self.extraction_repo, self.games, self.settings, self.emit,
                                  created=self.created_paths)
        self.post.choose_executable = self.library.auto_executable
        self.post.before_update = self.library.backup_saves
        self.library.on_game_exit = self._game_exited
        self.post.recover()
        self.downloads = DownloadManager(self.download_repo, self.settings, self.http_engine, self.torrents,
                                         self.emit, client=self.client, on_completed=self._after_download,
                                         log_event=db.log_event)
        self.downloads.is_playing = lambda: bool(self.library.running_ids())   # "slower while playing"
        await self.downloads.start()
        self.downloads._apply_rates()
        self.settings.subscribe(self._on_setting)
        if s.start_with_session:                 # keep the autostart entry pointing at this copy of the app
            from ezgd.platform import autostart
            await asyncio.to_thread(autostart.apply, True)
        self.http_cache.prune()
        self._maintenance_task = asyncio.create_task(self.maintenance())
        adblock.set_data_dir(self.paths.data / "adblock")
        filterlists.set_data_dir(self.paths.data / "adblock" / "filters")
        self._adblock_task = asyncio.create_task(self._adblock_loop())
        self._protondb_task = asyncio.create_task(self._protondb_loop())
        self._reaper_task = asyncio.create_task(self._reap_games())
        try:                     # games installed before update checks: their version from the archive names
            for e in self.library.repo.all():
                if e.game_id and not (e.release or {}).get("version"):
                    self._ensure_release(e)
        except Exception:
            log.exception("could not work out installed versions")
        from ezgd.sources import parsing
        self._bg_tasks = [asyncio.create_task(parsing.warm_up()), asyncio.create_task(self._resume_installs()),
                          asyncio.create_task(self._game_updates_loop()),
                          asyncio.create_task(self._wishlist_loop()),
                          asyncio.create_task(self._app_update_loop())]
        self.started = True
        log.info("core started (db schema v%d, %d cached games)", db.schema_version, self.games.count())

    async def shutdown(self) -> None:
        if not self.started:
            if self.db:
                self.db.close()
            return
        # a running install stops now (else the app would only exit once it is done — hours for a big game); it
        # stays marked as running, so the next start finishes it
        self.post.stop_all()
        try:
            self.library.resume_all()    # a game frozen from the quick menu must not stay frozen after the app
        except Exception:
            log.exception("could not resume suspended games")
        from ezgd.sources import parsing
        parsing.shutdown()
        if getattr(self, "_verify_task", None):
            self._verify_task.cancel()
        if getattr(self, "_adblock_task", None):
            self._adblock_task.cancel()
        if getattr(self, "_protondb_task", None):
            self._protondb_task.cancel()
        for t in getattr(self, "_bg_tasks", ()):
            t.cancel()
        for t in list(getattr(self, "_import_tasks", ())):
            t.cancel()                   # interrupted extractions are marked failed on next start
        if getattr(self, "_reaper_task", None):
            self._reaper_task.cancel()
            self.library.reap()          # record playtime of games that already exited
        if getattr(self, "_restored", False):        # downloads were already stopped by restore
            await self.client.aclose()
            self.db.close()
            self.started = False
            return
        try:
            await self.downloads.shutdown()
        finally:
            await self.client.aclose()
            try:
                self.db.backup(self.paths.backups / "last-exit.sqlite3")
            except Exception:
                log.exception("exit backup failed")
            self.db.close()
            self.started = False

    def _on_setting(self, key: str, value: Any) -> None:
        if key == "log_level":
            set_level(value)
        elif key == "sources_enabled":
            self.registry.set_enabled(value)
        elif key == "hide_withdrawn_games":
            self.registry.hide_withdrawn = bool(value)
        elif key == "hide_adult_games":
            self.registry.hide_adult = bool(value)
        elif key == "cache_ttl_minutes":
            self.fetcher.default_ttl = int(value) * 60
        elif key == "start_with_session":
            from ezgd.platform import autostart
            autostart.apply(bool(value))
        self.downloads.apply_settings(key, value)
        self.emit("settings_changed", key)

    # ------------------------------------------------------------- settings
    async def get_settings(self) -> dict:
        return self.settings.as_dict()

    async def set_setting(self, key: str, value: Any) -> Any:
        return self.settings.set(key, value)

    async def import_config(self, path: str) -> list[str]:
        return self.settings.import_toml(Path(path))

    # -------------------------------------------------------------- catalog
    def sources_info(self) -> list[dict]:
        out = []
        custom = {r["id"]: r for r in self.custom_sources.all()}
        for a in self.registry.adapters.values():
            st = self.registry.status[a.id]
            stored = self.source_repo.get(a.id) or {}
            out.append({"id": a.id, "name": a.info.name, "homepage": a.info.homepage,
                        "description": a.info.description, "notes": a.info.notes,
                        "capabilities": sorted(a.info.capabilities), "enabled": self.registry.enabled.get(a.id, True),
                        "status": st.state if st.state != "unknown" else stored.get("status", "unknown"),
                        "message": st.message or stored.get("status_message"),
                        "custom": a.id in custom, "details": custom.get(a.id, {}).get("details", {})})
        return out

    async def add_custom_source(self, url: str) -> dict:
        """Check a site the user wants to add; raises SiteRejected (plain-language reason) if it cannot be."""
        log.info("checking site to add: %s", url)
        try:
            check = await check_site(self.fetcher, url, existing=set(self.registry.adapters))
        except SiteRejected as exc:
            log.info("site not added (%s): %s", url, exc)
            raise
        adapter = WordPressSiteAdapter(self.fetcher, check.source_id, check.name, check.base_url)
        self.custom_sources.add(check.source_id, check.name, check.base_url, check.kind, check.details())
        self.registry.add_adapter(adapter)
        enabled = dict(self.settings.settings.sources_enabled)
        enabled[check.source_id] = True
        self.settings.set("sources_enabled", enabled)
        log.info("site added: %s (%s), %d/%d posts with downloads", check.name, check.base_url,
                 check.posts_with_downloads, check.posts_checked)
        self.emit("sources_changed", check.source_id)
        return {"id": check.source_id, "name": check.name, "base_url": check.base_url, **check.details()}

    async def remove_custom_source(self, source_id: str) -> None:
        if not any(r["id"] == source_id for r in self.custom_sources.all()):
            raise SourceError("Only sites you added yourself can be removed; built-in ones can be turned off.")
        self.custom_sources.remove(source_id)
        self.registry.remove_adapter(source_id)
        log.info("site removed: %s", source_id)
        self.emit("sources_changed", source_id)

    async def get_sources(self) -> list[dict]:
        return self.sources_info()

    def source_password(self, source_id: str | None) -> str | None:
        adapter = self.registry.adapters.get(source_id or "")
        return adapter.info.archive_password if adapter else None

    async def entry_password(self, entry_id: int) -> str | None:
        """Archive password for a library entry: from its download, its game page, or its source."""
        e = self.library.get(entry_id)
        task = self.download_repo.get(e.download_id) if e.download_id else None
        if task and task.archive_password:
            return task.archive_password
        g = self.games.get(e.game_id) if e.game_id else None
        if g and g.extra.get("archive_password"):
            return g.extra["archive_password"]
        return self.source_password(e.source)

    async def latest(self, page: int = 1, sources: list[str] | None = None, genre: str | None = None):
        return await self.registry.latest(page, set(sources) if sources else None, genre)

    async def search(self, query: str, page: int = 1, sources: list[str] | None = None):
        return await self.registry.search(query, page, set(sources) if sources else None)

    async def cached_recent(self, limit: int = 40) -> list[Game]:
        active = [a.id for a in self.registry.active()]
        return self.registry.visible(self.games.recent(active, limit=limit))

    async def featured(self, force: bool = False):
        sections, fetched_at, error = await self.registry.featured("onlinefix", force=force)
        unknown = [g for s in sections for g in s.games if not g.withdrawn_known]
        if unknown and self.registry.hide_withdrawn and not (self._verify_task and not self._verify_task.done()):
            self._verify_task = asyncio.create_task(self._verify_withdrawn(unknown))
        return sections, fetched_at, error

    async def _verify_withdrawn(self, games: list[Game]) -> None:
        """Carousel entries do not say whether a guide was closed: check their pages in the background."""
        seen: set[str] = set()
        unique = [g for g in games if not (g.id in seen or seen.add(g.id))]
        hidden = await self.registry.verify_withdrawn(unique)
        if hidden:
            log.info("hiding %d withdrawn game(s): %s", len(hidden), ", ".join(hidden))
            self.emit("games_hidden", hidden)

    async def genres(self) -> list[str]:
        return await self.registry.genres()

    async def game(self, game_id: str) -> Game | None:
        return self.games.get(game_id)

    async def details(self, game_id: str, force: bool = False) -> Game:
        g = self.games.get(game_id)
        if g is None:
            raise SourceError("Unknown game.")
        if g.download_methods is not None and not force and g.details_checked_at and \
                time.time() - g.details_checked_at < 1800:
            return g
        fresh = await self.registry.details(g)
        if fresh.withdrawn and self.registry.hide_withdrawn:
            self.emit("games_hidden", [fresh.id])
        return fresh

    async def related(self, game_id: str) -> list[Game]:
        """Same game offered by other sources (duplicate detection)."""
        g = self.games.get(game_id)
        if g is None:
            return []
        out = {x.id: x for x in self.games.by_dedup_key(g.dedup_key) if x.id != g.id}
        if g.steam_app_id:
            for x in self.games.by_steam_app(g.steam_app_id):
                if x.id != g.id:
                    out.setdefault(x.id, x)
        return self.registry.visible(list(out.values()))

    async def installed_keys(self) -> set[str]:
        return self.library_repo.dedup_keys()

    # ------------------------------------------------------------ downloads
    async def prepare_download(self, game_id: str, method_index: int) -> dict:
        """Resolve a method into a concrete transfer the user can review before starting."""
        g = self.games.get(game_id)
        if g is None or not g.download_methods or not (0 <= method_index < len(g.download_methods)):
            raise SourceError("That download option is no longer available; reopen the game page.")
        if g.withdrawn and self.registry.hide_withdrawn:
            raise SourceError(f"{g.title}: the source has closed this guide, so it is not offered for download.")
        method = g.download_methods[method_index]
        try:
            resolved = await self.registry.resolve(g, method)
        except ManualActionRequired:
            # the adapter may have learned this method needs a browser (e.g. sign-in-only torrent): keep that
            self.games.save(g)
            self.emit("game_updated", g.id)
            raise
        token = secrets.token_hex(8)
        prep = PreparedDownload(token=token, game=g, method=method, kind=resolved.kind,
                                name=resolved.file_name or g.title, size=resolved.size_bytes,
                                checksum=resolved.checksum, referer=resolved.referer,
                                archive_password=(resolved.archive_password or method.archive_password
                                                  or self.source_password(g.source)))
        if resolved.kind == "torrent":
            if resolved.torrent_bytes:
                try:
                    meta = inspect_torrent(resolved.torrent_bytes)
                except TorrentError as exc:
                    raise SourceError(str(exc)) from exc
                prep.torrent_bytes = resolved.torrent_bytes
                prep.name = meta.name
                prep.size = meta.total_size
                prep.files = [{"index": f.index, "path": f.path, "size": f.size} for f in meta.files]
            else:
                prep.magnet = resolved.url
        else:
            prep.url = resolved.url
        self._prepared = {k: v for k, v in self._prepared.items() if time.time() - v.created < 3600}
        self._prepared[token] = prep
        return prep.public()

    async def start_prepared(self, token: str, dest_dir: str, *, selected_files: list[int] | None = None,
                             auto_extract: bool = True, extract_dest: str | None = None) -> dict:
        prep = self._prepared.pop(token, None)
        if prep is None:
            raise DownloadCommandError("This download request expired; please try again.")
        common = dict(game_id=prep.game.id, source=prep.game.source, source_url=prep.game.url,
                      archive_password=prep.archive_password, auto_extract=auto_extract,
                      extract_dest=extract_dest)
        if prep.kind == "torrent":
            return await self.downloads.add_torrent(title=prep.game.title, dest_dir=dest_dir,
                                                    torrent_bytes=prep.torrent_bytes, magnet=prep.magnet,
                                                    selected_files=selected_files, **common)
        return await self.downloads.add_http(prep.url, title=prep.game.title, dest_dir=dest_dir,
                                             filename=prep.name, size=prep.size, checksum=prep.checksum,
                                             referer=prep.referer, **common)

    async def add_link(self, text: str, dest_dir: str, title: str | None = None, auto_extract: bool = True) -> dict:
        return await self.downloads.add_from_text(text, dest_dir=dest_dir, title=title, auto_extract=auto_extract)

    async def add_torrent_file(self, path: str, dest_dir: str, selected_files: list[int] | None = None,
                               auto_extract: bool = True) -> dict:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > 16 * 1024 * 1024:
            raise DownloadCommandError("Choose a valid .torrent file.")
        return await self.downloads.add_torrent(title="", dest_dir=dest_dir, torrent_bytes=p.read_bytes(),
                                                selected_files=selected_files, auto_extract=auto_extract)

    async def add_browser_torrent(self, game_id: str, path: str) -> dict:
        """A .torrent file the user downloaded in the in-app browser: download it with the built-in engine,
        linked to its game (the .torrent itself is removed once it has been handed over)."""
        p = Path(path)
        if not p.is_file() or p.stat().st_size > 16 * 1024 * 1024:
            raise DownloadCommandError("The downloaded .torrent file could not be read.")
        data = p.read_bytes()
        try:
            inspect_torrent(data)
        except TorrentError as exc:
            raise DownloadCommandError(f"The downloaded .torrent file is not valid: {exc}") from exc
        game = self.games.get(game_id)
        task = await self.downloads.add_torrent(
            title=game.title if game else "",
            dest_dir=download_folder(self.settings.settings, game.title if game else None), torrent_bytes=data,
            game_id=game_id, source=game.source if game else None, source_url=game.url if game else None,
            archive_password=(game.extra.get("archive_password") if game else None))
        p.unlink(missing_ok=True)
        return task

    async def inspect_torrent_file(self, path: str) -> dict:
        meta = inspect_torrent(Path(path).read_bytes())
        return {"name": meta.name, "size": meta.total_size,
                "files": [{"index": f.index, "path": f.path, "size": f.size} for f in meta.files]}

    async def downloads_snapshot(self) -> list[dict]:
        return self.downloads.snapshot_all()

    async def download_command(self, command: str, task_id: int, *args) -> Any:
        allowed = {"pause", "resume", "retry", "cancel", "remove", "set_priority", "set_files", "recheck",
                   "change_destination"}
        if command not in allowed:
            raise DownloadCommandError(f"unknown command {command}")
        return await getattr(self.downloads, command)(task_id, *args)

    async def pause_all(self) -> None:
        for t in list(self.downloads.tasks.values()):
            if not t.status.is_finished and t.status is not DownloadStatus.PAUSED:
                await self.downloads.pause(t.id)

    async def resume_all(self) -> None:
        for t in list(self.downloads.tasks.values()):
            if t.status is DownloadStatus.PAUSED:
                await self.downloads.resume(t.id)

    async def summary(self) -> dict:
        d = self.downloads.summary()
        d["extracting"] = self.post.running_count()
        d["torrent_error"] = self.torrent_error or (self.torrents.listen_error if self.torrents else None)
        d["listen_port"] = self.torrents.listen_port if self.torrents else None
        return d

    # -------------------------------------------------------------- library
    async def library_entries(self) -> list[LibraryEntry]:
        return await asyncio.to_thread(self.library.all)

    async def _after_download(self, task, content) -> None:
        entry = await self.post.on_download_completed(task, content)
        if entry is not None and not entry.steam_app_id:
            try:
                await self.lookup_steam_app_id(entry.id)
            except Exception as exc:          # best effort; the user can set it in Properties
                log.info("Steam App ID lookup for %s failed: %s", entry.title, exc)

    async def _reap_games(self) -> None:
        # (Steam's files are never changed in the background — the one exception is removing crash-prone
        # shortcuts an older version wrote, once at start and only while Steam is closed.)
        try:
            msg = await asyncio.to_thread(self.library.check_steam_shortcuts)
            if msg:
                self.emit("notify", ("Steam", msg))
        except Exception:
            log.exception("Steam shortcut check failed")
        n = 0
        while True:
            await asyncio.sleep(2)
            try:
                self.library.reap()
            except Exception:
                log.exception("game reaper failed")
            n += 1
            if n % 10 == 1:                              # every 20 s
                try:
                    await self._apply_ready_fixes()
                except Exception:
                    log.exception("applying waiting fixes failed")

    async def _apply_ready_fixes(self) -> None:
        """Fix-only downloads (OnlineFix "Friend's Pass" games): put the fix into the free Steam game as soon as
        Steam has finished installing it — once per game and session (a failure is shown, not retried)."""
        tried = self.__dict__.setdefault("_fixes_tried", set())
        for e, archive, folder in await asyncio.to_thread(self.post.fixes_ready_for_steam):
            if e.id in tried or self.library.is_running(e.id):
                continue
            tried.add(e.id)
            log.info("%s: the Steam game is installed in %s — applying the fix %s", e.title, folder, archive.name)
            self.emit("notify", ("Adding the fix", f"{e.title}: the Steam game is installed — putting the fix "
                                                   f"into it now (replaced files are backed up)."))
            await self.extract_entry(e.id, str(archive), str(folder))

    async def lookup_steam_app_id(self, entry_id: int, use_network: bool = True) -> int | None:
        """Find the real Steam App ID (used to pick Proton per-game fixes).

        Order: the game's own page (SteamRIP lists it), the same game from another
        source, then Steam's public store search — exact title matches only.
        """
        from ezgd.catalog.normalize import dedup_key
        from ezgd.library import appid
        e = self.library.get(entry_id)
        found = None
        g = self.games.get(e.game_id) if e.game_id else None
        if g and g.steam_app_id:
            found = g.steam_app_id
        if found is None and e.executable:              # the release's own files say it (steam_appid.txt, emulators)
            found = await asyncio.to_thread(appid.from_files, Path(e.executable), Path(e.install_path or "."))
        if found is None:
            found = next((x.steam_app_id for x in self.games.by_dedup_key(e.dedup_key) if x.steam_app_id), None)
        keys = appid.title_keys(e.title)
        if found is None:                              # ProtonDB's index knows ~34 000 titles (offline)
            found = await asyncio.to_thread(appid.from_protondb, self._protondb_index(), keys)
        if found is None and use_network:
            try:
                found = await self._steam_app_by_search(e.title)
            except RuntimeError as exc:
                log.info("Steam search for %r failed: %s", e.title, exc)
        _ = dedup_key
        if found:
            self.library.update(entry_id, steam_app_id=found)
        return found

    async def steam_base_info(self, game_id: str) -> dict | None:
        """OnlineFix pages that only offer a fix for a free Steam game: is that game installed, and its Steam id."""
        from ezgd.library import appid as appid_mod
        from ezgd.platform import steam as steam_mod
        g = self.games.get(game_id)
        name = g.extra.get("steam_base_game") if g else None
        if not name:
            return None
        hit = await asyncio.to_thread(steam_mod.find_installed_game, self.library.steam_root(), name)
        app = g.extra.get("steam_base_appid") or (hit["appid"] if hit else None)
        if not app:                                    # the main game's Steam id (the trial is a licence for it)
            from urllib.parse import quote
            url = f"https://store.steampowered.com/api/storesearch/?term={quote(steam_mod.base_title(name))}" \
                  "&l=english&cc=US"
            try:
                res = await self.client.fetch(url, max_bytes=512 * 1024, headers={"Accept": "application/json"})
                items = json.loads(res.content).get("items") or [] if res.status == 200 else []
            except (ValueError, AttributeError, Exception):
                items = []
            want = appid_mod.key(steam_mod.base_title(name))
            exact = [i for i in items if isinstance(i, dict) and appid_mod.key(str(i.get("name", ""))) == want]
            if len({i.get("id") for i in exact}) == 1:
                app = int(exact[0]["id"])
        if app and g.extra.get("steam_base_appid") != app:
            g.extra["steam_base_appid"] = app
            self.games.save(g)
        return {"name": name, "appid": app, "installed": bool(hit and hit["complete"]),
                "folder": str(hit["folder"]) if hit else None}

    async def install_steam_base(self, game_id: str) -> dict:
        """Open Steam's own install window for the free game (Steam asks; the app changes nothing in Steam)."""
        from urllib.parse import quote
        from ezgd.platform import steam as steam_mod
        info = await self.steam_base_info(game_id)
        if not info:
            raise SourceError("This game does not need a Steam game.")
        if info["appid"] and steam_mod.steam_command() is not None:
            # its store page in Steam: "Install Game" there adds the free trial / Friend's Pass licence
            steam_mod.open_steam_url(f"steam://store/{info['appid']}")
            return {**info, "opened": "steam"}
        return {**info, "opened": "store",
                "url": f"https://store.steampowered.com/search/?term={quote(info['name'])}"}

    async def library_call(self, method: str, *args, **kwargs) -> Any:
        allowed = {"candidates", "set_executable", "mark_installed", "update", "launch", "add_to_steam",
                   "organize", "collections",
                   "remove", "import_folder", "duplicates", "allow_execute", "launch_plan", "get", "preview",
                   "stop", "running_ids", "suggest_overrides", "protonfix_status", "prefix_info", "prefix_tool",
                   "suggestions", "apply_suggestion", "fixes_for", "set_fixes",
                   "diagnose", "apply_diagnosis_fix", "enable_fix",
                   "reset_prefix", "installer_info", "verify_repack", "run_installer", "delete_repack",
                   "steam_overlay_status", "prepare_steam_shortcuts", "launch_via_steam",
                   "remove_steam_shortcuts", "last_run_status", "start_steam", "stop_completely",
                   "backup_saves", "save_backups", "restore_save_backup", "delete_save_backup",
                   "suspend", "resume", "resume_all", "suspended_ids", "game_volume", "set_game_volume", "protons",
                   "session_seconds", "game_processes", "auto_executable", "set_mangohud"}
        if method not in allowed:
            raise ValueError(f"unknown library call {method}")
        fn = getattr(self.library, method)
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ---------------------------------------------------------- system tools
    async def system_status(self) -> dict:
        from ezgd.platform import deps
        statuses = await asyncio.to_thread(deps.status)
        proton_ok = await asyncio.to_thread(deps._proton_capable) if deps.IS_LINUX else True
        return {"family": deps.detect_family() if deps.IS_LINUX else "windows",
                "tools": [s.__dict__ for s in statuses], "can_run_windows_games": proton_ok,
                "missing_required": [s.name for s in statuses if s.importance == "required" and not s.installed]}

    async def system_install_plan(self) -> dict:
        from ezgd.platform import deps
        p = await asyncio.to_thread(deps.plan)
        return {"family": p.family, "packages": p.install, "unavailable": p.unavailable, "notes": p.notes,
                "command": p.shell}

    async def system_install(self) -> int:
        """Install missing tools with the system package manager behind a graphical password prompt."""
        from ezgd.platform import deps
        p = await asyncio.to_thread(deps.plan)
        if not p.command:
            return 0
        steps = ([["apt-get", "update"]] if p.family == "debian" else []) + [p.command]
        rc = 0
        for cmd in steps:
            argv = deps.elevated(cmd, gui=True)
            self.emit("system_install", {"line": "$ " + " ".join(argv)})
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT,
                                                        stdin=asyncio.subprocess.DEVNULL)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                self.emit("system_install", {"line": raw.decode("utf-8", "replace").rstrip()})
            rc = await proc.wait()
            if rc != 0:
                break
        self.library.protons(refresh=True)
        self.emit("system_install", {"done": True, "code": rc})
        return rc

    async def library_environment(self) -> dict:
        return await asyncio.to_thread(self.library.environment)

    async def library_archives(self, entry_id: int) -> list[str]:
        e = self.library.get(entry_id)
        roots = [p for p in (e.download_path, e.install_path) if p]
        found: list[str] = []
        for r in roots:
            if Path(r).exists():
                found += [str(p) for p in await asyncio.to_thread(find_archives, Path(r), 3)]
        return sorted(set(found))

    async def extract_entry(self, entry_id: int, archive: str, dest: str | None = None,
                            password: str | None = None) -> bool:
        e = self.library.get(entry_id)
        pw = password if password is not None else await self.entry_password(entry_id)
        dest_p = Path(dest) if dest else self.post.default_extract_dest(e, Path(archive))
        return await self.post.extract(e, Path(archive), dest_p, pw,
                                       download_root=Path(e.download_path) if e.download_path else None)

    async def import_downloaded_file(self, game_id: str, path: str, *, verify: bool = True,
                                     extract: bool = True) -> dict:
        """Add a file the user downloaded in their browser to the library, linked to its game page.

        If the source publishes a SHA-256 (cracked-games.org), the file is checked first; a mismatch is an
        error unless ``verify`` is False (the user chose to import it anyway).
        """
        return await self.import_downloaded_files(game_id, [path], verify=verify, extract=extract)

    async def import_downloaded_files(self, game_id: str, paths: list[str], *, verify: bool = True,
                                      extract: bool = True) -> dict:
        """Several files from one browser session (game + fix, multi-part archives) → one library entry."""
        g = self.games.get(game_id)
        if g is None:
            raise SourceError("Open the game's page again, then import the file.")
        files = [Path(p).expanduser() for p in paths]
        if not files:
            raise SourceError("No files to import.")
        for p in files:
            self.created_paths.add(p, "import", game_id)
        for p in files:
            if not p.is_file():
                raise SourceError(f"{p} is not a file.")
        expected = (g.extra.get("sha256") or "").lower() or None
        actual = None
        verified = None
        if expected:
            sums = {p: await asyncio.to_thread(file_checksum, p, "sha256") for p in files}
            match = next((p for p, h in sums.items() if h == expected), None)
            if match is not None:
                verified, actual = True, expected
            elif len(files) == 1:
                actual = sums[files[0]]
                if verify:
                    raise ChecksumMismatchError(
                        f"{files[0].name} does not match the SHA-256 published by "
                        f"{self.registry.adapter(g.source).info.name} (expected {expected[:12]}…, got "
                        f"{actual[:12]}…). The download may be incomplete, a different build, or not the file "
                        "from this page. Nothing was imported.")
                verified = False
            # several files (parts) and none is the published one: the checksum covers something else
        # parts of multi-part archives join the entry already collecting that set (other parts may come later)
        volumes = {p: v for p in files if (v := volume_of(p)) is not None}
        if volumes:
            return await self._import_volumes(g, volumes, [p for p in files if p not in volumes], verified,
                                              extract)
        main = max(files, key=lambda p: p.stat().st_size)
        updating = self.post.updating_entry(g.id, g.title)
        if updating is not None:                  # the new version of a game in the Library: it goes over that one
            entry = updating
            entry.download_path = str(main)
            self.library.repo.update(entry)
        else:
            entry = self.library.import_folder(g.title, main, game_id)
        if verified:
            entry.notes = f"SHA-256 verified against {g.url}"
            self.library.repo.update(entry)
        found: list[Path] = []
        for p in files:
            found += await asyncio.to_thread(find_archives, p)
        archives, left = choose_archives(sorted(set(found), key=lambda p: p != main))
        if left:
            entry.notes = "; ".join(x for x in (entry.notes, "Not extracted automatically: "
                                                + ", ".join(p.name for p in left)) if x)
            self.library.repo.update(entry)
        extracting = bool(extract and archives)
        if extracting:
            password = await self.entry_password(entry.id)
            dest = (self.post.update_dest(entry) if updating is not None else None) or \
                self.post.default_extract_dest(entry, archives[0])

            async def run() -> None:
                for a in archives:                # one after another into the same game folder
                    # the folder the file was imported from: "delete the archive after extraction" applies here too
                    if not await self.post.extract(entry, a, dest, password, download_root=a.parent):
                        break
            self._watch_task(asyncio.create_task(run()), "Installing failed", entry.title, entry.id)
        try:
            await self.lookup_steam_app_id(entry.id)
        except Exception as exc:              # best effort, as after a download
            log.info("Steam App ID lookup for %s failed: %s", entry.title, exc)
        return {"entry_id": entry.id, "title": entry.title, "verified": verified, "sha256": actual,
                "extracting": extracting, "files": [p.name for p in files]}

    def _watch_task(self, task: asyncio.Task, what: str, title: str = "", entry_id: int | None = None) -> None:
        """A job running in the background: kept until done, and if it fails, logged and shown to the user — never
        lost silently (an import whose zip used an unsupported compression vanished without a word)."""
        self._import_tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._import_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            log.error("%s: %s", what, title or "background job", exc_info=exc)
            extra = {"library": entry_id} if entry_id is not None else {}
            self.emit("notify", (what, f"{title}: {exc}" if title else str(exc), extra))
        task.add_done_callback(done)

    async def _import_volumes(self, g: Game, volumes: dict, others: list[Path], verified, extract: bool) -> dict:
        entry = None
        summaries, extracting, complete = [], False, True
        by_set: dict[str, list[Path]] = {}
        for p, v in volumes.items():
            by_set.setdefault(v.set_key, []).append(p)
        for set_key, parts in by_set.items():
            entry = self.post.entry_for_set(g.id, g.title, set_key) or self.post.updating_entry(g.id, g.title)
            if entry is None:
                entry = self.library.import_folder(g.title, parts[0], g.id)
            if verified:
                entry.notes = f"SHA-256 verified against {g.url}"
                self.library.repo.update(entry)
            password = await self.entry_password(entry.id)
            part = parts[-1]
            status = vol_set_status(part.parent, set_key, self.post.expected_parts(g.id, set_key))
            summaries.append(status.summary)
            complete = complete and status.complete
            if status.complete and extract:
                extracting = True
            # record the parts and, once complete, extract in the background (it can take minutes)
            dest = self.post.update_dest(entry) if (entry.release or {}).get("updating") else None
            self._watch_task(asyncio.create_task(self.post.add_volume(entry, part, volumes[part], password,
                                                                      extract=extract, dest=dest)),
                             "Installing failed", entry.title, entry.id)
        for p in others:                            # e.g. a separately downloaded fix archive
            if entry is not None and detect_format(p):
                await self.extract_entry(entry.id, str(p))
        try:
            await self.lookup_steam_app_id(entry.id)
        except Exception as exc:
            log.info("Steam App ID lookup for %s failed: %s", entry.title, exc)
        return {"entry_id": entry.id, "title": entry.title, "verified": verified, "sha256": None,
                "extracting": extracting, "files": [p.name for p in volumes] + [p.name for p in others],
                "parts": "; ".join(summaries), "complete": complete}

    # ------------------------------------------------------------ uninstall
    def _uninstall_items(self, entry_id: int) -> list[dict]:
        """Everything that belongs to a library entry, grouped, with what may be deleted."""
        e = self.library.get(entry_id)
        others = [o for o in self.library.all() if o.id != e.id]
        shared = {Path(p).resolve() for o in others for p in (o.install_path, o.download_path) if p}

        def usable(p: Path) -> tuple[bool, str]:
            if not p.exists():
                return False, "already gone"
            r = p.resolve()
            if r in shared or any(is_within(s, r) for s in shared if s != r):
                return False, "also used by another game in your Library — kept"
            if not self.library.safe_to_delete(p):
                return False, "outside the app's download/installation folders — delete it yourself if you want"
            return True, ""

        def item(key: str, title: str, paths: list[Path], default: bool, note: str = "") -> dict | None:
            seen, rows = set(), []
            for p in paths:
                rp = p.resolve() if p.exists() else p
                if rp in seen:
                    continue
                seen.add(rp)
                ok, why = usable(p)
                rows.append({"path": str(p), "ok": ok, "why": why, "size": dir_size(p) if ok else 0})
            if not rows:
                return None
            return {"key": key, "title": title, "paths": rows, "default": default and any(r["ok"] for r in rows),
                    "size": sum(r["size"] for r in rows), "note": note}

        items = []
        install = [Path(e.install_path)] if e.install_path else []
        items.append(item("files", "Installed game files", install, True))
        downloads: list[Path] = []
        if e.download_path:
            dp = Path(e.download_path)
            v = volume_of(dp)
            if v is not None and dp.parent.is_dir():          # every part of a multi-part archive
                downloads += [p for p in dp.parent.iterdir()
                              if (pv := volume_of(p)) is not None and pv.set_key == v.set_key]
            downloads.append(dp)
        tasks = [t for t in self.downloads.tasks.values()
                 if (e.download_id and t.id == e.download_id) or (e.game_id and t.game_id == e.game_id)]
        for t in tasks:
            if t.filename:
                downloads.append(Path(t.dest_dir) / t.filename)
        downloads = [p for p in downloads if not install or not is_within(p, install[0])]
        items.append(item("downloads", "Downloaded archives", downloads, True,
                          f"{len(tasks)} entr{'y' if len(tasks) == 1 else 'ies'} in Downloads are removed too"
                          if tasks else ""))
        root = Path(e.setup["root"]) if (e.setup or {}).get("root") else None
        if root is not None and root.exists():          # a repack's installer files (FitGirl …)
            covered = [*install, *downloads]
            if not any(root.resolve() == c.resolve() or is_within(root, c) for c in covered if c.exists()):
                items.append(item("repack", "Repack installer files", [root], True,
                                  "setup.exe and its data files — not needed once the game is installed."))
        prefix = self.library.prefix_dir(e.id)
        if prefix.exists():
            it = item("prefix", "Proton prefix (Windows settings and saved games)", [prefix], True,
                      "Your saves for this game are usually stored here.")
            if it:
                for r in it["paths"]:          # the app's own prefix folder is always safe to delete
                    r["ok"], r["why"], r["size"] = True, "", dir_size(prefix)
                it["size"] = sum(r["size"] for r in it["paths"])
                it["default"] = True
            items.append(it)
        if e.steam_shortcut_appid:
            items.append({"key": "shortcut", "title": "Steam shortcut", "paths": [], "default": True, "size": 0,
                          "note": "Removed from your Steam library (restart Steam to see it go)."})
        return [i for i in items if i]

    async def uninstall_plan(self, entry_id: int) -> dict:
        e = self.library.get(entry_id)
        items = await asyncio.to_thread(self._uninstall_items, entry_id)
        return {"entry_id": e.id, "title": e.title, "running": self.library.is_running(e.id), "items": items}

    async def uninstall_game(self, entry_id: int, keys: list[str], backup_saves: bool = True) -> dict:
        """Remove the selected parts of a game, its download entries, logs and finally the library entry.

        If something that was selected cannot be deleted, the library entry is kept (so the user can retry) and
        :class:`UninstallIncomplete` explains which paths are left and why.
        """
        e = self.library.get(entry_id)
        log.info("uninstall requested for %s (%s)", e.title, ", ".join(keys) or "entry only")
        if self.library.is_running(e.id):
            raise SourceError("Stop the game before uninstalling it.")
        items = {i["key"]: i for i in await asyncio.to_thread(self._uninstall_items, entry_id)}
        from ezgd.library import saves as _saves
        if backup_saves and "prefix" in keys and "prefix" in items and \
                not _saves.users_dir(self.library.prefix_dir(entry_id)).is_dir():
            log.info("uninstall of %s: no saves to back up (never started) — uninstalling", e.title)
            backup_saves = False                       # nothing to keep: never a reason not to uninstall
        if "prefix" in keys and "prefix" in items and backup_saves:
            # the prefix holds the saves: zip them first (kept after the uninstall, found again on a reinstall)
            try:
                await asyncio.to_thread(self.library.backup_saves, entry_id, "before-uninstall")
            except Exception as exc:
                raise SourceError(f"Your saves could not be backed up first ({exc}), so nothing was deleted. Untick "
                                  "“Back up my saves first” to uninstall anyway.") from exc
        freed, deleted, kept, failed = 0, [], [], []
        tasks = [t for t in list(self.downloads.tasks.values())
                 if (e.download_id and t.id == e.download_id) or (e.game_id and t.game_id == e.game_id)]
        for t in tasks:                                     # the list entries go in any case
            try:
                await self.downloads.remove(t.id, delete_files=False)
            except Exception as exc:
                log.info("could not remove download %s: %s", t.id, exc)

        def wipe(paths: list[dict]) -> None:
            nonlocal freed
            for r in paths:
                p = Path(r["path"])
                if not r["ok"]:
                    if r["why"] != "already gone":
                        kept.append({"path": str(p), "why": r["why"]})
                    continue
                errors: list[str] = []
                try:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p, onexc=lambda _f, path, exc: errors.append(f"{path}: {exc}"))
                    elif p.exists() or p.is_symlink():
                        p.unlink()
                except OSError as exc:
                    errors.append(str(exc))
                if p.exists() or p.is_symlink():          # never report success without checking
                    why = errors[0] if errors else "it is still there after deleting"
                    if "Permission denied" in why:
                        why = "permission denied — the folder or a file in it is not yours or is read-only"
                    elif "Device or resource busy" in why or "Text file busy" in why:
                        why = "a file in it is in use (is the game or another program still running?)"
                    failed.append({"path": str(p), "why": why})
                    log.warning("uninstall %s: could not delete %s: %s", e.title, p, "; ".join(errors[:3]))
                else:
                    freed += r["size"]
                    deleted.append(str(p))
        for key in ("files", "downloads", "repack", "prefix"):
            if key in items and key in keys:
                await asyncio.to_thread(wipe, items[key]["paths"])
        if "shortcut" in items and "shortcut" in keys and e.steam_shortcut_appid:
            root = self.library.steam_root()
            if root is not None:
                await asyncio.to_thread(steam_remove_shortcut, root, e.steam_shortcut_appid)
        if failed:
            e.notes = "Uninstall incomplete — could not delete: " + "; ".join(f["path"] for f in failed)
            self.library.repo.update(e)
            self.emit("library_changed", e.id)
            raise UninstallIncomplete(e.title, failed, kept, deleted, freed)
        log_file = self.paths.logs / f"game-{e.id}.log"
        log_file.unlink(missing_ok=True)
        self.library.repo.delete(e.id)
        self.emit("library_changed", e.id)
        log.info("uninstalled %s: %d item(s), %d bytes freed, %d kept", e.title, len(deleted), freed, len(kept))
        return {"title": e.title, "deleted": deleted, "kept": kept, "freed": freed}

    # ---------------------------------------------------------- leftovers
    def _leftovers(self) -> list[dict]:
        """Folders/archives in the install and download folders that nothing in the app uses any more.

        Only things the app created (recorded since 2026-10-10) are pre-selected; older ones are offered only if
        their name looks like a game download, unselected; everything else is never listed.
        """
        s = self.settings.settings
        created = self.created_paths.paths()
        titles = {g_key: title for g_key, title in self.games.dedup_titles()}
        used: set[Path] = set()
        for e in self.library.all():
            for p in (e.install_path, e.download_path):
                if p:
                    used.add(Path(p).resolve())
        for t in self.downloads.tasks.values():
            used.add((Path(t.dest_dir) / (t.filename or "")).resolve())
            used.add(Path(t.dest_dir).resolve())
        sets = {(u.parent, v.set_key) for u in used if (v := volume_of(u)) is not None}   # all parts of a set
        out = []
        for root in {Path(s.install_dir), Path(s.download_dir)}:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.name.startswith(".") or child.name == "Browser downloads":
                    continue
                rc = child.resolve()
                if any(rc == u or is_within(u, rc) or is_within(rc, u) for u in used):
                    continue
                cv = volume_of(child) if child.is_file() else None
                if cv is not None and (rc.parent, cv.set_key) in sets:
                    continue
                if not self.library.safe_to_delete(child):
                    continue
                if str(child) in created:
                    why, tick = "created by the app", True
                else:
                    why = self._looks_like_game_download(child, titles)
                    if why is None:
                        continue          # not ours: e.g. an engine or other program kept in the Games folder
                    tick = False          # from before the app recorded what it creates: the user decides
                out.append({"path": str(child), "size": dir_size(child), "is_dir": child.is_dir(), "why": why,
                            "default": tick,
                            "where": "Games folder" if root == Path(s.install_dir) else "Downloads folder"})
        return out

    _RELEASE_MARKS = re.compile(r"(-OFME\b|\bOFME\b|SteamRIP|Fix[_ .-]?Repair|cracked-games|Free[ ._-]Download|"
                                r"\b(FitGirl|DODI|ElAmigos|CODEX|RUNE|TENOKE|EMPRESS|PLAZA|SKIDROW|GOG)\b)", re.I)

    def _looks_like_game_download(self, path: Path, titles: dict[str, str]) -> str | None:
        """Why a folder/archive the app has no record of looks like one of its downloads, else None."""
        m = self._RELEASE_MARKS.search(path.name)
        if m:
            return f"looks like a game release (“{m.group(0)}” in its name)"
        name = re.sub(r"(\.part\d+)?\.(rar|zip|7z|001)$", "", path.name, flags=re.I)
        name = re.sub(r"[._]+", " ", name)
        name = re.sub(r"\s*[\(\[]?\d+[\)\]]?$", "", name)                      # "… (1)"
        name = re.split(r"\s+(v\d[\w.]*|build\s*\d+|update\s*\d+)\b", name, flags=re.I)[0]
        title = titles.get(dedup_key(name))
        if title:
            return f"named like the game “{title}” from the catalog"
        return None

    async def find_leftovers(self) -> list[dict]:
        return await asyncio.to_thread(self._leftovers)

    async def delete_leftovers(self, paths: list[str]) -> dict:
        allowed = {r["path"]: r for r in await asyncio.to_thread(self._leftovers)}
        deleted, failed, freed = [], [], 0

        def run() -> None:
            nonlocal freed
            for p in paths:
                r = allowed.get(p)
                if r is None:
                    failed.append({"path": p, "why": "no longer a leftover (something uses it now)"})
                    continue
                path = Path(p)
                try:
                    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
                except OSError as exc:
                    failed.append({"path": p, "why": str(exc)})
                    continue
                if path.exists():
                    failed.append({"path": p, "why": "it is still there after deleting"})
                else:
                    deleted.append(p)
                    freed += r["size"]
                    self.created_paths.remove(p)
        await asyncio.to_thread(run)
        log.info("leftovers: deleted %d, failed %d, freed %d bytes", len(deleted), len(failed), freed)
        return {"deleted": deleted, "failed": failed, "freed": freed}

    async def cancel_extraction(self, ext_id: int) -> bool:
        return self.post.cancel(ext_id)

    async def recently_downloaded(self, limit: int = 12) -> list[dict]:
        return [t.snapshot() for t in self.download_repo.recent_completed(limit)]

    # ---------------------------------------------------------------- media
    async def cover(self, url: str) -> bytes | None:
        if not url or not url.startswith(("http://", "https://")):
            return None
        key = hashlib.sha1(url.encode()).hexdigest()
        path = self.paths.covers / key
        if path.exists():
            try:
                return path.read_bytes()
            except OSError:
                return None
        try:
            # like a browser rendering a page: no per-page politeness delay, but the UI caps
            # concurrent cover requests and every cover is cached on disk
            res = await self.client.fetch(url, max_bytes=MAX_COVER_BYTES, headers={"Accept": "image/*"},
                                          polite=False)
        except Exception as exc:
            log.debug("cover %s failed: %s", url, exc)
            return None
        if res.status != 200 or not res.headers.get("content-type", "").startswith("image/"):
            return None
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(res.content)
        os.replace(tmp, path)
        return res.content

    def _prune_covers(self) -> None:
        limit = self.settings.settings.cover_cache_mb * 1024 * 1024
        files = sorted(self.paths.covers.glob("*"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > limit:
            p = files.pop(0)
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

    # ---------------------------------------------------------- maintenance
    async def cache_stats(self) -> dict:
        covers = sum(p.stat().st_size for p in self.paths.covers.glob("*"))
        return {"http_cache_bytes": self.http_cache.size_bytes(), "cover_bytes": covers,
                "games": self.games.count()}

    async def clear_cache(self, include_catalog: bool = False) -> dict:
        n = self.http_cache.clear()
        for p in self.paths.covers.glob("*"):
            p.unlink(missing_ok=True)
        if include_catalog:
            self.games.clear()
        return {"responses": n}

    async def backup_database(self) -> str:
        path = self.db.backup(self.paths.backups / f"manual-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
        self.db.prune_backups(keep=15)
        return str(path)

    async def list_backups(self) -> list[dict]:
        return [{"path": str(p), "size": p.stat().st_size, "mtime": p.stat().st_mtime} for p in self.db.list_backups()]

    async def restore_database(self, path: str) -> None:
        """Restore requires a restart of the core: downloads are stopped first, and nothing from the
        in-memory state may be written into the restored database afterwards."""
        await self.downloads.shutdown()
        self.db.restore(Path(path))
        self._restored = True

    async def recent_events(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    async def report_problem(self, source: str, kind: str, url: str | None, message: str, comment: str = "") -> str:
        st = self.registry.status.get(source)
        return str(write_report(self.paths.reports, source=source, kind=kind, url=url, message=message,
                                user_comment=comment, status=st.state if st else None))

    # ---------------------------------------------------- debugging assistant
    async def linux_ratings(self, items: list) -> list[dict | None]:
        """How well games run on Linux (ProtonDB players' reports, offline index) — for the Store's cards.
        ``items``: ``[(steam_app_id or None, title), …]``; games without an App ID are found by title. Nothing is
        fetched: without the index (Settings → Library → ProtonDB) every answer is None."""
        from ezgd.library import appid, protondb

        def work() -> list[dict | None]:
            path = self._protondb_index()
            if not path.exists():
                return [None] * len(items)
            games = protondb._load(path).get("games") or {}
            titles = self._protondb_titles()
            out = []
            for app, title in items:
                a = app or next((titles[k] for k in appid.title_keys(title or "") if titles.get(k)), None)
                out.append(protondb.rating(games.get(str(a)), a) if a else None)
            return out
        return await asyncio.to_thread(work)

    async def protondb_summary(self, app_id: int | None) -> dict | None:
        """ProtonDB's public rating summary for a Steam game (cached for a day); None when unknown/offline."""
        if not app_id:
            return None
        try:
            res = await self.fetcher.get(f"https://www.protondb.com/api/v1/reports/summaries/{int(app_id)}.json",
                                         ttl=24 * 3600, max_bytes=64 * 1024)
            if res.status != 200:
                return None
            d = json.loads(res.body)
            return {"tier": d.get("tier"), "total": d.get("total"), "confidence": d.get("confidence"),
                    "trending": d.get("trendingTier")}
        except Exception as exc:                  # only a hint for the assistant
            log.info("ProtonDB lookup for %s failed: %s", app_id, exc)
            return None

    async def run_trials(self, entry_id: int, findings: list[dict], generated: list[dict] | None = None,
                         deep: bool = False, repairs: list | None = None, symptom: str = "",
                         protons: bool = True) -> list[dict]:
        """“Try fixes for me”: test candidate setups one after another (each in a throw-away prefix).
        ``deep`` (“Try everything”): many more setups; stops at the first one that keeps the game running."""
        if self.library.is_running(entry_id):
            raise SourceError("Stop the game first.")
        cands = self.library.trial_candidates(entry_id, findings, generated, deep=deep, protons=protons)
        if getattr(self, "_trial_cancel", None) is None or self._trial_cancel.is_set():
            self._trial_cancel = threading.Event()
        found: list = [] if repairs is None else repairs
        # stop at the first setup that works: the rest would only make you wait
        results = await self._run_candidates(entry_id, cands, stop_on_success=True, repairs=found, symptom=symptom)
        if found and repairs is None:              # the UI offers them (they change the real prefix: not tested)
            self.emit("trial_repairs", {"id": entry_id, "findings": found})
        return results

    MAX_ADAPTIVE = 4                     # setups added from what the test runs revealed

    def _key(self, overrides: dict) -> str:
        return repr(sorted((k, repr(v)) for k, v in (overrides or {}).items()))

    async def _learn_from_trial(self, entry_id: int, n: int, symptom: str = "") -> tuple[list[dict], list[dict]]:
        """Analyse test run ``n``'s detailed log: (new setups worth testing, repairs to make in the real prefix)."""
        ev = self.library.trial_evidence.get(entry_id, {}).get(n)
        if not ev:
            return [], []
        try:
            d = await asyncio.to_thread(self.library.diagnose, entry_id, None, False, symptom, None, None, None, ev)
        except Exception as exc:
            log.info("analysing trial %s failed: %s", n, exc)
            return [], []
        strong = [f for f in d["findings"] if f.get("fix") and f["confidence"] >= 0.6
                  and not f["key"].startswith(("exit:", "regression"))]
        cands = [c for c in self.library.trial_candidates(entry_id, strong, []) if c["overrides"]]
        repairs = [f for f in strong if f["fix"]["kind"] in ("winetricks", "apply_archive", "repair_files")]
        return cands, repairs

    async def investigate(self, entry_id: int, symptom: str = "") -> dict:
        """The assistant runs the game once itself — current setup, a throw-away copy of the prefix, Wine's detailed
        logging — and analyses that run (no need to do a diagnostic launch by hand)."""
        if self.library.is_running(entry_id):
            raise SourceError("Stop the game first.")
        self._trial_cancel = threading.Event()
        cand = {"label": "Current setup", "overrides": {}}
        self.emit("trial", {"id": entry_id, "index": 1, "total": 1, "label": "Test run with detailed logging",
                            "status": "running"})
        r = await asyncio.to_thread(self.library.run_trial, entry_id, cand, 1, self._trial_cancel)
        self.emit("trial", {"id": entry_id, "index": 1, "total": 1, "label": "Test run with detailed logging",
                            "status": "done", "result": r})
        ev = self.library.trial_evidence.get(entry_id, {}).get(1)
        if r["outcome"] == "ran":
            ev = {**(ev or {}), "exit_code": None, "seconds": r.get("seconds")}
        d = await self.diagnose_game(entry_id, symptom, evidence=ev)
        d["investigation"] = r
        return d

    async def _run_candidates(self, entry_id: int, cands: list[dict], first: int = 1,
                              stop_on_success: bool = False, adaptive: bool = True,
                              repairs: list | None = None, symptom: str = "") -> list[dict]:
        """Test the setups one by one. ``adaptive``: a failed test's detailed log is analysed at once — a cause it
        reveals becomes the next setup tested (and repairs for the real prefix are collected in ``repairs``)."""
        results = []
        cands = list(cands)
        queued = {self._key(c["overrides"]) for c in cands}
        added = 0
        total = first - 1 + len(cands)
        n = first - 1
        while cands:
            cand = cands.pop(0)
            n += 1
            if self._trial_cancel.is_set():
                break
            self.emit("trial", {"id": entry_id, "index": n, "total": total, "label": cand["label"],
                                "status": "running"})
            r = await asyncio.to_thread(self.library.run_trial, entry_id, cand, n, self._trial_cancel)
            r["gen_id"] = cand.get("gen_id")
            r["generated"] = bool(cand.get("generated"))
            r["combined"] = bool(cand.get("combined"))
            results.append(r)
            self.emit("trial", {"id": entry_id, "index": n, "total": total, "label": cand["label"],
                                "status": "done", "result": r})
            # the baseline (index 1, no overrides) "running" says nothing about a fix; a later one is the answer
            if stop_on_success and r["outcome"] == "ran" and cand.get("overrides"):
                break
            if adaptive and r["outcome"] in ("crashed", "exited") and added < self.MAX_ADAPTIVE:
                new, fixes = await self._learn_from_trial(entry_id, n, symptom)
                at = 0
                for c in new:
                    k = self._key(c["overrides"])
                    if k not in queued and added < self.MAX_ADAPTIVE:
                        queued.add(k)
                        added += 1
                        c = {**c, "label": f"{c['label']} (found in test {n})", "adaptive": True}
                        cands.insert(at, c)                   # what the evidence says goes next
                        at += 1
                        total += 1
                if repairs is not None:
                    for f in fixes:
                        if all(f["fix"] != x["fix"] for x in repairs):
                            repairs.append(f)
        log.info("trials for entry %s: %s", entry_id, "; ".join(f"{r['label']}={r['outcome']}" for r in results))
        return results

    async def cancel_trials(self) -> None:
        ev = getattr(self, "_trial_cancel", None)
        if ev is not None:
            ev.set()

    async def apply_trial(self, entry_id: int, overrides: dict, gen_id: str | None = None,
                          problems: list[str] | None = None) -> str:
        """Make a tested setup the game's real one (config edits get a backup); remember generated fixes that worked."""
        from ezgd.library import fixgen
        from ezgd.library.diagnose import engine_of
        changes = {k: v for k, v in overrides.items() if k in ("compat", "launch_args", "proton_version")}
        edits = overrides.get("edits") or []
        exe = overrides.get("executable")
        verbs = overrides.get("winetricks") or []
        if not changes and not edits and not exe and not verbs:
            return "That is already the current setup."
        if verbs:                                  # it worked in the test copy: now into the real prefix
            code = await asyncio.to_thread(self.library.run_winetricks, entry_id, verbs, True)
            if code not in (0, None):
                raise SourceError(f"Installing {' '.join(verbs)} failed: "
                                  + self.library._winetricks_failure(entry_id, " ".join(verbs)))
        if changes:
            self.library.update(entry_id, **changes)
        if exe and exe != self.library.get(entry_id).executable:
            self.library.set_executable(entry_id, exe)
        self._expect_followup(entry_id, "the setup from the test", self._override_keys(overrides))
        backups = await asyncio.to_thread(fixgen.backup_and_apply, edits) if edits else []
        e = self.library.get(entry_id)
        if gen_id and problems:
            exe = Path(e.executable) if e.executable else None
            await asyncio.to_thread(fixgen.remember, self.library.learned_path,
                                    engine=engine_of(exe) if exe else None, problems=problems, gen_id=gen_id,
                                    title=e.title)
        msg = "Saved — Play now uses the setup that worked in the test."
        if backups:
            msg += " The original config file was kept as " + ", ".join(Path(b).name for b in backups) + "."
        return msg

    def _extraction_records(self, entry_id: int) -> list[dict]:
        rows = [r for r in self.extraction_repo.for_library(entry_id) if r.get("status") == "completed"]
        return [{"id": r["id"], "archive": r["archive_path"], "dest": r["dest_dir"]}       # oldest first:
                for r in sorted(rows, key=lambda r: r["id"])]                               # later (fixes) win

    async def integrity_report(self, entry_id: int):
        from ezgd.installation import integrity
        recs = self._extraction_records(entry_id)
        if not recs:
            return None
        pw = await self.entry_password(entry_id)
        return await asyncio.to_thread(integrity.check, recs, pw, self.settings.settings.unrar_tool or None)

    async def diagnose_game(self, entry_id: int, symptom: str = "", evidence: dict | None = None) -> dict:
        e = self.library.get(entry_id)
        pdb = await self.protondb_summary(e.steam_app_id)
        rep = await self.integrity_report(entry_id)
        recs = self._extraction_records(entry_id)
        dest = recs[0]["dest"] if recs else None
        from ezgd.library import protondb
        community = await asyncio.to_thread(protondb.for_game, self._protondb_index(), e.steam_app_id)
        d = await asyncio.to_thread(self.library.diagnose, entry_id, pdb, True, symptom,
                                    rep.to_dict() if rep else None, dest, community, evidence)
        try:
            self._apply_learning(e, d.get("findings") or [])
        except Exception:                          # learning is a hint; never break the diagnosis
            log.exception("applying learned fix outcomes failed")
        ge = await self.ge_proton_status()
        if ge.get("newer_available"):
            d["facts"].append(f"Newer GE-Proton available: {ge['latest']}"
                              + (f" (you have {ge['newest_installed']})" if ge.get("newest_installed") else ""))
        d["ge"] = ge
        return d

    # ------------------------------------------------- ProtonDB community fixes
    def _protondb_index(self) -> Path:
        from ezgd.library import protondb
        return self.paths.data / "protondb" / protondb.INDEX_FILE

    async def protondb_status(self) -> dict:
        from ezgd.library import protondb
        st = await asyncio.to_thread(protondb.info, self._protondb_index())
        st["enabled"] = self.settings.settings.protondb_fixes
        return st

    async def update_protondb(self, force: bool = False) -> dict:
        """Download the newest ProtonDB report dump (≈70 MB) and rebuild the per-game index."""
        from ezgd.downloads.models import DownloadKind, DownloadTask
        from ezgd.library import protondb
        res = await self.fetcher.get(protondb.LISTING, ttl=0 if force else 6 * 3600, max_bytes=512 * 1024,
                                     headers={"Accept": "application/vnd.github+json"})
        if res.status != 200:
            raise SourceError(f"GitHub answered HTTP {res.status}")
        latest = protondb.latest_dump(json.loads(res.body))
        if latest is None:
            raise SourceError("No ProtonDB data dump was found")
        cur = await asyncio.to_thread(protondb.info, self._protondb_index())
        if cur.get("source") == latest["name"] and not force:
            return {**cur, "result": "up to date"}
        url = latest.get("download_url") or ("https://raw.githubusercontent.com/bdefore/protondb-data/master/reports/"
                                             + latest["name"])
        cache = self.paths.cache / "protondb"
        task = DownloadTask(id=None, kind=DownloadKind.HTTP, title=latest["name"], uri=url, dest_dir=str(cache),
                            filename=latest["name"])
        log.info("downloading ProtonDB data %s", latest["name"])
        path = await self.http_engine.run(task, lambda t: self.emit("protondb_update", {
            "received": t.downloaded_bytes, "total": latest.get("size", 0), "status": "downloading"}), lambda t: None)
        self.emit("protondb_update", {"status": "indexing"})
        try:
            stats = await asyncio.to_thread(protondb.build_index, Path(path), self._protondb_index())
        finally:
            Path(path).unlink(missing_ok=True)
        log.info("ProtonDB index: %s", stats)
        self.emit("protondb_update", {"status": "done", **stats})
        return {**(await asyncio.to_thread(protondb.info, self._protondb_index())), "result": "updated"}

    async def _protondb_loop(self) -> None:
        await asyncio.sleep(60)                       # not during start-up
        while True:
            try:
                if self.settings.settings.protondb_fixes:
                    await self.update_protondb()      # cheap when the newest dump is already indexed
            except Exception as exc:
                log.info("ProtonDB update failed: %s", exc)
            await asyncio.sleep(24 * 3600)

    # ------------------------------------------------------- Proton on demand
    async def ge_proton_status(self) -> dict:
        """The newest GE-Proton on GitHub vs. what is installed (for the assistant and Settings)."""
        from ezgd.platform import proton_install as pi
        root = self.library.steam_root()
        have = [n for n in pi.installed(root) if pi.ge_version(n)]
        newest_local = max(have, key=pi.ge_version) if have else None
        try:
            res = await self.fetcher.get(pi.RELEASES, ttl=6 * 3600, max_bytes=512 * 1024,
                                         headers={"Accept": "application/vnd.github+json"})
            rel = pi.parse_release(res.body) if res.status == 200 else None
        except Exception as exc:
            log.info("GE-Proton release check failed: %s", exc)
            rel = None
        newer = bool(rel and (newest_local is None or pi.ge_version(rel.tag) > pi.ge_version(newest_local)))
        return {"latest": rel.tag if rel else None, "size": rel.size if rel else 0, "installed": have,
                "newest_installed": newest_local, "newer_available": newer}

    async def install_ge_proton(self) -> str:
        """Download (SHA-512 verified) and install the latest GE-Proton; returns its name."""
        from ezgd.downloads.models import DownloadKind, DownloadTask
        from ezgd.platform import proton_install as pi
        res = await self.fetcher.get(pi.RELEASES, ttl=0, force=True, max_bytes=512 * 1024,
                                     headers={"Accept": "application/vnd.github+json"})
        if res.status != 200:
            raise SourceError(f"GitHub answered HTTP {res.status}")
        rel = pi.parse_release(res.body)
        root = self.library.steam_root()
        if pi.is_installed(root, rel.tag):
            return rel.tag
        sha = await self.fetcher.get(rel.sha_url, ttl=0, force=True, max_bytes=64 * 1024)
        digest = pi.parse_sha512(sha.text(), rel.tar_name)
        cache = self.paths.cache / "proton"
        task = DownloadTask(id=None, kind=DownloadKind.HTTP, title=rel.tag, uri=rel.tar_url, dest_dir=str(cache),
                            filename=rel.tar_name, expected_checksum=f"sha512:{digest}")

        def progress(t) -> None:
            self.emit("proton_install", {"tag": rel.tag, "received": t.downloaded_bytes, "total": rel.size,
                                         "status": t.status_detail or "downloading"})
        log.info("installing %s", rel.tag)
        path = await self.http_engine.run(task, progress, lambda _t: None)
        self.emit("proton_install", {"tag": rel.tag, "received": rel.size, "total": rel.size, "status": "unpacking"})
        await asyncio.to_thread(self.library.pin_current_protons)    # the new GE must not take over working games
        try:
            await asyncio.to_thread(pi.extract, Path(path), root, rel.tag)
        finally:
            Path(path).unlink(missing_ok=True)
        self.library.protons(refresh=True)
        self.emit("proton_install", {"tag": rel.tag, "received": rel.size, "total": rel.size, "status": "done"})
        self.emit("settings_changed", "protons")
        log.info("%s installed", rel.tag)
        return rel.tag

    # fixes "Fix my game" applies on its own: strong evidence, reversible or additive, no admin rights, no download
    AUTO_FIX_KINDS = ("repair_files", "write_file", "edit", "copy_files", "copy_folder", "apply_archive",
                      "set_executable", "winetricks", "enable_fix", "launch_args", "restore_setup", "set_overrides",
                      "separate_prefix")
    AUTO_FIX_CONFIDENCE = 0.7

    def _outcomes_path(self) -> Path:
        return self.paths.data / "fix_outcomes.json"

    def fix_stats(self) -> dict:
        try:
            recs = json.loads(self._outcomes_path().read_text("utf-8"))
        except (OSError, ValueError):
            recs = []
        latest: dict[str, bool] = {}
        for r in recs:                              # one verdict per game: the latest answer counts
            latest[r.get("game", "")] = bool(r.get("worked"))
        n, ok = len(latest), sum(latest.values())
        return {"games": n, "fixed": ok, "rate": round(100 * ok / n) if n else None}

    async def record_outcome(self, entry_id: int, worked: bool, actions: list[str] | None = None) -> dict:
        e = self.library.get(entry_id)
        p = self._outcomes_path()
        try:
            recs = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            recs = []
        rec = {"game": e.title, "worked": worked, "actions": actions or [], "at": time.time(),
               **self._outcome_context(e)}
        keys = (self._followups().get(str(entry_id)) or {}).get("keys")
        if keys:
            rec["keys"] = keys
        recs.append(rec)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(recs[-2000:]), "utf-8")
        log.info("fix outcome for %s: %s", e.title, "worked" if worked else "did not work")
        return self.fix_stats()

    async def fix_my_game(self, entry_id: int, symptom: str = "", test: bool = True) -> dict:
        """One button: check, repair what has strong evidence, test setups, and give one answer."""
        e = self.library.get(entry_id)
        if self.library.is_running(entry_id):
            raise SourceError("Stop the game first.")

        progress = [0.0]
        stage_at = {"Checking the game": 0.02, "Investigating": 0.1, "Repairing": 0.2, "Fixing the cause": 0.3,
                    "Testing": 0.55, "Thinking again": 0.85}

        def step(name: str, status: str, detail: str = "", at: float | None = None) -> None:
            if at is not None:
                progress[0] = max(progress[0], at)
            elif status == "running":
                progress[0] = max(progress[0], stage_at.get(name, progress[0]))
            self.emit("fixmygame", {"id": entry_id, "step": name, "status": status, "detail": detail,
                                    "progress": round(progress[0], 3)})
        before = {"launch_args": e.launch_args or "", "compat": e.compat_config.to_dict(),
                  "proton_version": e.proton_version or "", "executable": e.executable}
        step("Checking the game", "running", "files, Steam setup, fix archives, logs and your system")
        d = await self.diagnose_game(entry_id, symptom)
        step("Checking the game", "done", d["summary"])
        top = max((f["confidence"] for f in d["findings"] if f.get("fix")), default=0.0)
        seen_ev = None                                 # the investigation's test run (its evidence is kept)
        if test and top < 0.6 and d.get("suggest_diagnostic_run", True):
            # weak evidence: look first (what "Investigate" did) — one start with Wine's detailed logging
            step("Investigating", "running", "starting the game once with detailed logging (about a minute)")
            if getattr(self, "_trial_cancel", None) is None or self._trial_cancel.is_set():
                self._trial_cancel = threading.Event()
            r = await asyncio.to_thread(self.library.run_trial, entry_id, {"label": "Current setup", "overrides": {}},
                                        1, self._trial_cancel)
            ev = self.library.trial_evidence.get(entry_id, {}).get(1)
            if ev and r["outcome"] != "ran":
                d = await self.diagnose_game(entry_id, symptom, evidence=ev)
                seen_ev = ev
            step("Investigating", "done", r["detail"] + (f" — {d['summary']}" if ev and r["outcome"] != "ran" else ""))
        applied: list[str] = []
        step("Repairing", "running")
        await self._auto_repair(entry_id, d["findings"], applied, step, "Repairing")
        step("Repairing", "done", f"{len(applied)} repair(s)" if applied else "nothing to repair automatically")
        verdict: dict = {"applied": applied, "before": before, "findings": d["findings"][:5]}
        unfixable = next((f for f in d["findings"] if not f["fixable"] and f["confidence"] >= 0.7), None)
        if not test:
            verdict.update(status="changed" if applied else "nothing", kept=None)
            return verdict
        # the root cause first: act on what the evidence names (the game's own error message, a missing DLL or
        # runtime), test once, read that test, repeat — a fixed list of setups (every Proton…) only as a last resort
        d2 = await self.diagnose_game(entry_id, symptom, evidence=seen_ev)    # findings after the repairs
        rounds, d2, acted = await self._root_cause(entry_id, symptom, d2, applied, step,
                                                   tested=seen_ev is not None and not applied)
        verdict["applied"] = applied
        if rounds and rounds[-1]["outcome"] == "ran":
            step("Testing", "running", "it ran — checking once more", at=0.9)
            again = await asyncio.to_thread(self.library.run_trial, entry_id,
                                            {"label": "Second check", "overrides": {}}, len(rounds) + 1,
                                            self._trial_cancel)
            if again["outcome"] == "ran":
                kept = "; ".join(a.split(" — ")[0] for a in (acted or applied)) or "the repaired setup"
                verdict.update(status="fixed", kept=kept, trials=rounds + [again])
                step("Testing", "done", f"✓ the game kept running after: {kept} (checked twice)", at=1.0)
                return verdict
            step("Testing", "running", f"✗ the second check failed ({again['detail']})")
            rounds.append(again)
        if self._trial_cancel.is_set():
            verdict.update(status="not_fixed", kept=None, trials=rounds, reason="Stopped by you.")
            step("Testing", "done", "stopped", at=1.0)
            return verdict
        # nothing certain left: test setups — other Protons only when the evidence is about Wine/Proton itself
        protons = not acted or any(f["key"].startswith(("proton-lacks", "unimplemented", "exception", "dump",
                                                        "graphics", "wine")) for f in d2["findings"][:3])
        step("Testing", "running", "no single clear cause left — testing setups"
             + ("" if protons else " (not other Protons: the cause is not Proton)"), at=0.6)
        found_repairs: list = []
        results = rounds + await self.run_trials(entry_id, d2["findings"], d2.get("generated"), repairs=found_repairs,
                                                 symptom=symptom, protons=protons)
        if getattr(self, "_trial_cancel", None) is None:
            self._trial_cancel = threading.Event()
        if not any(r["outcome"] == "ran" for r in results) and not self._trial_cancel.is_set():
            pairs = self.library.combine_candidates(results)   # some games need two fixes together
            if pairs:
                step("Testing", "running", f"no single fix worked — trying {len(pairs)} combination(s)")
                results += await self._run_candidates(entry_id, pairs, first=len(results) + 1)
        if not any(r["outcome"] == "ran" for r in results) and not self._trial_cancel.is_set():
            # still nothing: the general fixes the evidence did not point at ("Try everything")
            tried = {repr(sorted((k, repr(v)) for k, v in (r.get("overrides") or {}).items())) for r in results}
            more = [c for c in self.library.trial_candidates(entry_id, d2["findings"], (d2.get("generated") or [])
                                                             + (d2.get("generated_deep") or []), deep=True)
                    if repr(sorted((k, repr(v)) for k, v in c["overrides"].items())) not in tried]
            if more:
                step("Testing", "running", f"trying {len(more)} general fix(es) as well")
                results += await self._run_candidates(entry_id, more, first=len(results) + 1, stop_on_success=True)
        if not any(r["outcome"] == "ran" for r in results) and not self._trial_cancel.is_set():
            # second round: the test runs logged Wine's details (DLLs, exceptions) — new evidence to reason over
            ev = self.library.trial_evidence.get(entry_id, {})
            d3 = await self.diagnose_game(entry_id, symptom, evidence=ev[min(ev)] if ev else None)
            known = {(f["fix"]["kind"], f["fix"]["value"]) for f in d["findings"] + d2["findings"] if f.get("fix")}
            new = [f for f in found_repairs + d3["findings"] if f.get("fix") and f["confidence"] >= 0.6
                   and (f["fix"]["kind"], f["fix"]["value"]) not in known and f["fix"]["kind"] in self.AUTO_FIX_KINDS]
            new = [f for i, f in enumerate(new) if all(f["fix"] != g["fix"] for g in new[:i])]
            if new:
                step("Thinking again", "running", "the test runs showed more: " + "; ".join(f["title"] for f in new[:3]))
                n_before = len(applied)
                await self._auto_repair(entry_id, new[:3], applied, step, "Thinking again", threshold=0.6)
                if len(applied) > n_before:
                    base = self.library.trial_candidates(entry_id, d3["findings"], [])[:1]     # the repaired setup
                    results += await self._run_candidates(entry_id, base, first=len(results) + 1)
                step("Thinking again", "done", f"{len(applied) - n_before} more repair(s)")
                verdict["applied"] = applied
        # best first: really got into the game (not stuck at start-up), drew frames, got furthest
        def rank(r: dict):
            return (bool(r.get("stuck")), {True: 0, None: 1, False: 2}[r.get("frames")], -len(r.get("progress") or []))
        good = sorted([r for r in results if r["outcome"] == "ran"], key=rank)
        verdict["trials"] = results
        base = results[0] if results and not results[0].get("overrides") else None
        diff = None
        if base is not None and base["outcome"] == "ran" and not base.get("stuck"):
            # the bare test copy runs, yet the real starts fail: what differs is how the game is really started
            diff = await asyncio.to_thread(self._real_start_difference, entry_id, base)
            if diff:
                verdict["difference"] = diff["text"]
                step("Testing", "running", diff["text"])
                if diff.get("fix"):
                    kind, value = diff["fix"]
                    msg = await self.apply_fix(entry_id, kind, value)
                    applied.append(f"{diff['title']} — {msg}")
        confirmed = None
        for r in good[:3]:                       # a setup must pass twice before it is kept (one lucky run ≠ fixed)
            if not r["overrides"]:
                confirmed = r
                break
            if self._trial_cancel.is_set():
                break
            step("Testing", "running", f"checking “{r['label']}” once more")
            again = await asyncio.to_thread(self.library.run_trial, entry_id,
                                            {"label": f"{r['label']} (second check)", "overrides": r["overrides"]},
                                            len(results) + 1, self._trial_cancel)
            if again["outcome"] == "ran":
                confirmed = r
                break
            step("Testing", "running", f"✗ “{r['label']}” failed the second check ({again['detail']}) — not kept")
        if confirmed is not None and not confirmed["overrides"] and not (diff and diff.get("fix")):
            verdict.update(status="not_fixed", kept=None,
                           reason=(diff["text"] if diff else "The game runs in the test with its current setup, "
                                   "so the problem is not in the launch setup.") + " Start it again; if it still "
                                  "fails, choose what happens under “What goes wrong?” and check again.")
            step("Testing", "done", "the current setup ran in the test")
            return verdict
        if confirmed is not None:
            best = confirmed
            if best["overrides"]:
                await self.apply_trial(entry_id, best["overrides"], best.get("gen_id"),
                                       [f["key"] for f in d["findings"][:3]] + ([f"symptom:{symptom}"] if symptom else []))
            kept = best["label"] if best["overrides"] else diff["title"]
            verdict.update(status="fixed", kept=kept)
            step("Testing", "done", f"✓ {kept} kept the game running (checked twice)" if best["overrides"]
                 else f"✓ {kept}")
        else:
            ge = await self.ge_proton_status()
            if ge.get("newer_available"):
                verdict["offer_ge"] = {"tag": ge["latest"], "size": ge["size"]}
            verdict.update(status="not_fixed", kept=None,
                           reason=(unfixable["title"] + " — " + unfixable["explanation"]) if unfixable else
                           "None of the setups kept the game running. Run a diagnostic launch and check the "
                           "findings, or try the game's other release.")
            step("Testing", "done", "✗ no setup kept the game running")
        return verdict

    ROOT_KINDS = ("repair_files", "write_file", "edit", "copy_files", "copy_folder", "apply_archive", "set_executable",
                  "winetricks", "enable_fix", "launch_args", "set_overrides", "proton", "add_args")
    ROOT_CONFIDENCE = 0.85               # what the evidence names this clearly is fixed, not tested among others
    ROOT_ROUNDS = 5

    async def _root_cause(self, entry_id: int, symptom: str, d: dict, applied: list[str], step,
                          tested: bool = False) -> tuple[list[dict], dict, list[str]]:
        """Fix the cause the evidence names, test once, read what that test showed, repeat. Returns (the test runs,
        the last diagnosis, what was changed)."""
        if getattr(self, "_trial_cancel", None) is None or self._trial_cancel.is_set():
            self._trial_cancel = threading.Event()
        results: list[dict] = []
        acted: list[str] = []
        done: set = set()
        for rnd in range(1, self.ROOT_ROUNDS + 1):
            if self._trial_cancel.is_set():
                break
            base = 0.3 + 0.5 * (rnd - 1) / self.ROOT_ROUNDS
            already = {a.split(" — ")[0] for a in applied}       # (repaired in the step before)
            causes = [f for f in d["findings"] if f.get("fix") and f["confidence"] >= self.ROOT_CONFIDENCE
                      and f["fix"]["kind"] in self.ROOT_KINDS and (f["fix"]["kind"], f["fix"]["value"]) not in done
                      and f["title"] not in already]
            if not causes and (rnd > 1 or tested):
                break                                   # nothing certain (and the game was just tested as it is)
            label = "Current setup"
            if causes:
                f = causes[0]
                fx = f["fix"]
                done.add((fx["kind"], fx["value"]))
                step("Fixing the cause", "running", f"{f['title']} → {fx['label']}", at=base)
                try:
                    if fx["kind"] == "winetricks":
                        step("Fixing the cause", "running", f"installing {fx['value']} (a few minutes)", at=base)
                        code = await asyncio.to_thread(self.library.run_winetricks, entry_id, fx["value"].split(), True)
                        if code not in (0, None):
                            raise SourceError(self.library._winetricks_failure(entry_id, fx["value"]))
                        msg = f"installed {fx['value']}"
                    else:
                        msg = await self.apply_fix(entry_id, fx["kind"], fx["value"])
                        if fx["kind"] == "proton":           # its own prefix, before anything is installed into it
                            await asyncio.to_thread(self.library.ensure_prefix_for, self.library.get(entry_id))
                    applied.append(f"{f['title']} — {msg}")
                    acted.append(f"{fx['label']} — {msg}")
                    label = f"After: {fx['label']}"
                    step("Fixing the cause", "running", f"✓ {fx['label']}", at=base + 0.04)
                except Exception as exc:
                    applied.append(f"{f['title']} — could not apply: {exc}")
                    step("Fixing the cause", "running", f"✗ {fx['label']}: {exc}", at=base + 0.04)
                    d = {**d, "findings": [g for g in d["findings"] if g is not f]}
                    continue
            step("Testing", "running", "starting the game once — " + ("did that fix it?" if causes else
                                                                     "what does it do?"), at=base + 0.05)
            r = await asyncio.to_thread(self.library.run_trial, entry_id, {"label": label, "overrides": {}},
                                        len(results) + 1, self._trial_cancel)
            results.append(r)
            mark = "✓" if r["outcome"] == "ran" else "✗"
            step("Testing", "running", f"{mark} {label}: {r['detail']}", at=base + 0.09)
            if r["outcome"] == "ran":
                break
            ev = self.library.trial_evidence.get(entry_id, {}).get(len(results))
            d = await self.diagnose_game(entry_id, symptom, evidence=ev)    # what did this test show?
        return results, d, acted

    def _real_start_difference(self, entry_id: int, base: dict) -> dict | None:
        """The current setup ran in the test copy but the game's real starts fail: what is different?"""
        from ezgd.library import history
        rs = history.runs(self.paths.logs / f"game-{entry_id}.log", limit=3)
        if not rs or not rs[-1].failed:
            return None
        last = rs[-1]
        e = self.library.get(entry_id)
        if last.facet("overlay") in ("full", "friends list") and "no_steam_overlay" not in e.compat_config.fixes:
            how = "started through Steam with the overlay" if last.facet("through_steam") else "with the Steam overlay"
            return {"title": "The Steam overlay stops this game",
                    "text": f"In the test (no Steam overlay) the game runs; your real starts, {how}, fail — so the "
                            "overlay is the difference. It is switched off for this game now.",
                    "fix": ("enable_fix", "no_steam_overlay")}
        if str(base.get("prefix", "")).startswith("a fresh prefix"):
            return {"title": "The game's prefix is damaged",
                    "text": "The game runs in a fresh prefix but not in its own, so its prefix is damaged. Diagnose "
                            "→ “Rebuild the prefix” makes a new one (the old one is kept, saves can be copied back)."}
        return {"title": "It runs in the test",
                "text": "The game ran in the test copy with exactly its current setup, but failed when you started "
                        "it — often something passing (Steam not signed in yet, a first-start setup that has "
                        "finished now)."}

    async def _auto_repair(self, entry_id: int, findings: list[dict], applied: list[str], step, name: str,
                           threshold: float | None = None) -> None:
        """Apply the clear-cut fixes among ``findings`` (strong evidence, a repair the app can do itself)."""
        threshold = self.AUTO_FIX_CONFIDENCE if threshold is None else threshold
        for f in findings:
            fx = f.get("fix")
            if not fx or fx["kind"] not in self.AUTO_FIX_KINDS or f["confidence"] < threshold:
                continue
            try:
                if fx["kind"] == "winetricks":
                    step(name, "running", f"installing {fx['value']} (this can take a few minutes)")
                    await asyncio.to_thread(self.library.run_winetricks, entry_id, fx["value"].split(), True)
                    msg = f"installed {fx['value']}"
                else:
                    msg = await self.apply_fix(entry_id, fx["kind"], fx["value"])
                applied.append(f"{f['title']} — {msg}")
                step(name, "running", f"✓ {f['title']}")
            except Exception as exc:                 # one failed repair must not stop the rest
                applied.append(f"{f['title']} — could not apply: {exc}")
                step(name, "running", f"✗ {f['title']}: {exc}")

    async def undo_fix_my_game(self, entry_id: int, before: dict) -> str:
        changes = {k: v for k, v in before.items() if k in ("launch_args", "compat", "proton_version")}
        self.library.update(entry_id, **changes)
        if before.get("executable") and before["executable"] != self.library.get(entry_id).executable:
            self.library.set_executable(entry_id, before["executable"])
        return "The game's previous launch setup is back (file repairs are kept)."

    FIX_LABELS = {"repair_files": "repairing the game's files", "apply_archive": "applying the fix archive",
                  "winetricks": "installing a component", "enable_fix": "switching on a fix",
                  "set_executable": "choosing another executable", "add_args": "adding launch options",
                  "launch_args": "fixing the launch options", "proton": "switching Proton",
                  "restore_setup": "going back to the setup that worked", "reset_prefix": "rebuilding the prefix"}

    async def apply_fix(self, entry_id: int, kind: str, value: str) -> str:
        """Apply a fix from the debugging assistant; the next run of the game then tells whether it helped."""
        if kind in ("winetricks", "separate_prefix", "reset_prefix", "copy_files", "copy_folder"):
            # these change the prefix (where the saves are): zip the saves first
            await asyncio.to_thread(self.library.backup_saves, entry_id, "before-fix", True)
        msg = await self._apply_fix(entry_id, kind, value)
        if kind not in ("restore_saves", "system", "open_url"):
            what = self.FIX_LABELS.get(kind, "the fix")
            if kind == "winetricks":
                what = f"installing {value}"
            self._expect_followup(entry_id, what, [f"{kind}:{value}"])
        return msg

    # ------------------------------------------------------------- leftover archives
    def _leftover_archives(self) -> list[dict]:
        """Archives of games that were extracted successfully but are still on disk (extracted before "delete the
        archive" was on, or by an older version) — only inside the app's own folders."""
        from ezgd.installation import volumes as vol
        out, seen = [], set()
        for e in self.library.repo.all():
            if e.status.value in ("extracting", "downloaded"):
                continue                         # not extracted (or extracting now): the archive is still needed
            for r in self.extraction_repo.for_library(e.id):
                if r.get("status") != "completed":
                    continue
                a = Path(r["archive_path"])
                if a in seen or not a.is_file() or self.post.app_folder_of(a) is None:
                    continue
                files = [a]
                v = vol.volume_of(a)
                if v is not None:
                    files += [p for p in a.parent.iterdir() if p != a and (pv := vol.volume_of(p)) is not None
                              and pv.set_key == v.set_key]
                seen.update(files)
                out.append({"game": e.title, "archive": str(a), "files": [str(f) for f in files],
                            "size": sum(f.stat().st_size for f in files if f.is_file())})
            # a torrent's "Fix Repair" next to a game archive that was extracted: not needed (the game has the fix)
            dp = Path(e.download_path) if e.download_path else None
            done = any(r.get("status") == "completed" for r in self.extraction_repo.for_library(e.id))
            if done and dp is not None and dp.is_dir() and self.post.app_folder_of(dp / "x") is not None:
                from ezgd.installation import archives as ar
                from ezgd.installation.pipeline import _FIX_ONLY
                for a in ar.find_archives(dp, 2):
                    if a not in seen and _FIX_ONLY.search(" ".join(a.parts[-2:])):
                        seen.add(a)
                        out.append({"game": e.title, "archive": str(a), "files": [str(a)],
                                    "size": a.stat().st_size, "fix": True})
        return out

    async def leftover_archives(self) -> dict:
        items = await asyncio.to_thread(self._leftover_archives)
        return {"items": items, "size": sum(i["size"] for i in items)}

    async def delete_leftover_archives(self) -> dict:
        """Delete the archives :meth:`leftover_archives` lists (the user confirmed); returns what was freed."""
        items = await asyncio.to_thread(self._leftover_archives)
        freed, n = 0, 0
        for it in items:
            a = Path(it["archive"])
            root = self.post.app_folder_of(a)
            if root is None:
                continue
            size = it["size"]
            deleted = await asyncio.to_thread(self.post._delete_archive, a, root)
            if deleted:
                freed += size
                n += len(deleted)
        log.info("deleted %d leftover archive file(s), %d bytes", n, freed)
        return {"files": n, "freed": freed}

    # ------------------------------------------------------------- follow-up
    def _followup_path(self) -> Path:
        return self.paths.data / "fix_followup.json"

    def _followups(self) -> dict:
        try:
            d = json.loads(self._followup_path().read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _expect_followup(self, entry_id: int, what: str, keys: list[str] | None = None) -> None:
        d = self._followups()
        prev = d.get(str(entry_id), {})
        whats = [w for w in prev.get("what", []) if w != what][-4:] + [what]
        all_keys = [k for k in prev.get("keys", []) if k not in (keys or [])][-8:] + list(keys or [])
        d[str(entry_id)] = {"what": whats, "keys": all_keys, "at": time.time()}
        try:
            self._followup_path().write_text(json.dumps(d), "utf-8")
        except OSError:
            pass

    def _game_exited(self, entry_id: int, code: int | None, played: int) -> None:
        """After a fix: did the next start work? Tell the user and count it (no need to answer "did it work?")."""
        d = self._followups()
        p = d.pop(str(entry_id), None)
        if not p or time.time() - p.get("at", 0) > 14 * 86400:
            if played < 60 and code not in (None, 0, -15, 143):
                self._explain_soon(entry_id, played, code)        # a failed start: say why right away
            return
        try:
            self._followup_path().write_text(json.dumps(d), "utf-8")
        except OSError:
            pass
        try:
            e = self.library.get(entry_id)
        except Exception:
            return
        what = " + ".join(p.get("what") or ["the fix"])
        keys = p.get("keys") or []
        if played >= 120 or (code in (0, None) and played >= 60):
            self._record_outcome(e.title, True, p.get("what") or [], keys, e)
            self.emit("notify", ("The fix worked", f"{e.title} ran for {max(1, played // 60)} min after {what}. "
                                                   "The assistant remembers what helped."))
        elif played < 60 and code not in (-15, 143):
            self._record_outcome(e.title, False, p.get("what") or [], keys, e)
            self.emit("notify", ("Still not working", f"{e.title} stopped again after {played}s (after {what}). "
                                                      "Open Diagnose: it has analysed this run and has the next "
                                                      "suggestion — or press “Try fixes for me”."))

    def _explain_soon(self, entry_id: int, played: int, code) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._explain_failure(entry_id, played, code))

    async def _explain_failure(self, entry_id: int, played: int, code) -> None:
        """A game stopped within a minute: analyse that run now and put the likely cause into the notification."""
        await asyncio.sleep(1.5)                                   # the exit line is written first
        try:
            e = self.library.get(entry_id)
            d = await self.diagnose_game(entry_id)
        except Exception as exc:
            log.info("explaining the failed start of %s failed: %s", entry_id, exc)
            return
        top = next((f for f in d["findings"] if f["confidence"] >= 0.5 and not f["key"].startswith("exit:")), None)
        why = (f"Likely cause: {top['title']}." + (f" Fix: “{top['fix']['label']}” in Diagnose." if top.get("fix")
                                                   else "")) if top else \
            "No clear cause in the log — Diagnose can investigate it automatically."
        self.emit("notify", (f"{e.title} stopped after {played}s", f"Exit code {code}. {why}",
                             {"diagnose": entry_id}))

    # ------------------------------------------------------------- before the first start
    PREFLIGHT_KEYS = ("predicted-dll", "predicted-file", "redist", "steam-ref-runtime", "onlinefix-missing",
                      "override-missing", "bitness", "fix-archive:", "crack-folder:", "emulator", "integrity",
                      "advisor:")

    async def preflight(self, entry_id: int) -> list[dict]:
        """Problems that can be seen without starting the game (missing runtimes, a fix not in the folder, damaged
        files …), for the Library's "before you play" hint. Cached per game until its files or prefix change."""
        e = self.library.get(entry_id)
        if not e.executable or not Path(e.executable).is_file():
            return []
        from ezgd.library.diagnose import installed_verbs
        pfx = self.library.prefix_dir(entry_id)
        try:
            key = (e.executable, Path(e.executable).stat().st_mtime, len(installed_verbs(pfx)),
                   e.compat_config.dll_overrides, tuple(e.compat_config.fixes))
        except OSError:
            return []
        cache = self.__dict__.setdefault("_preflight_cache", {})
        if cache.get(entry_id, (None,))[0] == key:
            return cache[entry_id][1]
        try:
            d = await self.diagnose_game(entry_id)
        except Exception as exc:
            log.info("pre-flight check of %s failed: %s", e.title, exc)
            return []
        out = [f for f in d["findings"] if f.get("fix") and f["key"].startswith(self.PREFLIGHT_KEYS)
               and f["confidence"] >= (0.45 if f["key"].startswith(("redist", "steam-ref")) else 0.6)]
        cache[entry_id] = (key, out[:3])
        return out[:3]

    def _record_outcome(self, title: str, worked: bool, actions: list[str], keys: list[str] | None = None,
                        entry: LibraryEntry | None = None) -> None:
        p = self._outcomes_path()
        try:
            recs = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            recs = []
        rec = {"game": title, "worked": worked, "actions": actions, "at": time.time(), "auto": True}
        if keys:
            rec["keys"] = list(keys)
        if entry is not None:
            rec.update(self._outcome_context(entry))
        recs.append(rec)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(recs[-2000:]), "utf-8")
        except OSError:
            pass

    async def _apply_fix(self, entry_id: int, kind: str, value: str) -> str:
        """Apply a fix from the debugging assistant (file repairs and downloads here, the rest in the library)."""
        import json as _json
        from ezgd.installation import integrity
        e = self.library.get(entry_id)
        log.info("applying fix %s for %s", kind, e.title)
        if kind == "repair_files":
            rep = await self.integrity_report(entry_id)
            if rep is None or not rep.issues:
                return "No damaged files were found any more."
            pw = await self.entry_password(entry_id)
            r = await asyncio.to_thread(integrity.repair, rep, pw, self.settings.settings.unrar_tool or None)
            if r["failed"]:
                raise SourceError("Some files could not be restored: " + "; ".join(r["failed"]))
            return f"Restored {r['repaired']} file(s) from the archive." + (
                f" {r['no_archive']} could not be restored (their archive is gone) — download the game again."
                if r["no_archive"] else "")
        if kind == "apply_archive":
            spec = _json.loads(value)
            archive, dest = Path(spec["archive"]), Path(spec["dest"])
            pw = await self.entry_password(entry_id)
            bdir = await asyncio.to_thread(integrity.backup_overwritten, archive, dest)
            from ezgd.installation import archives as ar
            res = await asyncio.to_thread(ar.extract, archive, dest, password=pw,
                                          rar_tool=self.settings.settings.unrar_tool or None)
            ext_id = self.extraction_repo.create(None, entry_id, str(archive), str(dest))
            self.extraction_repo.update(ext_id, status="completed", progress=1.0, files_count=res.files)
            self.library._auto_onlinefix_overrides(self.library.get(entry_id))   # a fix's proxy DLLs need it
            self.emit("library_changed", entry_id)
            gone = ""
            if self.settings.settings.delete_archive_after_extract:
                root = self.post.app_folder_of(archive)
                if root is not None and await asyncio.to_thread(self.post._delete_archive, archive, root):
                    gone = " The archive was deleted (Settings: delete the archive after extracting)."
            return f"Applied {archive.name} ({res.files} files)" + (
                f"; the files it replaced are kept in {bdir.name}" if bdir else "") + "." + gone
        if kind == "download_fix":
            if not e.game_id:
                raise SourceError("This game is not linked to its page, so the fix cannot be downloaded.")
            recs = self._extraction_records(entry_id)
            dest = recs[0]["dest"] if recs else (e.install_path or "")
            prep = await self.prepare_download(e.game_id, int(value))
            snap = await self.start_prepared(prep["token"], download_folder(self.settings.settings, e.title),
                                             auto_extract=True, extract_dest=dest)
            self.emit("download_added", snap)
            return "The Fix Repair is downloading; it is extracted over the game when it finishes (backups are kept)."
        return await asyncio.to_thread(self.library.apply_diagnosis_fix, entry_id, kind, value)

    # ------------------------------------------------------------- resume interrupted installs
    async def _resume_installs(self) -> None:
        """Extractions the app was closed during: finish them now (the archive is still there; the staging folder of
        the interrupted run was removed, so it simply starts over)."""
        await asyncio.sleep(3)
        todo, self.post.resumable = list(self.post.resumable), []
        for r in todo:
            try:
                e = self.library.get(r["entry_id"])
            except Exception:
                continue
            if e.status is LibraryStatus.EXTRACTING:
                continue
            log.info("finishing the interrupted install of %s from %s", e.title, r["archive"])
            self.emit("notify", ("Finishing the install", f"{e.title}: the app was closed while it was being "
                                                          "installed — continuing now."))
            try:
                await self.extract_entry(e.id, r["archive"], r["dest"])
            except Exception as exc:
                log.warning("resuming the install of %s failed: %s", e.title, exc)

    async def interrupted_install(self, entry_id: int) -> dict | None:
        return await asyncio.to_thread(self.post.interrupted_install, entry_id)

    async def finish_install(self, entry_id: int) -> bool:
        info = await asyncio.to_thread(self.post.interrupted_install, entry_id)
        if info is None:
            raise SourceError("There is no interrupted install to finish for this game (or its archive is gone).")
        return await self.extract_entry(entry_id, info["archive"], info["dest"])

    # ------------------------------------------------------------- game updates
    GAME_UPDATES_EVERY = 6 * 3600

    def _ensure_release(self, e: LibraryEntry) -> dict:
        """Games installed before update checks existed: what is installed, from their archives' names."""
        from ezgd.library import updates
        if (e.release or {}).get("version"):
            return e.release
        names = [Path(r["archive"]).name for r in self._extraction_records(e.id)]
        g = self.games.get(e.game_id) if e.game_id else None
        rel = updates.installed_release(names, g.version if g else None)
        if rel:
            rel["assumed"] = rel.get("from") == "page"
            e.release = {**(e.release or {}), **rel}
            self.library.repo.update(e)
        return e.release or {}

    async def check_game_updates(self, entry_id: int | None = None, manual: bool = False) -> list[dict]:
        """Re-read installed games' pages and compare versions. ``manual``: the user asked (every source is read;
        in the background only sources that allow it)."""
        from ezgd.library import updates
        entries = [self.library.get(entry_id)] if entry_id is not None else await asyncio.to_thread(self.library.all)
        out = []
        to_update: list[tuple[int, Game]] = []
        for e in entries:
            if not e.game_id or e.status not in (LibraryStatus.INSTALLED, LibraryStatus.EXTRACTED):
                continue
            g = self.games.get(e.game_id)
            adapter = self.registry.adapters.get(g.source) if g else None
            if g is None or adapter is None or not self.registry.enabled.get(adapter.id, True):
                continue
            if not manual and not adapter.info.background_ok:
                continue
            rel = self._ensure_release(e)
            if not rel.get("version"):
                continue
            try:
                fresh = await self.registry.details(g)
            except Exception as exc:
                log.info("update check of %s failed: %s", e.title, exc)
                out.append({"entry_id": e.id, "title": e.title, "error": str(exc)})
                continue
            latest = updates.parse_version(fresh.version) or fresh.version
            new = updates.checked(rel, latest, fresh.updated_at)
            if rel.get("assumed") and not rel.get("checked_at"):
                # the installed version was only known from the page: this first look is the baseline
                new["version"] = new.get("latest") or new.get("version")
                new["status"] = updates.CURRENT
            e = self.library.get(e.id)
            auto = (new["status"] == updates.UPDATE and self.settings.settings.auto_update_games
                    and not (e.release or {}).get("updating") and (e.release or {}).get("auto_tried") != new.get("latest"))
            if auto:
                new["auto_tried"] = new.get("latest")
                new["notified"] = new.get("latest")         # (the automatic update says so itself)
                to_update.append((e.id, fresh))
            elif (e.release or {}).get("auto_tried"):
                new["auto_tried"] = e.release["auto_tried"]
            if new["status"] == updates.UPDATE and new.get("notified") != new.get("latest"):
                new["notified"] = new.get("latest")
                self.emit("notify", ("Update available", f"{e.title}: version {new['latest']} is out on "
                                                         f"{adapter.info.name} (you have {new['version']}). Open the "
                                                         "game in your Library to update — your saves are kept.",
                                     {"library": e.id}))
            if (e.release or {}).get("updating"):
                new["updating"] = True
                new["update_started_at"] = e.release.get("update_started_at")
            e.release = new
            self.library.repo.update(e)
            self.emit("library_changed", e.id)
            out.append({"entry_id": e.id, "title": e.title, "status": new["status"],
                        "installed": new.get("version"), "latest": new.get("latest"),
                        "text": updates.describe(new)})
        for eid, fresh in to_update:
            try:
                await self.auto_update_game(eid, fresh)
            except Exception as exc:                   # one game failing must not stop the others
                log.warning("automatic update of entry %s failed: %s", eid, exc)
        return out

    async def auto_update_game(self, entry_id: int, game: Game) -> dict | None:
        """Automatic game updates (Settings → Library): download the new version from the game's page and install
        it over the old one — the same path as pressing Update (saves backed up first). Only in-app downloads
        (torrent preferred, it resumes); a running game, browser-only links or multi-part sets are left to the user
        (they get the usual "update available" message)."""
        from ezgd.catalog.models import MethodKind
        from ezgd.installation.volumes import volume_info
        e = self.library.get(entry_id)
        latest = (e.release or {}).get("latest") or game.version or "a new version"

        def tell(why: str) -> None:
            self.emit("notify", ("Update available", f"{e.title}: version {latest} is out — {why} Open the game in "
                                                     "your Library to update it.", {"library": e.id}))
        if self.library.is_running(entry_id):
            tell("it was not updated automatically because it is running.")
            return None
        methods = list(enumerate(game.download_methods or []))
        usable = [(i, m) for i, m in methods if m.supported and m.kind in (MethodKind.TORRENT, MethodKind.DIRECT)
                  and volume_info(m.file_name or m.label.split("—")[-1].strip()) is None]
        usable.sort(key=lambda im: 0 if im[1].kind == MethodKind.TORRENT else 1)
        if not usable:
            tell("its page only offers downloads that need a browser (or come in parts).")
            return None
        info = await self.begin_game_update(entry_id)
        last_error = None
        for idx, _m in usable:
            try:
                prep = await self.prepare_download(game.id, idx)
            except Exception as exc:                    # this mirror did not resolve: the next one
                last_error = exc
                continue
            s = self.settings.settings
            game_dir = Path(s.install_dir or Path.home() / "Games") / safe_dirname(game.title)
            dest = game_dir if s.downloads_in_game_folder else Path(s.download_dir or Path.home() / "Downloads")
            snap = await self.start_prepared(prep["token"], str(dest), auto_extract=True)
            log.info("automatic update of %s to %s started (download %s)", e.title, latest, snap.get("id"))
            self.emit("notify", ("Updating " + e.title, f"Version {latest} is downloading by itself and will be "
                                 "installed over your game — your saves are backed up first.", {"library": e.id}))
            return {**info, "download": snap}
        await self.cancel_game_update(entry_id)
        tell(f"the automatic download did not start ({last_error}).")
        return None

    async def _game_updates_loop(self) -> None:
        await asyncio.sleep(180)
        while True:
            if self.settings.settings.check_game_updates:
                try:
                    await self.check_game_updates()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("game update check failed")
            await asyncio.sleep(self.GAME_UPDATES_EVERY)

    async def begin_game_update(self, entry_id: int) -> dict:
        """The user wants the new version: the next download (or import) of this game goes over the installed one."""
        e = self.library.get(entry_id)
        if not e.game_id:
            raise SourceError("This game is not linked to a game page, so the app cannot download its update.")
        if self.library.is_running(entry_id):
            raise SourceError("Quit the game before updating it.")
        rel = dict(e.release or {})
        rel["updating"] = True
        rel["update_started_at"] = time.time()
        e.release = rel
        self.library.repo.update(e)
        self.emit("library_changed", e.id)
        dest = await asyncio.to_thread(self.post.update_dest, e)
        return {"entry_id": e.id, "game_id": e.game_id, "title": e.title, "dest": str(dest) if dest else None,
                "installed": rel.get("version"), "latest": rel.get("latest")}

    async def cancel_game_update(self, entry_id: int) -> None:
        e = self.library.get(entry_id)
        e.release = {k: v for k, v in (e.release or {}).items() if k not in ("updating", "update_started_at")}
        self.library.repo.update(e)
        self.emit("library_changed", e.id)

    # ------------------------------------------------------------- wishlist
    WISHLIST_EVERY = 6 * 3600
    WISHLIST_PER_ROUND = 40

    def _local_matches(self, it: dict) -> list[Game]:
        from ezgd.catalog import wishlist as wl
        found: dict[str, Game] = {}
        if it.get("steam_app_id"):
            for g in self.games.by_steam_app(int(it["steam_app_id"])):
                found[g.id] = g
        for g in self.games.by_dedup_key(it["dedup_key"]):
            if wl.same_game(it, g):
                found[g.id] = g
        for g in self.games.search_local(wl.search_text(it["title"]), None, limit=80):
            if wl.same_game(it, g):
                found[g.id] = g
        enabled = {a.id for a in self.registry.active()}
        return [g for g in found.values() if g.source in enabled and not (self.registry.hide_adult and g.adult)]

    def _library_has(self, it: dict, entries: list[LibraryEntry] | None = None) -> bool:
        from ezgd.catalog import wishlist as wl
        for e in entries if entries is not None else self.library.repo.all():
            if it.get("steam_app_id") and e.steam_app_id == it["steam_app_id"]:
                return True
            if e.dedup_key == it["dedup_key"] or wl.loose_key(e.title) == wl.loose_key(it["title"]):
                return True
        return False

    async def wishlist(self) -> list[dict]:
        from ezgd.catalog import wishlist as wl

        def build() -> list[dict]:
            out = []
            entries = self.library.repo.all()
            for it in self.wishlist_repo.all():
                local = wl.summarize(self._local_matches(it))
                lib = self._library_has(it, entries)
                # what the catalog knows now (also games found while browsing since the last check)
                fresh = {m["game_id"]: m for m in local}
                merged = [fresh.pop(m["game_id"], m) for m in it["matches"]] + list(fresh.values())
                out.append({**it, "matches": merged, "status": wl.status_of(merged, lib), "in_library": lib})
            return out
        return await asyncio.to_thread(build)

    async def wishlist_keys(self) -> dict:
        rows = self.wishlist_repo.all()
        return {"keys": sorted({r["dedup_key"] for r in rows}),
                "apps": sorted({r["steam_app_id"] for r in rows if r["steam_app_id"]})}

    async def steam_search(self, term: str) -> list[dict]:
        from ezgd.catalog import steamstore
        res = await steamstore.search(self.client, term)
        keys = await self.wishlist_keys()
        for r in res:
            r["wishlisted"] = r["appid"] in keys["apps"]
        return res

    async def wishlist_add(self, steam_app_id: int | None = None, title: str | None = None,
                           game_id: str | None = None) -> dict:
        """Add a game by Steam App ID (found with Steam's search) or from the catalog (``game_id``)."""
        from ezgd.catalog import steamstore
        cover = hero = release_text = None
        coming = False
        g = self.games.get(game_id) if game_id else None
        if g is not None:
            title = title or g.title
            steam_app_id = steam_app_id or g.steam_app_id
            cover = g.cover_url
        if steam_app_id:
            try:
                info = (await steamstore.items(self.client, [int(steam_app_id)])).get(int(steam_app_id))
            except Exception as exc:
                log.info("Steam info for %s failed: %s", steam_app_id, exc)
                info = None
            if info:
                title = title or info["name"]
                cover = info["capsule"] or info["header"] or cover
                hero = info["hero"]
                coming = info["coming_soon"]
                release_text = self._release_text(info)
        title = (title or "").strip()
        if not title:
            raise SourceError("Which game? Search for it first.")
        existing = self.wishlist_repo.find(int(steam_app_id) if steam_app_id else None, dedup_key(title))
        if existing is not None:
            return existing
        item_id = self.wishlist_repo.add(title, dedup_key(title), int(steam_app_id) if steam_app_id else None, cover,
                                         hero, release_text, coming)
        log.info("wishlist: added %s (Steam %s)", title, steam_app_id)
        await self.wishlist_check(item_id, network=False, notify=False)
        self.emit("wishlist_changed", item_id)
        task = asyncio.create_task(self._quiet(self.wishlist_check(item_id, notify=False)))
        self._import_tasks.add(task)
        task.add_done_callback(self._import_tasks.discard)
        return self.wishlist_repo.get(item_id)

    @staticmethod
    async def _quiet(coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("background task failed: %s", exc)

    @staticmethod
    def _release_text(info: dict) -> str | None:
        if info.get("coming_soon"):
            return info.get("release_text") or "Coming soon"
        if info.get("released"):
            t = time.gmtime(int(info["released"]))
            return f"Released {t.tm_mday} {time.strftime('%b %Y', t)}"
        return None

    async def wishlist_item_for(self, game_id: str) -> dict | None:
        """The wishlist entry of a catalog game (by Steam App ID or title), if it is on the wishlist."""
        from ezgd.catalog import wishlist as wl
        g = self.games.get(game_id)
        if g is None:
            return None
        for it in self.wishlist_repo.all():
            if wl.same_game(it, g):
                return it
        return None

    async def wishlist_toggle(self, game_id: str) -> bool:
        """Add a catalog game to the wishlist, or remove it. Returns whether it is on the wishlist now."""
        it = await self.wishlist_item_for(game_id)
        if it is not None:
            await self.wishlist_remove(it["id"])
            return False
        await self.wishlist_add(game_id=game_id)
        return True

    async def games_by_ids(self, ids: list[str]) -> list[Game]:
        return [g for g in (self.games.get(i) for i in ids) if g is not None]

    async def wishlist_remove(self, item_id: int) -> None:
        self.wishlist_repo.remove(item_id)
        self.emit("wishlist_changed", item_id)

    async def wishlist_check(self, item_id: int | None = None, manual: bool = False, network: bool = True,
                             notify: bool = True) -> list[dict]:
        """Look for wishlisted games: the local catalog always; the sources' search when ``network`` — in the
        background only sources that allow it, and not for games that are not released yet."""
        from ezgd.catalog import wishlist as wl
        items = [self.wishlist_repo.get(item_id)] if item_id is not None else self.wishlist_repo.all()
        items = [i for i in items if i]
        if item_id is None and not manual:
            items = sorted(items, key=lambda i: i.get("checked_at") or 0)[:self.WISHLIST_PER_ROUND]
        sources = {a.id for a in self.registry.active() if "search" in a.info.capabilities
                   and (manual or a.info.background_ok)}
        results = []
        for it in items:
            found = {g.id: g for g in self._local_matches(it)}
            if network and sources and (manual or not it.get("coming_soon")):
                try:
                    res = await self.registry.search(wl.search_text(it["title"]), 1, sources)
                    for g in res.games:
                        if wl.same_game(it, g):
                            found[g.id] = g
                except Exception as exc:
                    log.info("wishlist search for %s failed: %s", it["title"], exc)
            summary = wl.summarize(list(found.values()))
            lib = self._library_has(it)
            status = wl.status_of(summary, lib)
            before = {m["game_id"] for m in it["matches"] if not m["withdrawn"]}
            now_avail = [m for m in summary if not m["withdrawn"] and m["game_id"] not in before]
            values = {"matches": summary, "status": status}
            if network:
                values["checked_at"] = time.time()
            if notify and now_avail and status == wl.AVAILABLE:
                names = ", ".join(sorted({self.registry.adapters[m["source"]].info.name for m in now_avail
                                          if m["source"] in self.registry.adapters}))
                self.emit("notify", ("Wishlist: now available", f"{it['title']} is available on {names}.",
                                     {"wishlist": it["id"]}))
                values["notified_at"] = time.time()
            self.wishlist_repo.update(it["id"], **values)
            results.append({**it, **values, "in_library": lib})
        if results:
            self.emit("wishlist_changed", None)
        return results

    async def _refresh_wishlist_steam(self) -> None:
        """Release status of games not out yet (Steam's release date moves)."""
        from ezgd.catalog import steamstore
        rows = [r for r in self.wishlist_repo.all() if r["steam_app_id"] and (r["coming_soon"] or not r["cover_url"])]
        for i in range(0, len(rows), 40):
            chunk = rows[i:i + 40]
            info = await steamstore.items(self.client, [r["steam_app_id"] for r in chunk])
            for r in chunk:
                x = info.get(r["steam_app_id"])
                if x:
                    self.wishlist_repo.update(r["id"], coming_soon=x["coming_soon"], release_text=self._release_text(x),
                                              cover_url=r["cover_url"] or x["capsule"],
                                              hero_url=r["hero_url"] or x["hero"])

    async def _wishlist_loop(self) -> None:
        await asyncio.sleep(150)
        n = 0
        while True:
            if self.wishlist_repo.all():
                try:
                    if n % 4 == 0:
                        await self._refresh_wishlist_steam()
                    await self.wishlist_check()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("wishlist check failed")
            n += 1
            await asyncio.sleep(self.WISHLIST_EVERY)

    # ------------------------------------------------------------- Steam artwork (couch mode)
    def _json_cache(self, name: str) -> dict:
        """A small JSON cache file, read once and then kept in memory (see _save_json_cache)."""
        caches = self.__dict__.setdefault("_json_caches", {})
        if name not in caches:
            try:
                caches[name] = json.loads((self.paths.cache / name).read_text("utf-8"))
            except (OSError, ValueError):
                caches[name] = {}
        return caches[name]

    def _save_json_cache(self, name: str) -> None:
        tmp = self.paths.cache / f".{name}.tmp"
        try:
            tmp.write_text(json.dumps(self._json_cache(name)), "utf-8")
            os.replace(tmp, self.paths.cache / name)
        except OSError:
            pass

    async def steam_art(self, app_ids: list[int], need: str = "") -> dict[int, dict]:
        """Big artwork for library games (portrait capsule, hero) and controller support, cached on disk for a
        week. ``need="controller"`` also refreshes entries saved before controller support was recorded."""
        from ezgd.catalog import steamstore
        cache = self._json_cache("steam-art.json")
        want = list(dict.fromkeys(int(a) for a in app_ids if a))
        now = time.time()
        stale = [a for a in want if now - (cache.get(str(a)) or {}).get("at", 0) > 7 * 86400
                 or (need and need not in (cache.get(str(a)) or {}))]
        changed = False
        for k in range(0, len(stale), 50):
            chunk = stale[k:k + 50]
            try:
                info = await steamstore.items(self.client, chunk)
            except Exception as exc:
                log.info("Steam artwork lookup failed: %s", exc)
                break
            for a in chunk:
                cache[str(a)] = {**(info.get(a) or {}), "at": time.time()}
                if a not in info:
                    cache[str(a)].setdefault("controller", None)       # not a game on Steam (any more)
            changed = True
        if changed:
            self._save_json_cache("steam-art.json")
        return {a: cache.get(str(a)) or {} for a in want}

    # ------------------------------------------------------------- controller support (couch mode's Store)
    TITLE_MISS_DAYS = 14

    def _protondb_titles(self) -> dict[str, int | None]:
        """ProtonDB's ~34 000 game titles → Steam App ID (offline; None where a title is ambiguous)."""
        from ezgd.library import appid, protondb
        path = self._protondb_index()
        if not path.exists():
            return {}
        stamp = path.stat().st_mtime
        cached = self.__dict__.get("_pdb_titles")
        if cached and cached[0] == stamp:
            return cached[1]
        out: dict[str, int | None] = {}
        for app, g in (protondb._load(path).get("games") or {}).items():
            if g.get("t"):
                k = appid.key(g["t"])
                out[k] = int(app) if k not in out or out[k] == int(app) else None
        self._pdb_titles = (stamp, out)
        return out

    async def _steam_app_by_search(self, title: str) -> int | None:
        """Steam's store search for a title (and its variants: "… Online", edition names); exact matches only."""
        from urllib.parse import quote
        from ezgd.library import appid
        keys = appid.title_keys(title)
        for term in appid.search_terms(title):
            url = f"https://store.steampowered.com/api/storesearch/?term={quote(term)}&l=english&cc=US"
            res = await self.client.fetch(url, max_bytes=512 * 1024, headers={"Accept": "application/json"})
            if res.status == 429:
                raise RuntimeError("Steam asks to slow down (HTTP 429)")
            if res.status != 200:
                continue
            try:
                items = json.loads(res.content).get("items") or []
            except (ValueError, AttributeError):
                items = []
            exact = {i["id"] for i in items if isinstance(i, dict) and i.get("type") == "app"
                     and isinstance(i.get("id"), int) and appid.key(str(i.get("name", ""))) in keys}
            if len(exact) == 1:
                return exact.pop()
        return None

    async def controller_support(self, items: list[tuple[str, str, int | None]], searches: int = 6) -> dict:
        """Which games work with a controller, by Steam's store data: ``items`` are (key, title, Steam App ID or
        None). The App ID comes from the site, a cache, ProtonDB's title list (offline), or — for at most
        ``searches`` titles per call — Steam's store search. Returns {key: {"app", "controller", "pending"}}:
        controller is "full" / "partial" / "none", or None when the game is not on Steam; ``pending`` means "ask
        again" (its search did not fit into this call)."""
        from ezgd.library import appid
        titles = self._json_cache("steam-titles.json")
        pdb = await asyncio.to_thread(self._protondb_titles)
        now = time.time()
        apps: dict[str, int | None] = {}
        to_search: list[tuple[str, str]] = []
        learned = False
        for key, title, app in items:
            if app:
                apps[key] = int(app)
                continue
            hit = titles.get(key)
            if hit is not None and (hit[0] or now - hit[1] < self.TITLE_MISS_DAYS * 86400):
                apps[key] = hit[0] or None
                continue
            found = None
            for k in appid.title_keys(title):
                if pdb.get(k):
                    found = pdb[k]
                    break
            if found:
                apps[key] = found
                titles[key] = [found, now]
                learned = True
            else:
                to_search.append((key, title))
        pending: set[str] = set()
        throttled = now < self.__dict__.get("_steam_search_pause", 0.0)
        sem = asyncio.Semaphore(3)

        async def look(key: str, title: str) -> None:
            async with sem:
                try:
                    found = await self._steam_app_by_search(title)
                except Exception as exc:
                    log.info("Steam search for %r failed: %s", title, exc)
                    if "429" in str(exc):
                        self._steam_search_pause = time.time() + 60
                    pending.add(key)
                    return
                apps[key] = found
                titles[key] = [found or 0, time.time()]
        if throttled:
            pending.update(k for k, _t in to_search)
        else:
            pending.update(k for k, _t in to_search[searches:])
            await asyncio.gather(*(look(k, t) for k, t in to_search[:searches]))
        if learned or to_search:
            self._save_json_cache("steam-titles.json")
        info = await self.steam_art([a for a in apps.values() if a], need="controller")
        out = {}
        for key, _title, _app in items:
            a = apps.get(key)
            out[key] = {"app": a, "controller": (info.get(a) or {}).get("controller") if a else None,
                        "pending": key in pending}
        return out

    # ------------------------------------------------------------- storage
    async def storage_report(self) -> dict:
        return await asyncio.to_thread(self._storage_report)

    def _storage_report(self) -> dict:
        s = self.settings.settings
        games = []
        used_prefixes: set[str] = set()
        for e in self.library.all():
            inst = Path(e.install_path) if e.install_path else None
            files = dir_size(inst) if inst and inst.exists() else 0
            dl = 0
            if e.download_path:
                dp = Path(e.download_path)
                if dp.exists() and not (inst and is_within(dp, inst)):
                    dl = dir_size(dp)
            pfx = self.library.prefix_dir(e.id)
            used_prefixes.add(pfx.name)
            prefix = dir_size(pfx) if pfx.exists() else 0
            sf = self.library.save_folder(e)
            saves = dir_size(sf) if sf.exists() else 0
            games.append({"entry_id": e.id, "title": e.title, "files": files, "downloads": dl, "prefix": prefix,
                          "saves": saves, "total": files + dl + prefix, "install_path": e.install_path,
                          "source": e.source, "running": self.library.is_running(e.id)})
        games.sort(key=lambda g: -g["total"])
        old_prefixes = []
        if self.paths.prefixes.is_dir():
            for d in sorted(self.paths.prefixes.iterdir()):
                if not d.is_dir() or d.name in used_prefixes or d.name.startswith(("trial", ".")):
                    continue
                base = d.name.split(".", 1)[0]
                if not re.fullmatch(r"game-\d+", base):
                    continue
                why = ("an older copy, kept when the prefix was rebuilt or Proton changed" if base in used_prefixes
                       else "its game is no longer in the Library")
                old_prefixes.append({"path": str(d), "size": dir_size(d), "why": why})
        leftovers = self._leftovers()
        archives = self._leftover_archives()
        cache = self.http_cache.size_bytes() + sum(p.stat().st_size for p in self.paths.covers.glob("*") if p.is_file())
        disks, seen = [], set()
        for label_, p in (("Games", s.install_dir), ("Downloads", s.download_dir), ("App data", str(self.paths.data))):
            q = Path(p)
            while not q.exists() and q != q.parent:
                q = q.parent
            try:
                st = os.stat(q)
                du = shutil.disk_usage(q)
            except OSError:
                continue
            if st.st_dev in seen:
                continue
            seen.add(st.st_dev)
            disks.append({"label": label_, "path": str(q), "total": du.total, "used": du.used, "free": du.free})
        return {"games": games, "old_prefixes": old_prefixes,
                "leftovers": {"count": len(leftovers), "size": sum(x["size"] for x in leftovers)},
                "archives": {"count": len(archives), "size": sum(a.get("size", 0) for a in archives)},
                "cache": cache,
                "save_backups": dir_size(self.paths.save_backups) if self.paths.save_backups.exists() else 0,
                "db_backups": dir_size(self.paths.backups) if self.paths.backups.exists() else 0,
                "logs": dir_size(self.paths.logs) if self.paths.logs.exists() else 0, "disks": disks}

    async def delete_old_prefixes(self, paths: list[str]) -> dict:
        """Delete prefixes no game uses any more (only folders inside the app's prefixes folder)."""
        def run() -> dict:
            allowed = {r["path"]: r for r in self._storage_report()["old_prefixes"]}
            freed, deleted, failed = 0, [], []
            for p in paths:
                r = allowed.get(p)
                d = Path(p)
                if r is None or not is_within(d, self.paths.prefixes) or d.resolve() == self.paths.prefixes.resolve():
                    failed.append({"path": p, "why": "not an unused prefix"})
                    continue
                shutil.rmtree(d, ignore_errors=True)
                if d.exists():
                    failed.append({"path": p, "why": "could not be deleted completely"})
                else:
                    deleted.append(p)
                    freed += r["size"]
            log.info("deleted %d unused prefix(es), %d bytes", len(deleted), freed)
            return {"deleted": deleted, "failed": failed, "freed": freed}
        return await asyncio.to_thread(run)

    # ------------------------------------------------------------- learning from fix outcomes
    @staticmethod
    def _override_keys(overrides: dict) -> list[str]:
        keys = []
        c = overrides.get("compat") or {}
        keys += [f"option:{o}" for o in c.get("options", [])]
        keys += [f"enable_fix:{f}" for f in c.get("fixes", [])]
        if c.get("dll_overrides"):
            keys.append(f"set_overrides:{c['dll_overrides']}")
        if overrides.get("proton_version"):
            keys.append(f"proton:{overrides['proton_version']}")
        keys += [f"winetricks:{v}" for v in overrides.get("winetricks") or []]
        if overrides.get("executable"):
            keys.append("set_executable")
        if overrides.get("launch_args"):
            keys.append(f"launch_args:{overrides['launch_args']}")
        return keys

    def _outcome_context(self, e: LibraryEntry) -> dict:
        ctx = {"source": getattr(e, "source", None), "steam_app_id": getattr(e, "steam_app_id", None)}
        exe = getattr(e, "executable", None)
        if exe:
            try:
                from ezgd.library.diagnose import engine_of
                ctx["engine"] = engine_of(Path(exe))
            except Exception:
                pass
        return ctx

    def _apply_learning(self, e: LibraryEntry, findings: list[dict]) -> None:
        """What the app saw on this computer: a fix that did not help this game ranks lower; one that helped other
        games (the same engine or crack) ranks higher. Each adjusted finding says why."""
        try:
            recs = json.loads(self._outcomes_path().read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(recs, list) or not findings:
            return
        ctx = self._outcome_context(e)
        failed_here: set[str] = set()
        helped: dict[str, set[str]] = {}
        similar: dict[str, set[str]] = {}
        for r in recs:
            if not isinstance(r, dict):
                continue
            same = r.get("game") == e.title or bool(e.steam_app_id and r.get("steam_app_id") == e.steam_app_id)
            for k in r.get("keys") or []:
                if same and not r.get("worked"):
                    failed_here.add(k)
                elif same and r.get("worked"):
                    failed_here.discard(k)
                elif r.get("worked"):
                    helped.setdefault(k, set()).add(r.get("game", ""))
                    if (ctx.get("engine") and r.get("engine") == ctx["engine"]) or \
                            (ctx.get("source") and r.get("source") == ctx["source"]):
                        similar.setdefault(k, set()).add(r.get("game", ""))
        changed = False
        for f in findings:
            fx = f.get("fix")
            if not fx:
                continue
            k = f"{fx.get('kind')}:{fx.get('value')}"
            if k in failed_here:
                f["confidence"] = round(f["confidence"] * 0.5, 2)
                f["learned"] = "You tried this for this game before — it did not help."
                changed = True
            elif helped.get(k):
                n_sim = len(similar.get(k, ()))
                n = len(helped[k])
                f["confidence"] = round(min(0.97, f["confidence"] + 0.06 * min(3, n) + 0.04 * min(3, n_sim)), 2)
                like = " like this one" if n_sim else ""
                f["learned"] = f"This fixed {n} other game{'s' if n != 1 else ''}{like} on this computer."
                changed = True
        if changed:
            findings.sort(key=lambda f: -f["confidence"])

    # ------------------------------------------------------------- app updates (GitHub releases)
    APP_UPDATE_EVERY = 24 * 3600

    def _app_update_path(self) -> Path:
        return self.paths.data / "app-update.json"

    def _app_update_state(self) -> dict:
        try:
            d = json.loads(self._app_update_path().read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_app_update_state(self, d: dict) -> None:
        try:
            self._app_update_path().write_text(json.dumps(d), "utf-8")
        except OSError:
            pass

    async def app_update_status(self) -> dict:
        from ezgd import __version__
        from ezgd.core import selfupdate
        st = self._app_update_state()
        app = selfupdate.install_dir()
        staged = bool(app and selfupdate.staged_version(app) and st.get("staged_version"))
        return {**st, "current": __version__, "repo": self.settings.settings.update_repo or selfupdate.DEFAULT_REPO,
                "packaged": app is not None, "install_dir": str(app) if app else None, "staged": staged,
                "can_install": app is not None and os.name != "nt"}

    async def set_update_repo(self, text: str) -> str:
        from ezgd.core import selfupdate
        repo = selfupdate.parse_repo(text) if (text or "").strip() else ""
        if repo.lower() == selfupdate.DEFAULT_REPO.lower():
            repo = ""                                   # the official repository is the default; store nothing
        self.settings.set("update_repo", repo)
        return repo or selfupdate.DEFAULT_REPO

    async def check_app_update(self, manual: bool = True) -> dict:
        from ezgd.core import selfupdate
        info = await selfupdate.check(self.client, self.settings.settings.update_repo or selfupdate.DEFAULT_REPO)
        st = self._app_update_state()
        st.update(last_check=time.time(), latest=info)
        if info["newer"] and st.get("notified") != info["version"]:
            st["notified"] = info["version"]
            self.emit("notify", ("App update available", f"EZ Game Downloader {info['version']} is out (you have "
                                                         f"{info['current']}). Settings → App updates installs it.",
                                 {"app_update": True}))
        self._save_app_update_state(st)
        self.emit("app_update", info)
        return info

    async def _app_update_loop(self) -> None:
        await asyncio.sleep(60)
        try:
            from ezgd.core import selfupdate
            await asyncio.to_thread(selfupdate.cleanup_previous, selfupdate.install_dir())
        except Exception:
            log.exception("removing the previous version failed")
        while True:
            s = self.settings.settings
            if s.check_app_updates and \
                    time.time() - self._app_update_state().get("last_check", 0) > self.APP_UPDATE_EVERY - 60:
                try:
                    await self.check_app_update(manual=False)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.info("app update check failed: %s", exc)
            await asyncio.sleep(3600)

    async def download_app_update(self) -> dict:
        """Download, verify (SHA-256) and unpack the latest release next to the app; installing needs a restart."""
        from ezgd.core import selfupdate
        app = selfupdate.install_dir()
        if app is None:
            raise SourceError("This copy runs from source code — update it with git/pip, or use a packaged build.")
        info = await self.check_app_update()
        if not info["newer"]:
            raise SourceError(f"You already have the newest version ({info['current']}).")
        last = [0.0]

        def progress(got: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] > 0.3 or got == total:
                last[0] = now
                self.emit("app_update_progress", {"received": got, "total": total, "status": "downloading"})
        try:
            archive = await selfupdate.download(self.client, info, self.paths.updates, progress)
            self.emit("app_update_progress", {"received": 1, "total": 1, "status": "unpacking"})
            try:
                await asyncio.to_thread(selfupdate.stage, archive, app, info["version"])
            finally:
                archive.unlink(missing_ok=True)
        except selfupdate.UpdateError as exc:
            self.emit("app_update_progress", {"status": "failed", "error": str(exc)})
            raise SourceError(str(exc)) from exc
        st = self._app_update_state()
        st["staged_version"] = info["version"]
        self._save_app_update_state(st)
        self.emit("app_update_progress", {"received": 1, "total": 1, "status": "ready"})
        log.info("app update %s downloaded, verified and unpacked", info["version"])
        return info

    async def install_app_update(self, argv: list[str]) -> None:
        """Start the swap script; the UI quits the app right after this returns."""
        from ezgd.core import selfupdate
        app = selfupdate.install_dir()
        if app is None:
            raise SourceError("This copy runs from source code.")
        try:
            await asyncio.to_thread(selfupdate.schedule_swap, app, self.paths.data, self.paths.logs,
                                    selfupdate.relaunch_args(argv))
        except selfupdate.UpdateError as exc:
            raise SourceError(str(exc)) from exc
        st = self._app_update_state()
        st.pop("staged_version", None)
        self._save_app_update_state(st)

    # ------------------------------------------------------------ ad blocklist
    ADBLOCK_CHECK_EVERY = 24 * 3600          # ask GitHub at most once a day (conditional request)

    def _adblock_meta_path(self) -> Path:
        return self.paths.data / "adblock" / "meta.json"

    def _adblock_meta(self) -> dict:
        try:
            return json.loads(self._adblock_meta_path().read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_adblock_meta(self, meta: dict) -> None:
        p = self._adblock_meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta), "utf-8")
        os.replace(tmp, p)

    async def _adblock_loop(self) -> None:
        await asyncio.sleep(5)
        try:                                      # compile the filter lists off the GUI thread, before first use
            await asyncio.to_thread(filterlists.engine)
        except Exception:
            log.exception("filter lists could not be compiled")
        await asyncio.sleep(15)                   # not during start-up
        while True:
            s = self.settings.settings
            if s.browser_adblock and s.browser_adblock_auto_update and \
                    time.time() - self._adblock_meta().get("checked_at", 0) > self.ADBLOCK_CHECK_EVERY:
                try:
                    await self.update_adblock()
                except Exception as exc:          # never let this break the app
                    log.info("ad blocklist update failed: %s", exc)
            await asyncio.sleep(6 * 3600)

    async def update_adblock(self, force: bool = False) -> dict:
        """Fetch the newest StevenBlack list (conditional request), validate it and switch to it."""
        meta = self._adblock_meta()
        headers = {}
        if not force and meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        meta["checked_at"] = time.time()
        try:
            res = await self.client.fetch(adblock.SOURCE_URL, headers=headers, max_bytes=16 * 1024 * 1024)
            if res.status == 304:
                meta["last_error"] = None
                result = "up to date"
            elif res.status == 200:
                text = res.content.decode("utf-8", "replace")
                info = await asyncio.to_thread(adblock.install_update, text)
                meta.update(etag=res.headers.get("etag"), updated_at=time.time(), last_error=None,
                            date=info.get("date"), count=info.get("count"))
                result = "updated"
                log.info("ad blocklist updated: %s domains, list date %s", info.get("count"), info.get("date"))
            else:
                raise SourceError(f"GitHub answered HTTP {res.status}")
        except Exception as exc:
            meta["last_error"] = str(exc)
            self._save_adblock_meta(meta)
            raise SourceError(f"Could not update the ad blocklist: {exc}") from exc
        changed = await self._update_filter_lists(meta, force)
        if changed:
            result = "updated"
        self._save_adblock_meta(meta)
        return {**self.adblock_status(), "result": result}

    async def _update_filter_lists(self, meta: dict, force: bool = False) -> int:
        """EasyList & co.: one conditional request per list; the engine is rebuilt if any changed."""
        lists = meta.setdefault("lists", {})
        changed = 0
        for fl in filterlists.LISTS:
            m = lists.setdefault(fl.key, {})
            headers = {"If-None-Match": m["etag"]} if m.get("etag") and not force else {}
            try:
                res = await self.client.fetch(fl.url, headers=headers, max_bytes=filterlists.MAX_BYTES)
                if res.status == 304:
                    m["last_error"] = None
                    continue
                if res.status != 200:
                    raise SourceError(f"HTTP {res.status}")
                n = await asyncio.to_thread(filterlists.install_list, fl.key, res.content.decode("utf-8", "replace"))
                m.update(etag=res.headers.get("etag"), updated_at=time.time(), rules=n, last_error=None)
                changed += 1
            except Exception as exc:              # one list failing keeps its current copy
                m["last_error"] = str(exc)
                log.info("filter list %s not updated: %s", fl.name, exc)
        if changed:
            await asyncio.to_thread(filterlists.reload)
            log.info("filter lists updated: %d changed, %s rules in total", changed,
                     filterlists.info().get("rules"))
        return changed

    def adblock_status(self) -> dict:
        meta = self._adblock_meta()
        fl = filterlists.info()
        return {**adblock.info(), "checked_at": meta.get("checked_at"), "updated_at": meta.get("updated_at"),
                "last_error": meta.get("last_error"), "auto": self.settings.settings.browser_adblock_auto_update,
                "filter_rules": fl.get("rules", 0), "filter_lists": len(fl.get("lists", {})),
                "filters_available": bool(fl.get("available"))}

    async def get_adblock_status(self) -> dict:
        return await asyncio.to_thread(self.adblock_status)

    async def maintenance(self) -> None:
        """Start-up housekeeping: keep the cover cache within its configured size."""
        try:
            await asyncio.to_thread(self._prune_covers)
        except OSError as exc:
            log.warning("cover cache maintenance failed: %s", exc)
