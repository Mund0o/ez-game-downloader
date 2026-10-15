"""Couch mode's browser: the in-app browser full screen, driven by the controller.

* Left stick: a big pointer (faster the further it is pushed); D-pad: nudge it. A: click (held: the click is held —
  for "press and hold" checks). Right stick: scroll; LT / RT: a page up / down.
* B: back (on the first page: close the browser). LB / RB: the other tabs. Y / △: on-screen keyboard that types into
  the page (opens by itself when a text box is clicked). Start / Options: Reload, Stop this page, ad blocker,
  close. The guide button (PS / Xbox / Steam): close the browser.

The pointer's clicks, moves and scrolls are real input events for the page — robot checks accept them.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

from ezgd.ui import theme
from ezgd.ui.couch_pages import Keyboard
from ezgd.ui.window_control import WindowControl

log = logging.getLogger(__name__)

POINTER_SPEED = 1500            # logical px / s at full deflection (scaled with the screen)
SCROLL_SPEED = 2600             # wheel units / s at full deflection
NUDGE = 22


class _Overlay(QWidget):
    """Drawn over the page: the pointer, the button hints, the keyboard and the menu. Never takes the mouse."""

    def __init__(self, cb):
        super().__init__(cb.dlg)
        self.cb = cb
        self.keyboard: Keyboard | None = None
        self._hit: list = []
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)

    # the host interface the on-screen keyboard draws with (same as couch mode's window)
    def _s(self) -> float:
        return self.cb.couch._s()

    def _font(self, px: float, weight: int = 400):
        return self.cb.couch._font(px, weight)

    def _schedule_frame(self, ms: int = 16) -> None:
        QTimer.singleShot(ms, self, self.update)

    def paintEvent(self, _e) -> None:                         # noqa: N802
        import shiboken6
        if not self.cb.active or not shiboken6.isValid(self.cb.couch):
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            self._hit = []
            self.cb.paint(p)
            if self.keyboard is not None:
                self.keyboard.paint(p)
        except Exception:
            log.exception("couch browser: drawing failed")
        finally:
            p.end()


class CouchBrowser:
    def __init__(self, couch, dlg):
        self.couch = couch
        self.dlg = dlg
        self.active = False
        self.pointer = QPointF(0, 0)
        self.held = False
        self.menu: list[tuple[str, object]] | None = None
        self.menu_focus = 0
        self.overlay = _Overlay(self)
        self.wc = WindowControl(dlg, dlg.windowTitle())
        self._tick = QTimer(dlg)
        self._tick.setInterval(16)
        self._tick.setTimerType(Qt.PreciseTimer)
        self._tick.timeout.connect(self._step)
        self._last = time.monotonic()
        self._scroll_rest = 0.0
        self._hscroll_rest = 0.0
        self._clicks = 0
        dlg.went_away.connect(lambda: QTimer.singleShot(0, self._check_gone))

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        s = self.couch._s()
        self.dlg.couch_mode(max(1.0, min(1.75, s)))
        bar = round(self.bar_height())
        self.dlg.layout().setContentsMargins(0, 0, 0, bar)
        # couch mode stays where it is, underneath: whichever way the browser closes, couch mode is right there
        self.couch.wc.keep_above(False)
        self.couch.place_window(self.dlg, self.wc)      # full screen (or the display settings' choice)
        self.dlg.raise_()
        self.dlg.activateWindow()
        self.overlay.setGeometry(self.dlg.rect())
        self.overlay.raise_()
        self.overlay.show()
        # a way out without a controller: a big ✕ for the mouse, Esc on the keyboard
        from PySide6.QtGui import QKeySequence, QShortcut
        from PySide6.QtWidgets import QPushButton
        s = self.couch._s()
        self.close_button = QPushButton("✕  Close", self.dlg)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setStyleSheet(
            f"QPushButton {{ background: rgba(20,22,30,215); color: white; border: 2px solid rgba(255,255,255,90);"
            f" border-radius: {round(22 * s)}px; padding: {round(8 * s)}px {round(22 * s)}px;"
            f" font-size: {round(19 * s)}px; font-weight: 800; }}"
            f" QPushButton:hover {{ background: {theme.CURRENT.danger}; border-color: white; }}")
        self.close_button.clicked.connect(self.close)
        self.close_button.adjustSize()
        self.close_button.show()
        self._place_close()
        self._esc = QShortcut(QKeySequence(Qt.Key_Escape), self.dlg)
        self._esc.setContext(Qt.WindowShortcut)
        self._esc.activated.connect(self.close)
        self.active = True
        self.couch.pads.pointer_mode = True
        r = self.view_rect()
        self.pointer = QPointF(r.center())
        self._last = time.monotonic()
        self._tick.start()
        QTimer.singleShot(300, self.dlg, lambda: (self.wc.keep_above(True), self.wc.activate()) if self.active
                          else None)                      # (not after it closed again: it is a desktop window too)
        self._follower = _ResizeFollower(self)
        self.dlg.installEventFilter(self._follower)
        self._clicker = _MouseClicks(self)
        v = self.dlg.view
        for w in ((v.focusProxy() or v) if v is not None else None, self.close_button):
            if w is not None:
                w.installEventFilter(self._clicker)

    def _place_close(self) -> None:
        b = getattr(self, "close_button", None)
        if b is None:
            return
        s = self.couch._s()
        bar = self.bar_height()
        b.move(round(self.dlg.width() - b.width() - 24 * s), round(self.dlg.height() - bar + (bar - b.height()) / 2))
        b.raise_()

    def _check_gone(self) -> None:
        import shiboken6
        if not self.active:
            return
        if not shiboken6.isValid(self.dlg) or (not self.dlg.isVisible() and not self.dlg.isMinimized()):
            self.stop()

    def stop(self) -> None:
        """The browser closed, or hid itself because a download started: back to couch mode."""
        if not self.active:
            return
        self.active = False
        self.held = False
        import shiboken6
        clicker, self._clicker = getattr(self, "_clicker", None), None
        if clicker is not None and shiboken6.isValid(clicker):
            clicker.cb = None                                   # no more clicks heard from this window
        if shiboken6.isValid(self.dlg):                 # the window may already be gone (closed and disposed)
            self._tick.stop()
            self.overlay.hide()
            self.wc.keep_above(False)
        if not shiboken6.isValid(self.couch):
            return
        self.couch.pads.pointer_mode = False
        self.couch.browser = None
        self.couch._away = None
        self.couch._come_back()                          # in front again (and filling its screen)
        if any(d.state in ("downloading", "paused", "completed") for d in getattr(self.dlg, "started", [])):
            self.couch.toasts.show("Download started", "It continues on the Downloads tab and goes into your "
                                                       "Library when it is done.", "success")

    def close(self) -> None:
        """Back to couch mode first, then close the window (closing disposes of it)."""
        self.menu = None
        self.stop()
        self.dlg.close()

    # ------------------------------------------------------------ geometry
    def bar_height(self) -> float:
        return 76 * self.couch._s()

    def view_rect(self) -> QRectF:
        v = self.dlg.view
        if v is None:
            return QRectF(self.dlg.rect())
        top_left = v.mapTo(self.dlg, QPoint(0, 0))
        return QRectF(top_left.x(), top_left.y(), v.width(), v.height())

    def _target(self):
        """What is under the pointer: the page (its input widget), or e.g. a button in the notice bar."""
        v = self.dlg.view
        hit = self.dlg.childAt(self.pointer.toPoint())
        if hit is not None and hit is not self.overlay and v is not None and hit is not v \
                and not v.isAncestorOf(hit):
            return hit
        return (v.focusProxy() or v) if v is not None else None

    def _local(self, target) -> QPointF:
        return QPointF(target.mapFrom(self.dlg, self.pointer.toPoint()))

    # -------------------------------------------------------- page events
    def _mouse(self, kind, button=Qt.NoButton, buttons=Qt.NoButton) -> None:
        t = self._target()
        if t is None:
            return
        local = self._local(t)
        glob = QPointF(t.mapToGlobal(local.toPoint()))
        QApplication.sendEvent(t, QMouseEvent(kind, local, glob, button, buttons, Qt.NoModifier))

    def _wheel(self, dx: int, dy: int) -> None:
        t = self._target()
        if t is None or not (dx or dy):
            return
        local = self._local(t)
        glob = QPointF(t.mapToGlobal(local.toPoint()))
        QApplication.sendEvent(t, QWheelEvent(local, glob, QPoint(0, 0), QPoint(dx, dy), Qt.NoButton,
                                              Qt.NoModifier, Qt.NoScrollPhase, False))

    def _key(self, key, text: str = "") -> None:
        t = self._target()
        if t is None:
            return
        for kind in (QEvent.KeyPress, QEvent.KeyRelease):
            QApplication.sendEvent(t, QKeyEvent(kind, key, Qt.NoModifier, text))

    def type_text(self, text: str, enter: bool = True) -> None:
        for ch in text:
            if ch == " ":
                key = Qt.Key_Space
            elif ch.isascii() and ch.isalnum():
                key = Qt.Key(ord(ch.upper()))
            else:
                key = Qt.Key_unknown
            self._key(key, ch)
        if enter:
            self._key(Qt.Key_Return, "\r")

    # ------------------------------------------------------------- input
    def button(self, name: str, pressed: bool) -> None:
        """Raw button changes: A is the mouse button (held = held)."""
        if name != "a" or self.menu is not None or self.overlay.keyboard is not None:
            return
        if pressed and not self.held:
            self.held = True
            self.couch.sounds.play("click")                  # the pointer's mouse button
            self._mouse(QEvent.MouseButtonPress, Qt.LeftButton, Qt.LeftButton)
            self.overlay.update()
        elif not pressed and self.held:
            self.held = False
            self._mouse(QEvent.MouseButtonRelease, Qt.LeftButton, Qt.NoButton)
            self.overlay.update()
            self._clicks += 1
            QTimer.singleShot(250, self.dlg, lambda n=self._clicks: self._maybe_keyboard(n))

    def command(self, name: str) -> None:
        kb = self.overlay.keyboard
        if kb is not None:
            before = (kb.row, kb.col)
            kb.command(name)
            if name == "a" or name == "start":
                self.couch.sounds.play("select")
            elif (kb.row, kb.col) != before:
                self.couch.sounds.play("move")
            self.overlay.update()
            return
        if self.menu is not None:
            before = self.menu_focus
            self._menu_command(name)
            if name == "a":
                self.couch.sounds.play("select")
            elif self.menu is not None and self.menu_focus != before:
                self.couch.sounds.play("move")
            return
        if name == "a":
            return                                       # handled as press / release (button)
        if name in ("up", "down", "left", "right"):
            s = self.couch._s()
            dx = {"left": -1, "right": 1}.get(name, 0) * NUDGE * s
            dy = {"up": -1, "down": 1}.get(name, 0) * NUDGE * s
            self._move(dx, dy)
        elif name == "b":
            self.back()
        elif name in ("lt", "rt"):
            self._key(Qt.Key_PageDown if name == "rt" else Qt.Key_PageUp)
        elif name in ("lb", "rb"):
            self._switch_tab(1 if name == "rb" else -1)
        elif name in ("x", "select"):
            self.open_keyboard()
        elif name == "start":
            self.open_menu()
        elif name == "home":
            self.close()
        self.overlay.update()

    def back(self) -> None:
        page = self.dlg.current
        if page.history().canGoBack() or (len(self.dlg.tabs) > 1 and page is self.dlg.tabs[-1]):
            self.dlg.back()
        else:
            self.close()

    def _switch_tab(self, d: int) -> None:
        tabs = self.dlg.tabs
        if len(tabs) < 2:
            return
        i = tabs.index(self.dlg.current) if self.dlg.current in tabs else len(tabs) - 1
        self.dlg._switch_to(tabs[(i + d) % len(tabs)])

    def open_keyboard(self, *_a) -> None:
        self.overlay.keyboard = Keyboard(self.overlay, "Type into the page", self._typed, done_label="Enter",
                                         on_cancel=self.overlay.update)
        self.overlay.update()

    def _typed(self, text: str) -> None:
        self.overlay.keyboard = None
        if text:
            self.type_text(text)
        self.overlay.update()

    def _maybe_keyboard(self, click: int) -> None:
        """A text box was clicked: open the keyboard for it (only for the latest click)."""
        if click != self._clicks:
            return
        from PySide6.QtWebEngineCore import QWebEngineScript
        js = ("(function(){var e=document.activeElement;if(!e)return false;var t=(e.type||'').toLowerCase();"
              "return e.isContentEditable||e.tagName==='TEXTAREA'||(e.tagName==='INPUT'&&['button','submit',"
              "'checkbox','radio','range','color','file','image','reset','hidden'].indexOf(t)<0);})()")
        try:
            self.dlg.current.runJavaScript(js, QWebEngineScript.ApplicationWorld,
                                           lambda editable: self.open_keyboard() if editable and self.active and
                                           click == self._clicks and self.overlay.keyboard is None
                                           and self.menu is None else None)
        except RuntimeError:
            pass

    # -------------------------------------------------------------- menu
    def open_menu(self) -> None:
        from ezgd.ui.dialogs import browser as br
        self.menu = [("Reload", lambda: self.dlg._replace_current(True)),
                     ("Stop this page", lambda: self.dlg._replace_current(False)),
                     ("Ad blocker: " + ("on" if br.ADBLOCK.enabled else "off"),
                      lambda: self.dlg.shield.setChecked(not br.ADBLOCK.enabled)),
                     ("Type text…", self.open_keyboard),
                     ("Close the browser", self.close)]
        self.menu_focus = 0
        self.overlay.update()

    def _menu_command(self, name: str) -> None:
        n = len(self.menu or [])
        if name == "up":
            self.menu_focus = max(0, self.menu_focus - 1)
        elif name == "down":
            self.menu_focus = min(n - 1, self.menu_focus + 1)
        elif name == "a" and n:
            fn = self.menu[self.menu_focus][1]
            self.menu = None
            fn()
        elif name in ("b", "start"):
            self.menu = None
        self.overlay.update()

    # ------------------------------------------------------------ motion
    def _move(self, dx: float, dy: float) -> None:
        r = QRectF(0, 0, self.dlg.width(), self.dlg.height() - self.bar_height())   # page + notice bar
        x = min(max(self.pointer.x() + dx, r.left() + 1), r.right() - 2)
        y = min(max(self.pointer.y() + dy, r.top() + 1), r.bottom() - 2)
        if x != self.pointer.x() or y != self.pointer.y():
            self.pointer = QPointF(x, y)
            self._mouse(QEvent.MouseMove, Qt.NoButton, Qt.LeftButton if self.held else Qt.NoButton)
            self.overlay.update()

    def _step(self) -> None:
        now = time.monotonic()
        dt = min(0.05, now - self._last)
        self._last = now
        if self.menu is not None or self.overlay.keyboard is not None or not self.dlg.isVisible():
            return
        lx, ly, rx, ry = self.couch.pads.sticks()
        s = self.couch._s()
        if lx or ly:
            mag = min(1.0, (lx * lx + ly * ly) ** 0.5)
            speed = POINTER_SPEED * s * (mag ** 1.8) / max(mag, 1e-6)
            self._move(lx * speed * dt, ly * speed * dt)
        if rx or ry:
            self._scroll_rest += -ry * SCROLL_SPEED * dt
            self._hscroll_rest += -rx * SCROLL_SPEED * dt
            dy, dx = int(self._scroll_rest), int(self._hscroll_rest)
            self._scroll_rest -= dy
            self._hscroll_rest -= dx
            self._wheel(dx, dy)

    # ------------------------------------------------------------- paint
    def paint(self, p: QPainter) -> None:
        couch = self.couch
        s = couch._s()
        W, H = self.overlay.width(), self.overlay.height()
        bar = self.bar_height()
        # the button hints
        p.fillRect(QRectF(0, H - bar, W, bar), QColor(10, 12, 18))
        p.fillRect(QRectF(0, H - bar, W, 1 * s), QColor(255, 255, 255, 30))
        y = H - bar / 2
        x = 40 * s
        if self.overlay.keyboard is not None:
            hints = [("a", "Type"), ("b", "Delete"), ("start", "Enter")]
        elif self.menu is not None:
            hints = [("a", "Select"), ("b", "Back")]
        else:
            hints = [("LS", "Pointer"), ("a", "Click"), ("RS", "Scroll"), ("x", "Type"), ("b", "Back")]
            if len(self.dlg.tabs) > 1:
                hints += [("lb", ""), ("rb", "Tabs")]
            hints += [("start", "Menu")]
        for action, text in hints:
            x = couch._glyph(p, x, y, action, text)
        p.setFont(couch._font(16 * s, 600))
        p.setPen(QColor(255, 255, 255, 150))
        if couch.pad_family == "" and self.overlay.keyboard is None and self.menu is None:
            x = couch._glyph(p, x, y, "Esc", "Close")
        url = self.dlg.current.url().host() if self.dlg.current is not None else ""
        room = W - x - 40 * s - (self.close_button.width() + 30 * s if getattr(self, "close_button", None) else 0)
        p.drawText(QRectF(x, H - bar, max(0.0, room), bar), Qt.AlignVCenter | Qt.AlignRight,
                   f"{self.dlg.host_label} · {url}")
        if self.menu is not None:
            self._paint_menu(p, s, W, H)
        elif self.overlay.keyboard is None:
            self._paint_pointer(p, s)

    def _paint_pointer(self, p: QPainter, s: float) -> None:
        x, y = self.pointer.x(), self.pointer.y()
        k = 1.35 * s
        path = QPainterPath()
        path.moveTo(x, y)
        for dx, dy in ((0, 26), (6.5, 20), (11, 30), (15, 28), (10.5, 18.5), (19, 18.5)):
            path.lineTo(x + dx * k, y + dy * k)
        path.closeSubpath()
        p.setPen(QPen(QColor(0, 0, 0, 220), 2.4 * s))
        p.setBrush(QColor(theme.CURRENT.accent) if self.held else QColor("white"))
        p.drawPath(path)
        p.setBrush(Qt.NoBrush)

    def _paint_menu(self, p: QPainter, s: float, W: float, H: float) -> None:
        items = self.menu or []
        mw, rh = 520 * s, 64 * s
        mh = 40 * s + len(items) * (rh + 10 * s) + 30 * s
        r = QRectF((W - mw) / 2, (H - mh) / 2, mw, mh)
        p.fillRect(QRectF(0, 0, W, H - self.bar_height()), QColor(0, 0, 0, 150))
        path = QPainterPath()
        path.addRoundedRect(r, 22 * s, 22 * s)
        p.fillPath(path, QColor(theme.CURRENT.bg2))
        y = r.top() + 30 * s
        for k, (label, _fn) in enumerate(items):
            rr = QRectF(r.left() + 26 * s, y, r.width() - 52 * s, rh)
            ip = QPainterPath()
            ip.addRoundedRect(rr, 14 * s, 14 * s)
            focused = k == self.menu_focus
            p.fillPath(ip, QColor(255, 255, 255, 235) if focused else QColor(255, 255, 255, 22))
            p.setPen(QColor(11, 13, 19) if focused else QColor("white"))
            p.setFont(self.couch._font(22 * s, 700))
            p.drawText(rr.adjusted(24 * s, 0, -24 * s, 0), Qt.AlignVCenter | Qt.AlignLeft, label)
            y += rh + 10 * s


class _MouseClicks(QObject):
    """A real mouse's clicks on the page (and the ✕ Close button) sound like the pointer's — in couch mode only."""

    def __init__(self, cb):
        super().__init__(cb.dlg)
        self.cb = cb

    def eventFilter(self, obj, ev) -> bool:                     # noqa: N802
        cb = self.cb
        if cb is not None and ev.type() == QEvent.MouseButtonPress and ev.spontaneous() and cb.active:
            cb.couch.sounds.play("click")
        return False


class _ResizeFollower(QObject):
    """Keeps the overlay over the whole window when it changes size."""

    def __init__(self, cb):
        super().__init__(cb.dlg)
        self.cb = cb

    def eventFilter(self, obj, ev) -> bool:                     # noqa: N802
        if ev.type() == QEvent.Resize and self.cb.active:
            self.cb.overlay.setGeometry(self.cb.dlg.rect())
            self.cb.dlg.layout().setContentsMargins(0, 0, 0, round(self.cb.bar_height()))
            self.cb.overlay.raise_()
            self.cb._place_close()
        return False
