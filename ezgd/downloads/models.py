"""Download task model shared by the queue, both engines, the DB and the UI."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DownloadKind(StrEnum):
    HTTP = "http"
    TORRENT = "torrent"


class DownloadStatus(StrEnum):
    QUEUED = "queued"
    METADATA = "metadata"        # torrent: waiting for metadata from peers (magnet)
    CHECKING = "checking"        # torrent: verifying existing pieces on disk
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    VERIFYING = "verifying"      # http: computing checksum of the finished file
    COMPLETED = "completed"      # transfer finished (NOT the same as extracted/installed)
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in (DownloadStatus.METADATA, DownloadStatus.CHECKING,
                        DownloadStatus.DOWNLOADING, DownloadStatus.VERIFYING)

    @property
    def is_finished(self) -> bool:
        return self in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED)


class Verification(StrEnum):
    NONE = "none"                # no trusted checksum available → "downloaded", not "verified"
    VERIFIED = "verified"        # matched a checksum from a trusted source API
    MISMATCH = "mismatch"
    PIECES = "pieces"            # torrent: every piece hash verified by libtorrent


@dataclass
class TorrentFile:
    index: int
    path: str
    size: int
    priority: int = 4            # libtorrent: 0 = skip, 1..7
    progress: float = 0.0


@dataclass
class DownloadTask:
    kind: DownloadKind
    title: str
    uri: str                     # http(s) URL, magnet URI, or path to a stored .torrent
    dest_dir: str
    status: DownloadStatus = DownloadStatus.QUEUED
    id: int | None = None
    game_id: str | None = None
    source: str | None = None
    source_url: str | None = None
    referer: str | None = None
    filename: str | None = None
    status_detail: str | None = None
    error: str | None = None
    priority: int = 0
    total_bytes: int | None = None
    downloaded_bytes: int = 0
    expected_checksum: str | None = None
    verification: Verification = Verification.NONE
    etag: str | None = None
    last_modified: str | None = None
    info_hash: str | None = None
    torrent_file: str | None = None
    resume_data: bytes | None = None
    selected_files: list[int] | None = None     # torrent file indices to download; None = all
    archive_password: str | None = None
    auto_extract: bool = True
    extract_dest: str | None = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    # ---- runtime only (not persisted)
    speed: float = 0.0
    upload_speed: float = 0.0
    eta: float | None = None
    peers: int = 0
    seeds: int = 0
    files: list[TorrentFile] = field(default_factory=list)
    hash_failures: int = 0

    PERSISTED = ("kind", "title", "game_id", "source", "source_url", "uri", "referer", "dest_dir",
                 "filename", "status", "status_detail", "error", "priority", "total_bytes",
                 "downloaded_bytes", "expected_checksum", "verification", "etag", "last_modified",
                 "info_hash", "torrent_file", "resume_data", "selected_files", "archive_password",
                 "auto_extract", "extract_dest", "attempts", "created_at", "updated_at",
                 "started_at", "completed_at")

    @property
    def progress(self) -> float:
        if not self.total_bytes:
            return 1.0 if self.status is DownloadStatus.COMPLETED else 0.0
        return max(0.0, min(1.0, self.downloaded_bytes / self.total_bytes))

    def to_row(self) -> dict[str, Any]:
        row = {k: getattr(self, k) for k in self.PERSISTED}
        row["kind"] = str(self.kind)
        row["status"] = str(self.status)
        row["verification"] = str(self.verification)
        row["selected_files"] = None if self.selected_files is None else json.dumps(self.selected_files)
        row["auto_extract"] = int(bool(self.auto_extract))
        return row

    @classmethod
    def from_row(cls, row) -> "DownloadTask":
        r = dict(row)
        sel = r.get("selected_files")
        return cls(
            id=r["id"], kind=DownloadKind(r["kind"]), title=r["title"], uri=r["uri"], dest_dir=r["dest_dir"],
            status=DownloadStatus(r["status"]), game_id=r.get("game_id"), source=r.get("source"),
            source_url=r.get("source_url"), referer=r.get("referer"), filename=r.get("filename"),
            status_detail=r.get("status_detail"), error=r.get("error"), priority=r.get("priority") or 0,
            total_bytes=r.get("total_bytes"), downloaded_bytes=r.get("downloaded_bytes") or 0,
            expected_checksum=r.get("expected_checksum"),
            verification=Verification(r.get("verification") or "none"),
            etag=r.get("etag"), last_modified=r.get("last_modified"), info_hash=r.get("info_hash"),
            torrent_file=r.get("torrent_file"), resume_data=r.get("resume_data"),
            selected_files=None if sel is None else json.loads(sel),
            archive_password=r.get("archive_password"), auto_extract=bool(r.get("auto_extract", 1)),
            extract_dest=r.get("extract_dest"), attempts=r.get("attempts") or 0,
            created_at=r["created_at"], updated_at=r["updated_at"], started_at=r.get("started_at"),
            completed_at=r.get("completed_at"),
        )

    def snapshot(self) -> dict[str, Any]:
        """Plain dict for the UI thread (no shared mutable state)."""
        d = {k: getattr(self, k) for k in self.PERSISTED if k != "resume_data"}
        d.update(id=self.id, kind=str(self.kind), status=str(self.status),
                 verification=str(self.verification), progress=self.progress, speed=self.speed,
                 upload_speed=self.upload_speed, eta=self.eta, peers=self.peers, seeds=self.seeds,
                 hash_failures=self.hash_failures,
                 files=[f.__dict__.copy() for f in self.files])
        return d
