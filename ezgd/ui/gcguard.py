"""Run Python's garbage collector on the Qt main thread only.

Crash found on 2026-10-10 (core dumps of the app): the cyclic garbage collector, triggered automatically by
allocations on a worker thread (extraction, file scans, diagnosis), walked PySide objects while the main thread
was deleting a widget (the Library/Downloads pages re-render after Play, Uninstall or a finished download). The
collector then touched a half-destroyed Qt wrapper → SIGSEGV in ``visit_decref`` (libshiboken6). It looked random
because it needs both at the same moment.

Fix: automatic collection is switched off for the whole process and a main-thread timer collects instead — the
main thread cannot be deleting a widget while it is collecting. Young objects are collected often (cheap), the
whole heap now and then, so memory use stays the same in practice.
"""

from __future__ import annotations

import gc
import sys
import time

from PySide6.QtCore import QObject, QTimer

YOUNG_MS = 500          # generation 0: cheap, frequent
FULL_EVERY = 60         # every 60th tick (30 s): all generations
MAX_DEFER = 120.0       # the bigger collections never wait longer than this

_quiet_until = 0.0


_libc = None


def trim() -> None:
    """Give memory that was freed back to the system (Linux/glibc keeps freed blocks of a fragmented heap: couch
    mode's pictures left ~100 MB behind). About 10 ms, so only at quiet moments."""
    global _libc
    if _libc is False or not sys.platform.startswith("linux"):
        return
    try:
        if _libc is None:
            import ctypes
            _libc = ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
    except Exception:
        _libc = False


def defer(seconds: float = 1.5) -> None:
    """The user is moving through couch mode: hold the bigger (slower) collections back for a moment, so they
    never land in the middle of an animation. Young objects are still collected."""
    global _quiet_until
    _quiet_until = max(_quiet_until, time.monotonic() + seconds)


class MainThreadGC(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._ticks = 0
        self._owed = 0                     # 1: a gen-1 collection is due, 2: a full one
        self._owed_since = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(YOUNG_MS)
        self._timer.timeout.connect(self._collect)

    def start(self) -> None:
        gc.disable()                       # no collection may start on another thread any more
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        gc.collect()

    def _collect(self) -> None:
        self._ticks += 1
        if self._ticks % FULL_EVERY == 0:
            self._owe(2)
        elif self._ticks % 10 == 0:
            self._owe(1)
        now = time.monotonic()
        if self._owed and (now >= _quiet_until or now - self._owed_since > MAX_DEFER):
            if self._owed == 2:
                gc.collect()
                trim()
            else:
                gc.collect(1)
            self._owed = 0
        else:
            gc.collect(0)

    def _owe(self, level: int) -> None:
        if not self._owed:
            self._owed_since = time.monotonic()
        self._owed = max(self._owed, level)


def release() -> None:
    """Collect everything and give the freed memory back (after something big closed)."""
    gc.collect()
    trim()


def freeze_startup() -> None:
    """Move what is alive after start-up out of the collector's way (``gc.freeze``). Only once: things frozen later
    (a couch window) would never be collected if they hold reference cycles."""
    global _frozen
    if not _frozen:
        _frozen = True
        gc.collect()
        gc.freeze()


_frozen = False


def install(app) -> MainThreadGC:
    guard = MainThreadGC(app)
    guard.start()
    return guard
