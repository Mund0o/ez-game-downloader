"""Download queue: scheduling, persistence, recovery and user commands.

Runs entirely on the core asyncio loop. Every state change is written to
SQLite so the queue survives crashes and restarts: interrupted transfers come
back as *queued* and resume (HTTP via Range, torrents via resume data).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from ezgd.core.fsutil import InsufficientSpaceError, ensure_free_space, format_bytes, is_within, safe_filename
from ezgd.database.repositories import DownloadRepository
from ezgd.downloads.http_engine import DownloadError, HttpEngine
from ezgd.downloads.models import DownloadKind, DownloadStatus, DownloadTask, TorrentFile
from ezgd.downloads.torrent_engine import TorrentEngine, TorrentError, inspect_torrent, magnet_info_hash
from ezgd.net.client import SafeHttpClient
from ezgd.net.urls import UnsafeURLError, validate_url

log = logging.getLogger(__name__)

Emit = Callable[[str, Any], None]
ACTIVE = {DownloadStatus.METADATA, DownloadStatus.CHECKING, DownloadStatus.DOWNLOADING, DownloadStatus.VERIFYING}


class DownloadCommandError(Exception):
    pass


class DuplicateDownloadError(DownloadCommandError):
    pass


class DownloadManager:
    def __init__(self, repo: DownloadRepository, settings, http: HttpEngine, torrents: TorrentEngine | None,
                 emit: Emit, *, client: SafeHttpClient | None = None,
                 on_completed: Callable[[DownloadTask, Path | None], Any] | None = None,
                 log_event: Callable[[str, str, str, str | None], None] | None = None):
        self.repo = repo
        self.settings = settings
        self.http = http
        self.torrents = torrents
        self.emit = emit
        self.client = client
        self.on_completed = on_completed
        self.log_event = log_event or (lambda *a: None)
        self.tasks: dict[int, DownloadTask] = {}
        self._jobs: dict[int, asyncio.Task] = {}
        self._post_jobs: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        self._last_emit: dict[int, float] = {}
        self._last_persist: dict[int, float] = {}
        self._last_resume_save = time.monotonic()

    # ------------------------------------------------------------ lifecycle
    @property
    def max_concurrent(self) -> int:
        return int(self.settings.settings.max_concurrent_downloads)

    async def start(self) -> None:
        for task in self.repo.all():
            self.tasks[task.id] = task
            self._recover(task)
        self._loop_task = asyncio.create_task(self._run(), name="download-scheduler")

    def _recover(self, task: DownloadTask) -> None:
        changed = False
        if task.status in ACTIVE:
            task.status = DownloadStatus.QUEUED
            task.status_detail = "Resuming after restart"
            changed = True
        if task.kind is DownloadKind.HTTP and not task.status.is_finished:
            part = self.http.part_path(task)
            size = part.stat().st_size if part and part.exists() else 0
            if size != task.downloaded_bytes:
                task.downloaded_bytes = size
                changed = True
        if task.kind is DownloadKind.TORRENT and task.torrent_file and Path(task.torrent_file).exists():
            try:
                meta = inspect_torrent(Path(task.torrent_file).read_bytes())
                task.files = meta.files
                for f in task.files:
                    if task.selected_files is not None and f.index not in task.selected_files:
                        f.priority = 0
            except Exception as exc:
                log.warning("stored torrent for task %s is unreadable: %s", task.id, exc)
        if task.kind is DownloadKind.TORRENT and task.status is DownloadStatus.PAUSED and self.torrents:
            try:
                self.torrents.add(task, paused=True)
            except TorrentError as exc:
                log.warning("could not reload paused torrent %s: %s", task.id, exc)
        if changed:
            self.repo.update(task)

    async def shutdown(self) -> None:
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for tid, job in list(self._jobs.items()):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        if self.torrents:
            await asyncio.to_thread(self.torrents.save_all_resume_data)
        for task in self.tasks.values():
            self.repo.update(task)
        if self.torrents:
            await asyncio.to_thread(self.torrents.shutdown)

    # ------------------------------------------------------------ scheduler
    async def _run(self) -> None:
        while not self._stopping:
            try:
                self._schedule()
                if self.torrents:
                    self._poll_torrents()
            except Exception:
                log.exception("download scheduler error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    def wake(self) -> None:
        self._wake.set()

    def _active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ACTIVE)

    def _schedule(self) -> None:
        free = self.max_concurrent - self._active_count()
        if free <= 0:
            return
        queued = sorted((t for t in self.tasks.values() if t.status is DownloadStatus.QUEUED),
                        key=lambda t: (-t.priority, t.id))
        for task in queued[:free]:
            self._start(task)

    def _start(self, task: DownloadTask) -> None:
        task.started_at = task.started_at or time.time()
        task.error = None
        if task.kind is DownloadKind.HTTP:
            task.status = DownloadStatus.DOWNLOADING
            task.status_detail = "Connecting…"
            self._jobs[task.id] = asyncio.create_task(self._run_http(task), name=f"http-{task.id}")
        else:
            if self.torrents is None:
                self._fail(task, "The torrent engine is not available.")
                return
            try:
                if task.id in self.torrents.handles:
                    self.torrents.resume(task)
                else:
                    self.torrents.add(task)
            except TorrentError as exc:
                self._fail(task, str(exc))
                return
        self._persist(task, force=True)
        self._publish(task, force=True)

    async def _run_http(self, task: DownloadTask) -> None:
        try:
            path = await self.http.run(task, lambda t: self._publish(t), lambda t: self._persist(t))
        except asyncio.CancelledError:
            task.speed = 0.0
            self._persist(task, force=True)
            self._publish(task, force=True)
            return
        except DownloadError as exc:
            self._fail(task, str(exc))
            return
        except Exception as exc:  # unexpected: keep the app alive, record it
            log.exception("download %s crashed", task.id)
            self._fail(task, f"Unexpected error: {type(exc).__name__}: {exc}")
            return
        finally:
            self._jobs.pop(task.id, None)
            self.wake()
        await self._complete(task, path)

    def _poll_torrents(self) -> None:
        assert self.torrents is not None
        events = self.torrents.poll()
        now = time.monotonic()
        for name, task in events:
            if name == "completed":
                if not self.settings.settings.torrent_seed_after_complete:
                    self.torrents.request_resume_data(task)
                self._spawn_post(self._complete(task, self.torrents.content_path(task)))
            elif name == "failed":
                self.torrents.pause(task)
                self._fail(task, task.error or "Torrent failed")
            elif name in ("metadata", "resume_data"):
                self._persist(task, force=True)
            self._publish(task, force=name != "updated")
            if name == "updated":
                self._persist(task)
        if now - self._last_resume_save > 60:
            self._last_resume_save = now
            for task in self.tasks.values():
                if task.kind is DownloadKind.TORRENT and task.status in ACTIVE:
                    self.torrents.request_resume_data(task)

    def _spawn_post(self, coro) -> None:
        job = asyncio.create_task(coro)
        self._post_jobs.add(job)
        job.add_done_callback(self._post_jobs.discard)

    async def _complete(self, task: DownloadTask, content: Path | None) -> None:
        task.status = DownloadStatus.COMPLETED
        task.completed_at = task.completed_at or time.time()
        task.speed = 0.0
        task.eta = None
        if task.kind is DownloadKind.HTTP:
            task.downloaded_bytes = task.total_bytes or task.downloaded_bytes
            content = Path(task.dest_dir) / task.filename if task.filename else None
        if task.kind is DownloadKind.TORRENT and self.torrents and not self.settings.settings.torrent_seed_after_complete:
            await asyncio.sleep(0.5)          # let the resume-data alert arrive
            self.torrents.flush_alerts()
            self.torrents.remove(task, delete_files=False)
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.emit("download_completed", task.snapshot())
        self.emit("notify", ("Download finished", task.title))
        self.log_event("info", "download", f"Completed: {task.title}", str(task.id))
        if self.on_completed:
            try:
                await self.on_completed(task, content)
            except Exception:
                log.exception("post-processing failed for download %s", task.id)
        self.wake()

    def _fail(self, task: DownloadTask, message: str) -> None:
        task.status = DownloadStatus.FAILED
        task.error = message
        task.status_detail = None
        task.speed = task.upload_speed = 0.0
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.log_event("error", "download", f"{task.title}: {message}", str(task.id))
        self.emit("notify", ("Download failed", f"{task.title}: {message}"))
        self.wake()

    def _persist(self, task: DownloadTask, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist.get(task.id, 0) < 5:
            return
        self._last_persist[task.id] = now
        if task.id in self.tasks:
            self.repo.update(task)

    def _publish(self, task: DownloadTask, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit.get(task.id, 0) < 0.4:
            return
        self._last_emit[task.id] = now
        self.emit("download_updated", task.snapshot())

    # ------------------------------------------------------------- commands
    def _get(self, task_id: int) -> DownloadTask:
        try:
            return self.tasks[task_id]
        except KeyError:
            raise DownloadCommandError("Download not found.") from None

    def _check_dest(self, dest_dir: str) -> Path:
        p = Path(dest_dir).expanduser()
        if not p.is_absolute():
            raise DownloadCommandError("Choose an absolute destination folder.")
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DownloadCommandError(f"Cannot use {p}: {exc.strerror or exc}") from exc
        if not os.access(p, os.W_OK):
            raise DownloadCommandError(f"The folder {p} is not writable.")
        return p

    def _register(self, task: DownloadTask) -> dict:
        self.repo.insert(task)
        self.tasks[task.id] = task
        self._publish(task, force=True)
        self.log_event("info", "download", f"Queued: {task.title}", str(task.id))
        self.wake()
        return task.snapshot()

    async def add_http(self, url: str, *, title: str, dest_dir: str, filename: str | None = None,
                       size: int | None = None, checksum: str | None = None, referer: str | None = None,
                       game_id: str | None = None, source: str | None = None, source_url: str | None = None,
                       archive_password: str | None = None, auto_extract: bool = True,
                       extract_dest: str | None = None, priority: int = 0) -> dict:
        try:
            validate_url(url, allow_private=getattr(self.client, "allow_private", False))
        except UnsafeURLError as exc:
            raise DownloadCommandError(f"Invalid download URL: {exc}") from exc
        for t in self.tasks.values():
            if t.uri == url and not t.status.is_finished:
                raise DuplicateDownloadError(f"'{t.title}' is already in the download list.")
        dest = self._check_dest(dest_dir)
        if size:
            try:
                ensure_free_space(dest, size)
            except InsufficientSpaceError as exc:
                raise DownloadCommandError(str(exc)) from exc
        task = DownloadTask(kind=DownloadKind.HTTP, title=title, uri=url, dest_dir=str(dest),
                            filename=safe_filename(filename) if filename else None, total_bytes=size,
                            expected_checksum=checksum, referer=referer, game_id=game_id, source=source,
                            source_url=source_url, archive_password=archive_password, auto_extract=auto_extract,
                            extract_dest=extract_dest, priority=priority)
        return self._register(task)

    async def add_torrent(self, *, title: str, dest_dir: str, torrent_bytes: bytes | None = None,
                          magnet: str | None = None, selected_files: list[int] | None = None,
                          game_id: str | None = None, source: str | None = None, source_url: str | None = None,
                          archive_password: str | None = None, auto_extract: bool = True,
                          extract_dest: str | None = None, priority: int = 0) -> dict:
        if self.torrents is None:
            raise DownloadCommandError("The torrent engine is not available.")
        if bool(torrent_bytes) == bool(magnet):
            raise DownloadCommandError("Provide either a .torrent file or a magnet link.")
        dest = self._check_dest(dest_dir)
        files: list[TorrentFile] = []
        torrent_path = None
        try:
            if torrent_bytes:
                meta = inspect_torrent(torrent_bytes)
                info_hash = meta.info_hash
                files = meta.files
                if selected_files is not None:
                    selected_files = sorted({i for i in selected_files if 0 <= i < len(files)})
                    if not selected_files:
                        raise DownloadCommandError("Select at least one file to download.")
                wanted = sum(f.size for f in files if selected_files is None or f.index in selected_files)
                ensure_free_space(dest, wanted)
                total = wanted
                title = title or meta.name
            else:
                assert magnet
                if not magnet.startswith("magnet:?"):
                    raise DownloadCommandError("Not a magnet link.")
                info_hash = magnet_info_hash(magnet)
                total = None
        except TorrentError as exc:
            raise DownloadCommandError(str(exc)) from exc
        except InsufficientSpaceError as exc:
            raise DownloadCommandError(str(exc)) from exc
        for t in self.tasks.values():
            if t.info_hash == info_hash and t.status is not DownloadStatus.CANCELLED:
                raise DuplicateDownloadError(f"This torrent is already in the download list ('{t.title}').")
        if torrent_bytes:
            torrent_path = str(self.torrents.store_torrent_file(torrent_bytes, info_hash))
        task = DownloadTask(kind=DownloadKind.TORRENT, title=title or info_hash, uri=magnet or torrent_path,
                            dest_dir=str(dest), info_hash=info_hash, torrent_file=torrent_path,
                            selected_files=selected_files, total_bytes=total, game_id=game_id, source=source,
                            source_url=source_url, archive_password=archive_password, auto_extract=auto_extract,
                            extract_dest=extract_dest, priority=priority)
        for f in files:
            if selected_files is not None and f.index not in selected_files:
                f.priority = 0
        task.files = files
        return self._register(task)

    async def add_from_text(self, text: str, *, dest_dir: str, title: str | None = None,
                            auto_extract: bool = True) -> dict:
        """User-entered link: magnet, URL of a .torrent file, or a direct file URL."""
        text = text.strip()
        if text.startswith("magnet:?"):
            return await self.add_torrent(title=title or "", magnet=text, dest_dir=dest_dir, auto_extract=auto_extract)
        try:
            validate_url(text, allow_private=getattr(self.client, "allow_private", False))
        except UnsafeURLError as exc:
            raise DownloadCommandError(f"Not a valid link: {exc}") from exc
        if text.lower().split("?")[0].endswith(".torrent") and self.client is not None:
            res = await self.client.fetch(text, max_bytes=16 * 1024 * 1024, polite=False)
            if res.status != 200:
                raise DownloadCommandError(f"Could not fetch the .torrent file (HTTP {res.status}).")
            return await self.add_torrent(title=title or "", torrent_bytes=res.content, dest_dir=dest_dir,
                                          auto_extract=auto_extract)
        name = title or text.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download"
        return await self.add_http(text, title=name, dest_dir=dest_dir, auto_extract=auto_extract)

    async def pause(self, task_id: int) -> dict:
        task = self._get(task_id)
        if task.status.is_finished:
            raise DownloadCommandError("This download has already finished.")
        if task.kind is DownloadKind.HTTP:
            task.status = DownloadStatus.PAUSED
            task.status_detail = None
            job = self._jobs.get(task_id)
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        else:
            if self.torrents and task_id in self.torrents.handles:
                self.torrents.pause(task)
            task.status = DownloadStatus.PAUSED
        task.speed = 0.0
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.wake()
        return task.snapshot()

    async def resume(self, task_id: int) -> dict:
        task = self._get(task_id)
        if task.status not in (DownloadStatus.PAUSED, DownloadStatus.QUEUED):
            raise DownloadCommandError("Only paused downloads can be resumed.")
        task.status = DownloadStatus.QUEUED
        task.status_detail = None
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.wake()
        return task.snapshot()

    async def retry(self, task_id: int) -> dict:
        task = self._get(task_id)
        if task.status not in (DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            raise DownloadCommandError("Only failed or cancelled downloads can be retried.")
        task.status = DownloadStatus.QUEUED
        task.error = None
        task.status_detail = None
        task.completed_at = None
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.wake()
        return task.snapshot()

    async def cancel(self, task_id: int, delete_partial: bool = True) -> dict:
        task = self._get(task_id)
        if task.status is DownloadStatus.COMPLETED:
            raise DownloadCommandError("Completed downloads cannot be cancelled; remove them instead.")
        job = self._jobs.get(task_id)
        task.status = DownloadStatus.CANCELLED
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        if task.kind is DownloadKind.HTTP and delete_partial:
            part = self.http.part_path(task)
            if part and part.exists() and is_within(part, task.dest_dir):
                part.unlink()
            task.downloaded_bytes = 0
        if task.kind is DownloadKind.TORRENT and self.torrents:
            self.torrents.remove(task, delete_files=delete_partial)
            task.resume_data = None
        task.speed = 0.0
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.wake()
        return task.snapshot()

    async def remove(self, task_id: int, delete_files: bool = False) -> list[str]:
        """Remove from the list. With ``delete_files`` the downloaded file/folder is deleted too —
        but only the exact content of this download inside its destination folder."""
        task = self._get(task_id)
        deleted: list[str] = []
        content: Path | None = None
        if task.kind is DownloadKind.TORRENT and self.torrents:
            content = self.torrents.content_path(task)
        elif task.filename:
            content = Path(task.dest_dir) / task.filename
        if not task.status.is_finished:
            await self.cancel(task_id, delete_partial=delete_files)
        if delete_files and content is not None and content.exists():
            dest = Path(task.dest_dir)
            if is_within(content, dest) and Path(os.path.realpath(content)) != Path(os.path.realpath(dest)):
                if content.is_dir():
                    shutil.rmtree(content)
                else:
                    content.unlink()
                deleted.append(str(content))
            else:
                raise DownloadCommandError("Refusing to delete files outside the download folder.")
        if task.kind is DownloadKind.TORRENT and self.torrents:
            self.torrents.remove(task)
            if task.torrent_file and Path(task.torrent_file).exists() and \
                    is_within(task.torrent_file, self.torrents.torrents_dir):
                others = [t for t in self.tasks.values() if t.id != task.id and t.torrent_file == task.torrent_file]
                if not others:
                    Path(task.torrent_file).unlink(missing_ok=True)
        self.tasks.pop(task_id, None)
        self.repo.delete(task_id)
        self.emit("download_removed", task_id)
        self.wake()
        return deleted

    async def set_priority(self, task_id: int, priority: int) -> dict:
        task = self._get(task_id)
        task.priority = max(-10, min(10, int(priority)))
        if self.torrents and task.kind is DownloadKind.TORRENT:
            self.torrents.set_priority(task, task.priority > 0)
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.wake()
        return task.snapshot()

    async def set_files(self, task_id: int, selected: list[int] | None) -> dict:
        task = self._get(task_id)
        if task.kind is not DownloadKind.TORRENT:
            raise DownloadCommandError("File selection only applies to torrents.")
        if selected is not None and not selected:
            raise DownloadCommandError("Select at least one file.")
        if self.torrents:
            self.torrents.set_file_selection(task, selected)
        task.selected_files = selected
        for f in task.files:
            f.priority = 4 if selected is None or f.index in selected else 0
        if task.files:
            task.total_bytes = sum(f.size for f in task.files if f.priority)
        if task.status is DownloadStatus.COMPLETED and task.total_bytes and task.downloaded_bytes < task.total_bytes:
            task.status = DownloadStatus.QUEUED
        self._persist(task, force=True)
        self._publish(task, force=True)
        self.wake()
        return task.snapshot()

    async def recheck(self, task_id: int) -> dict:
        task = self._get(task_id)
        if task.kind is not DownloadKind.TORRENT or self.torrents is None:
            raise DownloadCommandError("Only torrents can be re-checked.")
        if task_id not in self.torrents.handles:
            self.torrents.add(task, paused=False)
        try:
            self.torrents.recheck(task)
        except TorrentError as exc:
            raise DownloadCommandError(str(exc)) from exc
        task.status = DownloadStatus.CHECKING
        self._publish(task, force=True)
        return task.snapshot()

    async def change_destination(self, task_id: int, dest_dir: str) -> dict:
        task = self._get(task_id)
        if task.status in ACTIVE or task.downloaded_bytes > 0:
            raise DownloadCommandError("The destination can only be changed before any data was downloaded.")
        task.dest_dir = str(self._check_dest(dest_dir))
        self._persist(task, force=True)
        self._publish(task, force=True)
        return task.snapshot()

    # --------------------------------------------------------------- query
    def snapshot_all(self) -> list[dict]:
        return [t.snapshot() for t in sorted(self.tasks.values(), key=lambda t: t.id)]

    def summary(self) -> dict:
        active = [t for t in self.tasks.values() if t.status in ACTIVE]
        pending = active + [t for t in self.tasks.values() if t.status is DownloadStatus.QUEUED]
        total = sum(t.total_bytes or 0 for t in pending)
        done = sum(min(t.downloaded_bytes or 0, t.total_bytes or 0) for t in pending)
        speed = sum(t.speed for t in active)
        return {"active": len(active), "queued": len(pending) - len(active),
                "speed": speed, "upload": sum(t.upload_speed for t in self.tasks.values()),
                "speed_text": format_bytes(speed) + "/s",
                # all running and queued downloads together (the taskbar's progress, the tray's tooltip)
                "progress": done / total if total else None,
                "eta": (total - done) / speed if total and speed > 0 else None}

    def apply_settings(self, key: str, value: Any) -> None:
        s = self.settings.settings
        if key == "http_speed_limit_kbps":
            self.http.bucket.set_rate(int(value) * 1024)
        elif key == "http_max_retries":
            self.http.max_retries = int(value)
        elif key == "max_concurrent_downloads":
            self.wake()
        elif self.torrents and key.startswith("torrent_"):
            self.torrents.apply_settings({
                "listen_port": s.torrent_listen_port, "max_connections": s.torrent_max_connections,
                "download_limit_kbps": s.torrent_download_limit_kbps,
                "upload_limit_kbps": s.torrent_upload_limit_kbps, "enable_dht": s.torrent_enable_dht,
                "enable_lsd": s.torrent_enable_lsd, "enable_upnp": s.torrent_enable_upnp,
                "stall_minutes": s.torrent_stall_minutes})
