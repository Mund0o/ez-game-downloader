"""Steam Input for games the app starts itself: a game controller layout instead of Steam's desktop one.

While Steam runs it owns the controllers it supports (always Valve's: Steam Controller, Steam Deck; PlayStation /
Xbox / Switch pads when Steam Input is on for them) and picks a layout for them by **window focus**: the focused X11
window of a game Steam knows gets that game's layout, anything else — a Wayland window like the app's quick menu
included — Steam's **desktop layout** (app 413080: the stick / trackpad moves the mouse, buttons send keys). Steam
logs each choice in controller_ui.txt (``OnFocusWindowChanged to game window type: AppID 480`` / ``… to window type:
k_nGameIDControllerConfigs_Desktop``) and each switch in controller.txt (``Queueing activation for controller: 0
app: 480``). Games Steam knows: its own, the app's shortcuts it starts (play mode: Steam's gamepad template, "Last
Resort Path … controller_*_gamepad_joystick.vdf"), and games that sign in to it (OnlineFix games as Spacewar, 480).
A game the app starts directly that never talks to Steam — an emulator like shadPS4 (Bloodborne), most repacks —
only ever gets the desktop layout, and no proper gamepad (a friend's Bloodborne, 2026-10: "the mouse was moving with
the controller").

Steam's own link ``steam://forceinputappid/<app>`` makes Steam use that app's layout for *every* window, whatever has
the focus, until ``…/0``. Measured on Steam (2026-10-27, with a controller, reading both logs):

* a forced layout is switched to at once, and kept through focus changes (X11 and Wayland windows), through Steam's
  Steam button (its chord layout 443510 while held, then the forced one again) and through Big Picture opening and
  closing;
* ``…/0`` switches *nothing* by itself: the forced layout stays until the next X11 focus change makes Steam choose
  again. So handing back is "force the desktop layout, then ``/0``" when no game of the app is left;
* steam:// links reach Steam asynchronously (the ``steam`` command returns in ~10 ms), so a second link is sent only
  once Steam logged the first.

**Which layout** (measured 2026-10-28, links sent in the background, Steam 1792618285):

* Spacewar's (480) is not a gamepad: Steam picks Valve's own Steam Input API layout for Spacewar ("Space War Action
  Set Config Sample", workshop 1417465272) by itself the first time anything runs as 480 — every binding a Spacewar
  action, so Steam makes **no virtual pad** (``mapping uses xinput : false``) and a game that is not Spacewar gets
  nothing from a Steam Controller. OnlineFix games get it too, by themselves (they sign in as 480);
* a shortcut of the app (or the player's own shortcut for the game) is: Steam's gamepad template for it, or the
  layout the player chose for that shortcut in Steam — and a virtual pad within ~50 ms. Steam writes that switch
  with the id as a *signed* 32-bit number (``app: -1335484452`` for 2959482844); everything here compares ids
  unsigned (:func:`unsigned`);
* an id Steam does not know is ignored: nothing is switched or logged.

So with a Valve controller connected (one games cannot read themselves) the app asks for the game's own Steam
layout, else another of its shortcuts' (a gamepad template), and Spacewar's only as the last resort — and keeps the
first one that makes Steam's virtual pad (the manager checks; :meth:`Forced.retarget`). Without one, games read their
controller themselves and Spacewar's layout only keeps Steam's desktop layout (stick = mouse) out of the game.
The quick menu keeps the game's layout: over a forced game nothing changes (the controller stays the game's virtual
pad, which the menu reads too); over a game Steam knows (games Steam started) the menu would get Steam's desktop
layout the moment it takes the focus — Steam's virtual pad, the one the game reads, goes away and comes back as a new
device when the game has the focus again (a game may never pick it up again: a friend's Steam Controller "fell off a
cliff" after the menu). So the layout Steam chose for the focused game (its own log says which: ``to game window
type: AppID N``) is forced just before the menu takes the focus, and released once the game has it back
(:meth:`Forced.hold` / :meth:`Forced.let_go`).
While a game runs, Steam's choices are written to the app's log (evidence for the next report), and a forced layout
Steam lost (a Steam restart) is asked for again. A marker file remembers a forced layout, so one left by a crashed
app is handed back at the next start.
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

GAME_LAYOUT = 480                # Spacewar: what OnlineFix games get — Valve's layout for Spacewar, no gamepad
DESKTOP = 413080                 # Steam's desktop configuration
BIG_PICTURE = 413090             # Steam's Big Picture configuration
CHORD = 443510                   # Steam's guide-chord configuration (while the Steam button is held)
CLIENT_UI = 769                  # Steam's own windows and on-screen keyboard
ACTIVATED = re.compile(r"Queueing activation for controller: (\d+) app: (-?\d+)")
FOCUS = re.compile(r"OnFocusWindowChanged (.+?)\s*$")
NAMES = {DESKTOP: "desktop", BIG_PICTURE: "Big Picture", CHORD: "Steam button", GAME_LAYOUT: "Spacewar"}


def enabled() -> bool:
    return os.environ.get("EZGD_STEAM_INPUT", "1") != "0"      # (tests: never the real Steam client)


def unsigned(appid: int) -> int:
    """Steam's app ids are 32 bits: it writes a shortcut's (2³¹ and up) as a negative number in controller.txt
    (``app: -1335484452`` for 2959482844) and unsigned in its links and controller_ui.txt."""
    return int(appid) & 0xFFFFFFFF


def url(appid: int) -> str:
    return f"steam://forceinputappid/{unsigned(appid)}"


def log_size(root: Path | None, name: str = "controller.txt") -> int:
    try:
        return (root / "logs" / name).stat().st_size if root else 0
    except OSError:
        return 0


def activations(root: Path | None, since: int) -> list[tuple[int, int]]:
    """(controller, app) of each layout Steam switched to since byte ``since`` of controller.txt."""
    return read_activations(root, since)[0]


def _read_new(path: Path, since: int) -> tuple[str, int]:
    """Whole lines written to ``path`` after byte ``since``, and where to go on from next time."""
    try:
        with open(path, "rb") as fh:
            fh.seek(since if since <= fh.seek(0, 2) else 0)        # (a new, shorter log: from its start)
            data = fh.read(512 * 1024)
            end = fh.tell()
    except OSError:
        return "", since
    cut = data.rfind(b"\n") + 1                     # a line Steam is still writing is read next time
    if cut < len(data):
        data, end = data[:cut], end - (len(data) - cut)
    return data.decode("utf-8", "replace"), end


def read_activations(root: Path | None, since: int) -> tuple[list[tuple[int, int]], int]:
    """:func:`activations` and the byte the log was read up to (where to go on from next time)."""
    if root is None:
        return [], since
    text, end = _read_new(root / "logs" / "controller.txt", since)
    return [(int(c), unsigned(int(a))) for c, a in ACTIVATED.findall(text)], end


GUIDE_TAP = "Guide button sent to JS"                # (controller_ui.txt) the Steam button let go after a tap …
GUIDE_HOLD = "Guide button skipped due to length"    # … or after a long press (its chord: Steam's own)


def read_ui(root: Path | None, since: int) -> tuple[list[str], int, int]:
    """:func:`read_focus`, and how many times the Steam button was let go, since byte ``since``."""
    if root is None:
        return [], 0, since
    text, end = _read_new(root / "logs" / "controller_ui.txt", since)
    lines = text.splitlines()
    return ([m.group(1) for m in map(FOCUS.search, lines) if m],
            sum(GUIDE_TAP in ln or GUIDE_HOLD in ln for ln in lines), end)


def read_focus(root: Path | None, since: int) -> tuple[list[str], int]:
    """Steam's reasons for its layout choices (controller_ui.txt) since byte ``since``."""
    if root is None:
        return [], since
    text, end = _read_new(root / "logs" / "controller_ui.txt", since)
    return [m.group(1) for m in map(FOCUS.search, text.splitlines()) if m], end


def took(root: Path | None, since: int, appid: int, wait: float = 6.0, step: float = 0.5) -> bool | None:
    """Did Steam switch a controller to ``appid``'s layout? None: no controller switched at all (none connected,
    or Steam Input is off for it — nothing to judge)."""
    appid = unsigned(appid)
    end = time.monotonic() + wait
    while True:
        got = activations(root, since)
        if any(a == appid for _c, a in got):
            return True
        if time.monotonic() >= end:
            return False if got else None
        time.sleep(step)


GAME_WINDOW = re.compile(r"OnFocusWindowChanged (?:to game window type: AppID|URL Forcing to window type: "
                         r"k_EWindowTypeGame,) (\d+)")


def game_layout_now(root: Path | None) -> int | None:
    """The game layout Steam has now, from its last decision (controller_ui.txt): ``to game window type: AppID N``
    for the focused game — or ``URL Forcing … N``: a layout forced by a link stays Steam's until the next focus
    change, also after ``…/0`` (the quick menu's own hold, released while the game had the focus again: the next
    opening found "no game layout" and Steam took the pad away). Steam's own windows and keyboard flashing up in
    between (the Steam button) are skipped; the desktop and Steam's other layouts mean no game layout (None)."""
    if root is None:
        return None
    try:
        with open(root / "logs" / "controller_ui.txt", "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - 64 * 1024))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if "OnFocusWindowChanged" not in line:
            continue
        m = GAME_WINDOW.search(line)
        if m:
            app = unsigned(int(m.group(1)))
            return None if app in (DESKTOP, BIG_PICTURE, CLIENT_UI, CHORD) else app
        if "ClientUI" in line or "On Screen Keyboard" in line:
            continue
        return None
    return None


OPTED_IN = re.compile(r"Opted-in Controller Mask for AppId (\d+): ([0-9a-fA-F]+)")
OTHERS = 0xFFF                   # the opt-in bits of controllers other than Valve's (0x1000: every app has it)


def opted_in(root: Path | None, appid: int) -> int | None:
    """Which controllers Steam Input drives in ``appid``, from Steam's last start of it (controller.txt: ``Opted-in
    Controller Mask for AppId 3802265475: 1001``). 0x1000 is set for every app — Valve's own controllers are always
    Steam's —, the low bits are the other kinds' (measured 2026-10-28: a DualSense got a layout from an app with
    1001, Steam's "empty.vdf" from one with 1000). None: not in the log."""
    if root is None:
        return None
    appid = unsigned(appid)
    try:
        with open(root / "logs" / "controller.txt", "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - 512 * 1024))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = [int(m, 16) for a, m in OPTED_IN.findall(text) if unsigned(int(a)) == appid]
    return found[-1] if found else None


def virtual_pads(base: Path = Path("/sys/class/input")) -> list[str]:
    """Steam's virtual pads now (Steam Input: vendor 28de, product 11ff) — what a game reads the controller from."""
    out = []
    for d in base.glob("input*"):
        try:
            if (d / "id" / "vendor").read_text().strip() == "28de" and \
                    (d / "id" / "product").read_text().strip() == "11ff":
                out.append(d.name)
        except OSError:
            continue
    return sorted(out)


def name(appid: int) -> str:
    appid = unsigned(appid)
    return f"{appid} ({NAMES[appid]})" if appid in NAMES else str(appid)


class Forced:
    """The game layout the app forced, per game; handed back when the last game ends. Also watches (and logs) Steam's
    layout choices while any game of the app runs."""

    WATCH_SECONDS = 1.0            # how often Steam's logs are looked at while a game runs
    RETRY_SECONDS = 2.0            # between two requests for the game layout
    MAX_TRIES = 8                  # requests in a row that Steam did not answer with the game layout: give up
    ACK_SECONDS = 3.0              # how long Steam may take to switch to a forced layout (before the next link)
    STUCK_CHORD_SECONDS = 1.5      # the Steam button let go, its chord layout still Steam's that long: left on

    def __init__(self, marker: Path, opener=None, root: Callable[[], Path | None] | None = None,
                 steam_up: Callable[[], bool] | None = None):
        self.marker = marker
        self.games: dict[int, int] = {}            # forced: game → its layout (the last one's is Steam's now)
        self.watched: set[int] = set()             # every running game of the app (Steam's choices are logged)
        if opener is None:
            from ezgd.platform import steam
            opener = steam.open_steam_url
            steam_up = steam_up or (lambda: steam.is_steam_running())
        self._open = opener
        # Steam runs: links go to it. When it does not, they are not sent — ``steam -silent <link>`` starts Steam,
        # and a Steam that quit has forgotten any forced layout anyway (None: always sent)
        self._steam_up = steam_up
        self._root = root                  # Steam's folder (its controller logs); None: no watching, no waiting
        self._wake = threading.Event()
        self._watcher: threading.Thread | None = None
        self._appid = GAME_LAYOUT
        self._lock = threading.Lock()      # the core and the watcher both talk to Steam
        self._tries = 0
        self._next_try = 0.0
        self._generation = 0               # bumped by every start: a hand-back under way must not undo it
        self._from: tuple[int, int] | None = None    # where the next watcher starts reading Steam's two logs
        self._held: tuple[int, int] | None = None    # (game, app) whose layout is kept while the quick menu is open
        self._hold_token = 0               # bumped by every hold: a release meant for an earlier opening is ignored

    @property
    def active(self) -> bool:
        """A game layout is forced (also only while the quick menu is open)."""
        return bool(self.games) or self._held is not None

    @property
    def held(self) -> tuple[int, int] | None:
        return self._held

    def hold(self, entry_id: int, appid: int, wait: float = 1.5) -> int:
        """The quick menu is about to take the focus over a game Steam knows: keep its layout ``appid`` (forced) until
        :meth:`let_go`. Returns once Steam switched (at most ``wait`` s), with a token for let_go (0: nothing to do —
        a game layout is forced already)."""
        with self._lock:
            self._hold_token += 1
            if self.games:
                return 0
            token = self._hold_token
            again = self._held is not None and self._held[1] == appid
            self._held = (entry_id, appid)
            if again:
                return token                   # (opened again before the last release: still held)
            root = self._root() if self._root else None
            since = log_size(root)
            self._remember(appid)
            self._ask(appid)
        if root is not None:
            took(root, since, appid, wait=wait, step=0.03)
        return token

    def let_go(self, token: int, running: bool, others: bool = False) -> bool:
        """The quick menu is gone (``token`` from its :meth:`hold`). The game still ``running``: Steam follows the focus
        again from the next change (``…/0`` keeps the layout until then — the game's own, it has the focus back).
        Ended meanwhile: handed back as for a forced game (``others``: games of the app still run)."""
        with self._lock:
            if token != self._hold_token or self._held is None:
                return False                   # (opened again since, or handed back already)
            self._held = None
            if self.games:
                return False                   # a game started meanwhile is forced: its layout stays
            if running:
                self._ask(0)
                self._forget()
                return True
        self.hand_back(desktop=not others)
        return True

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
        """A game started: layout ``appid`` is forced (and kept) until it ends."""
        first = not self.games
        self._mark()                       # (Steam's answer to the link below is logged too)
        with self._lock:
            self._generation += 1
            self.games.pop(entry_id, None)
            self.games[entry_id] = unsigned(appid)
            if first:
                self._remember(appid)
            self._switch(unsigned(appid))
        self.watch(entry_id)

    def _switch(self, appid: int) -> None:
        """(Under the lock.) Steam is asked for ``appid``, the layout the app keeps from now on."""
        self._appid = appid
        self._tries = 0
        self._next_try = 0.0
        self._ask(appid)

    def _current(self) -> int | None:
        return next(reversed(self.games)) if self.games else None

    def retarget(self, entry_id: int, appid: int) -> bool:
        """Another layout for a forced game (the one asked for made no gamepad). Asked for at once when that game's
        is Steam's now; False when it is not (the game ended, or another started meanwhile)."""
        with self._lock:
            if entry_id not in self.games:
                return False
            self.games[entry_id] = unsigned(appid)
            if self._current() != entry_id:
                return False
            self._switch(unsigned(appid))
            return True

    def prefer(self, entry_id: int) -> None:
        """The player is back in this game (continued from the quick menu): its own layout again, when another
        game's is Steam's now."""
        with self._lock:
            appid = self.games.pop(entry_id, None)
            if appid is None:
                return
            self.games[entry_id] = appid               # (now the last: the game being played)
            if appid != self._appid:
                self._switch(appid)

    def _running(self) -> bool:
        return self._watcher is not None and self._watcher.is_alive()

    def _mark(self) -> None:
        """Where the watcher starts reading Steam's logs (set before the first link, so its answer is read)."""
        if self._root is not None and not self._running():
            root = self._root()
            self._from = (log_size(root), log_size(root, "controller_ui.txt")) if root is not None else None

    def watch(self, entry_id: int) -> None:
        """A game of the app runs (forced or not): Steam's layout choices go to the log while it does."""
        self.watched.add(entry_id)
        if self._root is not None and not self._running():
            if self._from is None:
                self._mark()
            self._watcher = threading.Thread(target=self._watch, name="steam-input-watch", daemon=True)
            self._watcher.start()

    def unwatch(self, entry_id: int) -> None:
        self.watched.discard(entry_id)
        self._wake.set()

    def _up(self) -> bool:
        try:
            return self._steam_up is None or bool(self._steam_up())
        except Exception:
            return True

    def _ask(self, appid: int) -> None:
        if not self._up():
            log.info("Steam is not running: the controller layout (app %s) is not asked for", appid)
            return
        try:
            self._open(url(appid))
        except Exception:
            log.exception("could not ask Steam for the controller layout (app %s)", appid)

    def _watch(self) -> None:
        root = self._root() if self._root else None
        if root is None:
            return
        pos, fpos = self._from or (log_size(root), log_size(root, "controller_ui.txt"))
        self._from = None
        last, said = self._appid if self.games else None, None
        stuck_from: float | None = None
        while self.watched or self.games:
            self._wake.wait(self.WATCH_SECONDS)
            self._wake.clear()
            why, released, fpos = read_ui(root, fpos)
            got, pos = read_activations(root, pos)
            for reason in why:
                log.info("Steam controller layout: %s", reason)
            for _c, app in got:
                if app != said:
                    said = app
                    log.info("Steam switched the controller to the %s layout", name(app))
            if got:
                last = got[-1][1]
                stuck_from = None
                if last == self._appid:
                    self._tries = 0
            if last == CHORD and released and stuck_from is None:
                stuck_from = time.monotonic()
            if last == CHORD and stuck_from is not None and time.monotonic() - stuck_from >= self.STUCK_CHORD_SECONDS:
                # The Steam button was let go, but Steam stayed on its chord layout — which takes the buttons (the
                # sticks still reach the game): a friend's Steam Controller "half worked" after the quick menu until
                # the Steam button was pressed again (2026-10-29; cause not confirmed — this line says when it is)
                stuck_from = None
                with self._lock:
                    if self.games:
                        log.warning("Steam kept its Steam-button layout after the button was let go (the buttons "
                                    "would do nothing in the game) — asking for the game's layout again")
                        self._ask(self._appid)
                    else:
                        log.warning("Steam kept its Steam-button layout after the button was let go (the buttons "
                                    "do nothing in the game until it is pressed again)")
            if not self.games or last not in (DESKTOP, BIG_PICTURE):
                continue                   # (the chord while the button is held; a game Steam knows)
            now = time.monotonic()
            if self._tries >= self.MAX_TRIES or now < self._next_try:
                continue
            self._tries += 1
            self._next_try = now + self.RETRY_SECONDS
            log.info("Steam lost the forced game layout while a game runs (Steam restarted?) — asking again")
            with self._lock:
                if self.games:
                    self._ask(self._appid)

    def end(self, entry_id: int, desktop: bool = True) -> bool:
        """The game ended. True when it was the last forced one: the layout is handed back (``desktop``: no game of
        the app is left, so the desktop layout is switched to first — see :meth:`hand_back`)."""
        self.unwatch(entry_id)
        if self._held is not None and self._held[0] == entry_id:
            self._held = None                  # the game the quick menu kept the layout for ended (quit from it)
            if not self.games:
                self.hand_back(desktop=desktop)
                return True
        with self._lock:
            if entry_id not in self.games:
                return False
            del self.games[entry_id]
            if self.games:                     # another game of the app still runs: its layout
                appid = self.games[self._current()]
                if appid != self._appid:
                    self._switch(appid)
                return False
        self.hand_back(desktop=desktop)
        return True

    def hand_back(self, desktop: bool = True, wait: bool = False) -> None:
        """Steam chooses the layout again. ``…/0`` alone keeps the forced game layout until an X11 focus change (the
        Steam Controller stayed a gamepad on the desktop), so with ``desktop`` the desktop layout is forced first and
        released once Steam switched to it. ``wait``: done before returning (the app is quitting), else in the
        background."""
        with self._lock:
            self.games.clear()
            self._held = None
            self._tries = 0
            self._next_try = 0.0
            generation = self._generation
            if not self._up():                     # (Steam quit: it forgot the forced layout — nothing to hand back)
                self._forget()
                return
            if not desktop:
                self._ask(0)
                self._forget()
                return
            root = self._root() if self._root else None
            since = log_size(root)
            self._ask(DESKTOP)

        def release() -> None:
            if root is not None:
                took(root, since, DESKTOP, wait=self.ACK_SECONDS)
            with self._lock:
                if self._generation != generation:
                    return                 # a game started meanwhile: its layout stays forced
                self._ask(0)
                self._forget()
        if wait:
            release()
        else:
            threading.Thread(target=release, name="steam-input-hand-back", daemon=True).start()

    def left_over(self) -> bool:
        """A layout forced by an app that did not end cleanly (its marker is still there)."""
        return self.marker.is_file()
