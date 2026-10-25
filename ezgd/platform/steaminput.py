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

Steam does not keep a forced layout through its own Steam button: pressing it switches to the guide-chord layout
(443510) and, on release, to the **desktop** one (413080) — not back to the forced app. Steam's virtual pad goes away
with it and the game has no controller until it ends (a friend's Dark Souls III, 2026-10-23: "controller
disconnected" 14 ms after the button, never back; controller.txt here: 480 → 443510 → 413080 within a second).
While a forced game runs, :class:`Forced` watches controller.txt and asks for the game layout again when Steam fell
back to its desktop or Big Picture layout. The app's quick menu is driven by the keys of Steam's desktop layout, so
opening it (``hold(True)``) asks for that layout itself — Steam does not always fall back to it after its Steam button
(a game Steam knows, one started through a shortcut, keeps the game layout) — and closing it asks for the game layout
again, or hands the choice back (``/0``) for a game whose layout is Steam's. Games started through Steam (play mode)
get the same game layout as the ones started here: Steam only sees the shortcut that runs play mode, not the game,
and its layout for it is not the game's.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

GAME_LAYOUT = 480                # Spacewar: what OnlineFix games already get
DESKTOP = 413080                 # Steam's desktop configuration
BIG_PICTURE = 413090             # Steam's Big Picture configuration
CHORD = 443510                   # Steam's guide-chord configuration (while the Steam button is held)
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
    return read_activations(root, since)[0]


def read_activations(root: Path | None, since: int) -> tuple[list[tuple[int, int]], int]:
    """:func:`activations` and the byte the log was read up to (where to go on from next time)."""
    if root is None:
        return [], since
    try:
        with open(root / "logs" / "controller.txt", "rb") as fh:
            fh.seek(since if since <= fh.seek(0, 2) else 0)        # (a new, shorter log: from its start)
            data = fh.read(512 * 1024)
            end = fh.tell()
    except OSError:
        return [], since
    cut = data.rfind(b"\n") + 1                     # a line Steam is still writing is read next time
    if 0 < cut < len(data):
        data, end = data[:cut], end - (len(data) - cut)
    return [(int(c), int(a)) for c, a in ACTIVATED.findall(data.decode("utf-8", "replace"))], end


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
    """The layout the app forced, per game (and the desktop one while the quick menu is over a game); handed back
    (``/0``) when the last game ends or the menu closes over a game whose layout is Steam's."""

    WATCH_SECONDS = 1.0            # how often controller.txt is looked at while a forced game runs
    RETRY_SECONDS = 2.0            # between two requests for the game layout
    MAX_TRIES = 8                  # requests in a row that Steam did not answer with the game layout: give up
    #                                (the menu resets this: the Steam button may drop the layout more than once)

    def __init__(self, marker: Path, opener=None, root: Callable[[], Path | None] | None = None):
        self.marker = marker
        self.games: set[int] = set()
        if opener is None:
            from ezgd.platform.steam import open_steam_url
            opener = open_steam_url
        self._open = opener
        self._root = root                  # Steam's folder (its controller log); None: no watching
        self.held = False                  # the app's quick menu is over the game: Steam's desktop layout is forced
        self._wake = threading.Event()
        self._watcher: threading.Thread | None = None
        self._appid = GAME_LAYOUT
        self._lock = threading.Lock()      # the quick menu (UI) and the watcher both talk to Steam
        self._tries = 0
        self._next_try = 0.0
        self._menu_only = False            # the menu is over a game whose layout is Steam's: hand /0 back, no 480

    @property
    def active(self) -> bool:
        """A game layout is forced, or the quick menu is holding Steam's desktop layout."""
        return bool(self.games or self._menu_only)

    def _remember(self, appid: int) -> None:
        try:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text(str(appid), "utf-8")
        except OSError:
            pass

    def _forget(self) -> None:
        try:
            self.marker.unlink()
        except OSError:
            pass

    def start(self, entry_id: int, appid: int = GAME_LAYOUT) -> None:
        first = not self.games
        self.games.add(entry_id)
        with self._lock:
            if first:
                self._remember(appid)
            self._appid = appid
            self._tries = 0
            self._next_try = 0.0
            self._menu_only = False
            if not self.held:                  # (a game started from the open quick menu stays on its layout)
                self._ask(appid)
        if self._root is not None and (self._watcher is None or not self._watcher.is_alive()):
            self._watcher = threading.Thread(target=self._watch, name="steam-input-watch", daemon=True)
            self._watcher.start()

    def hold(self, on: bool) -> None:
        """The quick menu opened (``True``) or closed over a game. Opened: Steam's desktop layout, whose keys drive
        the menu (after its Steam button Steam does not always fall back to it — a game Steam knows gets its game
        layout back). Closed over a forced game: the game layout at once, then watched for until Steam took it.
        Closed over a game whose layout the app leaves to Steam: ``/0``, so Steam chooses again."""
        if not on and not self.games and not self._menu_only:
            return                         # (nothing was forced, and the menu was not over a game here)
        with self._lock:
            self.held = bool(on)
            if on:
                self._menu_only = not self.games
                if self._menu_only:
                    self._remember(DESKTOP)    # (a crash with the menu open must not leave the desktop layout)
                self._tries = 0
                self._next_try = 0.0
                log.info("the quick menu is over the game — Steam's desktop controller layout, which drives it")
                self._ask(DESKTOP)
            elif self._menu_only:
                self._menu_only = False
                self._tries = 0
                self._next_try = 0.0
                log.info("the quick menu closed — handing the controller layout back to Steam")
                self._ask(0)
                self._forget()
            else:
                self._tries = 1
                self._next_try = time.monotonic() + self.RETRY_SECONDS
                log.info("the quick menu closed — asking for the game controller layout again")
                self._ask(self._appid)
        self._wake.set()

    def _ask(self, appid: int) -> None:
        try:
            self._open(url(appid))
        except Exception:
            log.exception("could not ask Steam for the controller layout (app %s)", appid)

    def _watch(self) -> None:
        root = self._root() if self._root else None
        if root is None:
            return
        pos = log_size(root)
        last = self._appid
        while self.games:
            self._wake.wait(self.WATCH_SECONDS)
            self._wake.clear()
            got, pos = read_activations(root, pos)
            if got:
                last = got[-1][1]
                if last == self._appid:
                    self._tries = 0
            if not self.games or self.held or last not in (DESKTOP, BIG_PICTURE):
                continue                   # (the chord while the button is held; another game Steam started)
            now = time.monotonic()
            if self._tries >= self.MAX_TRIES or now < self._next_try:
                continue
            self._tries += 1
            self._next_try = now + self.RETRY_SECONDS
            log.info("Steam went back to its %s controller layout while a game runs (its Steam button) — asking "
                     "for the game layout again", "desktop" if last == DESKTOP else "Big Picture")
            with self._lock:
                self._ask(self._appid)

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
        with self._lock:
            self.games.clear()               # (the app is quitting, or the last game ended: nothing left to force)
            self.held = False
            self._menu_only = False
            self._tries = 0
            self._next_try = 0.0
            self._ask(0)
            self._forget()

    def left_over(self) -> bool:
        """A layout forced by an app that did not end cleanly (its marker is still there)."""
        return self.marker.is_file()
