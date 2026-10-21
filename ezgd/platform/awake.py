"""Keep the screen from dimming and locking while a game is played with a controller.

Controller input is not "user activity" to the desktop: KDE (and GNOME) only reset their idle timer on keyboard and
mouse. A game played with a controller therefore dims after a few minutes, then locks — Steam avoids this only for
the games it starts itself. The app holds an *idle inhibitor* while one of its games runs (and while couch mode is
open): ``systemd-inhibit --what=idle``, which KDE's power management lists ("EZ Game Downloader: <game> is
running") and honours. The inhibitor lives exactly as long as its helper process, so it can never be left behind:
released when the game ends, and by the system when the app quits or crashes.

(org.freedesktop.ScreenSaver.Inhibit was tried first: PySide cannot send its UnInhibit cookie as the unsigned
number KDE requires, so a hold could only be released by quitting the app.)
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading

log = logging.getLogger(__name__)

APP = "EZ Game Downloader"


class KeepAwake:
    """Named holds (``"game:12"``, ``"couch"``); each one is one ``systemd-inhibit`` helper."""

    def __init__(self) -> None:
        self._holds: dict[str, tuple[subprocess.Popen, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return os.environ.get("EZGD_AWAKE", "1") != "0" and shutil.which("systemd-inhibit") is not None

    def hold(self, key: str, why: str) -> bool:
        with self._lock:
            cur = self._holds.get(key)
            if cur is not None and cur[0].poll() is None:
                if cur[1] == why:
                    return True
                self._end(cur[0])
            if not self.available():
                return False
            try:
                p = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle", f"--who={APP}", f"--why={why}", "--mode=block",
                     "sleep", "infinity"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            except OSError:
                log.exception("could not keep the screen awake")
                return False
            self._holds[key] = (p, why)
            log.info("keeping the screen awake: %s", why)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            cur = self._holds.pop(key, None)
        if cur is not None:
            self._end(cur[0])
            log.info("screen may dim again: %s", cur[1])

    def sync(self, wanted: dict[str, str], prefix: str) -> None:
        """Hold exactly ``wanted`` among the holds whose key starts with ``prefix`` (e.g. the running games)."""
        for key in [k for k in self._holds if k.startswith(prefix) and k not in wanted]:
            self.release(key)
        for key, why in wanted.items():
            self.hold(key, why)

    def held(self) -> dict[str, str]:
        return {k: why for k, (p, why) in self._holds.items() if p.poll() is None}

    def release_all(self) -> None:
        for key in list(self._holds):
            self.release(key)

    @staticmethod
    def _end(p: subprocess.Popen) -> None:
        try:                                    # the whole session: systemd-inhibit and its sleep
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


_shared: KeepAwake | None = None


def shared() -> KeepAwake:
    global _shared
    if _shared is None:
        _shared = KeepAwake()
    return _shared
