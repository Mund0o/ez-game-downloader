"""Bridge between the Qt GUI thread and the core asyncio loop.

The core (network, downloads, database work) runs on its own thread with its
own event loop, so the interface never blocks on I/O. GUI code calls
``bridge.call(core.something(...), on_ok, on_err, owner=widget)``; results
come back through a queued Qt signal on the GUI thread. Core events are
re-emitted as the ``event`` signal.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Callable, Coroutine

import shiboken6
from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class CoreThread(threading.Thread):
    def __init__(self):
        super().__init__(name="ezgd-core", daemon=True)
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        # drain callbacks scheduled during shutdown
        pending = asyncio.all_tasks(self.loop)
        for t in pending:
            t.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def start(self) -> None:
        super().start()
        self._ready.wait(5)

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.join(10)


class Bridge(QObject):
    event = Signal(str, object)
    error = Signal(str)
    _done = Signal(object, object, object, object)

    def __init__(self, thread: CoreThread, core, parent: QObject | None = None):
        super().__init__(parent)
        self.thread = thread
        self.core = core
        self.busy = 0
        core.set_emitter(lambda name, payload: self.event.emit(name, payload))
        self._done.connect(self._dispatch)

    def call(self, coro: Coroutine, on_ok: Callable[[Any], None] | None = None,
             on_err: Callable[[BaseException], None] | None = None, owner: QObject | None = None
             ) -> concurrent.futures.Future:
        if self.thread.loop.is_closed() or not self.thread.is_alive():
            coro.close()                      # app is shutting down: drop the request quietly
            fut: concurrent.futures.Future = concurrent.futures.Future()
            fut.cancel()
            return fut
        fut = self.thread.submit(coro)
        self.busy += 1

        def done(f: concurrent.futures.Future) -> None:
            try:
                res, err = f.result(), None
            except BaseException as exc:      # noqa: BLE001 - delivered to the GUI
                res, err = None, exc
            try:
                self._done.emit(owner, on_ok, on_err, (res, err))
            except RuntimeError:              # bridge already destroyed (app closing)
                pass

        fut.add_done_callback(done)
        return fut

    def _dispatch(self, owner, on_ok, on_err, payload) -> None:
        self.busy -= 1
        res, err = payload
        if owner is not None and not shiboken6.isValid(owner):
            return
        try:
            if err is not None:
                if isinstance(err, concurrent.futures.CancelledError):
                    return
                if on_err:
                    on_err(err)
                else:
                    log.warning("background call failed: %s", err)
                    self.error.emit(str(err) or type(err).__name__)
            elif on_ok:
                on_ok(res)
        except Exception:
            log.exception("UI callback failed")

    def run_sync(self, coro: Coroutine, timeout: float = 30) -> Any:
        """Block the GUI thread for a short core call (start-up/shutdown only)."""
        return self.thread.submit(coro).result(timeout)
