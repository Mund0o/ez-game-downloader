"""Asynchronous cover-art loading with an in-memory LRU and bounded concurrency."""

from __future__ import annotations

from collections import OrderedDict, deque

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage, QPixmap

MAX_IN_FLIGHT = 4
MEMORY_ITEMS = 400


class CoverCache(QObject):
    ready = Signal(str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self._mem: "OrderedDict[str, QPixmap | None]" = OrderedDict()
        self._queue: deque[str] = deque()
        self._in_flight: set[str] = set()

    def get(self, url: str | None) -> QPixmap | None:
        if not url:
            return None
        if url in self._mem:
            self._mem.move_to_end(url)
            return self._mem[url]
        if url not in self._in_flight and url not in self._queue:
            self._queue.appendleft(url)          # most recently requested first (visible items)
            self._pump()
        return None

    def _pump(self) -> None:
        while self._queue and len(self._in_flight) < MAX_IN_FLIGHT:
            url = self._queue.popleft()
            self._in_flight.add(url)
            self.bridge.call(self.bridge.core.cover(url), lambda data, u=url: self._loaded(u, data),
                             lambda err, u=url: self._loaded(u, None), owner=self)
        while len(self._queue) > 200:              # forget requests for long-gone items
            self._queue.pop()

    def _loaded(self, url: str, data: bytes | None) -> None:
        self._in_flight.discard(url)
        pm = None
        if data:
            img = QImage.fromData(data)
            if not img.isNull():
                if img.width() > 640:
                    img = img.scaledToWidth(640, Qt.SmoothTransformation)
                pm = QPixmap.fromImage(img)
        self._mem[url] = pm
        while len(self._mem) > MEMORY_ITEMS:
            self._mem.popitem(last=False)
        self.ready.emit(url)
        self._pump()
