"""Streaming HTTP(S) download engine.

* streams to ``<name>.part`` in fixed-size chunks (never buffers whole files);
* resumes with ``Range`` + ``If-Range`` when the server supports it and
  restarts cleanly when it does not;
* retries transient failures with bounded exponential backoff + jitter;
* honours a shared :class:`TokenBucket` bandwidth limit;
* refuses HTML interstitial pages masquerading as files;
* checks free disk space, verifies trusted checksums, then atomically
  renames the finished file.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import random
import re
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import unquote, urlsplit

import httpx

from ezgd.core.fsutil import InsufficientSpaceError, ensure_free_space, safe_filename, unique_path
from ezgd.downloads.models import DownloadStatus, DownloadTask, Verification
from ezgd.net.client import SafeHttpClient
from ezgd.net.ratelimit import TokenBucket
from ezgd.net.urls import UnsafeURLError

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
PART_SUFFIX = ".part"


class DownloadError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def filename_from_headers(headers: httpx.Headers, url: str) -> str | None:
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", cd, re.I)
    if m:
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8")
        except LookupError:
            return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return m.group(1).strip()
    tail = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return tail or None


def parse_content_range(value: str | None) -> tuple[int, int | None] | None:
    """'bytes 100-199/1000' → (100, 1000)."""
    if not value:
        return None
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip(), re.I)
    if not m:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), total


class SpeedMeter:
    def __init__(self, window: float = 4.0):
        self.window = window
        self.samples: deque[tuple[float, int]] = deque()

    def add(self, n: int) -> None:
        now = time.monotonic()
        self.samples.append((now, n))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    @property
    def rate(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        span = max(self.samples[-1][0] - self.samples[0][0], 0.25)
        return sum(n for _, n in list(self.samples)[1:]) / span

    def reset(self) -> None:
        self.samples.clear()


def file_checksum(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class HttpEngine:
    def __init__(self, client: SafeHttpClient, bucket: TokenBucket | None = None, *, max_retries: int = 5,
                 backoff_base: float = 2.0, backoff_max: float = 60.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.client = client
        self.bucket = bucket or TokenBucket(0)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep

    @staticmethod
    def part_path(task: DownloadTask) -> Path | None:
        if not task.filename:
            return None
        return Path(task.dest_dir) / (task.filename + PART_SUFFIX)

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2 ** max(0, attempt - 1))) * (0.8 + 0.4 * random.random())

    async def run(self, task: DownloadTask, on_progress: Callable[[DownloadTask], None],
                  on_checkpoint: Callable[[DownloadTask], None]) -> Path:
        """Download ``task`` to completion and return the final path.

        Raises :class:`DownloadError` for permanent failures. Cancellation
        (pause/cancel) propagates as ``asyncio.CancelledError`` and leaves the
        ``.part`` file in place for resuming.
        """
        dest = Path(task.dest_dir)
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DownloadError(f"Cannot create destination folder {dest}: {exc.strerror or exc}") from exc
        attempt = 0
        meter = SpeedMeter()
        while True:
            progressed_at_start = task.downloaded_bytes
            try:
                await self._attempt(task, dest, meter, on_progress, on_checkpoint)
                break
            except DownloadError as exc:
                if not exc.retryable:
                    raise
                err: Exception = exc
            except (httpx.TransportError, httpx.StreamError) as exc:
                err = DownloadError(f"Connection problem: {type(exc).__name__} {exc}".strip(), retryable=True)
            if task.downloaded_bytes > progressed_at_start:
                attempt = 0                 # made progress → this is a fresh failure
            attempt += 1
            task.attempts += 1
            if attempt > self.max_retries:
                raise DownloadError(f"{err} (gave up after {self.max_retries} retries)")
            delay = self._backoff(attempt)
            task.status_detail = f"Retrying in {delay:.0f}s ({attempt}/{self.max_retries}): {err}"
            task.speed = 0.0
            on_progress(task)
            on_checkpoint(task)
            log.info("download %s: %s", task.id, task.status_detail)
            await self._sleep(delay)
            meter.reset()
        task.speed = 0.0
        task.status_detail = None
        return await self._finalize(task, on_progress, on_checkpoint)

    async def _attempt(self, task: DownloadTask, dest: Path, meter: SpeedMeter,
                       on_progress: Callable[[DownloadTask], None],
                       on_checkpoint: Callable[[DownloadTask], None]) -> None:
        part = self.part_path(task)
        existing = part.stat().st_size if part and part.exists() else 0
        if task.total_bytes and existing > task.total_bytes:
            part.unlink()
            existing = 0
        headers = {"Accept-Encoding": "identity"}
        if task.referer:
            headers["Referer"] = task.referer
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
            validator = task.etag if task.etag and not task.etag.startswith("W/") else task.last_modified
            if validator:
                headers["If-Range"] = validator
        try:
            async with self.client.stream(task.uri, headers=headers) as resp:
                status = resp.status_code
                if status == 416 and existing > 0:
                    cr = resp.headers.get("content-range", "")
                    m = re.search(r"/(\d+)", cr)
                    if m and int(m.group(1)) == existing:
                        task.total_bytes = existing
                        task.downloaded_bytes = existing
                        return
                    part.unlink(missing_ok=True)
                    task.downloaded_bytes = 0
                    raise DownloadError("Server rejected the resume position; restarting", retryable=True)
                self._check_status(status)
                ctype = resp.headers.get("content-type", "").lower()
                if ctype.startswith("text/html"):
                    raise DownloadError("The server returned a web page instead of a file. The host probably "
                                        "requires a browser (captcha, login or waiting page).")
                if task.filename is None:
                    task.filename = safe_filename(filename_from_headers(resp.headers, str(resp.url)) or task.title)
                    part = self.part_path(task)
                    existing = 0
                    on_checkpoint(task)
                assert part is not None
                length = resp.headers.get("content-length")
                length_i = int(length) if length and length.isdigit() else None
                mode = "ab"
                if status == 206:
                    cr = parse_content_range(resp.headers.get("content-range"))
                    if cr is None or cr[0] != existing:
                        part.unlink(missing_ok=True)
                        task.downloaded_bytes = 0
                        raise DownloadError("Server sent an unexpected byte range; restarting", retryable=True)
                    task.total_bytes = cr[1] if cr[1] is not None else (existing + length_i if length_i else None)
                else:  # 200: full body
                    if existing:
                        log.info("download %s: server ignored Range; restarting from zero", task.id)
                    mode, existing = "wb", 0
                    task.total_bytes = length_i
                new_etag = resp.headers.get("etag")
                new_lm = resp.headers.get("last-modified")
                if status == 200:
                    task.etag, task.last_modified = new_etag, new_lm
                task.downloaded_bytes = existing
                if task.total_bytes:
                    try:
                        ensure_free_space(dest, task.total_bytes - existing)
                    except InsufficientSpaceError as exc:
                        raise DownloadError(str(exc)) from exc
                task.status = DownloadStatus.DOWNLOADING
                task.status_detail = None
                on_checkpoint(task)
                await self._stream_body(resp, part, mode, task, meter, on_progress, on_checkpoint)
        except UnsafeURLError as exc:
            raise DownloadError(f"Blocked unsafe download address: {exc}") from exc
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise DownloadError("The disk is full. Free some space and retry.") from exc
            if isinstance(exc, (PermissionError,)):
                raise DownloadError(f"Permission denied writing to {dest}") from exc
            raise DownloadError(f"File system error: {exc.strerror or exc}") from exc
        if task.total_bytes is not None and task.downloaded_bytes < task.total_bytes:
            raise DownloadError(f"Connection closed early ({task.downloaded_bytes} of {task.total_bytes} bytes)",
                                retryable=True)
        if task.total_bytes is None:
            task.total_bytes = task.downloaded_bytes

    @staticmethod
    def _check_status(status: int) -> None:
        if status in (200, 206):
            return
        if status in (404, 410):
            raise DownloadError(f"The file is no longer available (HTTP {status}); the link may have expired.")
        if status in (401, 403):
            raise DownloadError(f"Access denied (HTTP {status}). The link may have expired or needs a browser session.")
        if status == 429 or status >= 500:
            raise DownloadError(f"Server is busy or failing (HTTP {status})", retryable=True)
        raise DownloadError(f"Unexpected HTTP status {status}")

    async def _stream_body(self, resp: httpx.Response, part: Path, mode: str, task: DownloadTask,
                           meter: SpeedMeter, on_progress, on_checkpoint) -> None:
        last_ui = 0.0                      # report the first chunk immediately
        last_db = time.monotonic()
        last_db_bytes = task.downloaded_bytes
        with open(part, mode) as fh:
            async for chunk in resp.aiter_raw(CHUNK):
                if not chunk:
                    continue
                await self.bucket.consume(len(chunk))
                fh.write(chunk)
                task.downloaded_bytes += len(chunk)
                meter.add(len(chunk))
                if task.total_bytes and task.downloaded_bytes > task.total_bytes:
                    # the oversized .part is discarded at the start of the next attempt
                    raise DownloadError("Server sent more data than announced; restarting", retryable=True)
                now = time.monotonic()
                if now - last_ui >= 0.5:
                    last_ui = now
                    task.speed = meter.rate
                    remaining = (task.total_bytes - task.downloaded_bytes) if task.total_bytes else None
                    task.eta = remaining / task.speed if remaining is not None and task.speed > 0 else None
                    on_progress(task)
                if now - last_db >= 5 or task.downloaded_bytes - last_db_bytes >= 32 * 1024 * 1024:
                    fh.flush()
                    last_db, last_db_bytes = now, task.downloaded_bytes
                    on_checkpoint(task)
            fh.flush()
            os.fsync(fh.fileno())
        task.speed = meter.rate
        on_progress(task)

    async def _finalize(self, task: DownloadTask, on_progress, on_checkpoint) -> Path:
        part = self.part_path(task)
        assert part is not None and task.filename
        if not part.exists():
            raise DownloadError("The partial file disappeared before it could be finalised.")
        actual = part.stat().st_size
        if task.total_bytes is not None and actual != task.total_bytes:
            part.unlink(missing_ok=True)
            task.downloaded_bytes = 0
            raise DownloadError(f"Downloaded size {actual} does not match expected {task.total_bytes}; "
                                f"the partial file was discarded — retry to download again.")
        task.verification = Verification.NONE
        if task.expected_checksum:
            algo, _, expected = task.expected_checksum.partition(":")
            task.status = DownloadStatus.VERIFYING
            task.status_detail = f"Verifying {algo.upper()} checksum"
            on_progress(task)
            on_checkpoint(task)
            digest = await asyncio.to_thread(file_checksum, part, algo)
            if digest.lower() != expected.lower():
                task.verification = Verification.MISMATCH
                part.unlink(missing_ok=True)
                task.downloaded_bytes = 0
                raise DownloadError(f"{algo.upper()} checksum mismatch — the file is corrupted. "
                                    f"It was deleted; retry to download it again.")
            task.verification = Verification.VERIFIED
        final = unique_path(Path(task.dest_dir) / task.filename)
        os.replace(part, final)
        task.filename = final.name
        task.status_detail = None
        return final
