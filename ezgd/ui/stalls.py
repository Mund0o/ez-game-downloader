"""UI stalls in the log: how long the window stopped answering, where, and the code it was stuck in.

A friend's "the Downloads page lags every time I open it" could not be reproduced here (the page opened in 10–23 ms
with 120 downloads, couch mode's tab in 2–4 ms) — so the app writes the evidence itself. A timer on the Qt main
thread beats every 100 ms; a watcher thread notices when the beats stop and, while the main thread is still stuck,
takes its Python stack (the line it is stuck in). When the beats come back, one line goes to the log: the stall's
length, the page that was shown and that stack. Costs nothing measurable; at most 30 lines per 10 minutes.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger(__name__)

BEAT_MS = 100
STALL_SECONDS = 0.25            # shorter is not felt as lag
BUDGET = (30, 600.0)            # at most this many lines per this many seconds


class StallWatch(QObject):
    def __init__(self, where: Callable[[], str], parent=None):
        super().__init__(parent)
        self._where = where
        self._main = threading.main_thread().ident
        self._beat = time.monotonic()
        self._stack: list[str] | None = None
        self._lines: list[float] = []
        self._timer = QTimer(self)
        self._timer.setInterval(BEAT_MS)
        self._timer.timeout.connect(self._tick)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="ui-stall-watch", daemon=True)

    def start(self) -> None:
        self._beat = time.monotonic()
        self._timer.start()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._timer.stop()

    def _watch(self) -> None:
        while not self._stop.wait(BEAT_MS / 1000):
            if self._stack is None and time.monotonic() - self._beat > STALL_SECONDS:
                frame = sys._current_frames().get(self._main)
                if frame is not None:             # (the line the main thread is in right now: what blocks it)
                    self._stack = [f"{fs.filename.rsplit('/ezgd/', 1)[-1]}:{fs.lineno} {fs.name}"
                                   for fs in traceback.extract_stack(frame)[-8:]]

    def _tick(self) -> None:
        now = time.monotonic()
        gap = now - self._beat - BEAT_MS / 1000
        self._beat = now
        stack, self._stack = self._stack, None
        if gap < STALL_SECONDS:
            return
        self._lines = [t for t in self._lines if now - t < BUDGET[1]]
        if len(self._lines) >= BUDGET[0]:
            return
        self._lines.append(now)
        try:
            where = self._where()
        except Exception:
            where = "?"
        log.info("UI stall of %d ms on %s%s", gap * 1000, where,
                 (" — stuck in: " + " ← ".join(reversed(stack))) if stack else "")


_watch: StallWatch | None = None


def install(where: Callable[[], str], parent=None) -> StallWatch:
    global _watch
    if _watch is None:
        _watch = StallWatch(where, parent)
        _watch.start()
    return _watch
