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

from PySide6.QtCore import QObject, QTimer

YOUNG_MS = 500          # generation 0: cheap, frequent
FULL_EVERY = 60         # every 60th tick (30 s): all generations


class MainThreadGC(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._ticks = 0
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
            gc.collect()
        elif self._ticks % 10 == 0:
            gc.collect(1)
        else:
            gc.collect(0)


def install(app) -> MainThreadGC:
    guard = MainThreadGC(app)
    guard.start()
    return guard
