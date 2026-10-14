"""Parse site pages in a helper process.

Turning a page into games (BeautifulSoup over 100–700 KB of HTML or JSON) takes 10–70 ms of pure Python. In a
thread it still holds Python's global lock, so the window stuttered whenever new pages arrived — while scrolling
All Games, or the Store in couch mode. A helper process has its own interpreter: the window never waits for it, and
pages are parsed on another CPU core.

If the helper cannot be started or breaks, parsing continues in a thread (as before) for the rest of the session.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

log = logging.getLogger(__name__)

WORKERS = 2
_pool: ProcessPoolExecutor | None = None
_disabled = os.environ.get("EZGD_PARSE_IN_PROCESS", "1") == "0"


def _follow_parent() -> None:
    """In the helper: exit as soon as the app is gone — also when it crashed or was killed (a helper's queue keeps
    its pipe open, so it would otherwise wait forever)."""
    import threading
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch() -> None:
        from multiprocessing.connection import wait
        wait([parent.sentinel])
        os._exit(0)
    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def _pool_or_none() -> ProcessPoolExecutor | None:
    global _pool
    if _disabled:
        return None
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn"),
                                    initializer=_follow_parent)
    return _pool


def _warm() -> int:
    from ezgd.sources import ankergames, crackedgames, fitgirl, onlinefix, steamrip  # noqa: F401 — import once
    return os.getpid()


async def warm_up() -> None:
    """Start the helper (and import the parsers in it) before the first page needs it."""
    try:
        await parse(_warm)
    except Exception:
        log.debug("parse helper warm-up failed", exc_info=True)


async def parse(fn, *args):
    """``fn(*args)`` in the helper process (``fn`` must be a module-level function, its arguments picklable)."""
    global _disabled
    pool = _pool_or_none()
    if pool is not None:
        try:
            return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)
        except (BrokenProcessPool, OSError, EOFError, pickle.PicklingError, AttributeError, ImportError,
                RuntimeError) as exc:
            # (BrokenProcessPool is a RuntimeError too; other RuntimeErrors only count when the pool is shut down)
            if type(exc) is RuntimeError and "shutdown" not in str(exc).lower():
                raise                                  # an error from the parser itself
            log.warning("the parse helper process failed (%s); parsing in a thread from now on", exc)
            _disabled = True
            shutdown()
    return await asyncio.to_thread(fn, *args)


def shutdown() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
