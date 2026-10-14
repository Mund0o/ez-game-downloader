"""Asynchronous cover-art loading with an in-memory LRU and bounded concurrency.

Pictures are fetched by the core and *decoded in worker threads* (a JPEG takes a few milliseconds to decode — done
on the GUI thread for dozens of covers at once, that made scrolling stutter); only the cheap QImage → QPixmap step
happens on the GUI thread.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPixmap

log = logging.getLogger(__name__)

MAX_IN_FLIGHT = 6
MEMORY_ITEMS = 400
MAX_WIDTH = 640


class _Decoded(QObject):
    done = Signal(str, object)            # url, QImage | None (emitted from a worker, delivered on the GUI thread)
    variant = Signal(object, str, object)  # key, url, QImage | None


class CoverCache(QObject):
    ready = Signal(str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self._mem: "OrderedDict[str, QPixmap | None]" = OrderedDict()
        self._queue: deque[str] = deque()
        self._in_flight: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cover-decode")
        self._closed = False
        self._sig = _Decoded()            # no parent: it outlives this object until the workers have stopped
        self._sig.done.connect(self._decoded)
        self._sig.variant.connect(self._variant_done)
        self._images: "OrderedDict[str, QImage]" = OrderedDict()      # decoded originals, for variants
        self._variants: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._variant_jobs: set[tuple] = set()

    def variant(self, url: str | None, key: tuple, make) -> QPixmap | None:
        """A finished version of a cover (scaled, rounded, shaded — ``make(QImage) -> QImage`` builds it in a worker
        thread), or None until it is ready (``ready`` is emitted then)."""
        if not url:
            return None
        pm = self._variants.get(key)
        if pm is not None:
            self._variants.move_to_end(key)
            return pm
        img = self._images.get(url)
        if img is None:
            base = self.get(url)
            if base is None or base.isNull():
                return None
            img = base.toImage()
            self._images[url] = img
        if key in self._variant_jobs or self._closed:
            return None
        self._variant_jobs.add(key)

        def work() -> None:
            try:
                out = make(img)
            except Exception:
                log.exception("preparing a cover failed")
                out = None
            if self._closed:
                return
            try:
                self._sig.variant.emit(key, url, out)
            except RuntimeError:
                pass
        try:
            self._pool.submit(work)
        except RuntimeError:
            self._variant_jobs.discard(key)
        return None

    def _variant_done(self, key: tuple, url: str, img) -> None:
        if self._closed:
            return
        self._variant_jobs.discard(key)
        if img is None or img.isNull():
            return
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(img.devicePixelRatio())
        self._variants[key] = pm
        while len(self._variants) > 300:
            self._variants.popitem(last=False)
        self.ready.emit(url)

    def close(self) -> None:
        """Stop the decoders (the app is quitting): nothing may arrive afterwards."""
        self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)

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
        while self._queue and len(self._in_flight) < MAX_IN_FLIGHT and not self._closed:
            url = self._queue.popleft()
            self._in_flight.add(url)
            self.bridge.call(self.bridge.core.cover(url), lambda data, u=url: self._loaded(u, data),
                             lambda err, u=url: self._loaded(u, None), owner=self)
        while len(self._queue) > 200:              # forget requests for long-gone items
            self._queue.pop()

    def _loaded(self, url: str, data: bytes | None) -> None:
        if not data or self._closed:
            self._decoded(url, None)
            return

        def work() -> None:
            img = QImage.fromData(data)
            if not img.isNull() and img.width() > MAX_WIDTH:
                img = img.scaled(QSize(MAX_WIDTH, MAX_WIDTH * 4), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if self._closed:
                return
            try:
                self._sig.done.emit(url, None if img.isNull() else img)
            except RuntimeError:                   # shutting down
                pass
        try:
            self._pool.submit(work)
        except RuntimeError:                       # the pool was shut down
            self._decoded(url, None)

    def _decoded(self, url: str, img) -> None:
        if self._closed:
            return
        self._in_flight.discard(url)
        self._mem[url] = QPixmap.fromImage(img) if img is not None else None
        while len(self._mem) > MEMORY_ITEMS:
            self._mem.popitem(last=False)
        if img is not None:
            self._images[url] = img
            while len(self._images) > 80:
                self._images.popitem(last=False)
        self.ready.emit(url)
        self._pump()
