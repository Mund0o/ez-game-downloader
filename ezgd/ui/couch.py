"""Couch mode: a full-screen, controller-friendly library in the style of SteamOS's game mode.

* Takes over the whole screen (the main window is hidden while it is open).
* Controller (any Linux game controller), keyboard (arrows, Enter, Esc) and mouse all work.
* Home: the focused game's artwork fills the screen, with a big Play button and a shelf of your games.
  Library: every game in a grid. Downloads: what is downloading or installing.
* The big ✕ in the top-right corner leaves couch mode. If a game started from couch mode is still running it asks
  first, then closes that game completely (and checks it is really gone) before couch mode closes and the normal
  window comes back.

Everything is painted by this one widget (no desktop dialogs), so nothing can end up hidden behind it.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

from ezgd.ui import theme
from ezgd.ui.gamepad import Gamepads
from ezgd.ui.widgets.common import ToastHost, relative_time

log = logging.getLogger(__name__)

TABS = ["Home", "Library", "Downloads"]
X_INDEX = len(TABS)                       # the ✕ is the last item of the top bar
STATUS_TEXT = {"downloaded": "Not installed yet", "extracting": "Installing", "extracted": "Needs setup",
               "installed": "Ready to play", "broken": "Files missing"}


def playtime(seconds: int) -> str:
    if seconds < 60:
        return "Not played yet" if not seconds else "Played less than a minute"
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m} min played" if h else f"{m} min played"


@dataclass
class Overlay:
    title: str
    text: str = ""
    buttons: list[tuple[str, Callable[[], None], str]] = field(default_factory=list)   # (label, action, kind)
    focus: int = 0
    lines: list[tuple[str, str]] = field(default_factory=list)        # (label, value) facts
    busy: bool = False                                                 # "working…" (no buttons yet)
    cancel: Callable[[], None] | None = None                           # B / Esc
    tag: str = ""


class Images:
    """Full-resolution artwork for couch mode (the shared cover cache keeps small copies only)."""

    def __init__(self, owner: "CouchWindow"):
        self.owner = owner
        self.raw: dict[str, QImage | None] = {}
        self.scaled: dict[tuple, QPixmap] = {}
        self.pending: set[str] = set()

    def get(self, url: str | None) -> QImage | None:
        if not url:
            return None
        if url in self.raw:
            return self.raw[url]
        if url not in self.pending and len(self.pending) < 6:
            self.pending.add(url)
            b = self.owner.bridge
            b.call(b.core.cover(url), lambda data, u=url: self._loaded(u, data),
                   lambda _e, u=url: self._loaded(u, None), owner=self.owner)
        return None

    def _loaded(self, url: str, data: bytes | None) -> None:
        self.pending.discard(url)
        img = QImage.fromData(data) if data else QImage()
        if not img.isNull() and img.width() > 2560:
            img = img.scaledToWidth(2560, Qt.SmoothTransformation)
        self.raw[url] = None if img.isNull() else img
        self.owner.update()

    def fill(self, url: str | None, w: int, h: int, top: bool = False) -> QPixmap | None:
        """The image scaled to cover w×h (cropped), cached."""
        img = self.get(url)
        if img is None or w <= 0 or h <= 0:
            return None
        key = (url, w, h, top)
        pm = self.scaled.get(key)
        if pm is None:
            s = img.scaled(QSize(w, h), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = (s.width() - w) // 2
            y = 0 if top else (s.height() - h) // 2
            pm = QPixmap.fromImage(s.copy(x, y, w, h))
            if len(self.scaled) > 160:
                self.scaled.clear()
            self.scaled[key] = pm
        return pm

    def fit(self, url: str | None, w: int, h: int) -> QPixmap | None:
        img = self.get(url)
        if img is None or w <= 0 or h <= 0:
            return None
        key = (url, w, h, "fit")
        pm = self.scaled.get(key)
        if pm is None:
            pm = QPixmap.fromImage(img.scaled(QSize(w, h), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.scaled[key] = pm
        return pm


STARTING_WINDOW = 90.0                    # a game started this recently may still be on its way (Steam is slow)


class CouchWindow(QWidget):
    closed = Signal()

    def __init__(self, actions, entries: list, running: set[int]):
        super().__init__(None, Qt.Window)
        self.actions = actions
        self.bridge = actions.bridge
        self.setWindowTitle("EZ Games — Couch mode")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setMinimumSize(800, 500)
        self.reduce_motion = bool(actions.settings.get("reduce_motion"))
        self.images = Images(self)
        self.entries: list = []
        self.art: dict[int, dict] = {}
        self.running: set[int] = set(running)
        self.launched: set[int] = set()          # games started from couch mode (the ✕ closes these)
        self._launch_at: dict[int, float] = {}   # when each was started (monotonic)
        self.pending_kill: set[int] = set()      # left while these were still starting: the main window closes them
        self.extraction: dict[int, float] = {}
        self.downloads: list[dict] = []
        self.summary: dict = {}
        self.tab = 0
        self.zone = "main"                        # top | buttons | main
        self.top_index = 0
        self.button_index = 0
        self.index = {0: 0, 1: 0, 2: 0}           # focused item per tab
        self.overlay: Overlay | None = None
        self._exiting = False
        self._closing_watchdog: QTimer | None = None
        self._hit: list[tuple[QRectF, str, int]] = []
        self._scroll = {0: 0.0, 1: 0.0}           # animated: shelf x offset (Home), grid y offset (Library)
        self._scroll_target = {0: 0.0, 1: 0.0}
        self._focus_anim = 1.0
        self._bg = (None, None)                   # (previous hero url, current hero url)
        self._bg_t = 1.0
        self._paint_failed = False
        self._minimize_timer = QTimer(self)
        self._minimize_timer.setSingleShot(True)
        self._minimize_timer.timeout.connect(self._step_aside)
        self.toasts = ToastHost(self)
        self.toasts.reduce_motion = self.reduce_motion

        self._tick = QTimer(self)                 # animations (only runs while something moves)
        self._tick.setInterval(16)
        self._tick.timeout.connect(self._animate)
        self._focus_va = QVariantAnimation(self)
        self._focus_va.setDuration(170)
        self._focus_va.setEasingCurve(QEasingCurve.OutCubic)
        self._focus_va.valueChanged.connect(self._focus_value)
        self._bg_va = QVariantAnimation(self)
        self._bg_va.setDuration(420)
        self._bg_va.valueChanged.connect(self._bg_value)
        self._clock = QTimer(self)
        self._clock.setInterval(15_000)
        self._clock.timeout.connect(self.update)
        self._clock.start()
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_downloads)
        self._poll.start()

        self.pads = Gamepads(self)
        self.pads.action.connect(self._pad)
        self.pads.devices_changed.connect(self._pads_changed)
        self.pads.start()
        self.set_entries(entries)

    # ================================================================== data
    def set_entries(self, entries: list) -> None:
        """Library entries, most recently played first (Home) — the focused game stays focused."""
        cur = self._focused_entry()
        self.entries = sorted(entries, key=lambda e: (-(e.last_played_at or 0), -(e.added_at or 0), e.title.casefold()))
        if cur is not None:
            for tab in (0, 1):
                items = self._items(tab)
                pos = next((i for i, e in enumerate(items) if e.id == cur.id), None)
                if pos is not None:
                    self.index[tab] = pos
        for tab in (0, 1):
            n = len(self._items(tab))
            self.index[tab] = max(0, min(self.index[tab], n - 1))
        apps = [e.steam_app_id for e in entries if e.steam_app_id and e.steam_app_id not in self.art]
        if apps:
            self.bridge.call(self.bridge.core.steam_art(apps), self._art_loaded, owner=self)
        self._update_bg()
        self.update()

    def _art_loaded(self, art: dict) -> None:
        self.art.update({int(k): v for k, v in art.items()})
        self._update_bg()
        self.update()

    def _items(self, tab: int | None = None) -> list:
        tab = self.tab if tab is None else tab
        if tab == 0:
            return self.entries
        if tab == 1:
            return sorted(self.entries, key=lambda e: e.title.casefold())
        return []

    def _focused_entry(self):
        if self.tab not in (0, 1):
            return None
        items = self._items()
        i = self.index.get(self.tab, 0)
        return items[i] if 0 <= i < len(items) else None

    def capsule_url(self, e) -> str | None:
        return (self.art.get(e.steam_app_id) or {}).get("capsule") if e.steam_app_id else None

    def hero_url(self, e) -> str | None:
        a = self.art.get(e.steam_app_id) or {} if e.steam_app_id else {}
        return a.get("hero") or a.get("wide") or e.cover_url

    def ready(self, e) -> bool:
        return e.status.value == "installed" and bool(e.executable) and \
            (e.setup or {}).get("state") not in ("pending", "installing", "waiting_steam")

    def state_text(self, e) -> str:
        if e.id in self.running:
            return "Playing now"
        if e.status.value == "extracting" or e.id in self.extraction:
            return f"Installing {int(self.extraction.get(e.id, 0) * 100)}%"
        st = (e.setup or {}).get("state")
        if st == "pending":
            return "Needs installing"
        if st == "waiting_steam":
            return "Waiting for Steam"
        if e.status.value == "installed" and not e.executable:
            return "Needs setup"
        return STATUS_TEXT.get(e.status.value, e.status.value)

    # ---------------------------------------------------- from the main window
    def game_state(self, payload: dict) -> None:
        gid = payload.get("id")
        if gid is None:
            return
        if payload.get("running"):
            self.running.add(gid)
            if gid in self.launched and self.overlay is not None and self.overlay.tag == "starting":
                self.overlay = None
            if gid in self.launched:
                self._minimize_timer.start(2500)         # let the game have the screen
        else:
            self.running.discard(gid)
            if gid in self.launched:
                self._minimize_timer.stop()
                played = int(payload.get("played") or 0)
                self._come_back()
                e = next((x for x in self.entries if x.id == gid), None)
                if e is not None and played >= 60:
                    self.toasts.show("Welcome back", f"{e.title}: {playtime(played).replace(' played', '')} "
                                                     "this session.", "success")
        self.update()

    def extraction_state(self, state: dict) -> None:
        lid = state.get("library_id")
        if lid is None:
            return
        if state.get("status") == "running":
            self.extraction[lid] = float(state.get("progress") or 0)
        else:
            self.extraction.pop(lid, None)
        self.update()

    def _step_aside(self) -> None:
        """A game started from couch mode is running: get out of its way (couch mode comes back when it ends)."""
        if any(i in self.running for i in self.launched) and not self._exiting:
            self.showMinimized()

    def _come_back(self) -> None:
        if self._exiting:
            return
        if self.isMinimized() or not self.isFullScreen():
            self.showFullScreen()
        self.raise_()
        self.activateWindow()
        handle = self.windowHandle()
        if handle is not None:
            handle.requestActivate()

    def _poll_downloads(self) -> None:
        if not self.isVisible() or self.isMinimized():
            return
        self.bridge.call(self.bridge.core.summary(), self._summary, owner=self)
        if self.tab == 2:
            self.bridge.call(self.bridge.core.downloads_snapshot(), self._downloads, owner=self)

    def _summary(self, s: dict) -> None:
        self.summary = s
        self.update()

    def _downloads(self, items: list[dict]) -> None:
        order = {"downloading": 0, "metadata": 0, "checking": 1, "verifying": 1, "queued": 2, "paused": 3,
                 "failed": 4, "completed": 5, "cancelled": 6}
        items = [d for d in items if d.get("status") != "cancelled"]
        items.sort(key=lambda d: (order.get(d.get("status"), 9), -(d.get("updated_at") or 0)))
        self.downloads = items[:12]
        self.index[2] = max(0, min(self.index[2], len(self.downloads) - 1))
        self.update()

    # ============================================================== showing
    def open(self, screen=None) -> None:
        if screen is not None:
            self.setScreen(screen)
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.ActiveWindowFocusReason)
        if not self.entries:
            self.toasts.show("Your library is empty", "Download a game on the desktop first — it appears here.")
        n = self.pads.names()
        self.toasts.show("Couch mode", ("Controller: " + ", ".join(n) if n else "Use a controller, the arrow keys or "
                                        "the mouse.") + " Press ✕ (top right) to leave.")

    def _pads_changed(self, names: list) -> None:
        if names:
            self.toasts.show("Controller connected", ", ".join(names), "success")

    # ============================================================== input
    def _accepts_input(self) -> bool:
        """Never steer couch mode while a game is being played: only when it is the active window, or visible and
        no game is running at all (after a game ends the desktop may not hand focus back right away)."""
        if not self.isVisible() or self.isMinimized():
            return False
        if self.isActiveWindow():
            return True
        return not self.running

    def _pad(self, name: str) -> None:
        if not self._accepts_input():
            return
        modal = QApplication.activeModalWidget()
        if modal is not None and modal is not self:          # a desktop dialog: drive it like a keyboard
            from PySide6.QtGui import QKeyEvent
            key = {"up": Qt.Key_Backtab, "left": Qt.Key_Backtab, "down": Qt.Key_Tab, "right": Qt.Key_Tab,
                   "a": Qt.Key_Space, "b": Qt.Key_Escape}.get(name)
            target = QApplication.focusWidget() or modal
            if key is not None:
                for t in (QKeyEvent.KeyPress, QKeyEvent.KeyRelease):
                    QApplication.sendEvent(target, QKeyEvent(t, key, Qt.NoModifier))
            return
        self.command(name)

    def focusNextPrevChild(self, _next: bool) -> bool:        # noqa: N802 — Tab switches tabs here
        return False

    def resizeEvent(self, e) -> None:                         # noqa: N802
        super().resizeEvent(e)
        self.images.scaled.clear()
        self.toasts.relayout()

    def keyPressEvent(self, e) -> None:                       # noqa: N802 (Qt API)
        k = e.key()
        name = {Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
                Qt.Key_Return: "a", Qt.Key_Enter: "a", Qt.Key_Space: "a", Qt.Key_Escape: "b",
                Qt.Key_Backspace: "b", Qt.Key_I: "x", Qt.Key_Menu: "x", Qt.Key_PageUp: "lb",
                Qt.Key_PageDown: "rb", Qt.Key_Tab: "rb", Qt.Key_Backtab: "lb"}.get(k)
        if name is None:
            super().keyPressEvent(e)
            return
        self.command(name)

    def command(self, name: str) -> None:
        """One input, whatever it came from: up/down/left/right, a (confirm), b (back), x (details), lb/rb (tabs),
        start (go to ✕), select."""
        try:
            self._command(name)
        except Exception:
            log.exception("couch mode: handling %s failed", name)
        self.update()

    def _command(self, name: str) -> None:
        if self.overlay is not None:
            self._overlay_command(name)
            return
        if name in ("lb", "rb"):
            self._switch_tab((self.tab + (1 if name == "rb" else -1)) % len(TABS))
            return
        if name == "start":
            self.zone, self.top_index = "top", X_INDEX
            return
        if self.zone == "top":
            if name == "left":
                self.top_index = max(0, self.top_index - 1)
            elif name == "right":
                self.top_index = min(X_INDEX, self.top_index + 1)
            elif name == "down":
                self.zone = "main"
            elif name == "b":
                self.zone = "main"
            elif name == "a":
                if self.top_index == X_INDEX:
                    self.request_exit()
                else:
                    self._switch_tab(self.top_index)
                    self.zone = "main"
            return
        if self.zone == "buttons":
            n = len(self._hero_buttons())
            if name == "left":
                self.button_index = max(0, self.button_index - 1)
            elif name == "right":
                self.button_index = min(n - 1, self.button_index + 1)
            elif name in ("down", "b"):
                self.zone = "main"
            elif name == "up":
                self.zone, self.top_index = "top", self.tab
            elif name == "a":
                btns = self._hero_buttons()
                if 0 <= self.button_index < len(btns):
                    btns[self.button_index][1]()
            elif name == "x":
                self._details()
            return
        # main zone
        if self.tab == 2:
            n = len(self.downloads)
            if name == "up":
                if self.index[2] == 0 or not n:
                    self.zone, self.top_index = "top", self.tab
                else:
                    self.index[2] -= 1
            elif name == "down" and n:
                self.index[2] = min(n - 1, self.index[2] + 1)
            elif name == "b":
                self.zone, self.top_index = "top", X_INDEX
            return
        items = self._items()
        n = len(items)
        i = self.index[self.tab]
        cols = self._grid_cols() if self.tab == 1 else n or 1
        if name == "left" and n:
            self._move(max(0, i - 1))
        elif name == "right" and n:
            self._move(min(n - 1, i + 1))
        elif name == "up":
            if self.tab == 1 and i >= cols:
                self._move(i - cols)
            elif self.tab == 0 and n:
                self.zone, self.button_index = "buttons", 0
            else:
                self.zone, self.top_index = "top", self.tab
        elif name == "down" and self.tab == 1 and n:
            if i + cols < n:
                self._move(i + cols)
            elif i // cols < (n - 1) // cols:
                self._move(n - 1)
        elif name == "a" and n:
            self.primary(items[i])
        elif name == "x" and n:
            self._details()
        elif name == "b":
            self.zone, self.top_index = "top", X_INDEX

    def _switch_tab(self, tab: int) -> None:
        if tab == self.tab:
            return
        self.tab = tab
        self.top_index = tab
        self.zone = "main"
        if tab == 2:
            self.bridge.call(self.bridge.core.downloads_snapshot(), self._downloads, owner=self)
        self._update_bg()
        self._kick_focus()

    def _move(self, i: int) -> None:
        if i == self.index[self.tab]:
            return
        self.index[self.tab] = i
        self._update_bg()
        self._kick_focus()

    # ----------------------------------------------------------- mouse
    def _hit_at(self, pos) -> tuple[str, int] | None:
        for rect, kind, idx in reversed(self._hit):
            if rect.contains(QPointF(pos)):
                return kind, idx
        return None

    def mouseMoveEvent(self, e) -> None:                      # noqa: N802
        if self.overlay is not None:
            h = self._hit_at(e.position())
            if h and h[0] == "overlay" and h[1] != self.overlay.focus:
                self.overlay.focus = h[1]
                self.update()
            return
        h = self._hit_at(e.position())
        if not h:
            return
        kind, idx = h
        if kind == "tab" or kind == "x":
            self.zone, self.top_index = "top", idx
        elif kind == "button":
            self.zone, self.button_index = "buttons", idx
        elif kind == "row":
            self.zone = "main"
            self.index[2] = idx
        self.update()

    def mousePressEvent(self, e) -> None:                     # noqa: N802
        if e.button() == Qt.BackButton:
            self.command("b")
            return
        if e.button() != Qt.LeftButton:
            return
        h = self._hit_at(e.position())
        if self.overlay is not None:
            if h and h[0] == "overlay":
                self.overlay.focus = h[1]
                self._overlay_command("a")
            elif h and h[0] == "x":
                self.request_exit()
            self.update()
            return
        if not h:
            return
        kind, idx = h
        try:
            if kind == "x":
                self.request_exit()
            elif kind == "tab":
                self._switch_tab(idx)
            elif kind == "button":
                btns = self._hero_buttons()
                if 0 <= idx < len(btns):
                    btns[idx][1]()
            elif kind == "tile":
                # the first click selects (the shelf moves), a click on the selected game plays it
                items = self._items()
                if self.zone == "main" and self.index[self.tab] == idx and 0 <= idx < len(items):
                    self.primary(items[idx])
                else:
                    self.zone = "main"
                    self._move(idx)
        except Exception:
            log.exception("couch mode: click failed")
        self.update()

    def wheelEvent(self, e) -> None:                          # noqa: N802
        if self.overlay is not None:
            return
        dy = e.angleDelta().y() or e.angleDelta().x()
        if not dy:
            return
        if self.tab == 0:
            self.command("left" if dy > 0 else "right")
        elif self.tab == 1:
            self.command("up" if dy > 0 else "down")
        elif self.tab == 2:
            self.command("up" if dy > 0 else "down")

    # ============================================================== actions
    def _hero_buttons(self) -> list[tuple[str, Callable[[], None], str]]:
        e = self._focused_entry()
        if e is None:
            return []
        if e.id in self.running:
            first = ("■  STOP", lambda e=e: self.ask_stop(e), "stop")
        elif self.ready(e):
            first = ("▶  PLAY", lambda e=e: self.play(e), "play")
        else:
            first = ("ⓘ  " + self.state_text(e).upper(), lambda e=e: self.explain(e), "muted")
        return [first, ("DETAILS", self._details, "ghost")]

    def primary(self, e) -> None:
        if e.id in self.running:
            self.ask_stop(e)
        elif self.ready(e):
            self.play(e)
        else:
            self.explain(e)

    def explain(self, e) -> None:
        st = self.state_text(e)
        if st.startswith("Installing"):
            text = "The game is being unpacked. It can be played as soon as that has finished."
        elif st == "Needs installing":
            text = "This is a repack: its installer has to run first. Leave couch mode and press “Install game” in " \
                   "the Library."
        elif st == "Files missing":
            text = "The game's files were not found on disk. Check them in the Library on the desktop."
        else:
            text = "Choose the game's executable in the Library on the desktop first (Set up). After that it can be " \
                   "played from here."
        self.overlay = Overlay(e.title, f"{st}. {text}", [("OK", self._close_overlay, "primary"),
                                                          ("Leave couch mode", self.request_exit, "ghost")],
                               cancel=self._close_overlay)

    def play(self, e) -> None:
        if e.id in self.running:
            return
        self.overlay = Overlay(f"Starting {e.title}…", "The game opens in a moment.", busy=True, tag="starting",
                               cancel=self._close_overlay)

        def confirm(entry, plan, proceed) -> None:
            def go() -> None:
                self.overlay = Overlay(f"Starting {entry.title}…", "The game opens in a moment.", busy=True,
                                       tag="starting", cancel=self._close_overlay)
                self.update()
                proceed()
            lines = [("Runs with", plan.description)]
            if not entry.executable_trusted:
                lines.append(("First start", "This file has not been started by the app before. Only play games "
                                             "you trust."))
            def cancel() -> None:
                self._launch_at.pop(entry.id, None)           # not started after all
                self._close_overlay()
            self.overlay = Overlay(f"Play {entry.title}?", "", [("▶  PLAY", go, "play"), ("Cancel", cancel, "ghost")],
                                   lines=lines + [("File", entry.executable or "")], cancel=cancel)
            self.update()

        def started(entry) -> None:
            self.launched.add(entry.id)
            self._launch_at[entry.id] = time.monotonic()
            if self._exiting:
                return
            QTimer.singleShot(20_000, lambda i=entry.id: self._clear_starting(i))

        def failed(_err) -> None:
            self._launch_at.pop(e.id, None)
            if self._exiting:
                return
            if self.overlay is not None and self.overlay.tag == "starting":
                self.overlay = None
            self.update()
        self._launch_at[e.id] = time.monotonic()          # from now on the ✕ treats it as (about to be) running
        self.actions.launch(self, e, confirm=confirm, couch=True, started=started, failed=failed)
        self.update()

    def _clear_starting(self, entry_id: int) -> None:
        if self._exiting:
            return
        if self.overlay is not None and self.overlay.tag == "starting":
            self.overlay = None
            if entry_id not in self.running:
                self.toasts.show("The game has not shown up yet", "If nothing opens, check it in the Library on the "
                                 "desktop (Diagnose).", "warning")
            self.update()

    def ask_stop(self, e) -> None:
        self.overlay = Overlay(f"Quit {e.title}?", "The game is closed like with Stop in the Library — progress "
                               "the game has not saved is lost.",
                               [("Quit game", lambda e=e: self._stop([e.id], then_exit=False), "danger"),
                                ("Cancel", self._close_overlay, "ghost")], focus=1, cancel=self._close_overlay)

    def _details(self) -> None:
        e = self._focused_entry()
        if e is None:
            return
        lines = [("Status", self.state_text(e)), ("Play time", playtime(e.playtime_seconds).replace(" played", "")),
                 ("Last played", relative_time(e.last_played_at) if e.last_played_at else "Never"),
                 ("Launches", str(e.play_count)), ("From", theme.source_name(e.source) if e.source else "Imported")]
        rel = e.release or {}
        if rel.get("version"):
            v = rel["version"]
            if rel.get("status") == "update" and rel.get("latest"):
                v += f"  →  {rel['latest']} available (update on the desktop)"
            lines.append(("Version", v))
        buttons = [("▶  PLAY", lambda e=e: (self._close_overlay(), self.play(e)), "play")] if self.ready(e) and \
            e.id not in self.running else []
        if e.id in self.running:
            buttons.append(("■  STOP", lambda e=e: self.ask_stop(e), "stop"))
        buttons += [("Back up saves", lambda e=e: self._backup(e), "ghost"), ("Close", self._close_overlay, "ghost")]
        self.overlay = Overlay(e.title, "", buttons, lines=lines, cancel=self._close_overlay, tag="details")
        self.bridge.call(self.bridge.core.library_call("save_backups", e.id),
                         lambda r, e=e: self._details_saves(e, r), lambda _err: None, owner=self)

    def _details_saves(self, e, r: dict) -> None:
        if self.overlay is None or self.overlay.tag != "details" or self.overlay.title != e.title:
            return
        b = r.get("backups") or []
        text = f"{len(b)} backup{'s' if len(b) != 1 else ''}, newest {relative_time(b[0]['created'])}" if b else \
            ("None yet — made automatically after you play" if r.get("supported") else "Not available on this system")
        self.overlay.lines.append(("Save backups", text))
        self.update()

    def _backup(self, e) -> None:
        self.toasts.show("Backing up saves…", e.title)

        def done(b) -> None:
            self.toasts.show("Saves backed up" if b else "Nothing new to back up",
                             f"{e.title}: {b['files']} file(s)." if b else e.title, "success" if b else "info")
        self.bridge.call(self.bridge.core.library_call("backup_saves", e.id, "manual"), done,
                         lambda err: self.toasts.show("Could not back up", str(err), "error"), owner=self)

    def _close_overlay(self) -> None:
        self.overlay = None
        self.update()

    def _overlay_command(self, name: str) -> None:
        o = self.overlay
        if o is None:
            return
        if o.busy:
            if name == "b" and o.cancel is not None and o.tag == "starting":
                o.cancel()
            return
        n = len(o.buttons)
        if name in ("left", "up"):
            o.focus = max(0, o.focus - 1)
        elif name in ("right", "down"):
            o.focus = min(n - 1, o.focus + 1)
        elif name == "a" and n:
            o.buttons[o.focus][1]()
        elif name == "b":
            (o.cancel or self._close_overlay)()

    # ============================================================== leaving
    def request_exit(self) -> None:
        """The ✕: leave couch mode. Games started from couch mode are closed first (after asking)."""
        if self._exiting or (self.overlay is not None and self.overlay.tag == "closing"):
            return
        playing_ids = sorted(i for i in self.launched if i in self.running)
        starting_ids = self._starting_ids()
        if not playing_ids and not starting_ids:
            self._finish_exit()
            return
        names = ", ".join(e.title for e in self.entries if e.id in set(playing_ids) | starting_ids) or "the game"
        text = "Leaving couch mode closes the game you started here — progress it has not saved is lost."
        if starting_ids and not playing_ids:
            text = "It is still starting — it is closed as soon as it appears, then couch mode closes."
        self._come_back()
        self.overlay = Overlay(f"Quit {names}?", text,
                               [("Quit game and leave", lambda ids=playing_ids: self._stop(ids, then_exit=True),
                                 "danger"), ("Cancel", self._close_overlay, "ghost")], focus=1,
                               cancel=self._close_overlay, tag="exit")
        self.update()

    def _starting_ids(self) -> set[int]:
        """Games started from couch mode a moment ago that are not running yet (Steam can take a while)."""
        now = time.monotonic()
        return {i for i, t in self._launch_at.items() if i not in self.running and now - t < STARTING_WINDOW}

    def _stop(self, ids: list[int], then_exit: bool) -> None:
        if not ids:
            if then_exit:
                self._finish_exit(close_starting=True)
            return
        titles = ", ".join(e.title for e in self.entries if e.id in ids) or "the game"
        self.overlay = Overlay(f"Closing {titles}…", "Asking the game to quit; whatever is left of it after a few "
                               "seconds is closed by force.", busy=True, tag="closing")
        self.update()
        results: dict[int, dict] = {}
        if self._closing_watchdog is not None:
            self._closing_watchdog.stop()
        self._closing_watchdog = QTimer(self)
        self._closing_watchdog.setSingleShot(True)
        self._closing_watchdog.timeout.connect(lambda: self._stop_finished(ids, results, then_exit, timed_out=True))
        self._closing_watchdog.start(45_000)

        def one(i: int, r) -> None:
            results[i] = r if isinstance(r, dict) else {"stopped": False, "left": [], "error": str(r)}
            if len(results) == len(ids):
                self._stop_finished(ids, results, then_exit)
        for i in ids:
            self.bridge.call(self.bridge.core.library_call("stop_completely", i),
                             lambda r, i=i: one(i, r), lambda err, i=i: one(i, err), owner=self)

    def _stop_finished(self, ids: list[int], results: dict, then_exit: bool, timed_out: bool = False) -> None:
        if self._closing_watchdog is not None:
            self._closing_watchdog.stop()
            self._closing_watchdog = None
        if self.overlay is None or self.overlay.tag != "closing":
            return                                   # already handled (watchdog vs. answer)
        failed = [i for i in ids if not (results.get(i) or {}).get("stopped")]
        for i in ids:
            if i not in failed:
                self.running.discard(i)
                self.launched.discard(i)
        if not failed:
            self.overlay = None
            if then_exit:
                self._finish_exit(close_starting=True)
            else:
                self.toasts.show("Game closed", "", "success")
            self.update()
            return
        names = ", ".join(e.title for e in self.entries if e.id in failed) or "The game"
        left = sorted({p for i in failed for p in (results.get(i) or {}).get("left", [])})
        why = "it is still closing after 45 seconds" if timed_out else \
            (f"process{'es' if len(left) != 1 else ''} {', '.join(map(str, left[:6]))} would not stop" if left
             else (results.get(failed[0]) or {}).get("error") or "it did not stop")
        buttons = [("Try again", lambda: self._stop(failed, then_exit), "danger")]
        if then_exit:
            buttons.append(("Leave anyway", self._finish_exit, "ghost"))
        buttons.append(("Cancel", self._close_overlay, "ghost"))
        self.overlay = Overlay(f"{names} could not be closed completely", f"{why[0].upper()}{why[1:]}.", buttons,
                               cancel=self._close_overlay)
        self.update()

    def _finish_exit(self, close_starting: bool = False) -> None:
        """Close couch mode. ``close_starting``: the user chose to quit the games started here — those still on
        their way are handed to the main window, which closes them when they appear."""
        if close_starting:
            self.pending_kill = self._starting_ids()
        self._exiting = True
        self.overlay = None
        self.close()

    def closeEvent(self, e) -> None:                          # noqa: N802
        if not self._exiting:
            e.ignore()                     # Alt+F4 / the window manager: the same as the ✕ (asks if a game runs)
            self.request_exit()
            return
        self._shutdown()
        e.accept()
        self.closed.emit()

    def force_close(self) -> None:
        """The app is quitting: close without asking (games keep running, as when the app quits normally)."""
        self._exiting = True
        self.close()

    def _shutdown(self) -> None:
        for t in (self._tick, self._clock, self._poll, self._minimize_timer):
            t.stop()
        if self._closing_watchdog is not None:
            self._closing_watchdog.stop()
        self._focus_va.stop()
        self._bg_va.stop()
        try:
            self.pads.stop()
        except Exception:
            log.exception("couch mode: closing the controllers failed")

    # ============================================================== animation
    def _kick_focus(self) -> None:
        if self.reduce_motion:
            self._focus_anim = 1.0
        else:
            self._focus_va.stop()
            self._focus_va.setStartValue(0.0)
            self._focus_va.setEndValue(1.0)
            self._focus_va.start()
        if not self._tick.isActive():
            self._tick.start()

    def _focus_value(self, v) -> None:
        self._focus_anim = float(v)
        self.update()

    def _bg_value(self, v) -> None:
        self._bg_t = float(v)
        self.update()

    def _update_bg(self) -> None:
        e = self._focused_entry()
        url = self.hero_url(e) if e is not None else None
        if url == self._bg[1]:
            return
        self._bg = (self._bg[1], url)
        if self.reduce_motion:
            self._bg_t = 1.0
        else:
            self._bg_va.stop()
            self._bg_va.setStartValue(0.0)
            self._bg_va.setEndValue(1.0)
            self._bg_va.start()

    def _animate(self) -> None:
        moving = False
        for k in (0, 1):
            cur, tgt = self._scroll[k], self._scroll_target[k]
            if abs(cur - tgt) > 0.5:
                self._scroll[k] = tgt if self.reduce_motion else cur + (tgt - cur) * 0.22
                moving = True
            else:
                self._scroll[k] = tgt
        if moving:
            self.update()
        elif self._focus_va.state() != QVariantAnimation.Running:
            self._tick.stop()

    # ============================================================== painting
    def _s(self) -> float:
        return max(0.55, min(2.2, self.height() / 1080))

    def _font(self, px: float, weight: int = 400) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(max(9, int(px)))
        f.setWeight(QFont.Weight(weight))
        return f

    def paintEvent(self, _e) -> None:                         # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setRenderHint(QPainter.TextAntialiasing)
            self._hit = []
            p.setBrush(Qt.NoBrush)
            self._paint_background(p)
            if self.tab == 0:
                self._paint_home(p)
            elif self.tab == 1:
                self._paint_library(p)
            else:
                self._paint_downloads(p)
            self._paint_top(p)
            self._paint_bottom(p)
            if self.overlay is not None:
                p.setBrush(Qt.NoBrush)
                self._paint_overlay(p)
            self._paint_failed = False
        except Exception:
            if not self._paint_failed:
                log.exception("couch mode: drawing failed")
            self._paint_failed = True
            # never a blank screen without a way out: a plain page with the ✕
            p.fillRect(self.rect(), QColor(theme.CURRENT.bg0))
            self._hit = []
            try:
                self._paint_x(p)
                p.setPen(QColor("white"))
                p.setFont(self._font(22))
                p.drawText(self.rect(), Qt.AlignCenter, "Something went wrong drawing couch mode.\nPress ✕ (top right) "
                           "or Esc to leave.")
            except Exception:
                pass
        finally:
            p.end()

    def _paint_background(self, p: QPainter) -> None:
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor(theme.CURRENT.bg0))
        glow = QRadialGradient(QPointF(W * 0.2, H * 0.1), W * 0.9)
        acc = QColor(theme.CURRENT.accent)
        acc.setAlpha(70)
        glow.setColorAt(0, acc)
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), QBrush(glow))
        if self.tab in (0, 1):
            prev, cur = self._bg
            if prev and self._bg_t < 1.0:
                pm = self.images.fill(prev, W, H, top=True)
                if pm is not None:
                    p.setOpacity(1.0 - self._bg_t)
                    p.drawPixmap(0, 0, pm)
            pm = self.images.fill(cur, W, H, top=True)
            if pm is not None:
                p.setOpacity(self._bg_t if prev else 1.0)
                p.drawPixmap(0, 0, pm)
            p.setOpacity(1.0)
        shade = QLinearGradient(0, 0, 0, H)
        shade.setColorAt(0.0, QColor(4, 5, 9, 150))
        shade.setColorAt(0.45, QColor(4, 5, 9, 150 if self.tab == 0 else 205))
        shade.setColorAt(1.0, QColor(4, 5, 9, 245))
        p.fillRect(self.rect(), QBrush(shade))
        if self.tab == 0:
            side = QLinearGradient(0, 0, W * 0.65, 0)
            side.setColorAt(0, QColor(0, 0, 0, 170))
            side.setColorAt(1, QColor(0, 0, 0, 0))
            p.fillRect(self.rect(), QBrush(side))

    def _paint_top(self, p: QPainter) -> None:
        s, W = self._s(), self.width()
        h = 96 * s
        bar = QLinearGradient(0, 0, 0, h)
        bar.setColorAt(0, QColor(0, 0, 0, 150))
        bar.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, W, h), QBrush(bar))
        x = 56 * s
        p.setPen(QColor(theme.CURRENT.accent))
        p.setFont(self._font(26 * s, 800))
        p.drawText(QRectF(x, 0, 400 * s, h), Qt.AlignVCenter | Qt.AlignLeft, "EZ")
        x += QFontMetrics(p.font()).horizontalAdvance("EZ ") + 2 * s
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, 0, 400 * s, h), Qt.AlignVCenter | Qt.AlignLeft, "GAMES")
        x += QFontMetrics(p.font()).horizontalAdvance("GAMES") + 64 * s
        f = self._font(21 * s, 700)
        fm = QFontMetrics(f)
        p.setFont(f)
        for i, name in enumerate(TABS):
            tw = fm.horizontalAdvance(name)
            r = QRectF(x - 16 * s, h / 2 - 26 * s, tw + 32 * s, 52 * s)
            focused = self.zone == "top" and self.top_index == i and self.overlay is None
            selected = self.tab == i
            if focused:
                path = QPainterPath()
                path.addRoundedRect(r, 26 * s, 26 * s)
                p.fillPath(path, QColor(255, 255, 255, 235))
                p.setPen(QColor(10, 12, 18))
            else:
                p.setPen(QColor(255, 255, 255, 255 if selected else 150))
            p.drawText(r, Qt.AlignCenter, name)
            if selected and not focused:
                p.fillRect(QRectF(x, r.bottom() - 2 * s, tw, 3 * s), QColor(theme.CURRENT.accent))
            self._hit.append((r, "tab", i))
            x += tw + 44 * s
        # right side: downloads, clock, the ✕
        right = W - 120 * s
        p.setFont(self._font(22 * s, 600))
        p.setPen(QColor(255, 255, 255, 220))
        clock = time.strftime("%H:%M")
        cw = QFontMetrics(p.font()).horizontalAdvance(clock)
        p.drawText(QRectF(right - cw, 0, cw, h), Qt.AlignVCenter | Qt.AlignRight, clock)
        active = int(self.summary.get("active") or 0) + int(self.summary.get("extracting") or 0)
        if active:
            from ezgd.core.fsutil import format_speed
            txt = f"↓ {format_speed(self.summary.get('speed') or 0)}" if self.summary.get("active") else "Installing…"
            p.setFont(self._font(17 * s, 600))
            tw = QFontMetrics(p.font()).horizontalAdvance(txt)
            p.setPen(QColor(theme.CURRENT.success))
            p.drawText(QRectF(right - cw - 28 * s - tw, 0, tw, h), Qt.AlignVCenter | Qt.AlignRight, txt)
        self._paint_x(p)

    def _x_rect(self) -> QRectF:
        s = self._s()
        r = 34 * s
        c = QPointF(self.width() - 62 * s, 48 * s)
        return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)

    def _paint_x(self, p: QPainter) -> None:
        s = self._s()
        r = self._x_rect()
        focused = self.zone == "top" and self.top_index == X_INDEX and self.overlay is None
        hovered = r.contains(QPointF(self.mapFromGlobal(self.cursor().pos())))
        hot = focused or hovered
        if hot:
            for k in range(4, 0, -1):                       # glow
                g = QColor(theme.CURRENT.danger)
                g.setAlpha(28)
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawEllipse(r.adjusted(-k * 4 * s, -k * 4 * s, k * 4 * s, k * 4 * s))
        p.setPen(QPen(QColor(255, 255, 255, 230 if hot else 120), 2 * s))
        p.setBrush(QColor(theme.CURRENT.danger) if hot else QColor(20, 22, 30, 200))
        p.drawEllipse(r)
        pen = QPen(QColor("white"), 5 * s)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        k = r.width() * 0.22
        c = r.center()
        p.drawLine(QPointF(c.x() - k, c.y() - k), QPointF(c.x() + k, c.y() + k))
        p.drawLine(QPointF(c.x() + k, c.y() - k), QPointF(c.x() - k, c.y() + k))
        p.setBrush(Qt.NoBrush)
        self._hit.append((r.adjusted(-6 * s, -6 * s, 6 * s, 6 * s), "x", X_INDEX))
        if focused:
            p.setPen(QColor(255, 255, 255, 230))
            p.setFont(self._font(15 * s, 700))
            p.drawText(QRectF(r.left() - 160 * s, r.bottom() + 4 * s, r.width() + 160 * s, 24 * s),
                       Qt.AlignRight | Qt.AlignVCenter, "Leave couch mode")

    def _glyph(self, p: QPainter, x: float, y: float, letter: str, color: str, text: str) -> float:
        s = self._s()
        d = 30 * s
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        if letter in ("LB", "RB"):
            p.drawRoundedRect(QRectF(x, y - d / 2, d * 1.5, d), 8 * s, 8 * s)
            w = d * 1.5
        else:
            p.drawEllipse(QRectF(x, y - d / 2, d, d))
            w = d
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor("white"))
        p.setFont(self._font(14 * s, 800))
        p.drawText(QRectF(x, y - d / 2, w, d), Qt.AlignCenter, letter)
        p.setFont(self._font(18 * s, 600))
        p.setPen(QColor(255, 255, 255, 215))
        tw = QFontMetrics(p.font()).horizontalAdvance(text)
        p.drawText(QRectF(x + w + 10 * s, y - d / 2, tw + 4, d), Qt.AlignVCenter | Qt.AlignLeft, text)
        return x + w + 10 * s + tw + 36 * s

    def _paint_bottom(self, p: QPainter) -> None:
        s, W, H = self._s(), self.width(), self.height()
        h = 76 * s
        top = H - h
        p.fillRect(QRectF(0, top, W, h), QColor(0, 0, 0, 140))
        p.fillRect(QRectF(0, top, W, 1 * s), QColor(255, 255, 255, 30))
        y = top + h / 2
        x = 56 * s
        if self.overlay is not None:
            if not self.overlay.busy:
                x = self._glyph(p, x, y, "A", "#16a34a", "Select")
                x = self._glyph(p, x, y, "B", "#dc2626", "Back")
        else:
            e = self._focused_entry()
            if self.zone == "top":
                x = self._glyph(p, x, y, "A", "#16a34a", "Leave couch mode" if self.top_index == X_INDEX else "Open")
                x = self._glyph(p, x, y, "B", "#dc2626", "Back")
            elif self.tab in (0, 1) and e is not None:
                act = "Stop" if e.id in self.running else "Play" if self.ready(e) else "Why not?"
                x = self._glyph(p, x, y, "A", "#16a34a", act if self.zone == "main" else "Select")
                x = self._glyph(p, x, y, "Y", "#2563eb", "Details")
                x = self._glyph(p, x, y, "B", "#dc2626", "Exit…")
            else:
                x = self._glyph(p, x, y, "B", "#dc2626", "Exit…")
            x = self._glyph(p, x, y, "LB", "#475569", "")
            x = self._glyph(p, x - 30 * s, y, "RB", "#475569", "Switch")
        # right: the controllers
        names = self.pads.names()
        txt = ("🎮 " + names[0][:32] + (f" +{len(names) - 1}" if len(names) > 1 else "")) if names else \
            "Keyboard: arrows · Enter · Esc"
        p.setFont(self._font(16 * s, 500))
        p.setPen(QColor(255, 255, 255, 140))
        p.drawText(QRectF(W * 0.5, top, W * 0.5 - 56 * s, h), Qt.AlignVCenter | Qt.AlignRight, txt)

    # ------------------------------------------------------------- home
    def _pill(self, p: QPainter, r: QRectF, text: str, kind: str, focused: bool) -> None:
        s = self._s()
        colors = {"play": (theme.CURRENT.success, "white"), "stop": (theme.CURRENT.danger, "white"),
                  "danger": (theme.CURRENT.danger, "white"), "primary": (theme.CURRENT.accent, "white"),
                  "ghost": (QColor(255, 255, 255, 38), "white"), "muted": (QColor(255, 255, 255, 38), "white")}
        bg, fg = colors.get(kind, colors["ghost"])
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        if focused:
            for k in range(3, 0, -1):
                g = QColor(bg) if kind not in ("ghost", "muted") else QColor(255, 255, 255)
                g.setAlpha(40)
                gp = QPainterPath()
                rr = r.adjusted(-k * 3 * s, -k * 3 * s, k * 3 * s, k * 3 * s)
                gp.addRoundedRect(rr, rr.height() / 2, rr.height() / 2)
                p.fillPath(gp, g)
        c = QColor(bg)
        if kind in ("ghost", "muted") and focused:
            c = QColor(255, 255, 255, 235)
            fg = "#0b0d13"
        p.fillPath(path, c)
        if focused:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("white"), 3 * s))
            p.drawPath(path)
        p.setPen(QColor(fg))
        p.setFont(self._font(r.height() * 0.36, 800))
        p.drawText(r, Qt.AlignCenter, text)

    def _tile(self, p: QPainter, e, r: QRectF, focused: bool, idx: int) -> None:
        s = self._s()
        radius = 12 * s
        if focused:
            grow = 1.0 + 0.07 * self._focus_anim
            c = r.center()
            r = QRectF(c.x() - r.width() * grow / 2, c.y() - r.height() * grow / 2, r.width() * grow,
                       r.height() * grow)
            for k in range(5, 0, -1):
                g = QColor(theme.CURRENT.accent)
                g.setAlpha(26)
                gp = QPainterPath()
                gp.addRoundedRect(r.adjusted(-k * 4 * s, -k * 4 * s, k * 4 * s, k * 4 * s), radius + k * 4 * s,
                                  radius + k * 4 * s)
                p.fillPath(gp, g)
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.save()
        p.setClipPath(path)
        w, h = int(r.width()), int(r.height())
        cap = self.images.fill(self.capsule_url(e), w, h)
        if cap is not None:
            p.drawPixmap(r.topLeft(), cap)
        else:
            back = self.images.fill(e.cover_url, w, h)
            if back is not None:
                p.setOpacity(0.35)
                p.drawPixmap(r.topLeft(), back)
                p.setOpacity(1.0)
            else:
                g = QLinearGradient(r.topLeft(), r.bottomRight())
                g.setColorAt(0, QColor(theme.CURRENT.accent).darker(220))
                g.setColorAt(1, QColor(theme.CURRENT.bg2))
                p.fillRect(r, QBrush(g))
            front = self.images.fit(e.cover_url, int(r.width() - 16 * s), int(r.height() * 0.5))
            if front is not None:
                p.drawPixmap(QPointF(r.center().x() - front.width() / 2, r.top() + r.height() * 0.2), front)
            p.setPen(QColor("white"))
            p.setFont(self._font(max(13, r.width() * 0.085), 800))
            p.drawText(r.adjusted(12 * s, 0, -12 * s, -14 * s), Qt.AlignHCenter | Qt.AlignBottom | Qt.TextWordWrap,
                       e.title)
        ready = self.ready(e)
        if not ready and e.id not in self.running:
            p.fillRect(r, QColor(0, 0, 0, 110))
        p.restore()
        # badges
        badge = None
        if e.id in self.running:
            badge = ("● PLAYING", theme.CURRENT.success)
        elif (e.release or {}).get("status") == "update":
            badge = ("UPDATE", theme.CURRENT.warning)
        elif not ready:
            badge = (self.state_text(e).upper(), "#64748b")
        if badge:
            p.setFont(self._font(12 * s, 800))
            tw = QFontMetrics(p.font()).horizontalAdvance(badge[0]) + 16 * s
            br = QRectF(r.left() + 8 * s, r.top() + 8 * s, min(tw, r.width() - 16 * s), 24 * s)
            bp = QPainterPath()
            bp.addRoundedRect(br, 12 * s, 12 * s)
            p.fillPath(bp, QColor(badge[1]))
            p.setPen(QColor("white"))
            p.drawText(br, Qt.AlignCenter, badge[0])
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 255 if focused else 40), (3 if focused else 1) * s))
        p.drawPath(path)
        self._hit.append((r, "tile", idx))

    def _paint_home(self, p: QPainter) -> None:
        s, W, H = self._s(), self.width(), self.height()
        items = self._items(0)
        top_h, bot_h = 96 * s, 76 * s
        if not items:
            p.setPen(QColor("white"))
            p.setFont(self._font(40 * s, 800))
            p.drawText(QRectF(0, H * 0.32, W, 60 * s), Qt.AlignCenter, "No games yet")
            p.setFont(self._font(22 * s))
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(0, H * 0.32 + 70 * s, W, 40 * s), Qt.AlignCenter,
                       "Download a game on the desktop — it shows up here, ready to play.")
            return
        e = self._focused_entry()
        tile_h = min(H * 0.30, 330 * s)
        tile_w = tile_h * 2 / 3
        gap = 26 * s
        shelf_y = H - bot_h - 44 * s - tile_h
        # hero text
        left = 72 * s
        y = top_h + max(40 * s, (shelf_y - top_h) * 0.18)
        if e is not None:
            p.setFont(self._font(15 * s, 800))
            p.setPen(QColor(theme.CURRENT.accent).lighter(130))
            label_ = "PLAYING NOW" if e.id in self.running else ("CONTINUE PLAYING" if e.last_played_at else
                                                                  "IN YOUR LIBRARY")
            p.drawText(QRectF(left, y, W * 0.6, 24 * s), Qt.AlignLeft | Qt.AlignVCenter, label_)
            y += 30 * s
            title_font = self._font(min(76 * s, W * 0.05), 900)
            p.setFont(title_font)
            p.setPen(QColor("white"))
            tr = QRectF(left, y, W * 0.62, (shelf_y - y) * 0.45)
            bound = p.boundingRect(tr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, e.title)
            p.drawText(tr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, e.title)
            y += min(bound.height(), tr.height()) + 14 * s
            meta = [self.state_text(e), playtime(e.playtime_seconds)]
            if e.last_played_at:
                meta.append("last played " + relative_time(e.last_played_at))
            if e.source:
                meta.append(theme.source_name(e.source))
            p.setFont(self._font(21 * s, 500))
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(QRectF(left, y, W * 0.7, 30 * s), Qt.AlignLeft | Qt.AlignVCenter, "  ·  ".join(meta))
            y += 40 * s
            rel = e.release or {}
            if rel.get("status") == "update" and rel.get("latest"):
                p.setFont(self._font(17 * s, 700))
                p.setPen(QColor(theme.CURRENT.warning))
                p.drawText(QRectF(left, y, W * 0.7, 26 * s), Qt.AlignLeft | Qt.AlignVCenter,
                           f"Update available: {rel['latest']} (you have {rel.get('version')}) — update on the desktop")
                y += 34 * s
            y += 10 * s
            bx = left
            for i, (text, _fn, kind) in enumerate(self._hero_buttons()):
                bw = (260 if i == 0 else 200) * s
                r = QRectF(bx, y, bw, 68 * s)
                self._pill(p, r, text, kind, self.zone == "buttons" and self.button_index == i and
                           self.overlay is None)
                self._hit.append((r, "button", i))
                bx += bw + 22 * s
        # shelf
        p.setFont(self._font(17 * s, 800))
        p.setPen(QColor(255, 255, 255, 190))
        p.drawText(QRectF(left, shelf_y - 40 * s, 600 * s, 28 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   f"YOUR GAMES  ·  {len(items)}")
        i = self.index[0]
        # keep the focused tile in view: scroll so it sits at the left third when possible
        target = max(0.0, i * (tile_w + gap) - (W * 0.33 - left))
        maxoff = max(0.0, len(items) * (tile_w + gap) - gap - (W - 2 * left))
        self._scroll_target[0] = min(target, maxoff)
        if abs(self._scroll[0] - self._scroll_target[0]) > 0.5 and not self._tick.isActive():
            self._tick.start()
        off = self._scroll[0]
        focused_draw = None
        for k, ent in enumerate(items):
            x = left + k * (tile_w + gap) - off
            if x > W or x + tile_w < 0:
                continue
            r = QRectF(x, shelf_y, tile_w, tile_h)
            if k == i and self.zone == "main" and self.overlay is None:
                focused_draw = (ent, r, k)
                continue
            self._tile(p, ent, r, False, k)
        if focused_draw is not None:                         # drawn last: on top of its neighbours
            self._tile(p, focused_draw[0], focused_draw[1], True, focused_draw[2])
        elif 0 <= i < len(items):
            x = left + i * (tile_w + gap) - off
            r = QRectF(x, shelf_y, tile_w, tile_h)
            p.setPen(QPen(QColor(255, 255, 255, 110), 2 * s))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-4 * s, -4 * s, 4 * s, 4 * s), 14 * s, 14 * s)

    # ------------------------------------------------------------- library
    def _grid_geom(self) -> tuple[float, float, float, int]:
        s, W = self._s(), self.width()
        tile_h = min(self.height() * 0.27, 300 * s)
        tile_w = tile_h * 2 / 3
        gap = 30 * s
        left = 72 * s
        cols = max(1, int((W - 2 * left + gap) // (tile_w + gap)))
        return tile_w, tile_h, gap, cols

    def _grid_cols(self) -> int:
        return self._grid_geom()[3]

    def _paint_library(self, p: QPainter) -> None:
        s, W, H = self._s(), self.width(), self.height()
        items = self._items(1)
        tile_w, tile_h, gap, cols = self._grid_geom()
        top = 96 * s + 70 * s
        bottom = H - 76 * s
        left = (W - (cols * (tile_w + gap) - gap)) / 2
        p.setFont(self._font(34 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(left, 96 * s + 8 * s, W, 50 * s), Qt.AlignLeft | Qt.AlignVCenter, "Library")
        p.setFont(self._font(18 * s, 600))
        p.setPen(QColor(255, 255, 255, 160))
        ready = sum(1 for e in items if self.ready(e))
        p.drawText(QRectF(left + 170 * s, 96 * s + 8 * s, W, 50 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{len(items)} games · {ready} ready to play")
        if not items:
            return
        i = self.index[1]
        row = i // cols
        row_h = tile_h + gap + 18 * s
        view = bottom - top - 30 * s
        target = self._scroll_target[1]
        row_top, row_bottom = row * row_h, row * row_h + tile_h + 44 * s
        if row_top < target:
            target = row_top
        elif row_bottom - target > view:
            target = row_bottom - view
        self._scroll_target[1] = max(0.0, target)
        if abs(self._scroll[1] - self._scroll_target[1]) > 0.5 and not self._tick.isActive():
            self._tick.start()
        off = self._scroll[1]
        p.save()
        p.setClipRect(QRectF(0, top - 30 * s, W, bottom - top + 30 * s))
        focused_draw = None
        for k, e in enumerate(items):
            rr, cc = divmod(k, cols)
            x = left + cc * (tile_w + gap)
            y = top + rr * row_h - off
            if y > bottom or y + tile_h < top - 40 * s:
                continue
            r = QRectF(x, y, tile_w, tile_h)
            if k == i and self.zone == "main" and self.overlay is None:
                focused_draw = (e, r, k)
                continue
            self._tile(p, e, r, False, k)
        if focused_draw is not None:
            self._tile(p, *focused_draw[:2], True, focused_draw[2])
            e, r, _k = focused_draw
            p.setFont(self._font(17 * s, 700))
            p.setPen(QColor("white"))
            p.drawText(QRectF(r.left() - 30 * s, r.bottom() + 10 * s, r.width() + 60 * s, 26 * s), Qt.AlignCenter,
                       QFontMetrics(p.font()).elidedText(e.title, Qt.ElideRight, int(r.width() + 60 * s)))
        p.restore()

    # ------------------------------------------------------------- downloads
    def _paint_downloads(self, p: QPainter) -> None:
        from ezgd.core.fsutil import format_bytes, format_speed
        s, W, H = self._s(), self.width(), self.height()
        left = 96 * s
        y = 96 * s + 8 * s
        p.setFont(self._font(34 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(left, y, W, 50 * s), Qt.AlignLeft | Qt.AlignVCenter, "Downloads")
        y += 70 * s
        installing = [e for e in self.entries if e.id in self.extraction]
        rows = [("install", e) for e in installing] + [("dl", d) for d in self.downloads]
        if not rows:
            p.setFont(self._font(22 * s))
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(left, y + 40 * s, W - 2 * left, 40 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       "Nothing is downloading. Start downloads on the desktop — their progress shows here.")
            return
        row_h = 104 * s
        bottom = H - 76 * s - 10 * s
        sel = self.index[2]
        for k, (kind, d) in enumerate(rows):
            if y + row_h > bottom:
                break
            r = QRectF(left, y, W - 2 * left, row_h - 14 * s)
            path = QPainterPath()
            path.addRoundedRect(r, 16 * s, 16 * s)
            focused = self.zone == "main" and k == sel and self.overlay is None
            p.fillPath(path, QColor(255, 255, 255, 40 if focused else 18))
            if focused:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor("white"), 2 * s))
                p.drawPath(path)
            if kind == "install":
                title, status, frac, extra = d.title, "Installing", self.extraction.get(d.id, 0.0), ""
            else:
                title = d.get("title") or d.get("filename") or "Download"
                status = str(d.get("status") or "").capitalize()
                frac = float(d.get("progress") or 0)
                done, total = d.get("downloaded_bytes") or 0, d.get("total_bytes") or 0
                extra = f"{format_bytes(done)} of {format_bytes(total)}" if total else format_bytes(done)
                if d.get("status") == "downloading" and d.get("speed"):
                    extra += f"  ·  {format_speed(d['speed'])}"
                    if d.get("eta"):
                        m = int(d["eta"]) // 60
                        extra += f"  ·  {m // 60} h {m % 60} min left" if m >= 60 else f"  ·  {max(1, m)} min left"
            p.setPen(QColor("white"))
            p.setFont(self._font(22 * s, 700))
            p.drawText(QRectF(r.left() + 24 * s, r.top() + 10 * s, r.width() * 0.6, 34 * s),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(title, Qt.ElideRight, int(r.width() * 0.6)))
            p.setFont(self._font(17 * s, 600))
            p.setPen(QColor(255, 255, 255, 180))
            p.drawText(QRectF(r.left() + 24 * s, r.top() + 10 * s, r.width() - 48 * s, 34 * s),
                       Qt.AlignRight | Qt.AlignVCenter, f"{status}  ·  {frac * 100:.0f}%")
            p.drawText(QRectF(r.left() + 24 * s, r.top() + 44 * s, r.width() - 48 * s, 24 * s),
                       Qt.AlignLeft | Qt.AlignVCenter, extra)
            bar = QRectF(r.left() + 24 * s, r.bottom() - 18 * s, r.width() - 48 * s, 8 * s)
            bp = QPainterPath()
            bp.addRoundedRect(bar, 4 * s, 4 * s)
            p.fillPath(bp, QColor(255, 255, 255, 35))
            if frac > 0:
                fp = QPainterPath()
                fp.addRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * min(1.0, frac), bar.height()), 4 * s,
                                  4 * s)
                color = theme.CURRENT.success if (kind == "dl" and d.get("status") == "completed") else \
                    theme.CURRENT.danger if (kind == "dl" and d.get("status") == "failed") else theme.CURRENT.accent
                p.fillPath(fp, QColor(color))
            self._hit.append((r, "row", k))
            y += row_h

    # ------------------------------------------------------------- overlay
    def _paint_overlay(self, p: QPainter) -> None:
        o = self.overlay
        s, W, H = self._s(), self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 170))
        cw = min(W - 80 * s, 980 * s)
        f_title = self._font(36 * s, 900)
        f_text = self._font(21 * s, 500)
        f_line = self._font(19 * s, 500)
        inner = cw - 96 * s
        h = 56 * s
        p.setFont(f_title)
        th = p.boundingRect(QRectF(0, 0, inner, 1000), Qt.TextWordWrap, o.title).height()
        h += th + 16 * s
        if o.text:
            p.setFont(f_text)
            xh = p.boundingRect(QRectF(0, 0, inner, 1000), Qt.TextWordWrap, o.text).height()
            h += xh + 18 * s
        lh = []
        p.setFont(f_line)
        for k, v in o.lines:
            vh = p.boundingRect(QRectF(0, 0, inner - 220 * s, 1000), Qt.TextWordWrap, v).height()
            lh.append(max(30 * s, vh + 8 * s))
        h += sum(lh) + (12 * s if lh else 0)
        h += (88 * s if o.buttons else 60 * s) + 24 * s
        h = min(h, H - 80 * s)
        r = QRectF((W - cw) / 2, (H - h) / 2, cw, h)
        path = QPainterPath()
        path.addRoundedRect(r, 24 * s, 24 * s)
        p.fillPath(path, QColor(theme.CURRENT.bg2))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.5 * s))
        p.drawPath(path)
        x, y = r.left() + 48 * s, r.top() + 44 * s
        p.setFont(f_title)
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, y, inner, th), Qt.TextWordWrap, o.title)
        y += th + 16 * s
        if o.text:
            p.setFont(f_text)
            p.setPen(QColor(255, 255, 255, 205))
            p.drawText(QRectF(x, y, inner, xh), Qt.TextWordWrap, o.text)
            y += xh + 18 * s
        p.setFont(f_line)
        for (k, v), hh in zip(o.lines, lh):
            p.setPen(QColor(255, 255, 255, 140))
            p.drawText(QRectF(x, y, 210 * s, hh), Qt.AlignLeft | Qt.AlignTop, k)
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(QRectF(x + 220 * s, y, inner - 220 * s, hh), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, v)
            y += hh
        if o.busy:
            # a simple spinner: three dots that pulse
            t = time.monotonic()
            for k in range(3):
                a = 0.35 + 0.65 * max(0.0, math.sin(t * 5 - k * 0.7))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, int(255 * a)))
                p.drawEllipse(QPointF(x + 14 * s + k * 34 * s, r.bottom() - 52 * s), 9 * s, 9 * s)
            p.setBrush(Qt.NoBrush)
            if not self._tick.isActive():
                self._tick.start()
            QTimer.singleShot(60, self.update)
            return
        bx = x
        by = r.bottom() - 40 * s - 64 * s
        for k, (text, _fn, kind) in enumerate(o.buttons):
            p.setFont(self._font(64 * s * 0.36, 800))
            bw = max(200 * s, QFontMetrics(p.font()).horizontalAdvance(text) + 80 * s)
            br = QRectF(bx, by, bw, 64 * s)
            self._pill(p, br, text, kind, k == o.focus)
            self._hit.append((br, "overlay", k))
            bx += bw + 20 * s
        self._hit.append((self._x_rect(), "x", X_INDEX))
