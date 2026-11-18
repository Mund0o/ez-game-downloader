"""The quick menu: hold the guide button (PS / Xbox / Steam) while playing a game the app started — with "Our overlay
instead of Steam's" on (Settings); Steam's overlay stays in the game (its FPS counter) but never opens on that button
or Shift+Tab. Off, Steam's overlay is the one over the game and the quick menu does not show at all (the user's rule,
2026-10-28).

It slides in over the game — the game stays visible behind it — and offers: back to the game, switch to another
game (the one being played is suspended: frozen, using no processor or graphics time, and continues exactly where
it was when you come back), go to couch mode (suspending the game), the game's own volume, its start settings, and
quit. Only games the app runs (directly, or through Steam for the overlay) are ever touched; a game started in
Steam itself is not the app's and never shows up here.

The menu is its own see-through window kept above the game by the compositor (see window_control), drawn with
QPainter like couch mode, using couch mode's images, fonts and button prompts.

Behind it the game goes on, live. (0.6.1 to 0.6.15 laid a picture of the game, taken as the menu opened, over the
whole screen: the game looked frozen for as long as the menu was open — "it freezes the game, which feels weird",
the user, 2026-11-05.) The picture is only for games that minimize themselves the moment the menu takes the focus
(Dark Souls III in full screen), which would leave the desktop showing: KWin tells the app when the game's window
goes (window_control.watch_game_window); such a game gets its artwork behind the menu the first time and its
picture from then on.

Keyboard and mouse: arrows, Enter / Space, Esc / Backspace, Home / End, Page Up / Down, Tab, M (mute), / or Ctrl+F
(search friends), direct typing and Ctrl+V paste in chat and friend search; Shift+Tab opens and closes it (showing
the mouse pointer right away). One notch of the mouse wheel moves once (wheel_steps) — or steps volume over the
slider, scrolls chat history, and moves row by row in a multi-row game grid; the header's ← and ✕, the footer's
prompts, the volume slider + mute icon, friend-search clear ✕, and scrollbars all work with the mouse; a click on
the live game beside the panel returns to the game, and a right click (or Back mouse button) steps back. A key or
button still held as the menu closes is let go before the game gets the focus back — KWin hands a newly focused
window the keys being held, and the game then saw a fresh Esc (its own pause menu) or Enter.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable

import shiboken6
from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import (QBrush, QColor, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QTransform)
from PySide6.QtWidgets import QApplication, QWidget

from ezgd.ui import theme
from ezgd.ui.wheel_steps import WheelSteps
from ezgd.ui.window_control import OwnAppId, WindowControl

log = logging.getLogger(__name__)

TITLE = "EZ Games — Quick menu"
VOLUME_STEP = 10
PAGE = 5                         # Page Up / Down (and a controller's triggers): rows at a time
SPACEWAR = "480"                 # the app OnlineFix games sign in to Steam as (their invites are all "480")
HIDE_GRACE = 1.0                 # a game known to minimize: its picture stays this long unless KWin says it did not
RELEASE_WAIT = 1500              # ms the closing menu waits at most for the key / button that closed it to be let go

# games seen minimizing themselves as the menu took the focus (this run): their picture is taken before it opens
_MINIMIZERS: set[int] = set()
_watch_n = 0


ICONS = ("resume", "switch", "couch", "friends", "steamoverlay", "volume", "volume_low", "volume_muted",
         "mangohud", "settings", "quit")


def draw_icon(p: QPainter, key: str, r: QRectF, color: QColor, s: float) -> None:
    """Small line icons for the menu's actions (drawn, so they look the same with every font)."""
    p.save()
    pen = QPen(color, 2.4 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    x, y, w, h = r.left(), r.top(), r.width(), r.height()
    if key == "resume":                                    # ▶
        path = QPainterPath()
        path.moveTo(x + w * 0.22, y + h * 0.08)
        path.lineTo(x + w * 0.92, y + h * 0.5)
        path.lineTo(x + w * 0.22, y + h * 0.92)
        path.closeSubpath()
        p.setBrush(color)
        p.drawPath(path)
    elif key == "switch":                                  # ⇄
        for yy, a, b in ((0.3, 0.1, 0.9), (0.7, 0.9, 0.1)):
            p.drawLine(x + w * a, y + h * yy, x + w * b, y + h * yy)
            d = 0.18 if b > a else -0.18
            p.drawLine(x + w * b, y + h * yy, x + w * (b - d), y + h * (yy - 0.16))
            p.drawLine(x + w * b, y + h * yy, x + w * (b - d), y + h * (yy + 0.16))
    elif key == "couch":                                   # a grid of four tiles
        q = w * 0.4
        for cx in (0.05, 0.55):
            for cy in (0.05, 0.55):
                p.drawRoundedRect(QRectF(x + w * cx, y + h * cy, q, q), 2.5 * s, 2.5 * s)
    elif key == "friends":                                 # two people
        p.drawEllipse(QRectF(x + w * 0.12, y + h * 0.1, w * 0.32, h * 0.32))
        p.drawArc(QRectF(x + w * 0.0, y + h * 0.5, w * 0.56, h * 0.62), 0, 180 * 16)
        p.drawEllipse(QRectF(x + w * 0.58, y + h * 0.18, w * 0.26, h * 0.26))
        p.drawArc(QRectF(x + w * 0.5, y + h * 0.54, w * 0.46, h * 0.5), 0, 180 * 16)
    elif key == "steamoverlay":                            # a panel laid over a window
        p.drawRoundedRect(QRectF(x + w * 0.02, y + h * 0.1, w * 0.72, h * 0.6), 3 * s, 3 * s)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(x + w * 0.38, y + h * 0.38, w * 0.6, h * 0.52), 3 * s, 3 * s)
        p.setBrush(Qt.NoBrush)
    elif key in ("volume", "volume_low", "volume_muted"):  # a speaker and waves (or ✕ when muted)
        path = QPainterPath()
        path.moveTo(x + w * 0.05, y + h * 0.36)
        path.lineTo(x + w * 0.25, y + h * 0.36)
        path.lineTo(x + w * 0.5, y + h * 0.12)
        path.lineTo(x + w * 0.5, y + h * 0.88)
        path.lineTo(x + w * 0.25, y + h * 0.64)
        path.lineTo(x + w * 0.05, y + h * 0.64)
        path.closeSubpath()
        p.setBrush(color)
        p.drawPath(path)
        p.setBrush(Qt.NoBrush)
        if key == "volume_muted":
            p.drawLine(x + w * 0.63, y + h * 0.34, x + w * 0.91, y + h * 0.66)
            p.drawLine(x + w * 0.91, y + h * 0.34, x + w * 0.63, y + h * 0.66)
        else:
            p.drawArc(QRectF(x + w * 0.42, y + h * 0.3, w * 0.3, h * 0.4), -50 * 16, 100 * 16)
            if key == "volume":
                p.drawArc(QRectF(x + w * 0.42, y + h * 0.1, w * 0.56, h * 0.8), -50 * 16, 100 * 16)
    elif key == "mangohud":                                # a frame-time graph in a screen
        p.drawRoundedRect(QRectF(x + w * 0.04, y + h * 0.12, w * 0.92, h * 0.76), 3 * s, 3 * s)
        path = QPainterPath()
        path.moveTo(x + w * 0.18, y + h * 0.62)
        for fx, fy in ((0.36, 0.62), (0.46, 0.34), (0.58, 0.7), (0.68, 0.5), (0.82, 0.5)):
            path.lineTo(x + w * fx, y + h * fy)
        p.drawPath(path)
    elif key == "settings":                                # a gear
        import math
        c = r.center()
        for k in range(8):
            a = k * math.pi / 4
            p.drawLine(c.x() + math.cos(a) * w * 0.32, c.y() + math.sin(a) * h * 0.32,
                       c.x() + math.cos(a) * w * 0.5, c.y() + math.sin(a) * h * 0.5)
        p.drawEllipse(c, w * 0.3, h * 0.3)
        p.drawEllipse(c, w * 0.1, h * 0.1)
    elif key == "quit":                                    # ✕
        p.drawLine(x + w * 0.15, y + h * 0.15, x + w * 0.85, y + h * 0.85)
        p.drawLine(x + w * 0.85, y + h * 0.15, x + w * 0.15, y + h * 0.85)
    p.restore()


def session_text(seconds: int) -> str:
    m = seconds // 60
    if m < 1:
        return "Just started"
    h, m = divmod(m, 60)
    return (f"{h} h {m} min" if h else f"{m} min") + " this session"


@dataclass
class Row:
    key: str
    label: str
    note: str = ""
    kind: str = "row"                 # row | primary | danger | choice | header | game | friend
    value: str = ""
    action: Callable[[], None] | None = None
    left: Callable[[], None] | None = None
    right: Callable[[], None] | None = None
    entry: object = None              # kind "game": the library entry
    extra: dict = field(default_factory=dict)

    @property
    def focusable(self) -> bool:
        return self.kind not in ("header", "msg")


def _let_go(bridge, token: int, ms: int = 1000) -> None:
    """Hand the layout kept for a menu (``token``) back to Steam after ``ms`` — not tied to any window."""
    if token:
        QTimer.singleShot(ms, lambda: bridge.call(bridge.core.library_call("let_go_input_layout", token), None,
                                                  lambda _e: None))


class QuickMenu(OwnAppId, QWidget):
    GRID_COLS = 4
    app_id_role = "quick-menu"             # its own app to the desktop (see OwnAppId)

    def __init__(self, couch):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.couch = couch
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setWindowTitle(TITLE)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.wc = WindowControl(self, "Quick menu")
        self.game = None                   # the library entry the menu was opened for (None: only suspended ones)
        self.view = "main"                 # main | switch | confirm (Play X?) | starting | friends | friend
        self.target = None                 # confirm / starting: the game to switch to
        self.start_error = ""
        self.rows: list[Row] = []
        self.focus = 0
        self.grid: list = []               # switch view: library entries
        self.grid_focus = 0
        self.volume: int | None = None
        self.session: int | None = None
        self.busy = ""                     # "Suspending…" while an action is under way
        self.friends: dict | None = None   # Steam friends (core.steam_friends), asked for when the menu opens
        self.friends_error = ""
        self.friend_query = ""             # the friends list's search: only the friends whose name has it
        self.friend: dict | None = None    # friend view: the friend chosen
        self.chats: dict | None = None     # our overlay: Steam's chats through the client bridge (core.steam_chats)
        self.chat_msgs: list[dict] = []    # chat view: the conversation with self.friend
        self.keyboard = None               # chat view: the on-screen keyboard while writing
        self.flash = ""                    # a short note at the bottom ("Invite sent")
        self._flash_until = 0.0
        self._row_top = 0                  # long lists: the first row shown
        self._grid_top = 0                 # switch grid: the first row of tiles shown
        self._chat_top: int | None = None  # chat history: first message shown (None = pinned to newest)
        self._confirm_quit = 0.0           # Quit pressed once: press again before this time
        self._hits: list[tuple[QRectF, str, int]] = []
        self._hovered: tuple[str, int] | None = None
        self._hint_actions: list[str] = []
        self._saved_volume = 100           # volume before muting, restored when unmuted
        self._vol_track: QRectF | None = None
        self._dragging_vol = False
        self._vol_inflight = False
        self._vol_pending: int | None = None
        self._scroll_geom: tuple[float, float, float, int, str] | None = None
        self._dragging_scroll = False
        self._shown = 0.0                  # 0 → 1: sliding in (and back out when closing)
        self._wide = 0.0                   # 0 → 1: the panel widening for the game grid
        self._closing_then: Callable[[], None] | None = None
        self._pending_art = False
        self.opened_at = 0.0
        self._still = None                 # a game known to minimize: its picture, taken just before (a QPixmap)
        self._expect_hidden = False        # … such a game: it is expected to go as the menu opens
        self._watch = None                 # KWin watching the game's window (window_control.GameWatch)
        self._watch_token = 0
        self._game_report: int | None = None   # what KWin last said of it: 1 shown, 0 gone, -1 not found
        self._wheel = WheelSteps()         # mouse wheel → one step per notch
        self._keys_down: set[int] = set()  # keys pressed in the menu and not let go yet
        self._buttons_down = False         # … a mouse button
        self._release_wait = False         # closing: waiting for them before the game gets the focus back
        self._shades: dict = {}            # one line of the backdrop's shade per (width, t)
        self._layer: QImage | None = None  # sliding in / out: the panel drawn once, moved for every frame
        self._layer_hits: list = []
        self._layer_pending = False
        self._holding = False              # asking Steam to keep the game's controller layout (the menu opens next)
        self._layout_token = 0             # … what to hand back to Steam when the menu goes (see steaminput)
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._shown_step)
        self._anim.finished.connect(self._anim_done)
        self._wide_anim = QVariantAnimation(self)
        self._wide_anim.setDuration(220)
        self._wide_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._wide_anim.valueChanged.connect(self._wide_step)
        self._art = QTimer(self)                         # pictures still on their way: look again
        self._art.setInterval(120)
        self._art.timeout.connect(self._art_tick)
        self._tick = QTimer(self)                        # the clock and the session time
        self._tick.setInterval(15_000)
        self._tick.timeout.connect(self._refresh)
        self._chat_tick = QTimer(self)                   # our overlay: new messages / invites while the menu is open
        self._chat_tick.setInterval(3000)
        self._chat_tick.timeout.connect(lambda: self._load_chats() if self.isVisible() else self._chat_tick.stop())
        self._friends_tick = QTimer(self)                # friends' status while the friends list is open
        self._friends_tick.setInterval(30_000)
        self._friends_tick.timeout.connect(lambda: self._load_friends() if self.view in ("friends", "friend")
                                           and self.isVisible() else self._friends_tick.stop())

    # ================================================================= open / close
    def open_for(self, game, by_key: bool = False) -> None:
        """Show the menu for ``game`` (a library entry the app is running; None when every game is suspended).

        Over a game, Steam is first asked to keep the game's controller layout while the menu is open (see
        steaminput: the menu taking the focus made Steam take away the virtual pad the game reads, and bring a new
        one back afterwards — a friend's game never took it back). The menu shows once Steam switched (well under a
        second; never more than 2.5 s)."""
        if self._holding:
            return                                       # (waiting for Steam: the same press reported twice)
        if self.isVisible() or game is None or game.id in self.couch.suspended:
            self._open(game, by_key=by_key)
            return
        self._holding = True
        b = self.couch.bridge
        shown = [False]

        def show() -> None:
            if shown[0] or not shiboken6.isValid(self):
                return
            shown[0] = True
            self._holding = False
            self._open(game, by_key=by_key)

        def held(token) -> None:
            token = int(token or 0)
            if not shiboken6.isValid(self):              # the menu went meanwhile (its couch mode closed):
                _let_go(b, token, 0)                     # nothing to keep the layout for
                return
            self._layout_token = token                   # (also when it came after the time limit)
            if shown[0] and not self.isVisible():
                self._release_layout()                   # (gone again already)
            show()
        # (not owned by this window: Steam's answer must reach a release even if the menu is gone by then)
        b.call(b.core.library_call("hold_input_layout", game.id), held, lambda _e: show())
        QTimer.singleShot(2500, self, show)

    @property
    def opening(self) -> bool:
        """Asked for, not shown yet (Steam is switching): the menu is on its way — its couch mode must stay."""
        return self._holding

    def _open(self, game, by_key: bool = False) -> None:
        self.game = game
        self.opened_at = time.monotonic()
        self.view = "main"
        self._wide = 0.0
        self._wide_anim.stop()
        self._release_wait = False                       # (opened again while it waited to close)
        self.busy = ""
        self._confirm_quit = 0.0
        self.volume = None
        self.session = None
        self.friend = None
        self._row_top = 0
        self._grid_top = 0
        self._chat_top = None
        self._hovered = None
        self._dragging_vol = False
        self._dragging_scroll = False
        self._vol_pending = None
        self._build()
        self.focus = self._first()
        self._refresh()
        self._load_friends()
        self.keyboard = None
        self._load_chats()
        if self._own_overlay():
            self._chat_tick.start()
        screen = self.couch._target_screen or self.couch.screen() or QApplication.primaryScreen()
        if not self.isVisible():
            self._wheel.reset()
            self._keys_down.clear()
            self._buttons_down = False
            self._release_wait = False
            playing = game is not None and not self._suspended(game)
            # the game stays live behind the menu; a game seen minimizing itself: its picture, taken now — before
            # the menu takes the focus and it goes
            self._expect_hidden = playing and game.id in _MINIMIZERS
            self._still = self._picture_of_game(screen) if self._expect_hidden else None
            self._game_report = None
            self._watch_game(game if playing else None)
        if self.windowHandle() is None:
            self.create()
        if self.windowHandle() is not None and screen is not None:
            self.windowHandle().setScreen(screen)
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.pointer(bool(by_key and not self.couch.pads.steam_as_keys()))   # Shift+Tab on keyboard: show pointer
        self.showFullScreen()
        self.couch.pads.hold_virtual(game is not None)    # the game's pad answers the menu alone (gamepad.py)
        self.raise_()
        self.activateWindow()
        self.wc.keep_above_and_activate()
        QTimer.singleShot(250, self, self.wc.activate)    # Wayland: once more, after the game let go of the focus
        self._closing_then = None
        self._anim.stop()
        self._anim.setDuration(0 if self.couch.reduce_motion else 240)
        self._anim.setStartValue(self._shown)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._art.start()
        self._tick.start()
        self.couch.sounds.play("select")
        log.info("quick menu opened for %s", game.title if game else "the suspended games")

    def _watch_game(self, game) -> None:
        """KWin tells the menu when the game's window goes from behind it (``_game_window``) — for as long as the
        menu is open. Nothing without KWin: the game is simply what is behind the menu."""
        global _watch_n
        self._stop_watch()
        if game is None:
            return
        from ezgd.ui import window_control
        if not window_control.can_watch_windows():
            return
        _watch_n += 1
        token = self._watch_token = _watch_n
        b = self.couch.bridge

        def got(pids) -> None:
            if token != self._watch_token or not shiboken6.isValid(self):
                return                                   # (closed again meanwhile)
            self._watch = window_control.watch_game_window(list(pids or []), token, self._game_window)
        b.call(b.core.library_call("game_processes", game.id), got, lambda _e: None, owner=self)

    def _stop_watch(self) -> None:
        self._watch_token = 0
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.stop()

    def _game_window(self, token: int, state: int) -> None:
        """KWin about the game's window: 1 shown, 0 minimized (or gone), -1 none of its windows found."""
        if token != self._watch_token or not shiboken6.isValid(self):
            return
        before = self._game_hidden()
        self._game_report = state
        g = self.game
        if state == 0 and g is not None and g.id not in _MINIMIZERS:
            _MINIMIZERS.add(g.id)
            log.info("quick menu: %s minimized itself as the menu opened — its picture stays behind the menu "
                     "from now on", g.title)
        elif state == 1 and self._expect_hidden:
            left = HIDE_GRACE - (time.monotonic() - self.opened_at)
            if left > 0:                                 # (it may still go: look again when the grace is over)
                QTimer.singleShot(int(left * 1000) + 20, self, self.update)
        if self._game_hidden() != before:
            self.update()

    def _game_hidden(self) -> bool:
        """The game's window is not behind the menu (it minimized itself): its picture, or its artwork, is drawn
        there instead of the desktop."""
        if self._game_report == 0:
            return True
        if self._expect_hidden:                          # seen minimizing before: hidden until KWin says it stayed
            return not (self._game_report == 1 and time.monotonic() - self.opened_at >= HIDE_GRACE)
        return False

    def _picture_of_game(self, screen):
        """The screen as it is right before the menu opens — the game. Some games minimize themselves the moment
        another window takes the focus (Dark Souls III in full screen): the see-through menu then showed the desktop
        (a friend, 2026-10-23). For such a game the picture stays behind the menu instead (only for them: over any
        other game it hid the live game, which looked frozen). None: not possible here (not KDE)."""
        from PySide6.QtGui import QPixmap
        from ezgd.platform import hidden
        try:
            img = hidden.capture_screen(screen.name() if screen is not None else None)
        except Exception:
            log.exception("quick menu: the picture of the game could not be taken")
            return None
        if img is None or img.isNull() or screen is None:
            return None
        g = screen.geometry()
        dpr = max(1.0, img.width() / max(1, g.width()))
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)                  # the screen's real pixels: drawn 1:1, no scaling per frame
        return pm

    # Steam's controller layout is left alone while the menu is open (see steaminput): over a game whose layout the
    # app forced, the controller stays the game's virtual pad and drives the menu as a pad; over a game Steam knows,
    # Steam gives the focused menu its desktop layout (keys) and the game its own back when it has the focus again.
    def hideEvent(self, event) -> None:
        self._still = None
        self._stop_watch()
        self._keys_down.clear()
        self._buttons_down = False
        try:
            self.couch.pads.hold_virtual(False)           # the game reads its pad again
        except RuntimeError:
            pass                                          # (couch mode's window already gone: its pads went too)
        self._release_layout()
        super().hideEvent(event)

    def _release_layout(self) -> None:
        """The layout kept for the menu goes back to Steam — a moment after the menu went, once the game has the
        focus again (from then on Steam follows the focus, as before the menu). Not tied to this window: the hidden
        couch mode of a desktop start closes before that."""
        token, self._layout_token = self._layout_token, 0
        _let_go(self.couch.bridge, token)

    def pointer(self, shown: bool) -> None:
        """The mouse pointer over the menu: hidden while a controller (or keys) drive it, as in couch mode — with a
        Steam controller on Steam's desktop layout its right pad is a mouse, and the pointer stayed on screen."""
        shape = Qt.ArrowCursor if shown else Qt.BlankCursor
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    def close_menu(self, then: Callable[[], None] | None = None) -> None:
        """Slide out, then hide, then ``then`` (runs even if the menu is already gone)."""
        if not self.isVisible():
            if then:
                then()
            return
        self._closing_then = then
        self._anim.stop()
        self._anim.setDuration(0 if self.couch.reduce_motion else 160)
        self._anim.setStartValue(self._shown)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _anim_done(self) -> None:
        self._layer = None                               # (slid in or out: drawn directly again)
        if self._shown > 0.01:
            return
        if (self._keys_down or self._buttons_down) and self.isVisible() and self.isActiveWindow():
            # the key (or button) that closed the menu is still down: the game gets the focus once it is let go —
            # KWin hands a newly focused window the keys being held, and the game saw a fresh Esc or Enter
            if not self._release_wait:
                self._release_wait = True
                QTimer.singleShot(RELEASE_WAIT, self, self._released)
            return
        self._finish_close()

    def _released(self) -> None:
        """Closing, and the keys / buttons are let go (or waited for long enough): now the menu goes."""
        if not self._release_wait:
            return
        self._release_wait = False
        if self._shown <= 0.01 and self.isVisible():
            self._finish_close()

    def _finish_close(self) -> None:
        self._release_wait = False
        self._art.stop()
        self._tick.stop()
        self._friends_tick.stop()
        self._chat_tick.stop()
        self.keyboard = None
        self.wc.keep_above(False)
        self.hide()
        then, self._closing_then = self._closing_then, None
        if then:
            try:
                then()
            except Exception:
                log.exception("quick menu: the chosen action failed")

    def _art_tick(self) -> None:
        """Pictures still on their way: look again — the panel only (that is where they are), and for the first
        15 s only: a game with no picture at all kept the menu redrawing the whole 4K screen 8 times a second for
        as long as it was open. (A picture that comes later shows with the next change.)"""
        if not self._pending_art or time.monotonic() - self.opened_at > 15:
            return
        if self._game_hidden() and self._still is None:
            self.update()                                # (the game's artwork behind the menu)
        else:
            self._redraw()

    def update(self, *args) -> None:
        """The menu's content changed (every caller in here): a panel kept for sliding is drawn again."""
        self._layer = None
        super().update(*args)

    def _shown_step(self, v) -> None:
        self._shown = float(v)
        if self._shown <= 0.0 and self._closing_then is not None and self._anim.duration() == 0:
            QTimer.singleShot(0, self, self._anim_done)
        QWidget.update(self)                             # (a frame of the slide: the content is the same)

    def _wide_step(self, v) -> None:
        self._wide = float(v)
        self.update()

    def _refresh(self) -> None:
        if self.game is None:
            return
        b = self.couch.bridge
        gid = self.game.id
        b.call(b.core.library_call("game_volume", gid), lambda v: self._got("volume", v), lambda _e: None,
               owner=self)
        b.call(b.core.library_call("session_seconds", gid), lambda v: self._got("session", v), lambda _e: None,
               owner=self)

    def _got(self, what: str, value) -> None:
        if what == "volume" and (self._dragging_vol or self._vol_inflight):
            return
        if what == "volume" and isinstance(value, int) and value > 0:
            self._saved_volume = value
        setattr(self, what, value)
        if what == "volume":
            self._build()
        self.update()

    def _key_mode(self) -> bool:
        """Driven by a real keyboard / mouse (no controller prompts active)."""
        return self.couch.pad_family == "" and not self.couch.pads.steam_as_keys()

    # ================================================================= model
    def _suspended(self, e) -> bool:
        return e is not None and e.id in self.couch.suspended

    def _build(self) -> None:
        if self.view == "chat":
            self.rows = self._chat_rows()
            self.focus = min(self.focus, max(0, len(self.rows) - 1))
            if self.rows and not self.rows[self.focus].focusable:
                self.focus = len(self.rows) - 1
            return
        if self.view in ("friends", "friend"):
            self.rows = self._friends_rows() if self.view == "friends" else self._friend_rows()
            self.focus = min(self.focus, max(0, len(self.rows) - 1))
            if self.rows and not self.rows[self.focus].focusable:
                self.focus = self._first()
            return
        if self.view in ("confirm", "starting"):
            self.rows = self._target_rows()
            self.focus = min(self.focus, max(0, len(self.rows) - 1))
            if self.rows and not self.rows[self.focus].focusable:
                self.focus = self._first()
            return
        rows: list[Row] = []
        g = self.game
        others = [e for e in self.couch.entries if e.id in self.couch.suspended and (g is None or e.id != g.id)]
        rows += self._invite_rows()
        if g is not None:
            sus = self._suspended(g)
            rows.append(Row("resume", "Resume game" if sus else "Back to the game", "It continues right where it was"
                            if sus else "", "primary", action=self.resume))
            rows.append(Row("switch", "Switch game", "This game waits, suspended, right where you are",
                            action=self.open_switch))
            rows.append(Row("couch", "Go to couch mode", "Suspends the game while you look around",
                            action=self.to_couch))
            rows.append(self._friends_row())
            rows.append(Row("steamoverlay", "Steam overlay", "Steam's own over the game — its Steam button closes it",
                            action=self._steam_overlay))
            if self.volume is not None:
                val = "Muted" if self.volume == 0 else f"{self.volume} %"
                rows.append(Row("volume", "Game volume", "", "choice", val,
                                action=lambda: self._step_volume(VOLUME_STEP),
                                left=lambda: self._step_volume(-VOLUME_STEP),
                                right=lambda: self._step_volume(VOLUME_STEP)))
            import shutil
            if shutil.which("mangohud"):
                hud = "mangohud" in g.compat_config.fixes
                rows.append(Row("mangohud", "MangoHud", "FPS, frame times and temps — over the game now",
                                "row", "On" if hud else "Off", action=self._toggle_mangohud,
                                extra={"toggle": hud}))
            rows.append(Row("settings", "Game settings", "Proton, FPS limit, launch options — from its next start",
                            action=self.game_settings))
            rows.append(Row("froze", "It froze — quit it", "Keeps what it was doing, so the cause can be found",
                            action=self.froze))
            armed = time.monotonic() < self._confirm_quit
            rows.append(Row("quit", "Press again to quit" if armed else "Quit game",
                            "Progress the game has not saved is lost" if armed else "", "danger", action=self.quit))
        if others:
            rows.append(Row("h-sus", "Suspended", kind="header"))
            for e in others:
                rows.append(Row(f"game-{e.id}", e.title, "Suspended — continue where you left off", "game",
                                action=lambda e=e: self.switch_to(e), entry=e))
        if g is None:
            rows.append(Row("couch", "Back to couch mode", "", "primary" if not others else "row",
                            action=lambda: self.close_menu(self.couch._come_back)))
            rows.append(self._friends_row())
        self.rows = rows
        self.focus = min(self.focus, max(0, len(rows) - 1))
        if rows and not rows[self.focus].focusable:
            self.focus = self._first()

    # ----------------------------------------------------------------- Steam friends
    def _friends_row(self) -> Row:
        from ezgd.ui.couch_friends import summary
        note = self.friends_error or summary(self.friends)
        unread = sum(c.get("unread") or 0 for c in (self.chats or {}).get("chats") or [])
        if unread:
            note = f"{unread} new message{'s' if unread != 1 else ''} · {note}"
        return Row("friends", "Friends", note, action=self.open_friends)

    # ----------------------------------------------------------------- our overlay: Steam chats through the client
    def _own_overlay(self) -> bool:
        return bool(self.couch.actions.settings.get("own_overlay", False))

    def _bridged(self) -> bool:
        """Our overlay is on and the Steam client answers: chats, invites and joining go through it."""
        return self._own_overlay() and self.chats is not None

    def _load_chats(self) -> None:
        if not self._own_overlay():
            self.chats = None
            return
        b = self.couch.bridge

        def got(data: dict) -> None:
            before = self._chat_signature()
            self.chats = data
            if self.view == "chat" and self.friend is not None:
                acct = self._acct(self.friend)
                c = next((c for c in data.get("chats") or [] if c.get("accountid") == acct), None)
                if c is not None and (c.get("unread") or c.get("time", 0) > (self.chat_msgs[-1]["time"] if
                                                                            self.chat_msgs else 0)):
                    self._load_history()
            if self._chat_signature() != before and self.view in ("main", "friends", "friend"):
                self._build()
            self.update()

        def failed(_err) -> None:
            self.chats = None
        b.call(b.core.steam_chats(), got, failed, owner=self)

    def _chat_signature(self) -> tuple:
        return tuple((c.get("accountid"), c.get("unread"), bool(c.get("invite"))) for c in
                     (self.chats or {}).get("chats") or [])

    @staticmethod
    def _acct(f: dict) -> int:
        from ezgd.platform.steamfriends import to_account_id
        return int(f.get("accountid") or to_account_id(f["steamid"]))

    def _chat_of(self, f: dict) -> dict | None:
        acct = self._acct(f)
        return next((c for c in (self.chats or {}).get("chats") or [] if c.get("accountid") == acct), None)

    def _name_of(self, acct: int) -> str:
        from ezgd.platform.steamfriends import to_steamid64
        sid = to_steamid64(acct)
        f = next((f for f in (self.friends or {}).get("friends") or [] if f.get("steamid") == sid), None)
        c = next((c for c in (self.chats or {}).get("chats") or [] if c.get("accountid") == acct), None)
        return (f or {}).get("name") or (c or {}).get("name") or "A friend"

    def _invite_rows(self) -> list[Row]:
        """An invite a friend sent you (their last message, unread or from the last 15 minutes): one press joins."""
        if not self._bridged():
            return []
        out = []
        from ezgd.platform import steambridge
        now = time.time()
        for c in (self.chats or {}).get("chats") or []:
            inv = c.get("invite")
            if not steambridge.joinable(inv) or not (c.get("unread") or now - (c.get("time") or 0) < 15 * 60):
                continue                             # (an empty invite: Steam's own chat has no Join for it either)
            name = self._name_of(c["accountid"])
            from ezgd.platform.steamfriends import to_steamid64
            url = steambridge.join_url(inv, to_steamid64(c["accountid"]))
            act = "Enter or click" if self._key_mode() else self.couch._pad_labels().get("a", "A")
            out.append(Row(f"inv-{c['accountid']}", f"Join {name}", f"{name} invited you to play — {act} joins",
                           "primary", action=lambda u=url, n=name, a=inv["appid"]: self._join(u, n, a)))
        return out

    def _join(self, url: str, name: str, appid: str = "") -> None:
        """Join a friend's game through Steam (no window). An invite to the game being played goes to that game —
        Steam hands the running game the lobby, so it must keep running (suspending it first froze Slay the Spire 2
        on the invite). Only a different game suspends the one being played, like switching games."""
        b = self.couch.bridge
        g = self.game
        if not url:
            self._flash_note(f"Nothing to join — {name or 'your friend'}'s game gave Steam no lobby. Join from the game.")
            return
        playing = g is not None and g.id in self.couch.running and not self._suspended(g)
        if str(appid) == SPACEWAR and not playing:
            # OnlineFix games all join as Spacewar in Steam: with none of ours running, Steam would start Spacewar
            self._flash_note("Start the game first (Steam knows it only as Spacewar), then join.")
            return

        def go() -> None:
            b.call(b.core.steam_open(url), lambda _r: None,
                   lambda e: self.couch.actions.toasts.show("Could not join", str(e), "warning"), owner=self.couch)
        mine = str(((self.chats or {}).get("self") or {}).get("appid") or "")
        other = bool(appid) and mine not in ("", "0") and str(appid) != mine
        log.info("our overlay: joining %s (%s)", name, "another game" if other else "the game being played"
                 if playing else "no game running")
        if playing and other:
            self._then_suspend(lambda: self.close_menu(go))
        elif playing:
            self.close_menu(lambda: (self.couch.go_to_game(g), go()))
        else:
            self.close_menu(go)

    def _flash_note(self, text: str) -> None:
        self.flash, self._flash_until = text, time.monotonic() + 3.0
        QTimer.singleShot(3100, self, self.update)
        self.update()

    def open_chat(self, f: dict) -> None:
        self.friend = f
        self.view = "chat"
        self.chat_msgs = []
        self._row_top = 0
        self._chat_top = None
        self._build()
        self.focus = len(self.rows) - 1
        self._load_history()
        self.update()

    def _load_history(self) -> None:
        f = self.friend
        if f is None:
            return
        b = self.couch.bridge

        def got(msgs: list) -> None:
            if self.view != "chat" or self.friend is not f:
                return
            self.chat_msgs = msgs
            self._build()
            self.focus = len(self.rows) - 1                 # (the newest is at the bottom, next to "Write")
            self.update()
        b.call(b.core.steam_chat_history(self._acct(f), 30), got,
               lambda e: self._flash_note(str(e)), owner=self)

    def _chat_rows(self) -> list[Row]:
        from ezgd.platform import steambridge
        from ezgd.platform.steamfriends import to_steamid64
        f = self.friend or {}
        rows: list[Row] = []
        if not self.chat_msgs:
            rows.append(Row("h-none", "No messages yet — say hi.", kind="header", extra={"note": True, "tip": True}))
        now = time.time()
        for k, m in enumerate(self.chat_msgs):
            text = m["text"] if not m.get("invite") else ("You invited them to play" if m["mine"] else
                                                          f"{f.get('name', 'They')} invited you to play")
            rows.append(Row(f"msg-{k}", text, kind="msg", extra={"mine": m["mine"], "time": m["time"]}))
            if m.get("invite") and not m["mine"] and now - m["time"] < 6 * 3600:
                if not steambridge.joinable(m["invite"]):
                    rows.append(Row(f"nojoin-{k}", "Their game sent no lobby with it — join from inside the game",
                                    kind="header", extra={"note": True}))
                    continue
                url = steambridge.join_url(m["invite"], to_steamid64(self._acct(f)))
                rows.append(Row(f"join-{k}", "Join their game", "Steam joins you up", "primary",
                                action=lambda u=url, n=f.get("name", ""), a=m["invite"]["appid"]:
                                self._join(u, n, a)))
        write_note = "Type a message — Enter sends it" if self._key_mode() else \
            "The on-screen keyboard — Start sends it"
        rows.append(Row("write", "Write a message", write_note, "primary", action=self._write))
        return rows

    def _write(self) -> None:
        from ezgd.ui.couch_pages import Keyboard
        f = self.friend or {}
        self.keyboard = Keyboard(self, f"Message {f.get('name', '')}", self._send, done_label="Send", symbols=True,
                                 limit=400)
        self.update()

    def _send(self, text: str) -> None:
        f = self.friend
        if not text or f is None:
            return
        b = self.couch.bridge
        self._chat_top = None
        self.chat_msgs = self.chat_msgs + [{"mine": True, "time": int(time.time()), "text": text, "invite": None}]
        self._build()
        self.focus = len(self.rows) - 1
        b.call(b.core.steam_chat_send(self._acct(f), text), lambda _r: self._load_history(),
               lambda e: self._flash_note(f"Not sent: {e}"), owner=self)
        self.update()

    def _invite(self, f: dict) -> None:
        b = self.couch.bridge
        b.call(b.core.steam_invite(self._acct(f)),
               lambda _how: self._flash_note(f"Invite sent to {f.get('name', 'your friend')}"),
               lambda e: self._flash_note(str(e)), owner=self)

    # (the on-screen keyboard draws itself with these, as on couch mode's window)
    def _font(self, size: float, weight: int = 600):
        return self.couch._font(size, weight)

    def _schedule_frame(self, ms: int) -> None:
        QTimer.singleShot(ms, self, self.update)

    @property
    def _hit(self) -> list:
        return self._hits

    def _load_friends(self, force: bool = False) -> None:
        b = self.couch.bridge

        def got(data: dict) -> None:
            fresh = self.friends is None
            row = self.rows[self.focus] if 0 <= self.focus < len(self.rows) else None
            kept = (row.extra.get("friend") or {}).get("steamid") if row is not None else None
            self.friends, self.friends_error = data, ""
            if self.friend is not None:                  # (the friend view keeps showing the fresh status)
                self.friend = next((f for f in data.get("friends") or [] if f["steamid"] == self.friend["steamid"]),
                                   self.friend)
            self._build()
            if self.view == "friends":
                # the list opens on the first friend (in a game / online come first; the search is one up), and a
                # refresh that reorders it (someone came online) keeps the selection on the same friend
                at = next((k for k, r in enumerate(self.rows) if r.kind == "friend" and
                           (fresh or r.extra["friend"].get("steamid") == kept)), None)
                if at is not None and (fresh or kept):
                    self.focus = at
            self.update()

        def failed(err) -> None:
            self.friends_error = str(err)
            self._build()
            self.update()
        b.call(b.core.steam_friends(force), got, failed, owner=self)

    def open_friends(self) -> None:
        self.view = "friends"
        self.friend = None
        self.focus = 0
        self._row_top = 0
        self._build()
        self.focus = next((k for k, r in enumerate(self.rows) if r.kind == "friend"), self._first())
        self._load_friends()
        self._friends_tick.start()
        self.update()

    def _friends_rows(self) -> list[Row]:
        d = self.friends or {}
        rows: list[Row] = []
        note = self.friends_error or d.get("error") or ("Your friends list is private on Steam — showing the friends "
                                                        "Steam keeps here" if d.get("private") else "")
        if note:
            rows.append(Row("h-note", note, kind="header", extra={"note": True}))
        if self.friends is None and not self.friends_error:
            rows.append(Row("h-wait", "Asking Steam…", kind="header", extra={"note": True}))
        from ezgd.ui.couch_friends import matching, search_note
        found = matching(d.get("friends") or [], self.friend_query)
        if self.friend_query:
            s_note = "Enter searches again · Esc or ✕ shows everyone" if self._key_mode() else \
                "A searches again · B shows everyone"
            rows.append(Row("f-search", search_note(self.friend_query, len(found)), s_note, action=self.find_friend))
        elif d.get("friends"):
            s_note = "Type a name or press / to search" if self._key_mode() else "Search your friends by name"
            rows.append(Row("f-search", "Find a friend", s_note, action=self.find_friend))
        for k, f in enumerate(found):
            note = f.get("status") or ""
            c = self._chat_of(f) if self._bridged() else None
            if c and c.get("unread"):
                note = f"● {c['unread']} new" + (f" · {note}" if note else "")
            rows.append(Row(f"friend-{k}", f.get("name", ""), note, "friend",
                            action=lambda f=f: self.open_friend(f), extra={"friend": f}))
        return rows

    def find_friend(self) -> None:
        from ezgd.ui.couch_pages import Keyboard
        self.keyboard = Keyboard(self, "Find a friend", self._found, text=self.friend_query, done_label="Find",
                                 symbols=True, limit=40)
        self.update()

    def _found(self, text: str) -> None:
        self.friend_query = text.strip()
        self._row_top = 0
        self._build()
        self.focus = next((k for k, r in enumerate(self.rows) if r.kind == "friend"), self._first())
        self.update()

    def open_friend(self, f: dict) -> None:
        self.friend = f
        self.view = "friend"
        self.focus = 0
        self._row_top = 0
        self._build()
        self.update()

    def _friend_rows(self) -> list[Row]:
        from ezgd.ui.couch_friends import actions, invite_game
        f = self.friend or {}
        playing = self.game.title if self.game is not None and not self._suspended(self.game) else ""
        if self._bridged():
            me = (self.chats or {}).get("self") or {}
            game = (playing or "your game") if me.get("appid") else ""
            out = []
            for a, label, note in actions(f, game):
                if a == "chat":
                    c = self._chat_of(f)
                    label, note = "Message", (f"{c['unread']} new — " if c and c.get("unread") else "") + \
                        "Your chat with them, right here"
                elif a == "invite":
                    note = "One press — Steam sends it" if me.get("invitable", True) else \
                        "No lobby yet — host or open one in the game first"
                out.append(Row(f"fa-{a}", label, note, "primary" if a == "chat" else "row",
                               action=lambda a=a: self.friend_action(a)))
            return out
        return [Row(f"fa-{a}", label, note, "primary" if a == "chat" else "row",
                    action=lambda a=a: self.friend_action(a))
                for a, label, note in actions(f, invite_game(self.friends, playing))]

    def friend_action(self, action: str) -> None:
        """Chat / profile: Steam's window comes up over the game (which keeps running). Join / play their game:
        the game being played is suspended first, like switching games."""
        from ezgd.ui.couch_friends import open_action
        f = self.friend
        if f is None or self.busy:
            return
        if self._bridged() and action in ("chat", "invite", "join", "launch"):
            from ezgd.platform import steamfriends
            if action == "chat":
                self.open_chat(f)
            elif action == "invite":
                self._invite(f)
            else:
                url = steamfriends.join_url(f) if action == "join" else steamfriends.launch_url(f)
                if url:
                    self._join(url, f.get("name", ""), str(f.get("gameid") or ""))
                else:
                    self._flash_note(f"Nothing to join right now — {f.get('name', 'they')} are not in a joinable game.")
            return
        mine = str(((self.friends or {}).get("self") or {}).get("gameid") or "")
        same = action == "join" and (not mine or mine == str(f.get("gameid") or ""))
        if action in ("join", "launch") and not same:
            self._then_suspend(lambda: self.close_menu(lambda: open_action(self.couch, action, f)))
        elif same and self.game is not None and self.game.id in self.couch.running:
            # their game is the one being played: Steam hands it the lobby, so it must keep running (0.5.7 fixed
            # this for our overlay only — suspending froze Slay the Spire 2 on the invite)
            g = self.game
            self.close_menu(lambda: (self.couch.go_to_game(g), open_action(self.couch, action, f)))
        else:
            self.close_menu(lambda: open_action(self.couch, action, f))

    def _back_from_friends(self) -> None:
        if self.view == "chat":
            self.view = "friend"
            self.chat_msgs = []
            self._row_top = 0
            self._build()
            self.focus = self._first()
            self._load_chats()
            return
        if self.view == "friend":
            keep = self.friend
            self.view = "friends"
            self._build()
            self.focus = next((k for k, r in enumerate(self.rows) if r.extra.get("friend") is not None and keep and
                               r.extra["friend"].get("steamid") == keep.get("steamid")), self._first())
        else:
            self.view = "main"
            self.friend_query = ""
            self._friends_tick.stop()
            self._row_top = 0
            self._build()
            self.focus = next((k for k, r in enumerate(self.rows) if r.key == "friends"), self._first())

    def _target_rows(self) -> list[Row]:
        e, g = self.target, self.game
        rows: list[Row] = []
        if e is None:
            return rows
        waits = f"{g.title} waits, suspended, right where you are" if g is not None and \
            g.id in self.couch.running else ""
        if self.view == "confirm":
            first = "" if e.executable_trusted else "First start of this file — only play games you trust. "
            rows.append(Row("play", "▶  Play", (first + waits).strip(), "primary", action=self._play_target))
            rows.append(Row("cancel", "Cancel", "", action=self._cancel_confirm))
        elif self.start_error:
            rows.append(Row("h-err", self.start_error, kind="header"))
            if g is not None:
                rows.append(Row("back", f"Back to {g.title}", "It continues right where it was", "primary",
                                action=self.resume))
            rows.append(Row("close", "Close", "", action=self.close_menu))
        return rows

    def _first(self) -> int:
        return next((k for k, r in enumerate(self.rows) if r.focusable), 0)

    def _switch_games(self) -> list:
        """Games to switch to: suspended ones first, then everything ready to play (recently played first)."""
        cur = self.game.id if self.game is not None else None
        ready = [e for e in self.couch.entries if e.id != cur and (e.id in self.couch.running or self.couch.ready(e))]
        return sorted(ready, key=lambda e: (e.id not in self.couch.suspended, e.id not in self.couch.running,
                                            -(e.last_played_at or 0), e.title.casefold()))

    # ================================================================= actions
    def resume(self) -> None:
        g = self.game
        if g is None:
            self.close_menu()
            return
        self.couch.sounds.play("select")
        self.close_menu(lambda: self.couch.go_to_game(g))

    def open_switch(self) -> None:
        self.grid = self._switch_games()
        if not self.grid:
            self.couch.actions.toasts.show("No other games to play", "Games you install show up here.")
            return
        self.view = "switch"
        self.grid_focus = 0
        self._grid_top = 0
        self._wide_anim.stop()
        self._wide_anim.setDuration(0 if self.couch.reduce_motion else 220)
        self._wide_anim.setStartValue(self._wide)
        self._wide_anim.setEndValue(1.0)
        self._wide_anim.start()
        self.update()

    def close_switch(self) -> None:
        self.view = "main"
        self._wide_anim.stop()
        self._wide_anim.setDuration(0 if self.couch.reduce_motion else 200)
        self._wide_anim.setStartValue(self._wide)
        self._wide_anim.setEndValue(0.0)
        self._wide_anim.start()
        self.update()

    def _then_suspend(self, next_step: Callable[[], None]) -> None:
        """Suspend the game the menu is for (when it is playing), then ``next_step``."""
        g = self.game
        if g is None or g.id in self.couch.suspended:
            next_step()
            return
        self.busy = f"Suspending {g.title}…"
        self.update()

        def done(_r) -> None:
            self.busy = ""
            self.couch.suspended.add(g.id)
            next_step()

        def failed(err) -> None:
            self.busy = ""
            log.warning("quick menu: could not suspend %s: %s", g.title, err)
            self.couch.actions.toasts.show(f"{g.title} could not be suspended", f"{err} — it keeps running.", "warning")
            next_step()
        b = self.couch.bridge
        b.call(b.core.library_call("suspend", g.id), done, failed, owner=self)

    def switch_to(self, e) -> None:
        """Play ``e`` instead. A game that is running or suspended: the current one is suspended and ``e`` comes
        back right where it was. Any other game: "Play …?" right here in the menu — couch mode never comes forward."""
        if self.busy:
            return
        self.couch.sounds.play("select")
        if e.id in self.couch.running:
            self._then_suspend(lambda: self.close_menu(lambda: self.couch.go_to_game(e)))
            return
        self.target = e
        self.start_error = ""
        self.view = "confirm"
        self.focus = 0
        self._build()
        self.update()

    def _play_target(self) -> None:
        """PLAY on the question: suspend the current game, start the new one; the menu says "Starting…" until it
        runs, then gets out of the way."""
        e, c = self.target, self.couch
        if e is None:
            return
        self.view = "starting"
        self._build()
        self.update()

        def started(entry) -> None:
            c.launched.add(entry.id)
            c._launch_at[entry.id] = time.monotonic()

        def failed(err) -> None:
            c._launch_at.pop(e.id, None)
            if self.view == "starting" and self.target is e:
                self.start_error = str(err) or "It did not start."
                self._build()
                self.update()

        def launch() -> None:
            c._launch_at[e.id] = time.monotonic()        # (the ✕ treats it as on its way)
            c.actions.launch(self, e, confirm=lambda _entry, _plan, proceed: proceed(), couch=True,
                             started=started, failed=failed)
            QTimer.singleShot(45_000, self, lambda: self._start_timeout(e))
            from ezgd.ui import steam_bpm
            if steam_bpm.steam_running():               # started through Steam: its window may pop up meanwhile
                for ms in (1500, 4000, 8000):
                    QTimer.singleShot(ms, c, c._put_steam_away)
        self._then_suspend(launch)

    def target_running(self, entry_id: int) -> None:
        """The game being switched to runs: give it the screen (its window may take a moment to appear)."""
        e = self.target
        if self.view != "starting" or e is None or e.id != entry_id:
            return
        self.game = e
        self.close_menu(lambda: self.couch.bring_forward_soon(e))

    def _start_timeout(self, e) -> None:
        if self.view == "starting" and self.target is e and self.isVisible() and not self.start_error:
            self.start_error = "It has not shown up yet — it may still be loading (Steam can take a while)."
            self._build()
            self.update()

    def _cancel_confirm(self) -> None:
        self.target = None
        self.view = "switch"
        self.update()

    def to_couch(self) -> None:
        if self.busy:
            return
        self.couch.sounds.play("select")
        g = self.game

        def go() -> None:
            if self.couch.background:                    # asked for: couch mode opens for real
                self.couch._promote()
            else:
                self.couch._come_back()
            if g is not None:
                how = ("Enter or click on it continues it right where you were — or press Shift+Tab."
                       if self._key_mode() else
                       "A on it continues it right where you were — or hold the guide button.")
                self.couch.toasts.show(f"{g.title} is suspended", how)
        self._then_suspend(lambda: self.close_menu(go))

    def game_settings(self) -> None:
        g = self.game
        self.couch.sounds.play("select")

        def go() -> None:
            if self.couch.background:                    # a desktop start: couch mode only for these settings
                self.couch.visit_from_desktop()
            else:
                self.couch._come_back()
            self.couch.open_game_settings(g, back_to_game=True)
        self.close_menu(go)

    def quit(self) -> None:
        g = self.game
        if g is None or self.busy:
            return
        if time.monotonic() >= self._confirm_quit:            # first press: arm it (3 s)
            self._confirm_quit = time.monotonic() + 3.0
            self._build()
            QTimer.singleShot(3100, self, lambda: (self._build(), self.update()))
            self.update()
            return
        self._confirm_quit = 0.0

        def go() -> None:
            if not self.couch.background:                # (a desktop start: the desktop app, once the game is gone)
                self.couch._come_back()
            self.couch._stop([g.id], then_exit=False)
        self.close_menu(go)

    def froze(self) -> None:
        """The game hangs: what its threads are doing is kept first (ezgd.library.crashes), then it is stopped."""
        g = self.game
        if g is None or self.busy:
            return
        b = self.couch.bridge

        def go() -> None:
            if not self.couch.background:
                self.couch._come_back()
            b.call(b.core.report_freeze(g.id),
                   lambda verdict: self.couch.toasts.show(f"{g.title} stopped", f"Kept what it was doing: {verdict}"),
                   lambda err: self.couch.toasts.show("Could not stop the game", str(err), "error"), owner=self.couch)
        self.close_menu(go)

    def _steam_overlay(self) -> None:
        """Steam's own overlay over the game — friends, its browser, guides; what the Steam button opens without our
        overlay (a friend missed it, 2026-10-29). Back to the game first, then Steam opens it there; its Steam button
        closes it again (while it is open the button is Steam's: steambridge, couch.open_quick)."""
        g = self.game
        if g is None:
            return
        b, actions = self.couch.bridge, self.couch.actions
        self.resume()
        # (not owned by this window: a desktop start's menu is gone by then)
        QTimer.singleShot(600, lambda: b.call(b.core.steam_overlay_show(g.id), None,
                                              lambda err: actions.toasts.show("Steam overlay", str(err), "error")))

    def _toggle_mangohud(self) -> None:
        g = self.game
        if g is None:
            return
        on = "mangohud" not in g.compat_config.fixes
        b = self.couch.bridge

        def done(r: dict) -> None:
            def fresh(e) -> None:
                if self.game is not None and self.game.id == e.id:
                    self.game = e
                    self._build()
                    self.update()
            b.call(b.core.library_call("get", g.id), fresh, lambda _e: None, owner=self)
            if not r.get("live"):
                self.couch.actions.toasts.show(f"MangoHud {'on' if on else 'off'}", "From the next start of "
                                               f"{g.title} (it was started without MangoHud ready).")
        b.call(b.core.library_call("set_mangohud", g.id, on), done,
               lambda err: self.couch.actions.toasts.show("MangoHud", str(err), "error"), owner=self)

    def _step_volume(self, d: int) -> None:
        if self.game is None or self.volume is None:
            return
        self._set_volume(self.volume + d, snap=False)

    def _set_volume(self, value: int, snap: bool = False) -> None:
        if self.game is None or self.volume is None:
            return
        new = max(0, min(150, int(round(value))))
        if snap and abs(new - 100) <= 3:
            new = 100
        if new == self.volume:
            return
        if new > 0:
            self._saved_volume = new
        self.volume = new
        self._build()
        self._redraw()
        if self._vol_inflight:
            self._vol_pending = new
            return
        self._send_volume(self.game.id, new)

    def _send_volume(self, gid: int, val: int) -> None:
        self._vol_inflight = True
        b = self.couch.bridge

        def done(_v=None) -> None:
            self._vol_inflight = False
            nxt, self._vol_pending = self._vol_pending, None
            if nxt is not None and self.game is not None and self.game.id == gid:
                self._send_volume(gid, nxt)
        b.call(b.core.library_call("set_game_volume", gid, val), done, lambda _e: done(), owner=self)

    def _toggle_mute(self) -> None:
        if self.game is None or self.volume is None:
            return
        self.couch.sounds.play("select")
        self._set_volume(0 if self.volume > 0 else (self._saved_volume or 100), snap=False)

    def _volume_from_x(self, x: float) -> None:
        tr = self._vol_track
        if tr is None or tr.width() <= 0:
            return
        t = max(0.0, min(1.0, (x - tr.left()) / tr.width()))
        self._set_volume(round(t * 150), snap=True)

    # ================================================================= input
    def command(self, name: str) -> None:
        if self.busy or self._shown < 0.3 and self._closing_then is not None:
            return
        if name == "home":                               # the guide button again: back to the game
            if time.monotonic() - self.opened_at > 0.4:
                self.resume() if self.game is not None else self.close_menu()
            return
        if self.keyboard is not None:
            self.keyboard.command(name)
            self.update()
            return
        before = (self.view, self.focus, self.grid_focus, self._chat_top)
        if self.view == "switch":
            self._grid_command(name)
        elif self.view == "friends" and name == "x":
            self.find_friend()
        elif self.view == "friends" and name == "b" and self.friend_query:
            self._found("")                              # B: the search goes first, then the list
        elif self.view in ("friends", "friend", "chat") and name in ("b", "start"):
            self._back_from_friends()
        elif self.view in ("confirm", "starting") and name == "b":
            if self.view == "confirm":
                self._cancel_confirm()
            elif self.start_error:
                self.resume() if self.game is not None else self.close_menu()
        else:
            self._main_command(name)
        if (self.view, self.focus, self.grid_focus, self._chat_top) != before and name not in ("a", "b"):
            self.couch.sounds.play("move")
        self._redraw()

    def _redraw(self) -> None:
        """Draw again what a press changed. Only the panel when nothing else moved: the game around it stays as it
        is — a full frame repainted (and handed to the compositor) the whole 4K screen for every step."""
        if self.keyboard is not None or self._shown < 1.0 or self._anim.state() == QVariantAnimation.Running or \
                self._wide_anim.state() == QVariantAnimation.Running:
            self.update()
            return
        r = self._panel_rect(1.0).toAlignedRect()
        self.update(r.adjusted(-2, 0, 0, 0))

    def _step_rows(self, d: int, count: int = 1) -> None:
        """The selection ``count`` rows on (``d`` 1) or back (-1), over headings; as far as it goes."""
        n, j = len(self.rows), self.focus
        for _ in range(count):
            k = j + d
            while 0 <= k < n and not self.rows[k].focusable:
                k += d
            if not 0 <= k < n:
                break
            j = k
        self.focus = j

    def _chat_bounds(self, y: float | None = None, bottom: float | None = None) -> tuple[list[float], float, int]:
        """(row heights, bottom y of the message area, earliest start index that still fits above Write)."""
        s = self._s()
        y = 184 * s if y is None else y
        bottom = self.height() - 96 * s + 0.5 if bottom is None else bottom
        heights = [self._row_height(row) for row in self.rows]
        if len(self.rows) < 2 or self.rows[-1].key != "write" or y + sum(heights) - 10 * s <= bottom:
            return heights, bottom, 0
        wh = heights[-1] - 10 * s
        msg_bottom = bottom - wh - 12 * s
        mh = heights[:-1]
        max_top = len(mh)
        while max_top > 0 and y + sum(mh[max_top - 1:]) - 10 * s <= msg_bottom:
            max_top -= 1
        return heights, msg_bottom, min(max_top, max(0, len(mh) - 1))

    def _scroll_chat(self, delta: int) -> bool:
        """Scroll chat history by ``delta`` messages; True when the visible slice moved."""
        _heights, _mb, max_top = self._chat_bounds()
        if max_top <= 0:
            self._chat_top = None
            return False
        cur = max_top if self._chat_top is None else min(self._chat_top, max_top)
        new_top = max(0, min(max_top, cur + delta))
        self._chat_top = None if new_top >= max_top else new_top
        return new_top != cur

    def _main_command(self, name: str) -> None:
        n = len(self.rows)
        if self.view == "chat":
            if name in ("up", "down"):
                prev = self.focus
                self._step_rows(-1 if name == "up" else 1)
                if self.focus == prev:
                    self._scroll_chat(-1 if name == "up" else 1)
                return
            if name in ("lt", "rt"):
                self._scroll_chat(-PAGE if name == "lt" else PAGE)
                return
            if name == "first":
                self._scroll_chat(-10_000)
                return
            if name == "last" and n:
                self._chat_top = None
                self.focus = n - 1
                return
        if name in ("up", "down"):
            self._step_rows(-1 if name == "up" else 1)
        elif name in ("lt", "rt"):                       # Page Up / Down, a controller's triggers
            self._step_rows(-1 if name == "lt" else 1, PAGE)
        elif name in ("first", "last") and n:            # Home / End
            ends = [k for k, r in enumerate(self.rows) if r.focusable]
            if ends:
                self.focus = ends[0] if name == "first" else ends[-1]
        elif name in ("left", "right") and 0 <= self.focus < n:
            fn = self.rows[self.focus].left if name == "left" else self.rows[self.focus].right
            if fn:
                fn()
        elif name == "a" and 0 <= self.focus < n:
            row = self.rows[self.focus]
            if row.action:
                if row.key not in ("quit",):
                    self.couch.sounds.play("select")
                row.action()
        elif name in ("b", "start"):
            self.resume() if self.game is not None else self.close_menu(self.couch._come_back)

    def _grid_command(self, name: str) -> None:
        n, cols = len(self.grid), self.GRID_COLS
        i = self.grid_focus
        if name == "left" and i % cols:
            self.grid_focus = i - 1
        elif name == "right" and i % cols < cols - 1 and i + 1 < n:
            self.grid_focus = i + 1
        elif name == "up" and i >= cols:
            self.grid_focus = i - cols
        elif name == "down" and i + cols < n:
            self.grid_focus = i + cols
        elif name == "down" and i // cols < (n - 1) // cols:
            self.grid_focus = n - 1
        elif name in ("first", "last") and n:
            self.grid_focus = 0 if name == "first" else n - 1
        elif name in ("lt", "rt") and n:                 # a row up / down, as far as it goes
            self.grid_focus = max(0, i - cols) if name == "lt" else min(n - 1, i + cols)
        elif name == "a" and 0 <= i < n:
            self.switch_to(self.grid[i])
        elif name == "b":
            self.close_switch()

    KEYS = {Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
            Qt.Key_Return: "a", Qt.Key_Enter: "a", Qt.Key_Space: "a", Qt.Key_Escape: "b", Qt.Key_Backspace: "b",
            Qt.Key_Home: "first", Qt.Key_End: "last", Qt.Key_PageUp: "lt", Qt.Key_PageDown: "rt", Qt.Key_Back: "b"}

    def keyPressEvent(self, e) -> None:                       # noqa: N802
        if e.spontaneous() and not e.isAutoRepeat():
            self._keys_down.add(e.key())                 # (let go before the game gets the focus back)
        self.pointer(False)
        kb = self.keyboard
        steam = self.couch.key_action(e.key())          # (a Steam controller's buttons, sent as keys by Steam)
        self._prompts_for_keys(e.key(), steam)
        if kb is not None:
            # the same typing as couch mode's keyboard: a Steam controller's A arrives as Enter, which finished
            # (sent) the message at once, so nothing could be typed with it; on a real keyboard Enter submits at once
            from_pad = self.couch.pads.steam_as_keys() or (not kb.typed and bool(self.couch.pad_family))
            done = kb.key(e.key(), e.text(), steam, from_pad, e.isAutoRepeat(), e.modifiers())
            if done == "a":
                self.couch.sounds.play("select")
            elif done in ("up", "down", "left", "right"):
                self.couch.sounds.play("move")
            self.update()
            return
        if e.key() == Qt.Key_Backtab and not e.isAutoRepeat():
            self.command("home")                         # Shift+Tab opened it from the game: it closes it too
            return
        ctrl = bool(e.modifiers() & Qt.ControlModifier)
        alt = bool(e.modifiers() & Qt.AltModifier)
        if steam is None:
            if e.key() == Qt.Key_Tab and not (ctrl or alt):
                self._step_tab()
                return
            if self.view == "friends" and not alt and ((ctrl and e.key() == Qt.Key_F) or
                                                       (not ctrl and e.key() == Qt.Key_Slash)):
                self.find_friend()
                self.keyboard.typed = True
                self.update()
                return
            if self.view == "friends" and e.key() == Qt.Key_Delete and self.friend_query and not (ctrl or alt):
                self._found("")
                return
            if self.view in ("friends", "chat") and ((ctrl and e.key() == Qt.Key_V) or
                                                     (bool(e.modifiers() & Qt.ShiftModifier) and
                                                      e.key() == Qt.Key_Insert)):
                clip = QApplication.clipboard()
                pasted = " ".join((clip.text() if clip is not None else "").splitlines())
                if pasted:
                    self.find_friend() if self.view == "friends" else self._write()
                    self.keyboard.typed = True
                    self.keyboard.type(pasted)
                    self.update()
                return
            if self.view == "main" and e.key() == Qt.Key_M and not (ctrl or alt) and not e.isAutoRepeat() and \
                    self.volume is not None:
                self._toggle_mute()
                return
        name = steam or self.KEYS.get(e.key())
        if name is None and not (ctrl or alt) and e.text().strip() and e.text().isprintable():
            if self.view == "friends":
                self.find_friend()                       # a name typed on a real keyboard: the search
                self.keyboard.typed = True
                self.keyboard.type(e.text())
                self.update()
                return
            if self.view == "chat":
                self._write()                            # a message typed on a real keyboard: straight into chat
                self.keyboard.typed = True
                self.keyboard.type(e.text())
                self.update()
                return
        if name and e.isAutoRepeat() and name not in ("up", "down", "left", "right", "lt", "rt"):
            return                                       # a held button presses once (it sent 230 invites)
        if name:
            self.command(name)

    def _step_tab(self) -> None:
        """Tab cycles through every tile in Switch game and wraps around at the bottom of a list."""
        if self.busy or (self._shown < 0.3 and self._closing_then is not None):
            return
        if self.view == "switch":
            if len(self.grid) > 1:
                self.grid_focus = (self.grid_focus + 1) % len(self.grid)
                self.couch.sounds.play("move")
                self._redraw()
            return
        focusable = [k for k, r in enumerate(self.rows) if r.focusable]
        if len(focusable) > 1:
            pos = next((i for i, k in enumerate(focusable) if k > self.focus), 0)
            self.focus = focusable[pos]
            self.couch.sounds.play("move")
            self._redraw()

    def _prompts_for_keys(self, key: int, steam: str | None) -> None:
        """The button prompts follow what is used: a real keyboard's keys show Enter / Esc, a Steam controller sent
        as keys its own buttons (as in couch mode). Enter, Esc and Backspace alone do not switch: Steam's desktop
        layout sends them for the A / B of other controllers."""
        c = self.couch
        before = c.pad_family
        if steam is not None:
            c.pad_family = "steam"
        elif c.pad_family and (key in self.KEYS or key in (Qt.Key_Tab, Qt.Key_Slash, Qt.Key_M, Qt.Key_Delete)) and \
                key not in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Return, Qt.Key_Enter) and \
                not c.pads.steam_as_keys():
            c.pad_family = ""
        if c.pad_family != before and self.view in ("main", "friends", "friend", "chat"):
            self._build()

    def keyReleaseEvent(self, e) -> None:                     # noqa: N802
        if not e.isAutoRepeat():
            self._keys_down.discard(e.key())
            self._maybe_released()

    def _maybe_released(self) -> None:
        if self._release_wait and not self._keys_down and not self._buttons_down:
            self._released()

    def changeEvent(self, e) -> None:                         # noqa: N802
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self._keys_down.clear()                      # (their release goes elsewhere now)
            self._buttons_down = False
            self._maybe_released()
        super().changeEvent(e)

    def wheelEvent(self, e) -> None:                          # noqa: N802
        """A mouse wheel — and a Steam controller's left trackpad, a scroll wheel in Steam's desktop layout — moves
        the selection: once per notch (wheel_steps), however many parts the mouse sends a notch in."""
        step = self._wheel.steps(e)
        if not step or self.keyboard is not None:
            return
        if self.cursor().shape() == Qt.ArrowCursor:
            h = self._hit_at(e.position())
            if h and h[0] in ("vol_bar", "dec", "inc", "mute") and self.volume is not None:
                self._set_volume(self.volume + (5 if step > 0 else -5), snap=False)
                return
        if self.view == "chat":
            if self._scroll_chat(-step):
                self.couch.sounds.play("move")
                self._redraw()
            return
        if self.view == "switch":
            n, cols = len(self.grid), self.GRID_COLS
            if n > cols and self.cursor().shape() == Qt.ArrowCursor:
                i = self.grid_focus
                if step > 0 and i >= cols:
                    i -= cols
                elif step < 0 and i // cols < (n - 1) // cols:
                    i = min(n - 1, i + cols)
            else:
                i = max(0, min(n - 1, self.grid_focus - step))
            if i != self.grid_focus:
                self.grid_focus = i
                self.couch.sounds.play("move")
                self._redraw()
        else:
            self.command("up" if step > 0 else "down")

    def _hit_at(self, pos, kb_only: bool = False) -> tuple[str, int] | None:
        for r, kind, idx in reversed(self._hits):
            if kb_only and kind not in ("key", "kb_close", "kb_panel"):
                continue
            if r.contains(pos):
                return kind, idx
        return None

    def mouseMoveEvent(self, e) -> None:                      # noqa: N802
        self.pointer(True)
        pos = e.position()
        if self.keyboard is not None:
            h = self._hit_at(pos, kb_only=True)
            moved = False
            if h and h[0] == "key" and (self.keyboard.row, self.keyboard.col) != divmod(h[1], 100):
                self.keyboard.hit(h[1], False)
                self.couch.sounds.play("move")
                moved = True
            if h != self._hovered:
                self._hovered = h
                moved = True
            if moved:
                self.update()
            return
        if self._dragging_vol and e.buttons() & Qt.LeftButton:
            self._volume_from_x(pos.x())
            return
        if self._dragging_scroll and e.buttons() & Qt.LeftButton:
            self._scroll_from_y(pos.y())
            return
        h = self._hit_at(pos)
        dirty = False
        if h != self._hovered:
            self._hovered = h
            dirty = True
        if h is not None:
            kind, idx = h
            if kind in ("row", "dec", "inc", "vol_bar", "mute", "clear") and idx != self.focus and \
                    self.view in ("main", "friends", "friend", "chat", "confirm", "starting"):
                self.focus = idx
                self.couch.sounds.play("move")
                dirty = True
            elif kind == "tile" and idx != self.grid_focus and self.view == "switch":
                self.grid_focus = idx
                self.couch.sounds.play("move")
                dirty = True
        if dirty:
            self._redraw()

    def leaveEvent(self, e) -> None:                          # noqa: N802
        if self._hovered is not None:
            self._hovered = None
            self._redraw()
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:                     # noqa: N802
        self._buttons_down = True                        # (let go before the game gets the focus back)
        if e.button() in (Qt.RightButton, Qt.BackButton):
            if self.keyboard is not None and not self.couch.pads.steam_as_keys():
                self.keyboard.cancel()
                self.update()
                return
            self.command("b")
            return
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if not self.couch.pads.steam_as_keys() and self.couch.pad_family:
            self.couch.pad_family = ""
            if self.view in ("main", "friends", "friend", "chat"):
                self._build()
        if self.keyboard is not None:
            h = self._hit_at(pos, kb_only=True)
            if h and h[0] == "key":
                self.keyboard.typed = not self.couch.pads.steam_as_keys()
                self.couch.sounds.play("select")
                self.keyboard.hit(h[1], True)
            elif h is None or h[0] == "kb_close":
                self.keyboard.cancel()
            self.update()
            return
        h = self._hit_at(pos)
        if h is None:
            if pos.x() < self._panel_rect(1.0).left():
                # a click on the live game beside the panel: straight back to the game
                self.resume() if self.game is not None else self.close_menu(self.couch._come_back)
            return
        kind, idx = h
        if kind == "close":
            self.resume() if self.game is not None else self.close_menu(self.couch._come_back)
        elif kind == "back":
            self.command("b")
        elif kind == "hint":
            self._activate_hint(idx)
        elif kind == "vol_bar":
            self.focus = idx
            self._dragging_vol = True
            self._volume_from_x(pos.x())
        elif kind == "mute":
            self.focus = idx
            self._toggle_mute()
        elif kind == "clear":
            self.couch.sounds.play("select")
            self._found("")
        elif kind == "scrollbar":
            self._dragging_scroll = True
            self._scroll_from_y(pos.y())
        elif kind == "row":
            self.focus = idx
            self.command("a")
            if self.keyboard is not None and not self.couch.pads.steam_as_keys():
                self.keyboard.typed = True
        elif kind in ("dec", "inc"):                     # the ◂ ▸ of a choice (the game's volume)
            self.focus = idx
            self.command("left" if kind == "dec" else "right")
        elif kind == "tile":
            self.grid_focus = idx
            self.command("a")

    def _activate_hint(self, idx: int) -> None:
        if not 0 <= idx < len(self._hint_actions):
            return
        act = self._hint_actions[idx]
        if act in ("x", "/"):
            self.couch.sounds.play("select")
            self.find_friend()
            if self.keyboard is not None and not self.couch.pads.steam_as_keys():
                self.keyboard.typed = True
            self.update()
        elif act == "M":
            self._toggle_mute()
        elif act in ("a", "b", "start"):
            self.command(act)

    def _scroll_from_y(self, y: float) -> None:
        geom = self._scroll_geom
        if geom is None:
            return
        top_y, track_h, bh, max_top, mode = geom
        if max_top <= 0:
            return
        frac = max(0.0, min(1.0, (y - top_y - bh / 2) / max(1.0, track_h - bh)))
        new_top = round(frac * max_top)
        if mode == "chat":
            self._chat_top = None if new_top >= max_top else new_top
        elif mode == "rows":
            self._row_top = new_top
            if self.focus < new_top:
                nxt = next((k for k in range(new_top, len(self.rows)) if self.rows[k].focusable), None)
                if nxt is not None:
                    self.focus = nxt
            else:
                s = self._s()
                bottom = self.height() - 96 * s + 0.5
                heights = [self._row_height(r) for r in self.rows]
                last = new_top
                while last + 1 < len(self.rows) and sum(heights[new_top:last + 2]) <= bottom - top_y + 10 * s:
                    last += 1
                if self.focus > last:
                    prv = next((k for k in range(last, new_top - 1, -1) if self.rows[k].focusable), None)
                    if prv is not None:
                        self.focus = prv
        elif mode == "grid":
            cols = self.GRID_COLS
            self._grid_top = new_top
            frow = self.grid_focus // cols
            col = self.grid_focus % cols
            if frow < new_top:
                self.grid_focus = min(len(self.grid) - 1, new_top * cols + col)
            else:
                s = self._s()
                tw = (self._content_rect(1.0).width() - 88 * s - 22 * s * (cols - 1)) / cols
                vis = max(1, int(((self.height() - 100 * s) - top_y + 22 * s) // (tw * 1.5 + 22 * s)))
                if frow >= new_top + vis:
                    self.grid_focus = min(len(self.grid) - 1, (new_top + vis - 1) * cols + col)
        self._redraw()

    def mouseDoubleClickEvent(self, e) -> None:               # noqa: N802
        """A double click is one click: its second press did the row's action again (Quit pressed twice — the game
        quit without its question; a switch flipped on and back off)."""
        self._buttons_down = True

    def mouseReleaseEvent(self, e) -> None:                   # noqa: N802
        if e.button() == Qt.LeftButton:
            self._dragging_vol = False
            self._dragging_scroll = False
        self._buttons_down = bool(e.buttons())
        self._maybe_released()

    # ================================================================= painting
    def _s(self) -> float:
        return min(self.couch._s(), max(0.5, self.height() / 1000))     # the panel always fits the screen

    def paintEvent(self, _e) -> None:                         # noqa: N802
        t = self._shown
        self._hits = []
        self._pending_art = False
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            if self._game_hidden():                     # the game went (minimized itself): what stands in for it
                self._paint_game_stand_in(p)
                if t > 0.001:
                    self._paint_backdrop(p, t, over=True)
            elif t > 0.001:
                self._paint_backdrop(p, t, over=False)  # (also clears what was drawn before)
            else:
                p.setCompositionMode(QPainter.CompositionMode_Source)
                p.fillRect(self.rect(), Qt.transparent)
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            if t <= 0.001:
                return
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            if self._sliding():
                self._paint_slid(p, t)
            else:
                self._paint_panel(p, t)
        except Exception:
            log.exception("quick menu: drawing failed")
        finally:
            p.end()

    def _sliding(self) -> bool:
        return self._anim.state() == QVariantAnimation.Running and self.keyboard is None and \
            self._wide_anim.state() != QVariantAnimation.Running

    def _paint_slid(self, p: QPainter, t: float) -> None:
        """A frame of the slide in or out: the panel, drawn once as it is at rest, moved into place (the whole panel
        drawn again for every frame held the slide to ~35 frames a second on a 4K screen)."""
        margin = 6 * self._s()                            # (the accent line's left half)
        if self._layer is None:
            rest = self._panel_rect(1.0)
            dpr = self.devicePixelRatioF()
            img = QImage(max(1, round((rest.width() + margin) * dpr)), max(1, round(self.height() * dpr)),
                         QImage.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr)
            img.fill(Qt.transparent)
            lp = QPainter(img)
            lp.setRenderHint(QPainter.Antialiasing)
            lp.setRenderHint(QPainter.TextAntialiasing)
            lp.setRenderHint(QPainter.SmoothPixmapTransform)
            lp.translate(margin - rest.left(), 0)
            self._paint_panel(lp, 1.0)
            lp.end()
            self._layer = img
            self._layer_hits = list(self._hits)           # (where things are once it is in place)
            self._layer_pending = self._pending_art
        self._hits = list(self._layer_hits)
        self._pending_art = self._layer_pending
        p.save()
        p.setOpacity(min(1.0, t * 1.4))
        p.drawImage(QPointF(self._panel_rect(t).left() - margin, 0), self._layer)
        p.restore()

    def _widths(self) -> tuple[float, float]:
        """The panel's width over the game (narrow) and for the game grid (wide)."""
        s, W = self._s(), self.width()
        return min(620 * s, W * 0.44), min(1180 * s, W * 0.72)

    def _slid(self, pw: float, t: float) -> QRectF:
        s, W, H = self._s(), self.width(), self.height()
        x = W - pw * (0.35 + 0.65 * t) if t < 1 else W - pw
        return QRectF(x + (1 - t) * 60 * s, 0, pw, H)

    def _panel_rect(self, t: float) -> QRectF:
        narrow, wide = self._widths()
        return self._slid(narrow + (wide - narrow) * self._wide, t)

    def _content_rect(self, t: float) -> QRectF:
        """Where the panel's content goes: laid out for the width the panel is heading for (the grid's, as it widens
        for it), so nothing is laid out again, nor its pictures scaled again, for every frame of the widening."""
        narrow, wide = self._widths()
        return self._slid(wide if self.view == "switch" else narrow, t)

    def _paint_game_stand_in(self, p: QPainter) -> None:
        """Behind the menu when the game minimized itself: its picture from just before (a game seen doing it), or
        its artwork (the first time), else the theme's dark background — never the desktop."""
        p.setCompositionMode(QPainter.CompositionMode_Source)
        g = self.game
        if self._still is not None:
            p.drawPixmap(self.rect(), self._still)
        else:
            url = self.couch.hero_url(g) if g is not None else None
            img = self.couch.images.fill(url, self.width(), self.height()) if url else None
            if img is not None:
                p.drawImage(QRectF(self.rect()), img, QRectF(img.rect()))
            else:
                self._pending_art = bool(url)
                p.fillRect(self.rect(), QColor(theme.CURRENT.bg0))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)

    def _paint_backdrop(self, p: QPainter, t: float, over: bool) -> None:
        """The game behind stays visible, darkened — more towards the panel. One pass over the screen (a pass over
        a 4K screen costs milliseconds): the plain shade on the left quarter, and one line of the shade with its
        gradient, made once, repeated down the rest. ``over`` False also replaces what the window showed before
        (no clearing first)."""
        W, H = self.width(), self.height()
        gx = W * 0.25
        p.save()
        if not over:
            p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(QRectF(0, 0, gx, H), QColor(4, 6, 12, int(120 * t)))
        brush = QBrush(self._shade_line(t, gx))
        brush.setTransform(QTransform.fromTranslate(gx, 0))
        p.fillRect(QRectF(gx, 0, W - gx, H), brush)
        p.restore()

    def _shade_line(self, t: float, gx: float) -> QImage:
        """One line of the shade from ``gx`` to the right edge, at the screen's real pixels (kept for the few
        values of ``t`` an animation goes through)."""
        dpr = self.devicePixelRatioF()
        key = (round((self.width() - gx) * dpr), round(t * 100), dpr)
        line = self._shades.get(key)
        if line is None:
            line = QImage(max(1, key[0]), 1, QImage.Format_ARGB32_Premultiplied)
            line.fill(QColor(4, 6, 12, int(120 * t)))
            lp = QPainter(line)
            g = QLinearGradient(0, 0, line.width(), 0)          # (from a quarter of the screen to its edge)
            g.setColorAt(0.0, QColor(4, 6, 12, 0))
            g.setColorAt(1.0, QColor(4, 6, 12, int(150 * t)))
            lp.fillRect(line.rect(), g)
            lp.end()
            line.setDevicePixelRatio(dpr)
            if len(self._shades) > 40:
                self._shades.clear()
            self._shades[key] = line
        return line

    def _panel_back(self, panel: QRectF) -> QRectF:
        """The panel's background on screen: from its left edge to the screen's right, top to bottom (its rounded
        corners lie off-screen, so plain rectangles draw it — a rounded path over a third of a 4K screen, twice,
        was the dearest part of every frame)."""
        return QRectF(panel.left(), 0, max(0.0, self.width() - panel.left()), self.height())

    def _paint_panel(self, p: QPainter, t: float) -> None:
        self._vol_track = None
        self._scroll_geom = None
        self._hint_actions = []
        s, H = self._s(), self.height()
        panel = self._panel_rect(t)
        back = self._panel_back(panel)
        p.save()
        p.setOpacity(min(1.0, t * 1.4))
        bg = QColor(theme.CURRENT.bg0)
        bg.setAlpha(242)
        p.fillRect(back, bg)
        glow = QLinearGradient(panel.left(), 0, panel.left() + 220 * s, 0)    # a soft accent light on its edge
        acc = QColor(theme.CURRENT.accent)
        acc.setAlpha(46)
        glow.setColorAt(0.0, acc)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(back.left(), 0, min(back.width(), 220 * s), H), glow)
        edge = QColor(theme.CURRENT.accent)
        edge.setAlpha(150)
        p.setPen(QPen(edge, 2 * s))
        p.drawLine(panel.left(), 30 * s, panel.left(), H - 30 * s)
        pad = 44 * s
        content = self._content_rect(t)
        inner = content.adjusted(pad, 0, -pad, 0)
        if self._wide_anim.state() == QVariantAnimation.Running:
            # widening for the grid (or back): the content already where it ends up, shown as the panel uncovers
            # it, fading in
            p.setClipRect(back.adjusted(2 * s, 0, 0, 0), Qt.IntersectClip)
            k = self._wide if self.view == "switch" else 1.0 - self._wide
            p.setOpacity(p.opacity() * max(0.0, min(1.0, (k - 0.25) / 0.75)))
        y = self._paint_header(p, inner, 34 * s)
        if self.view == "switch":
            self._paint_switch(p, inner, y)
        elif self.view in ("confirm", "starting") and self.target is not None:
            y = self._paint_now_playing(p, inner, y + 18 * s, self.target)
            self._paint_target_text(p, inner, y + 22 * s)
        elif self.view == "friend" and self.friend is not None:
            y = self._paint_friend_card(p, inner, y + 18 * s)
            self._paint_rows(p, inner, y + 24 * s)
        elif self.view == "friends":
            self._paint_rows(p, inner, y + 14 * s)
        elif self.view == "chat" and self.friend is not None:
            y = self._paint_chat_head(p, inner, y + 14 * s)
            self._paint_rows(p, inner, y + 12 * s)
        else:
            if self.game is not None:                     # (the card gives way when the rows need the room)
                room = self.height() - 96 * s - (y + 18 * s + 24 * s) - self._rows_height()
                h = max(130 * s, min(inner.width() * 0.43, room))
                y = self._paint_now_playing(p, inner, y + 18 * s, height=h)
            self._paint_rows(p, inner, y + 24 * s)
        self._paint_hints(p, inner)
        if self.flash and time.monotonic() < self._flash_until:
            self._paint_flash(p, inner)
        if self.busy:
            self._paint_busy(p, panel)
        p.restore()
        if self.keyboard is not None:
            self.keyboard.paint(p)                      # (over everything, like on couch mode)

    def _paint_header(self, p: QPainter, r: QRectF, y: float) -> float:
        from ezgd.ui.couch import clock_text
        s = self._s()
        p.setFont(self.couch._font(20 * s, 900))
        fm = QFontMetrics(p.font())
        p.setPen(QColor(theme.CURRENT.accent))
        p.drawText(QRectF(r.left(), y, 60 * s, 30 * s), Qt.AlignLeft | Qt.AlignVCenter, "EZ")
        p.setPen(QColor("white"))
        p.drawText(QRectF(r.left() + fm.horizontalAdvance("EZ "), y, 200 * s, 30 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   "GAMES")
        cd = 30 * s
        cr = QRectF(r.right() - cd, y, cd, cd)
        hot_close = self._hovered == ("close", 0)
        p.setPen(QPen(QColor(255, 255, 255, 220 if hot_close else 75), 1.4 * s))
        p.setBrush(QColor(theme.CURRENT.danger) if hot_close else QColor(255, 255, 255, 22))
        p.drawEllipse(cr)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("white"), 2.2 * s, Qt.SolidLine, Qt.RoundCap))
        xk = cd * 0.23
        cc = cr.center()
        p.drawLine(QPointF(cc.x() - xk, cc.y() - xk), QPointF(cc.x() + xk, cc.y() + xk))
        p.drawLine(QPointF(cc.x() + xk, cc.y() - xk), QPointF(cc.x() - xk, cc.y() + xk))
        self._hits.append((cr.adjusted(-6 * s, -6 * s, 6 * s, 6 * s), "close", 0))
        p.setFont(self.couch._font(20 * s, 700))
        p.setPen(QColor(255, 255, 255, 200))
        p.drawText(QRectF(r.left(), y, r.width() - cd - 14 * s, 30 * s), Qt.AlignRight | Qt.AlignVCenter, clock_text())
        sec = {"switch": "SWITCH GAME", "confirm": "SWITCH GAME", "starting": "STARTING", "friends": "FRIENDS",
               "friend": "FRIEND", "chat": "CHAT"}.get(self.view, "QUICK MENU")
        sy = y + 38 * s
        p.setFont(self.couch._font(15 * s, 800))
        if self.view in ("switch", "confirm", "friends", "friend", "chat"):
            hot_back = self._hovered == ("back", 0)
            tw = QFontMetrics(p.font()).horizontalAdvance(sec)
            br = QRectF(r.left() - 8 * s, sy - 3 * s, tw + 38 * s, 28 * s)
            if hot_back:
                bp = QPainterPath()
                bp.addRoundedRect(br, 8 * s, 8 * s)
                p.fillPath(bp, QColor(255, 255, 255, 32))
            fg = QColor("white") if hot_back else QColor(255, 255, 255, 175)
            p.setPen(QPen(fg, 2.0 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            ax, ay = r.left() + 3 * s, sy + 11 * s
            p.drawLine(QPointF(ax + 12 * s, ay), QPointF(ax, ay))
            p.drawLine(QPointF(ax + 5 * s, ay - 4.5 * s), QPointF(ax, ay))
            p.drawLine(QPointF(ax + 5 * s, ay + 4.5 * s), QPointF(ax, ay))
            p.setPen(fg)
            p.drawText(QRectF(r.left() + 22 * s, sy, r.width() - 22 * s, 22 * s), Qt.AlignLeft | Qt.AlignVCenter, sec)
            self._hits.append((br.adjusted(-4 * s, -4 * s, 6 * s, 4 * s), "back", 0))
        else:
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(QRectF(r.left(), sy, r.width(), 22 * s), Qt.AlignLeft | Qt.AlignVCenter, sec)
            if self.view == "main" and self._key_mode():
                p.setFont(self.couch._font(13 * s, 700))
                p.setPen(QColor(255, 255, 255, 95))
                p.drawText(QRectF(r.left(), sy, r.width(), 22 * s), Qt.AlignRight | Qt.AlignVCenter, "Shift+Tab")
        return y + 66 * s

    def _paint_target_text(self, p: QPainter, r: QRectF, y: float) -> None:
        """Under the target's card: the question (and its buttons), or "Starting…"."""
        s, e = self._s(), self.target
        if self.view == "confirm" or self.start_error:
            p.setFont(self.couch._font(28 * s, 900))
            p.setPen(QColor("white"))
            text = f"Play {e.title}?" if self.view == "confirm" else "Not started"
            p.drawText(QRectF(r.left(), y, r.width(), 40 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(text, Qt.ElideRight, int(r.width())))
            self._paint_rows(p, r, y + 56 * s)
            return
        p.setFont(self.couch._font(28 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(r.left(), y, r.width(), 40 * s), Qt.AlignLeft | Qt.AlignVCenter, "Starting")
        t = time.monotonic()
        x0 = r.left() + QFontMetrics(p.font()).horizontalAdvance("Starting") + 18 * s
        for k in range(3):                               # three pulsing dots while it starts
            a = 0.35 + 0.65 * max(0.0, math.sin(t * 5 - k * 0.7))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(255 * a)))
            p.drawEllipse(QPointF(x0 + k * 22 * s, y + 26 * s), 5 * s, 5 * s)
        p.setBrush(Qt.NoBrush)
        g = self.game
        p.setFont(self.couch._font(17 * s, 600))
        p.setPen(QColor(255, 255, 255, 170))
        note = (f"{g.title} is suspended — it waits right where you left it." if g is not None and
                g.id in self.couch.suspended else "It gets the screen as soon as it is up.")
        p.drawText(QRectF(r.left(), y + 50 * s, r.width(), 60 * s), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                   note)
        QTimer.singleShot(60, self, self._redraw)          # (the dots are in the panel: only it is drawn again)

    def _paint_now_playing(self, p: QPainter, r: QRectF, y: float, entry=None, height=None) -> float:
        s = self._s()
        g = entry if entry is not None else self.game
        card = QRectF(r.left(), y, r.width(), height or r.width() * 0.43)
        radius = 20 * s
        path = QPainterPath()
        path.addRoundedRect(card, radius, radius)
        img = self.couch.images.fill(self.couch.hero_url(g), int(card.width()), int(card.height()), radius=radius)
        if img is not None:
            p.drawImage(card, img, QRectF(img.rect()))
        else:
            self._pending_art = True
            g2 = QLinearGradient(card.topLeft(), card.bottomRight())
            g2.setColorAt(0, QColor(theme.CURRENT.accent).darker(260))
            g2.setColorAt(1, QColor(theme.CURRENT.bg2))
            p.fillPath(path, g2)
        shade = QLinearGradient(0, card.top() + card.height() * 0.35, 0, card.bottom())
        shade.setColorAt(0, QColor(0, 0, 0, 0))
        shade.setColorAt(1, QColor(0, 0, 0, 215))
        p.fillPath(path, shade)
        p.setPen(QPen(QColor(255, 255, 255, 34), 1.2 * s))
        p.drawPath(path)
        sus = self._suspended(g)
        chip_text = "SUSPENDED" if sus else "PLAYING" if g.id in self.couch.running else \
            "STARTING" if self.view == "starting" else "READY TO PLAY"
        chip_color = QColor(theme.CURRENT.warning if sus else theme.CURRENT.success if g.id in self.couch.running
                            else theme.CURRENT.accent)
        p.setFont(self.couch._font(14 * s, 900))
        cw = QFontMetrics(p.font()).horizontalAdvance(chip_text) + 50 * s
        chip = QRectF(card.left() + 22 * s, card.bottom() - 100 * s, cw, 32 * s)
        cp = QPainterPath()
        cp.addRoundedRect(chip, 16 * s, 16 * s)
        p.fillPath(cp, QColor(8, 10, 16, 200))
        p.setPen(QPen(chip_color, 1.5 * s))
        p.drawPath(cp)
        dot = QRectF(chip.left() + 14 * s, chip.center().y() - 5 * s, 10 * s, 10 * s)
        p.setPen(Qt.NoPen)
        p.setBrush(chip_color)
        if sus:
            p.drawRect(QRectF(dot.left(), dot.top(), 3.5 * s, dot.height()))
            p.drawRect(QRectF(dot.right() - 3.5 * s, dot.top(), 3.5 * s, dot.height()))
        else:
            p.drawEllipse(dot)
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor("white"))
        p.drawText(chip.adjusted(30 * s, 0, -10 * s, 0), Qt.AlignLeft | Qt.AlignVCenter, chip_text)
        if self.session is not None and entry is None:
            p.setFont(self.couch._font(16 * s, 700))
            p.setPen(QColor(255, 255, 255, 190))
            p.drawText(QRectF(chip.right() + 14 * s, chip.top(), card.width(), chip.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, session_text(self.session))
        p.setFont(self.couch._font(30 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(card.left() + 22 * s, card.bottom() - 62 * s, card.width() - 44 * s, 46 * s),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(g.title, Qt.ElideRight, int(card.width() - 44 * s)))
        return card.bottom()

    def _row_height(self, row: Row) -> float:
        if row.kind == "msg":
            s = self._s()
            fm = QFontMetrics(self.couch._font(17 * s, 600))
            w = int(self._bubble_width() - 32 * s)
            h = fm.boundingRect(0, 0, max(40, w), 100000, int(Qt.TextWordWrap), row.label).height()
            return h + 30 * s + 10 * s
        if row.kind == "header" and row.extra.get("note"):
            return 64 * self._s()
        return (44 if row.kind == "header" else (92 if row.kind in ("game", "friend") else 78 if row.note else 62) + 10
                ) * self._s()

    def _paint_friend_card(self, p: QPainter, r: QRectF, y: float) -> float:
        """The friend view's top: their picture, name and what they are doing."""
        from ezgd.ui.couch_friends import paint_avatar, state_color
        s, f = self._s(), self.friend
        a = 116 * s
        if not paint_avatar(p, self.couch, f, QRectF(r.left() + 4 * s, y + 4 * s, a, a), s):
            self._pending_art = True
        x = r.left() + a + 30 * s
        p.setFont(self.couch._font(30 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, y + 16 * s, r.right() - x, 44 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(f.get("name", ""), Qt.ElideRight, int(r.right() - x)))
        p.setFont(self.couch._font(18 * s, 700))
        p.setPen(state_color(f))
        p.drawText(QRectF(x, y + 64 * s, r.right() - x, 30 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(f.get("status") or "", Qt.ElideRight, int(r.right() - x)))
        return y + a + 12 * s

    def _rows_height(self) -> float:
        return sum(self._row_height(row) for row in self.rows)

    def _paint_rows(self, p: QPainter, r: QRectF, y: float) -> None:
        s = self._s()
        y0 = y
        bottom = self.height() - 96 * s + 0.5              # (+ rounding)
        heights = [self._row_height(row) for row in self.rows]
        if getattr(self, "view", "main") == "chat" and len(self.rows) >= 2 and self.rows[-1].key == "write" and \
                y + sum(heights) - 10 * s > bottom:
            _h, msg_bottom, max_top = self._chat_bounds(y, bottom)
            top = max_top if self._chat_top is None else max(0, min(self._chat_top, max_top))
            if self.focus < len(self.rows) - 1:
                if self.focus < top:
                    top = self.focus
                while top < self.focus and y + sum(heights[top:self.focus + 1]) - 10 * s > msg_bottom:
                    top += 1
            self._chat_top = None if top >= max_top else top
            vis = 0
            for k in range(top, len(self.rows) - 1):
                row = self.rows[k]
                h = heights[k] - 10 * s
                if y + h > msg_bottom and vis > 0:
                    break
                vis += 1
                if row.kind == "msg":
                    self._bubble(p, row, QRectF(r.left(), y, r.width(), min(h, msg_bottom - y)))
                elif row.kind == "header":
                    p.setFont(self.couch._font(16 * s, 700))
                    p.setPen(QColor(theme.CURRENT.accent).lighter(150) if row.extra.get("tip") else
                             QColor(theme.CURRENT.warning))
                    p.drawText(QRectF(r.left() + 4 * s, y + 4 * s, r.width(), 50 * s),
                               Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap, row.label)
                else:
                    rect = QRectF(r.left(), y, r.width(), h)
                    arrows = self._row(p, row, rect, k == self.focus)
                    self._hits.append((rect, "row", k))
                    for hr, kind in arrows or ():
                        self._hits.append((hr, kind, k))
                y += heights[k]
            if max_top > 0:
                self._draw_scrollbar(p, r.right() + 18 * s, y0, msg_bottom - y0, top, max_top, vis,
                                     len(self.rows) - 1, "chat")
            wh = heights[-1] - 10 * s
            wrect = QRectF(r.left(), bottom - wh, r.width(), wh)
            self._row(p, self.rows[-1], wrect, self.focus == len(self.rows) - 1)
            self._hits.append((wrect, "row", len(self.rows) - 1))
            return
        # a list longer than the panel (friends): it starts at the row that keeps the focus in view
        top = min(getattr(self, "_row_top", 0), self.focus) if self.focus < len(self.rows) else 0
        while top < self.focus and y + sum(heights[top:self.focus + 1]) - 10 * s > bottom:
            top += 1
        overflow = y + sum(heights) - 10 * s > bottom
        self._row_top = top if overflow else 0
        drawn = 0
        for k, row in enumerate(self.rows):
            if k < self._row_top:
                continue
            if row.kind == "msg":
                h = self._row_height(row) - 10 * s
                if y + h > bottom:
                    break
                self._bubble(p, row, QRectF(r.left(), y, r.width(), h))
                y += h + 10 * s
                drawn += 1
                continue
            if row.kind == "header" and row.extra.get("note"):
                p.setFont(self.couch._font(16 * s, 700))
                p.setPen(QColor(theme.CURRENT.accent).lighter(150) if row.extra.get("tip") else
                         QColor(theme.CURRENT.warning))
                p.drawText(QRectF(r.left() + 4 * s, y + 4 * s, r.width(), 50 * s),
                           Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap, row.label)
                y += self._row_height(row)
                drawn += 1
                continue
            if row.kind == "header":
                nxt = self.rows[k + 1] if k + 1 < len(self.rows) else None
                if nxt is None or y + self._row_height(row) + self._row_height(nxt) - 10 * s > bottom:
                    break                                   # (never a heading with nothing under it)
                p.setFont(self.couch._font(15 * s, 900))
                p.setPen(QColor(theme.CURRENT.accent).lighter(140))
                p.drawText(QRectF(r.left() + 4 * s, y + 10 * s, r.width(), 28 * s), Qt.AlignLeft | Qt.AlignVCenter,
                           row.label.upper())
                y += 44 * s
                drawn += 1
                continue
            h = self._row_height(row) - 10 * s
            if y + h > bottom:
                break
            rect = QRectF(r.left(), y, r.width(), h)
            arrows = self._row(p, row, rect, k == self.focus)
            self._hits.append((rect, "row", k))
            for hr, kind in arrows or ():              # (after the row: they are found first)
                self._hits.append((hr, kind, k))
            y += h + 10 * s
            drawn += 1
        if overflow and hasattr(self, "_draw_scrollbar"):
            max_top = len(self.rows)
            while max_top > 0 and y0 + sum(heights[max_top - 1:]) - 10 * s <= bottom:
                max_top -= 1
            max_top = max(self._row_top, min(max_top, max(0, len(self.rows) - 1)))
            if max_top > 0:
                self._draw_scrollbar(p, r.right() + 18 * s, y0, bottom - y0, self._row_top, max_top, drawn,
                                     len(self.rows), "rows")

    def _draw_scrollbar(self, p: QPainter, x: float, top_y: float, track_h: float, cur_top: int, max_top: int,
                        vis_n: int, total_n: int, mode: str) -> None:
        s = self._s()
        if max_top <= 0 or track_h < 36 * s:
            return
        hot = getattr(self, "_dragging_scroll", False) or getattr(self, "_hovered", None) == ("scrollbar", 0)
        bw = (8 if hot else 6) * s
        bx = x - bw / 2
        tp = QPainterPath()
        tp.addRoundedRect(QRectF(bx, top_y, bw, track_h), bw / 2, bw / 2)
        p.fillPath(tp, QColor(255, 255, 255, 34 if hot else 18))
        frac = max(0.14, min(0.85, vis_n / max(1, total_n)))
        bh = max(32 * s, track_h * frac)
        by = top_y + (track_h - bh) * (min(cur_top, max_top) / max(1, max_top))
        kp = QPainterPath()
        kp.addRoundedRect(QRectF(bx, by, bw, bh), bw / 2, bw / 2)
        p.fillPath(kp, QColor(255, 255, 255, 185 if hot else 95))
        self._scroll_geom = (top_y, track_h, bh, max_top, mode)
        self._hits.append((QRectF(x - 12 * s, top_y, 24 * s, track_h), "scrollbar", 0))

    def _row(self, p: QPainter, row: Row, r: QRectF, focused: bool) -> list | None:
        """One row; returns the click areas of a choice's ◂ ▸ (kind "dec" / "inc")."""
        s = self._s()
        arrows: list = []
        radius = 16 * s
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        kind_color = {"primary": QColor(theme.CURRENT.success), "danger": QColor(theme.CURRENT.danger)}.get(row.kind)
        if focused:
            for k in range(3, 0, -1):
                gp = QPainterPath()
                gp.addRoundedRect(r.adjusted(-k * 3 * s, -k * 3 * s, k * 3 * s, k * 3 * s), radius + k * 3 * s,
                                  radius + k * 3 * s)
                p.fillPath(gp, QColor(255, 255, 255, 26))
            p.fillPath(path, kind_color or QColor(255, 255, 255, 238))
        else:
            if kind_color is not None:
                c = QColor(kind_color)
                c.setAlpha(60)
                p.fillPath(path, c)
            else:
                p.fillPath(path, QColor(255, 255, 255, 18))
        light = focused and kind_color is None
        fg = QColor(11, 13, 19) if light else QColor("white")
        sub = QColor(11, 13, 19, 165) if light else QColor(255, 255, 255, 150)
        hot_sub = self._hovered[0] if focused and getattr(self, "_hovered", None) else None
        pad = 22 * s
        x = r.left() + pad
        if row.key in ICONS:
            d = 38 * s
            ir = QRectF(x - 4 * s, r.center().y() - d / 2, d, d)
            ip = QPainterPath()
            ip.addEllipse(ir)
            hot_mute = row.key == "volume" and hot_sub == "mute"
            p.fillPath(ip, QColor(0, 0, 0, 58 if hot_mute else 34) if light else
                       QColor(255, 255, 255, 58 if hot_mute else (30 if kind_color is None else 44)))
            ikey = ("volume_muted" if self.volume == 0 else "volume_low" if (self.volume or 0) < 60 else "volume") \
                if row.key == "volume" else row.key
            draw_icon(p, ikey, ir.adjusted(9 * s, 9 * s, -9 * s, -9 * s), fg, s)
            if row.key == "volume" and self.volume is not None:
                arrows.append((ir.adjusted(-4 * s, -4 * s, 4 * s, 4 * s), "mute"))
            x = ir.right() + 18 * s
        if row.kind == "friend":
            from ezgd.ui.couch_friends import paint_avatar
            d = r.height() - 22 * s
            if not paint_avatar(p, self.couch, row.extra["friend"], QRectF(x - 6 * s, r.top() + 11 * s, d, d), s):
                self._pending_art = True
            x = x - 6 * s + d + 18 * s
        if row.kind == "game" and row.entry is not None:
            th = r.height() - 16 * s
            tw = th * 2 / 3
            tr = QRectF(x - 8 * s, r.top() + 8 * s, tw, th)
            img, _title, _key = self.couch.images.tile(self.couch.capsule_url(row.entry), row.entry.cover_url,
                                                       int(tw), int(th), 8 * s, False)
            if img is not None:
                p.drawImage(tr, img, QRectF(img.rect()))
            else:
                self._pending_art = True
                tp = QPainterPath()
                tp.addRoundedRect(tr, 8 * s, 8 * s)
                p.fillPath(tp, QColor(255, 255, 255, 30))
            x = tr.right() + 18 * s
        right_w = 0.0
        if "toggle" in row.extra:                           # an on / off switch
            on = bool(row.extra["toggle"])
            tw, th = 58 * s, 32 * s
            track = QRectF(r.right() - pad - tw, r.center().y() - th / 2, tw, th)
            tp = QPainterPath()
            tp.addRoundedRect(track, th / 2, th / 2)
            p.fillPath(tp, QColor(theme.CURRENT.success) if on else
                       (QColor(0, 0, 0, 60) if light else QColor(255, 255, 255, 46)))
            knob = th - 8 * s
            kx = track.right() - 4 * s - knob if on else track.left() + 4 * s
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("white"))
            p.drawEllipse(QRectF(kx, track.top() + 4 * s, knob, knob))
            p.setBrush(Qt.NoBrush)
            right_w = tw + 12 * s
        elif row.key == "f-search" and getattr(self, "friend_query", ""):
            cd = 28 * s
            cr = QRectF(r.right() - pad - cd, r.center().y() - cd / 2, cd, cd)
            hot_clr = hot_sub == "clear"
            cp = QPainterPath()
            cp.addEllipse(cr)
            p.fillPath(cp, QColor(theme.CURRENT.danger) if hot_clr else
                       (QColor(0, 0, 0, 40) if light else QColor(255, 255, 255, 34)))
            p.setPen(QPen(QColor("white") if hot_clr else fg, 2.0 * s, Qt.SolidLine, Qt.RoundCap))
            xk = cd * 0.22
            cc = cr.center()
            p.drawLine(QPointF(cc.x() - xk, cc.y() - xk), QPointF(cc.x() + xk, cc.y() + xk))
            p.drawLine(QPointF(cc.x() + xk, cc.y() - xk), QPointF(cc.x() - xk, cc.y() + xk))
            arrows.append((cr.adjusted(-8 * s, -8 * s, 8 * s, 8 * s), "clear"))
            right_w = cd + 14 * s
        elif row.key == "volume" and self.volume is not None:
            p.setFont(self.couch._font(18 * s, 800))
            fm = QFontMetrics(p.font())
            vw = max(fm.horizontalAdvance("150 %"), fm.horizontalAdvance("Muted")) + 6 * s
            aw = 26 * s
            label_room = fm.horizontalAdvance("Game volume") + 28 * s
            bar_w = max(0.0, min(116 * s, r.right() - pad - x - label_room - vw - 2 * aw - 22 * s))
            vx = r.right() - pad - vw
            p.setPen(fg)
            p.drawText(QRectF(vx, r.top(), vw, r.height()), Qt.AlignRight | Qt.AlignVCenter, row.value)
            inc_r = QRectF(vx - aw - 4 * s, r.center().y() - 16 * s, aw, 32 * s)
            if hot_sub == "inc":
                hp = QPainterPath()
                hp.addRoundedRect(inc_r, 8 * s, 8 * s)
                p.fillPath(hp, QColor(0, 0, 0, 42) if light else QColor(255, 255, 255, 42))
            p.setPen(fg)
            p.drawText(inc_r, Qt.AlignCenter, "▸")
            if bar_w >= 44 * s:
                hot_vol = getattr(self, "_dragging_vol", False) or hot_sub == "vol_bar"
                bh = (8 if hot_vol else 6) * s
                track = QRectF(inc_r.left() - bar_w - 8 * s, r.center().y() - bh / 2, bar_w, bh)
                tp = QPainterPath()
                tp.addRoundedRect(track, bh / 2, bh / 2)
                p.fillPath(tp, QColor(0, 0, 0, 55) if light else QColor(255, 255, 255, 42))
                tx100 = track.left() + track.width() * (100 / 150)
                p.setPen(QPen(QColor(0, 0, 0, 90) if light else QColor(255, 255, 255, 90), 1.4 * s))
                p.drawLine(QPointF(tx100, track.top() - 3 * s), QPointF(tx100, track.bottom() + 3 * s))
                frac = min(1.0, max(0.0, self.volume / 150))
                if frac > 0:
                    fill = QRectF(track.left(), track.top(), track.width() * frac, track.height())
                    fp = QPainterPath()
                    fp.addRoundedRect(fill, bh / 2, bh / 2)
                    p.fillPath(fp, QColor(theme.CURRENT.accent))
                kd = (16 if hot_vol else 13) * s
                kx = track.left() + track.width() * frac
                p.setPen(QPen(QColor(0, 0, 0, 70), 1.2 * s))
                p.setBrush(QColor("white"))
                p.drawEllipse(QPointF(kx, track.center().y()), kd / 2, kd / 2)
                p.setBrush(Qt.NoBrush)
                self._vol_track = track
                dec_left = track.left() - aw - 8 * s
                arrows.append((QRectF(track.left() - 4 * s, r.top(), track.width() + 8 * s, r.height()), "vol_bar"))
            else:
                dec_left = inc_r.left() - aw - 4 * s
            dec_r = QRectF(dec_left, r.center().y() - 16 * s, aw, 32 * s)
            if hot_sub == "dec":
                hp = QPainterPath()
                hp.addRoundedRect(dec_r, 8 * s, 8 * s)
                p.fillPath(hp, QColor(0, 0, 0, 42) if light else QColor(255, 255, 255, 42))
            p.setPen(fg)
            p.drawText(dec_r, Qt.AlignCenter, "◂")
            arrows.append((QRectF(dec_r.left() - 6 * s, r.top(), aw + 10 * s, r.height()), "dec"))
            arrows.append((QRectF(inc_r.left() - 4 * s, r.top(), aw + 10 * s, r.height()), "inc"))
            right_w = r.right() - pad - dec_r.left() + 10 * s
        elif row.value:
            p.setFont(self.couch._font(19 * s, 800))
            text = f"◂  {row.value}  ▸" if row.kind == "choice" else row.value
            fm = QFontMetrics(p.font())
            right_w = fm.horizontalAdvance(text) + 12 * s
            p.setPen(fg)
            p.drawText(QRectF(r.right() - pad - right_w, r.top(), right_w, r.height()),
                       Qt.AlignRight | Qt.AlignVCenter, text)
            if row.kind == "choice" and (row.left or row.right):
                x0 = r.right() - pad - fm.horizontalAdvance(text)          # where the ◂ starts
                arrow = fm.horizontalAdvance("◂ ") + 12 * s
                arrows = [(QRectF(x0 - 12 * s, r.top(), arrow + 12 * s, r.height()), "dec"),
                          (QRectF(r.right() - pad - arrow, r.top(), arrow + pad, r.height()), "inc")]
        inner_w = r.right() - pad - right_w - x
        if row.note:
            p.setFont(self.couch._font(21 * s, 800))
            p.setPen(fg)
            p.drawText(QRectF(x, r.top() + r.height() / 2 - 30 * s, inner_w, 30 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(row.label, Qt.ElideRight, int(inner_w)))
            p.setFont(self.couch._font(15 * s, 600))
            if row.kind == "friend" and not light:
                from ezgd.ui.couch_friends import state_color
                p.setPen(state_color(row.extra["friend"]))
            else:
                p.setPen(sub)
            p.drawText(QRectF(x, r.top() + r.height() / 2 + 2 * s, inner_w, 26 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(row.note, Qt.ElideRight, int(inner_w)))
        else:
            p.setFont(self.couch._font(21 * s, 800))
            p.setPen(fg)
            p.drawText(QRectF(x, r.top(), inner_w, r.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(row.label, Qt.ElideRight, int(inner_w)))
        return arrows

    def _paint_switch(self, p: QPainter, r: QRectF, y: float) -> None:
        s = self._s()
        cols = self.GRID_COLS
        gap = 22 * s
        tw = (r.width() - gap * (cols - 1)) / cols
        th = tw * 1.5
        focused = self.grid[self.grid_focus] if 0 <= self.grid_focus < len(self.grid) else None
        # the focused game's name and what A does
        p.setFont(self.couch._font(30 * s, 900))
        p.setPen(QColor("white"))
        title = focused.title if focused is not None else ""
        p.drawText(QRectF(r.left(), y + 6 * s, r.width(), 44 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(title, Qt.ElideRight, int(r.width())))
        p.setFont(self.couch._font(17 * s, 700))
        p.setPen(QColor(255, 255, 255, 170))
        what = ""
        if focused is not None:
            if focused.id in self.couch.suspended:
                what = "Suspended — continues right where you left off"
            elif focused.id in self.couch.running:
                what = "Running — switch to it"
            else:
                what = "Start it" + (f" — {self.game.title} waits, suspended" if self.game is not None else "")
        p.drawText(QRectF(r.left(), y + 50 * s, r.width(), 28 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(what, Qt.ElideRight, int(r.width())))
        top = y + 100 * s
        bottom = self.height() - 100 * s
        row_h = th + gap
        total_rows = max(1, (len(self.grid) + cols - 1) // cols)
        focus_row = self.grid_focus // cols
        visible_rows = max(1, int((bottom - top + gap) // row_h))
        max_top = max(0, total_rows - visible_rows)
        first_row = max(0, min(self._grid_top, max_top))
        if focus_row < first_row:
            first_row = focus_row
        elif focus_row >= first_row + visible_rows:
            first_row = focus_row - visible_rows + 1
        self._grid_top = first_row
        p.save()
        p.setClipRect(QRectF(r.left() - 30 * s, top - 20 * s, r.width() + 60 * s, bottom - top + 40 * s),
                      Qt.IntersectClip)
        for k, e in enumerate(self.grid):
            row, col = divmod(k, cols)
            if row < first_row or row >= first_row + visible_rows + 1:
                continue
            tr = QRectF(r.left() + col * (tw + gap), top + (row - first_row) * row_h, tw, th)
            self._tile(p, e, tr, k == self.grid_focus)
            self._hits.append((tr, "tile", k))
        p.restore()
        if max_top > 0:
            self._draw_scrollbar(p, r.right() + 26 * s, top, bottom - top, first_row, max_top, visible_rows,
                                 total_rows, "grid")

    def _tile(self, p: QPainter, e, r: QRectF, focused: bool) -> None:
        s = self._s()
        radius = 14 * s
        if focused:
            c = r.center()
            r = QRectF(c.x() - r.width() * 0.53, c.y() - r.height() * 0.53, r.width() * 1.06, r.height() * 1.06)
            for k in range(5, 0, -1):
                gp = QPainterPath()
                gp.addRoundedRect(r.adjusted(-k * 4 * s, -k * 4 * s, k * 4 * s, k * 4 * s), radius + k * 4 * s,
                                  radius + k * 4 * s)
                g = QColor(theme.CURRENT.accent)
                g.setAlpha(30)
                p.fillPath(gp, g)
        img, needs_title, _key = self.couch.images.tile(self.couch.capsule_url(e), e.cover_url, int(r.width()),
                                                        int(r.height()), radius, False)
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        if img is not None:
            p.drawImage(r, img, QRectF(img.rect()))
        else:
            self._pending_art = True
            p.fillPath(path, QColor(255, 255, 255, 22))
            needs_title = True
        if needs_title:
            shade = QLinearGradient(0, r.center().y(), 0, r.bottom())
            shade.setColorAt(0, QColor(0, 0, 0, 0))
            shade.setColorAt(1, QColor(0, 0, 0, 200))
            p.fillPath(path, shade)
            p.setFont(self.couch._font(16 * s, 800))
            p.setPen(QColor("white"))
            p.drawText(r.adjusted(12 * s, 0, -12 * s, -12 * s), Qt.AlignHCenter | Qt.AlignBottom | Qt.TextWordWrap,
                       e.title)
        badge = None
        if e.id in self.couch.suspended:
            badge = ("❚❚ SUSPENDED", theme.CURRENT.warning)
        elif e.id in self.couch.running:
            badge = ("● RUNNING", theme.CURRENT.success)
        if badge:
            p.setFont(self.couch._font(12 * s, 900))
            bw = QFontMetrics(p.font()).horizontalAdvance(badge[0]) + 18 * s
            br = QRectF(r.left() + 10 * s, r.top() + 10 * s, bw, 24 * s)
            bp = QPainterPath()
            bp.addRoundedRect(br, 12 * s, 12 * s)
            p.fillPath(bp, QColor(badge[1]))
            p.setPen(QColor(12, 12, 16))
            p.drawText(br, Qt.AlignCenter, badge[0])
        p.setPen(QPen(QColor(255, 255, 255, 230 if focused else 34), (3 if focused else 1) * s))
        p.drawPath(path)

    def _paint_hints(self, p: QPainter, r: QRectF) -> None:
        s = self._s()
        y = self.height() - 50 * s
        line = QColor(255, 255, 255, 30)
        p.setPen(QPen(line, 1 * s))
        p.drawLine(r.left(), y - 34 * s, r.right(), y - 34 * s)
        km = self._key_mode()
        if self.keyboard is not None:
            hints = [("a", "Type"), ("b", "Backspace"), ("x", "Space"), ("start", self.keyboard.done_label)]
        elif self.view == "switch":
            hints = [("a", "Play"), ("dpad", "Choose"), ("b", "Back")]
        elif self.view == "confirm":
            hints = [("a", "Select"), ("b", "Back")]
        elif self.view == "starting":
            hints = [] if not self.start_error else [("a", "Select")]
        elif self.view == "chat":
            hints = [("a", "Select"), ("b", "Back")]
        elif self.view == "friends":
            hints = ([("a", "Options")] if any(r.kind == "friend" for r in self.rows) else []) + \
                [("/" if km else "x", "Search"), ("b", "Show everyone" if self.friend_query else "Back")]
        elif self.view == "friend":
            hints = [("a", "Select"), ("b", "Back")]
        else:
            row = self.rows[self.focus] if 0 <= self.focus < len(self.rows) else None
            hints = [("a", "Select")]
            if row is not None and row.kind == "choice":
                hints = [("lr", "Change")] + ([("M", "Mute")] if km and row.key == "volume" else [])
            hints.append(("b", "Back to the game" if self.game is not None else "Close"))
        usable = self.couch.usable_hints(hints)
        gap = (12 if len(usable) >= 3 else 26) * s
        x = r.left()
        self._hint_actions = []
        for action, text in usable:
            clickable = action in ("a", "b", "x", "/", "start", "M") and self.keyboard is None
            idx = len(self._hint_actions)
            if clickable and self._hovered == ("hint", idx):
                # measure without drawing on screen, highlight, then draw the prompt over it
                f_lbl = self.couch._font(13.5 * s, 800)
                lbl = self.couch._pad_labels().get(action, action)
                gw = 32 * s if (action in ("a", "b", "x") and self.couch.pad_family != "") else \
                    max(40 * s, QFontMetrics(f_lbl).horizontalAdvance(lbl) + 18 * s)
                tw = QFontMetrics(self.couch._font(18 * s, 600)).horizontalAdvance(text) if text else 0
                hr = QRectF(x - 8 * s, y - 20 * s, gw + (10 * s + tw if text else 0) + 16 * s, 40 * s)
                hp = QPainterPath()
                hp.addRoundedRect(hr, 10 * s, 10 * s)
                p.fillPath(hp, QColor(255, 255, 255, 28))
            x_end = self.couch._glyph(p, x, y, action, text)
            if clickable:
                span = max(32 * s, x_end - 34 * s - x)
                self._hits.append((QRectF(x - 6 * s, y - 22 * s, span + 12 * s, 44 * s), "hint", idx))
                self._hint_actions.append(action)
            x = x_end + gap

    def _bubble_width(self) -> float:
        s = self._s()
        return (self._content_rect(1.0).width() - 88 * s) * 0.8

    def _bubble(self, p: QPainter, row: Row, r: QRectF) -> None:
        """A chat message: yours on the right in the accent colour, theirs on the left."""
        s = self._s()
        mine = row.extra.get("mine")
        p.setFont(self.couch._font(17 * s, 600))
        fm = QFontMetrics(p.font())
        maxw = self._bubble_width()
        tr = fm.boundingRect(0, 0, int(maxw - 32 * s), 100000, int(Qt.TextWordWrap), row.label)
        bw = min(maxw, tr.width() + 34 * s)
        box = QRectF(r.right() - bw if mine else r.left(), r.top(), bw, r.height())
        path = QPainterPath()
        path.addRoundedRect(box, 16 * s, 16 * s)
        c = QColor(theme.CURRENT.accent) if mine else QColor(255, 255, 255, 30)
        if mine:
            c.setAlpha(210)
        p.fillPath(path, c)
        p.setPen(QColor("white"))
        p.drawText(box.adjusted(16 * s, 14 * s, -16 * s, -12 * s), int(Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop),
                   row.label)

    def _paint_chat_head(self, p: QPainter, r: QRectF, y: float) -> float:
        from ezgd.ui.couch_friends import paint_avatar
        s, f = self._s(), self.friend or {}
        a = 52 * s
        if not paint_avatar(p, self.couch, f, QRectF(r.left() + 4 * s, y, a, a), s):
            self._pending_art = True
        x = r.left() + a + 22 * s
        p.setFont(self.couch._font(22 * s, 800))
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, y, r.right() - x, 30 * s), Qt.AlignLeft | Qt.AlignVCenter, f.get("name", ""))
        p.setFont(self.couch._font(15 * s, 700))
        from ezgd.ui.couch_friends import state_color
        p.setPen(state_color(f))
        p.drawText(QRectF(x, y + 28 * s, r.right() - x, 24 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   f.get("status") or "")
        return y + a + 6 * s

    def _paint_flash(self, p: QPainter, r: QRectF) -> None:
        s = self._s()
        box = QRectF(r.left(), self.height() - 150 * s, r.width(), 46 * s)
        path = QPainterPath()
        path.addRoundedRect(box, 14 * s, 14 * s)
        p.fillPath(path, QColor(0, 0, 0, 200))
        p.setFont(self.couch._font(17 * s, 700))
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignCenter, QFontMetrics(p.font()).elidedText(self.flash, Qt.ElideRight,
                                                                           int(box.width() - 30 * s)))

    def _paint_busy(self, p: QPainter, panel: QRectF) -> None:
        s = self._s()
        p.fillRect(self._panel_back(panel), QColor(0, 0, 0, 150))
        p.setFont(self.couch._font(22 * s, 800))
        p.setPen(QColor("white"))
        p.drawText(panel, Qt.AlignCenter, self.busy)
