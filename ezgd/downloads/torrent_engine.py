"""Embedded BitTorrent engine built on libtorrent (no external client needed).

The engine owns one ``lt.session``. It is driven from the core asyncio loop:
the download manager calls :meth:`poll` about once a second, which drains
libtorrent alerts and refreshes every task's live statistics.

Piece hashes are verified by libtorrent as pieces arrive (failed pieces are
re-downloaded and counted in ``hash_failures``); :meth:`recheck` re-hashes
everything on disk on demand.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt

from ezgd import __version__
from ezgd.downloads.models import DownloadStatus, DownloadTask, TorrentFile, Verification

log = logging.getLogger(__name__)

MAX_TORRENT_BYTES = 16 * 1024 * 1024


class TorrentError(Exception):
    pass


@dataclass
class TorrentMeta:
    name: str
    info_hash: str
    total_size: int
    files: list[TorrentFile]
    num_pieces: int
    piece_length: int
    private: bool
    trackers: list[str]


def _best_hash(ih) -> str:
    try:
        return str(ih.get_best())
    except AttributeError:       # libtorrent < 2
        return str(ih)


def real_files(ti: lt.torrent_info) -> list[TorrentFile]:
    """User-visible files. BEP 47 pad files are hidden but original indices are kept."""
    fs = ti.files()
    out = []
    for i in range(fs.num_files()):
        if fs.file_flags(i) & lt.file_storage.flag_pad_file:
            continue
        out.append(TorrentFile(i, fs.file_path(i), fs.file_size(i)))
    return out


def inspect_torrent(data: bytes) -> TorrentMeta:
    """Validate and describe a .torrent file without adding it to the session."""
    if not data or len(data) > MAX_TORRENT_BYTES:
        raise TorrentError("The torrent file is empty or unreasonably large.")
    try:
        ti = lt.torrent_info(lt.bdecode(data))
    except Exception as exc:
        raise TorrentError(f"Not a valid torrent file: {exc}") from exc
    files = real_files(ti)
    for f in files:
        if os.path.isabs(f.path) or ".." in Path(f.path).parts:
            raise TorrentError(f"Torrent contains an unsafe file path: {f.path}")
    try:
        trackers = [t.url for t in ti.trackers()]
    except Exception:
        trackers = []
    return TorrentMeta(name=ti.name(), info_hash=_best_hash(ti.info_hashes()),
                       total_size=sum(f.size for f in files), files=files, num_pieces=ti.num_pieces(),
                       piece_length=ti.piece_length(), private=ti.priv(), trackers=trackers)


def magnet_info_hash(uri: str) -> str:
    try:
        atp = lt.parse_magnet_uri(uri)
    except Exception as exc:
        raise TorrentError(f"Invalid magnet link: {exc}") from exc
    return _best_hash(atp.info_hashes)


def build_settings(cfg: dict) -> dict:
    port = int(cfg.get("listen_port", 6881))
    iface = cfg.get("listen_interface") or "0.0.0.0"
    iface6 = cfg.get("listen_interface6", "[::]")
    listen = f"{iface}:{port}" + (f",{iface6}:{port}" if iface6 else "")
    s = {
        "user_agent": f"EZGameDownloader/{__version__} libtorrent/{lt.__version__}",
        "listen_interfaces": listen,
        "enable_dht": bool(cfg.get("enable_dht", True)),
        "enable_lsd": bool(cfg.get("enable_lsd", True)),
        "enable_upnp": bool(cfg.get("enable_upnp", True)),
        "enable_natpmp": bool(cfg.get("enable_upnp", True)),
        "connections_limit": int(cfg.get("max_connections", 200)),
        "download_rate_limit": int(cfg.get("download_limit_kbps", 0)) * 1024,
        "upload_rate_limit": int(cfg.get("upload_limit_kbps", 0)) * 1024,
        "alert_mask": int(lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage
                          | lt.alert_category.tracker),
        # we schedule torrents ourselves; libtorrent's auto-queue stays out of the way
        "active_downloads": -1, "active_seeds": -1, "active_limit": -1,
    }
    if cfg.get("allow_local_peers"):     # tests: several sessions on one machine
        s["allow_multiple_connections_per_ip"] = True
    return s


class TorrentEngine:
    def __init__(self, cfg: dict, torrents_dir: Path):
        self.torrents_dir = torrents_dir
        self.torrents_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = dict(cfg)
        self.session = lt.session(build_settings(self.cfg))
        self.handles: dict[int, lt.torrent_handle] = {}
        self.tasks: dict[int, DownloadTask] = {}
        self._by_hash: dict[str, int] = {}
        self._pending_resume: set[int] = set()
        self._stall_since: dict[int, float] = {}
        self.listen_error: str | None = None
        self.events: list[tuple[str, DownloadTask]] = []    # drained by the manager

    # ------------------------------------------------------------- settings
    def apply_settings(self, cfg: dict) -> None:
        self.cfg.update(cfg)
        self.session.apply_settings(build_settings(self.cfg))

    def set_rate_limits(self, down_kbps: int, up_kbps: int) -> None:
        self.apply_settings({"download_limit_kbps": down_kbps, "upload_limit_kbps": up_kbps})

    @property
    def listen_port(self) -> int:
        return self.session.listen_port()

    # ------------------------------------------------------------------ add
    def store_torrent_file(self, data: bytes, info_hash: str) -> Path:
        path = self.torrents_dir / f"{info_hash}.torrent"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return path

    def _params(self, task: DownloadTask) -> lt.add_torrent_params:
        atp = None
        if task.resume_data:
            try:
                atp = lt.read_resume_data(task.resume_data)
            except Exception as exc:
                log.warning("torrent %s: resume data unusable (%s); starting from the torrent file", task.id, exc)
                atp = None
        if atp is None:
            if task.torrent_file and Path(task.torrent_file).exists():
                atp = lt.add_torrent_params()
                atp.ti = lt.torrent_info(str(task.torrent_file))
            elif task.uri.startswith("magnet:"):
                atp = lt.parse_magnet_uri(task.uri)
            else:
                raise TorrentError("The torrent file for this download is missing.")
        atp.save_path = str(task.dest_dir)
        flags = atp.flags
        flags &= ~lt.torrent_flags.auto_managed
        flags &= ~lt.torrent_flags.paused
        flags |= lt.torrent_flags.duplicate_is_error
        atp.flags = flags
        if task.selected_files is not None and atp.ti is not None:
            n = atp.ti.num_files()
            sel = set(task.selected_files)
            atp.file_priorities = [4 if i in sel else 0 for i in range(n)]
        return atp

    def add(self, task: DownloadTask, paused: bool = False) -> None:
        assert task.id is not None
        if task.id in self.handles:
            if not paused:
                self.resume(task)
            return
        Path(task.dest_dir).mkdir(parents=True, exist_ok=True)
        atp = self._params(task)
        if paused:
            atp.flags |= lt.torrent_flags.paused
        try:
            handle = self.session.add_torrent(atp)
        except Exception as exc:
            raise TorrentError(f"libtorrent refused the torrent: {exc}") from exc
        ih = _best_hash(handle.info_hashes())
        self.handles[task.id] = handle
        self.tasks[task.id] = task
        self._by_hash[ih] = task.id
        task.info_hash = ih
        if handle.status().has_metadata:
            self._load_files(task, handle)
        task.status = DownloadStatus.PAUSED if paused else DownloadStatus.CHECKING
        task.started_at = task.started_at or time.time()

    def _load_files(self, task: DownloadTask, handle: lt.torrent_handle) -> None:
        ti = handle.torrent_file()
        if ti is None:
            return
        prios = handle.get_file_priorities()
        task.files = real_files(ti)
        for f in task.files:
            f.priority = prios[f.index] if f.index < len(prios) else 4
        if task.selected_files is not None:
            sel = set(task.selected_files)
            task.total_bytes = sum(f.size for f in task.files if f.index in sel)
        else:
            task.total_bytes = sum(f.size for f in task.files)
        if not task.filename:
            task.filename = ti.name()

    # ------------------------------------------------------------- control
    def pause(self, task: DownloadTask) -> None:
        h = self.handles.get(task.id)
        if h is None:
            return
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()
        self.request_resume_data(task)
        task.status = DownloadStatus.PAUSED
        task.speed = task.upload_speed = 0.0

    def resume(self, task: DownloadTask) -> None:
        h = self.handles.get(task.id)
        if h is None:
            self.add(task)
            return
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.resume()
        task.status = DownloadStatus.DOWNLOADING

    def remove(self, task: DownloadTask, delete_files: bool = False) -> None:
        h = self.handles.pop(task.id, None)
        self.tasks.pop(task.id, None)
        self._pending_resume.discard(task.id)
        if task.info_hash:
            self._by_hash.pop(task.info_hash, None)
        if h is not None and h.is_valid():
            opts = lt.session.delete_files if delete_files else 0
            self.session.remove_torrent(h, opts)

    def recheck(self, task: DownloadTask) -> None:
        h = self.handles.get(task.id)
        if h is None:
            raise TorrentError("The torrent is not loaded; resume it first.")
        h.force_recheck()
        task.status = DownloadStatus.CHECKING

    def set_file_selection(self, task: DownloadTask, selected: list[int] | None) -> None:
        task.selected_files = selected
        h = self.handles.get(task.id)
        if h is None or not h.status().has_metadata:
            return
        n = h.torrent_file().num_files()
        sel = set(selected) if selected is not None else set(range(n))
        h.prioritize_files([4 if i in sel else 0 for i in range(n)])
        self._load_files(task, h)

    def set_priority(self, task: DownloadTask, high: bool) -> None:
        h = self.handles.get(task.id)
        if h is None:
            return
        if high:
            h.queue_position_top()

    def request_resume_data(self, task: DownloadTask) -> None:
        h = self.handles.get(task.id)
        if h is not None and h.is_valid() and h.status().has_metadata:
            h.save_resume_data(lt.torrent_handle.save_info_dict)
            self._pending_resume.add(task.id)

    def save_all_resume_data(self, timeout: float = 5.0) -> None:
        for task in list(self.tasks.values()):
            self.request_resume_data(task)
        deadline = time.monotonic() + timeout
        while self._pending_resume and time.monotonic() < deadline:
            self.session.wait_for_alert(250)
            self._drain_alerts()

    # ----------------------------------------------------------------- poll
    def poll(self) -> list[tuple[str, DownloadTask]]:
        """Drain alerts and refresh task stats. Returns [(event, task)] where event ∈
        {"updated", "metadata", "completed", "failed", "resume_data"}; events raised by
        :meth:`flush_alerts` between polls are included."""
        self._drain_alerts()
        for task_id, h in list(self.handles.items()):
            task = self.tasks.get(task_id)
            if task is None or not h.is_valid():
                continue
            self._update_from_status(task, h.status())
            self.events.append(("updated", task))
        out, self.events = self.events, []
        return out

    def flush_alerts(self) -> None:
        """Process pending alerts now; resulting events are returned by the next poll()."""
        self._drain_alerts()

    def _task_for(self, alert) -> DownloadTask | None:
        handle = getattr(alert, "handle", None)
        if handle is None:
            return None
        try:
            ih = _best_hash(handle.info_hashes())
        except Exception:
            return None
        tid = self._by_hash.get(ih)
        return self.tasks.get(tid) if tid is not None else None

    def _drain_alerts(self) -> None:
        for a in self.session.pop_alerts():
            try:
                self._handle_alert(a)
            except Exception:
                log.exception("error while handling libtorrent alert %s", type(a).__name__)

    def _handle_alert(self, a) -> None:
        if isinstance(a, lt.listen_failed_alert):
            self.listen_error = a.message()
            log.warning("torrent listen failed: %s", a.message())
            return
        if isinstance(a, lt.listen_succeeded_alert):
            self.listen_error = None
            return
        task = self._task_for(a)
        if task is None:
            return
        if isinstance(a, lt.metadata_received_alert):
            h = self.handles[task.id]
            ti = h.torrent_file()
            data = lt.bencode(lt.create_torrent(ti).generate())
            task.torrent_file = str(self.store_torrent_file(data, task.info_hash or _best_hash(ti.info_hashes())))
            if task.selected_files is not None:
                n = ti.num_files()
                sel = set(task.selected_files)
                h.prioritize_files([4 if i in sel else 0 for i in range(n)])
            self._load_files(task, h)
            self.events.append(("metadata", task))
        elif isinstance(a, lt.save_resume_data_alert):
            task.resume_data = bytes(lt.write_resume_data_buf(a.params))
            self._pending_resume.discard(task.id)
            self.events.append(("resume_data", task))
        elif isinstance(a, lt.save_resume_data_failed_alert):
            self._pending_resume.discard(task.id)
        elif isinstance(a, lt.hash_failed_alert):
            task.hash_failures += 1
            log.info("torrent %s: piece %s failed its hash check and will be re-downloaded",
                     task.id, getattr(a, "piece_index", "?"))
        elif isinstance(a, lt.file_error_alert):
            task.status = DownloadStatus.FAILED
            task.error = f"Disk error on {getattr(a, 'filename', lambda: '?')()}: {a.message()}"
            self.events.append(("failed", task))
        elif isinstance(a, lt.torrent_error_alert):
            task.status = DownloadStatus.FAILED
            task.error = f"Torrent error: {a.message()}"
            self.events.append(("failed", task))
        elif isinstance(a, lt.fastresume_rejected_alert):
            log.info("torrent %s: resume data rejected (%s); rechecking files", task.id, a.message())
        elif isinstance(a, lt.tracker_error_alert):
            task.status_detail = f"Tracker: {a.message()}"[:200]
        elif isinstance(a, lt.torrent_finished_alert):
            self.request_resume_data(task)

    def _update_from_status(self, task: DownloadTask, st) -> None:
        if task.status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            return
        states = lt.torrent_status.states
        if st.errc and st.errc.value() != 0:
            task.status = DownloadStatus.FAILED
            task.error = f"Torrent error: {st.errc.message()}"
            self.events.append(("failed", task))
            return
        task.downloaded_bytes = st.total_wanted_done
        if st.has_metadata:
            task.total_bytes = st.total_wanted
        task.speed = float(st.download_payload_rate)
        task.upload_speed = float(st.upload_payload_rate)
        task.peers = st.num_peers
        task.seeds = st.num_seeds
        remaining = st.total_wanted - st.total_wanted_done
        task.eta = remaining / task.speed if task.speed > 0 and remaining > 0 else None
        paused = bool(st.flags & lt.torrent_flags.paused)
        if task.files and st.has_metadata:
            try:
                fp = self.handles[task.id].file_progress()
                for f in task.files:
                    if f.index < len(fp):
                        f.progress = fp[f.index] / f.size if f.size else 1.0
            except Exception:
                pass
        if st.is_finished and st.has_metadata:
            if task.status is not DownloadStatus.COMPLETED:
                task.status = DownloadStatus.COMPLETED
                task.verification = Verification.PIECES
                task.completed_at = time.time()
                task.status_detail = "Seeding" if st.is_seeding and not paused else None
                task.eta = None
                self.events.append(("completed", task))
            else:
                task.status_detail = "Seeding" if st.is_seeding and not paused else None
            return
        if paused:
            task.status = DownloadStatus.PAUSED
            task.speed = task.upload_speed = 0.0
            return
        if st.state == states.downloading_metadata:
            task.status = DownloadStatus.METADATA
            task.status_detail = f"Fetching metadata from peers ({st.num_peers} connected)"
        elif st.state in (states.checking_files, states.checking_resume_data, states.queued_for_checking):
            task.status = DownloadStatus.CHECKING
            task.status_detail = f"Verifying existing data ({st.progress * 100:.0f}%)"
        else:
            task.status = DownloadStatus.DOWNLOADING
            stall_after = 60 * int(self.cfg.get("stall_minutes", 10))
            if task.speed == 0 and st.num_peers == 0:
                since = self._stall_since.setdefault(task.id, time.monotonic())
                if time.monotonic() - since > stall_after:
                    task.status_detail = "Stalled: no peers are currently sharing this torrent"
                else:
                    task.status_detail = "Looking for peers…"
            else:
                self._stall_since.pop(task.id, None)
                if task.status_detail and (task.status_detail.startswith(("Stalled", "Looking", "Fetching",
                                                                           "Verifying"))):
                    task.status_detail = None

    def content_path(self, task: DownloadTask) -> Path | None:
        """Path of the downloaded content (single file or top-level folder)."""
        h = self.handles.get(task.id)
        ti = h.torrent_file() if h is not None and h.is_valid() else None
        if ti is None and task.torrent_file and Path(task.torrent_file).exists():
            ti = lt.torrent_info(task.torrent_file)
        if ti is None:
            return None
        files = real_files(ti)
        if len(files) == 1 and "/" not in files[0].path.replace("\\", "/"):
            return Path(task.dest_dir) / files[0].path
        return Path(task.dest_dir) / ti.name()

    def shutdown(self) -> None:
        try:
            self.session.pause()
            self.save_all_resume_data()
        finally:
            for h in list(self.handles.values()):
                try:
                    if h.is_valid():
                        self.session.remove_torrent(h)
                except Exception:
                    pass
            self.handles.clear()
