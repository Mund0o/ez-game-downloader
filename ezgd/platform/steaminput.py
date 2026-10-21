"""Steam Input for games the app starts itself: a game controller layout instead of Steam's desktop one.

While Steam runs it owns the controllers it supports (always Valve's: Steam Controller, Steam Deck; PlayStation /
Xbox / Switch pads when Steam Input is on for them). Steam switches a controller to a game's layout only when it
knows a game is running: one it started, or one that signs in to Steam (OnlineFix games sign in as Spacewar, 480 —
Steam's controller.txt then shows ``Queueing activation for controller: 0 app: 480``). A game the app starts
directly that never talks to Steam — an emulator like shadPS4 (Bloodborne), most repacks — keeps the **desktop
layout** (app 413080): the stick / trackpad moves the mouse, buttons send keys, and the game gets no proper gamepad.
A friend's Bloodborne (2026-10): "controller problems … the mouse was moving with the controller".

Steam's own link ``steam://forceinputappid/<app>`` makes it use that app's layout until ``…/0`` hands the choice
back to Steam. The app uses Spacewar's (480): the layout OnlineFix games already get, Steam's gamepad template unless
the player changed it (Steam → Spacewar → Controller layout). Whether Steam took the switch is read back from
controller.txt and logged, not assumed. A marker file remembers a forced layout, so one left by a crashed app is
handed back at the next start (else the controller would stay a gamepad on the desktop).
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

GAME_LAYOUT = 480                # Spacewar: what OnlineFix games already get
DESKTOP = 413080                 # Steam's desktop configuration
ACTIVATED = re.compile(r"Queueing activation for controller: (\d+) app: (-?\d+)")


def enabled() -> bool:
    return os.environ.get("EZGD_STEAM_INPUT", "1") != "0"      # (tests: never the real Steam client)


def url(appid: int) -> str:
    return f"steam://forceinputappid/{int(appid)}"


def log_size(root: Path | None) -> int:
    try:
        return (root / "logs" / "controller.txt").stat().st_size if root else 0
    except OSError:
        return 0


def activations(root: Path | None, since: int) -> list[tuple[int, int]]:
    """(controller, app) of each layout Steam switched to since byte ``since`` of controller.txt."""
    if root is None:
        return []
    try:
        with open(root / "logs" / "controller.txt", "rb") as fh:
            fh.seek(since if since <= fh.seek(0, 2) else 0)
            text = fh.read(512 * 1024).decode("utf-8", "replace")
    except OSError:
        return []
    return [(int(c), int(a)) for c, a in ACTIVATED.findall(text)]


def took(root: Path | None, since: int, appid: int, wait: float = 6.0) -> bool | None:
    """Did Steam switch a controller to ``appid``'s layout? None: no controller switched at all (none connected,
    or Steam Input is off for it — nothing to judge)."""
    end = time.monotonic() + wait
    while True:
        got = activations(root, since)
        if any(a == appid for _c, a in got):
            return True
        if time.monotonic() >= end:
            return False if got else None
        time.sleep(0.5)


class Forced:
    """The layout the app forced, per game; handed back (``/0``) when the last of them ends."""

    def __init__(self, marker: Path, opener=None):
        self.marker = marker
        self.games: set[int] = set()
        if opener is None:
            from ezgd.platform.steam import open_steam_url
            opener = open_steam_url
        self._open = opener

    def start(self, entry_id: int, appid: int = GAME_LAYOUT) -> None:
        first = not self.games
        self.games.add(entry_id)
        if first:
            try:
                self.marker.parent.mkdir(parents=True, exist_ok=True)
                self.marker.write_text(str(appid), "utf-8")
            except OSError:
                pass
        self._open(url(appid))

    def end(self, entry_id: int) -> bool:
        """True when this was the last game: Steam chooses the layout again."""
        if entry_id not in self.games:
            return False
        self.games.discard(entry_id)
        if self.games:
            return False
        self.hand_back()
        return True

    def hand_back(self) -> None:
        self._open(url(0))
        try:
            self.marker.unlink()
        except OSError:
            pass

    def left_over(self) -> bool:
        """A layout forced by an app that did not end cleanly (its marker is still there)."""
        return self.marker.is_file()
