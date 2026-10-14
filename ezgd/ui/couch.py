"""Couch mode: a full-screen, controller-friendly app in the style of SteamOS's game mode.

* Takes over the whole screen (the main window is hidden while it is open).
* Controller (any Linux game controller), keyboard (arrows, Enter, Esc) and mouse all work.
* Home: the focused game's artwork fills the screen, with a big Play button and a shelf of your games.
  Library: every game in a grid, with a menu for everything the desktop Library does (install, choose the game
  file, update, saves, fix, uninstall). Store: browse and search every site, game pages and downloads.
  Wishlist, Downloads (pause, resume, cancel) and Settings — nothing needs the desktop window.
* The big ✕ in the top-right corner leaves couch mode. If a game started from couch mode is still running it asks
  first, then closes that game completely (and checks it is really gone) before couch mode closes and the normal
  window comes back.

Everything is painted by this one widget (no desktop dialogs), so nothing can end up hidden behind it. The only
exceptions are windows that need a mouse anyway (the in-app browser, a repack's installer): couch mode steps aside
for them and comes back when they close.
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

from ezgd.ui import gcguard, theme
from ezgd.ui.couch_pages import GamePage, Keyboard, Page, SettingsPage, StorePage, WishlistPage
from ezgd.ui.gamepad import Gamepads
from ezgd.ui.window_control import WindowControl
from ezgd.ui.widgets.common import ToastHost, relative_time

log = logging.getLogger(__name__)

TABS = ["Home", "Library", "Store", "Wishlist", "Downloads", "Settings"]
T_HOME, T_LIBRARY, T_STORE, T_WISHLIST, T_DOWNLOADS, T_SETTINGS = range(len(TABS))
X_INDEX = len(TABS)                       # the ✕ is the last item of the top bar
STATUS_TEXT = {"downloaded": "Not installed yet", "extracting": "Installing", "extracted": "Needs setup",
               "installed": "Ready to play", "broken": "Files missing"}


ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def _numerals(key: str) -> str:
    """"hades ii" and "hades 2" are the same game."""
    return " ".join(ROMAN.get(w, w) for w in key.split())


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
    menu: bool = False                                                 # buttons as a vertical list (a menu)
    notes: list[str] = field(default_factory=list)                     # menu: a second line per entry
    scroll: float = 0.0


class _ArtSignals(QObject):
    decoded = Signal(str, object)          # url, QImage | None
    ready = Signal(object, object)         # job key, QImage | None


def _rounded(img: QImage, radius: float, border: float = 0.0) -> QImage:
    """The image with rounded corners (transparent outside) and, optionally, a faint border — so a tile is drawn
    with one plain blit."""
    out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, img.width(), img.height()), radius, radius)
    p.setClipPath(path)
    p.drawImage(0, 0, img)
    if border:
        p.setClipping(False)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), border))
        inner = QPainterPath()
        inner.addRoundedRect(QRectF(border / 2, border / 2, img.width() - border, img.height() - border),
                             radius - border / 2, radius - border / 2)
        p.drawPath(inner)
    p.end()
    return out


def _cover(img: QImage, w: int, h: int, top: bool = False) -> QImage:
    s = img.scaled(QSize(w, h), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = max(0, (s.width() - w) // 2)
    y = 0 if top else max(0, (s.height() - h) // 2)
    return s.copy(x, y, w, h)


def _blur(img: QImage, w: int, h: int) -> QImage:
    small = img.scaled(QSize(32, 32), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    small = small.scaled(QSize(12, 12), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    return _cover(small, max(1, w // 4), max(1, h // 4)).scaled(QSize(w, h), Qt.IgnoreAspectRatio,
                                                                  Qt.SmoothTransformation)


class Images:
    """Artwork for couch mode, prepared off the GUI thread: pictures are decoded, scaled, given rounded corners and
    composed into whole backgrounds by worker threads, so moving the focus only ever draws finished pixmaps (made
    for the screen's real pixels — sharp at 125 % / 150 % scaling). Until a picture is ready its place shows a
    placeholder; nothing waits for it."""

    LIMIT = 200                               # finished tiles kept (least recently used go first)
    LAYERS = 4                                # finished full-screen backgrounds kept
    RAW = 60                                  # decoded originals kept (only needed to make tiles/backgrounds)
    CAPSULE_WAIT = 2.5                        # seconds a tile waits for Steam's cover before showing the site's
    FADE = 0.18

    def __init__(self, owner: "CouchWindow"):
        self.owner = owner
        self.raw: OrderedDict = OrderedDict()             # url → decoded QImage
        self.meta: dict[str, tuple[int, int] | None] = {}  # url → size of the picture (None: could not be loaded)
        self.asked: dict[str, float] = {}                 # url → when it was first wanted
        self.cache: OrderedDict = OrderedDict()
        self.layers: OrderedDict = OrderedDict()
        self.born: dict = {}                              # key → when it became ready (fade-in)
        self.jobs: set = set()
        self.pending: set[str] = set()
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="couch-art")
        # not a child of the window: it must outlive it until the workers have stopped (see shutdown)
        self.sig = _ArtSignals()
        self.sig.decoded.connect(self._decoded)
        self.sig.ready.connect(self._ready)
        self.closed = False

    def dpr(self) -> float:
        try:
            return max(1.0, float(self.owner.devicePixelRatioF()))
        except Exception:
            return 1.0

    def shutdown(self) -> None:
        """Stop the workers before the window goes: a job finishing later must not signal a deleted object."""
        self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)       # running jobs take well under a second

    def clear(self) -> None:
        self.cache.clear()
        self.layers.clear()

    # ------------------------------------------------------------ loading
    def get(self, url: str | None) -> QImage | None:
        if not url:
            return None
        img = self.raw.get(url)
        if img is not None:
            self.raw.move_to_end(url)
            return img
        if url in self.meta and self.meta[url] is None:
            return None                                 # could not be loaded: not asked again
        self.asked.setdefault(url, time.monotonic())
        if url not in self.pending and len(self.pending) < 16 and not self.closed:
            self.pending.add(url)
            b = self.owner.bridge
            b.call(b.core.cover(url), lambda data, u=url: self._fetched(u, data),
                   lambda _e, u=url: self._decoded(u, None), owner=self.owner)
        return None

    def _fetched(self, url: str, data: bytes | None) -> None:
        if not data or self.closed:
            self._decoded(url, None)
            return

        def work() -> None:
            img = QImage.fromData(data)
            if not img.isNull():
                # wide art may fill a 4K screen; portrait covers only ever fill a tile
                limit = QSize(3840, 2400) if img.width() >= img.height() * 1.45 else QSize(1600, 1200)
                if img.width() > limit.width() or img.height() > limit.height():
                    img = img.scaled(limit, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._emit(self.sig.decoded, url, None if img.isNull() else img)
        self._submit(work)

    def _decoded(self, url: str, img) -> None:
        if self.closed:
            return
        self.pending.discard(url)
        self.meta[url] = (img.width(), img.height()) if img is not None else None
        if img is not None:
            self.raw[url] = img
            while len(self.raw) > self.RAW:             # finished tiles stay; originals can be read again
                self.raw.popitem(last=False)
        self.owner.update()

    def _submit(self, fn) -> None:
        if self.closed:
            return
        try:
            self.pool.submit(fn)
        except RuntimeError:                           # shutting down
            pass

    def _emit(self, signal, *args) -> None:
        if self.closed:
            return
        try:
            signal.emit(*args)
        except RuntimeError:                           # the window is gone
            pass

    def _store(self, key: tuple) -> OrderedDict:
        return self.layers if key[0] == "layer" else self.cache

    def ready_since(self, key: tuple) -> float:
        return self.born.get(key, 0.0)

    def _job(self, key: tuple, make) -> QImage | None:
        """The finished image for ``key``, or None while a worker makes it (``make`` returns a QImage)."""
        store = self._store(key)
        pm = store.get(key)
        if pm is not None:
            store.move_to_end(key)
            return pm
        if make is None:
            return None
        if key not in self.jobs and not self.closed:
            self.jobs.add(key)

            def work() -> None:
                try:
                    img = make()
                except Exception:
                    log.exception("couch mode: preparing artwork failed")
                    img = None
                self._emit(self.sig.ready, key, img)
            self._submit(work)
        return None

    def _ready(self, key: tuple, img) -> None:
        self.jobs.discard(key)
        if img is None or img.isNull() or self.closed:
            return
        img.setDevicePixelRatio(key[-1])       # kept as a QImage: drawn directly, no copy into a QPixmap
        store = self._store(key)
        store[key] = img
        self.born[key] = time.monotonic()
        limit = self.LAYERS if store is self.layers else self.LIMIT
        while len(store) > limit:
            old, _ = store.popitem(last=False)
            self.born.pop(old, None)
        self.owner.update()

    # ------------------------------------------------------------ pictures
    def fill(self, url: str | None, w: int, h: int, top: bool = False, radius: float = 0.0) -> QImage | None:
        """The image scaled to cover w×h logical pixels (cropped around its centre)."""
        if not url or w <= 0 or h <= 0:
            return None
        dpr = self.dpr()
        pw, ph = max(1, round(w * dpr)), max(1, round(h * dpr))
        key = ("fill", url, pw, ph, top, round(radius, 1), dpr)
        done = self._job(key, None)
        if done is not None:
            return done
        img = self.get(url)
        if img is None:
            return None

        def make() -> QImage:
            out = _cover(img, pw, ph, top)
            return _rounded(out, radius * dpr) if radius else out
        return self._job(key, make)

    def tile(self, capsule: str | None, cover: str | None, w: int, h: int, radius: float, dim: bool
             ) -> tuple[QImage | None, bool, tuple | None]:
        """A finished game tile: Steam's portrait capsule, or the site's cover shown whole over a blur of itself
        (never stretched or cut off). Returns (image, needs_title, key): covers that are not portrait get the title
        drawn over them; ``key`` tells when it became ready (for the fade-in).

        A tile shows one picture only — no cover-then-capsule swap: while Steam's cover is on its way (up to
        ``CAPSULE_WAIT`` seconds) the placeholder stays. ``capsule`` may be ``"wait:<key>"`` while the Steam App ID
        itself is still being looked up."""
        dpr = self.dpr()
        pw, ph = max(1, round(w * dpr)), max(1, round(h * dpr))
        now = time.monotonic()
        src = None
        if capsule:
            if capsule.startswith("wait:"):
                if now - self.asked.setdefault(capsule, now) < self.CAPSULE_WAIT:
                    return None, True, None
            else:
                m = self.meta.get(capsule, False)
                if m:
                    src = capsule
                elif m is False:                        # not loaded yet
                    self.get(capsule)
                    if now - self.asked.get(capsule, now) < self.CAPSULE_WAIT:
                        return None, True, None
        if src is None and cover:
            m = self.meta.get(cover, False)
            if m:
                src = cover
            elif m is False:
                self.get(cover)
                return None, True, None
        if src is None:
            return None, True, None
        mw, mh = self.meta[src]
        aspect = mw / max(1, mh)
        portrait = src == capsule or 0.55 <= aspect <= 0.8
        key = ("tile", src, pw, ph, round(radius, 1), dim, dpr)
        done = self._job(key, None)                     # finished already: no need for the original
        if done is not None:
            return done, not portrait, key
        art = self.get(src)
        if art is None:
            return None, True, None

        def make() -> QImage:
            if portrait:
                out = _cover(art, pw, ph)
            else:
                out = _blur(art, pw, ph).convertToFormat(QImage.Format_ARGB32_Premultiplied)
                p = QPainter(out)
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                p.fillRect(out.rect(), QColor(0, 0, 0, 70))
                front = art.scaled(QSize(int(pw - 12 * dpr), int(ph * 0.62)), Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation)
                p.drawImage(int((pw - front.width()) / 2), int((ph * 0.72 - front.height()) / 2), front)
                band = QLinearGradient(0, ph * 0.6, 0, ph)
                band.setColorAt(0, QColor(0, 0, 0, 0))
                band.setColorAt(1, QColor(0, 0, 0, 215))
                p.fillRect(QRectF(0, ph * 0.6, pw, ph * 0.4), QBrush(band))
                p.end()
            if dim:
                p = QPainter(out)
                p.fillRect(out.rect(), QColor(0, 0, 0, 110))
                p.end()
            return _rounded(out, radius * dpr, border=max(1.0, radius / 12 * dpr))
        return self._job(key, make), not portrait, key

    def layer(self, url: str | None, W: int, H: int, mode: str) -> QImage | None:
        """The whole page background (colours, glow, the artwork, shading) as one opaque pixmap, so a frame
        starts with a single fast copy. A wide hero image spans the full width at its own shape (fading out at the
        bottom), anything else sits on the right; both over a soft blur of the same picture."""
        dpr = self.dpr()
        PW, PH = max(1, round(W * dpr)), max(1, round(H * dpr))
        if url and self.meta.get(url, False) is None:
            url = None                                  # could not be loaded: a plain background
        done = self._job(("layer", url, PW, PH, mode, theme.CURRENT.accent, dpr), None)
        if done is not None:
            return done
        img = self.get(url) if url else None
        if url and img is None:
            return None
        bg0, accent = QColor(theme.CURRENT.bg0), QColor(theme.CURRENT.accent)

        def make() -> QImage:
            canvas = QImage(PW, PH, QImage.Format_RGB32)
            canvas.fill(bg0)
            p = QPainter(canvas)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            glow = QRadialGradient(QPointF(PW * 0.2, PH * 0.1), PW * 0.9)
            acc = QColor(accent)
            acc.setAlpha(70)
            glow.setColorAt(0, acc)
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            p.fillRect(canvas.rect(), QBrush(glow))
            if img is not None:
                p.setOpacity(0.55)
                p.drawImage(0, 0, _blur(img, PW, PH))
                p.setOpacity(1.0)
                aspect = img.width() / max(1, img.height())
                if aspect >= 1.45:
                    hh = round(PW / aspect)
                    hero = img.scaled(QSize(PW, hh), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    hero = hero.copy(0, 0, PW, min(hh, PH)).convertToFormat(QImage.Format_ARGB32_Premultiplied)
                    hp = QPainter(hero)
                    hp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                    g = QLinearGradient(0, 0, 0, hero.height())
                    g.setColorAt(0.0, QColor(0, 0, 0, 255))
                    g.setColorAt(0.62, QColor(0, 0, 0, 255))
                    g.setColorAt(1.0, QColor(0, 0, 0, 0))
                    hp.fillRect(hero.rect(), QBrush(g))
                    hp.end()
                    p.drawImage(0, 0, hero)
                else:
                    ph_, pw_ = PH, round(PH * aspect)
                    if pw_ > PW * 0.62:
                        pw_ = round(PW * 0.62)
                        ph_ = round(pw_ / aspect)
                    art = img.scaled(QSize(pw_, ph_), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    art = art.convertToFormat(QImage.Format_ARGB32_Premultiplied)
                    ap = QPainter(art)
                    ap.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                    g = QLinearGradient(0, 0, pw_, 0)
                    g.setColorAt(0.0, QColor(0, 0, 0, 0))
                    g.setColorAt(0.4, QColor(0, 0, 0, 255))
                    g.setColorAt(1.0, QColor(0, 0, 0, 255))
                    ap.fillRect(art.rect(), QBrush(g))
                    g2 = QLinearGradient(0, 0, 0, ph_)
                    g2.setColorAt(0.0, QColor(0, 0, 0, 255))
                    g2.setColorAt(0.7, QColor(0, 0, 0, 255))
                    g2.setColorAt(1.0, QColor(0, 0, 0, 0))
                    ap.fillRect(art.rect(), QBrush(g2))
                    ap.end()
                    p.drawImage(PW - pw_, 0, art)
            dim = {"home": 150, "game": 175}.get(mode, 205)
            shade = QLinearGradient(0, 0, 0, PH)
            shade.setColorAt(0.0, QColor(4, 5, 9, 140))
            shade.setColorAt(0.45, QColor(4, 5, 9, dim))
            shade.setColorAt(1.0, QColor(4, 5, 9, 245))
            p.fillRect(canvas.rect(), QBrush(shade))
            if mode in ("home", "game"):
                side = QLinearGradient(0, 0, PW * 0.65, 0)
                side.setColorAt(0, QColor(0, 0, 0, 180))
                side.setColorAt(1, QColor(0, 0, 0, 0))
                p.fillRect(canvas.rect(), QBrush(side))
            p.end()
            return canvas
        return self._job(("layer", url, PW, PH, mode, theme.CURRENT.accent, dpr), make)


class CouchToasts:
    """Messages inside couch mode: big, painted in the bottom-right corner (desktop pop-ups would be tiny on a TV,
    or hidden behind the full-screen window)."""

    LIMIT = 3

    def __init__(self, owner: "CouchWindow"):
        self.owner = owner
        self.reduce_motion = False
        self.items: list[dict] = []
        self._recent: dict[str, float] = {}
        self._timer = QTimer(owner)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

    def show(self, title: str, text: str = "", kind: str = "info", action: tuple | None = None) -> None:
        key = f"{title}|{text}"
        now = time.monotonic()
        if now - self._recent.get(key, 0) < 3:          # collapse duplicates
            return
        self._recent[key] = now
        ToastHost.history.append({"time": time.time(), "title": title, "text": text, "kind": kind})
        del ToastHost.history[:-200]
        if ToastHost.history_changed:
            try:
                ToastHost.history_changed()
            except Exception:
                log.exception("notification history update failed")
        life = (10.0 if kind == "error" else 6.0) + min(14.0, 0.04 * len(text))
        self.items.append({"title": title, "text": text, "kind": kind, "action": action, "until": now + life,
                           "born": now})
        del self.items[:-self.LIMIT]
        if not self._timer.isActive():
            self._timer.start()
        self.owner.update()

    def relayout(self) -> None:
        self.owner.update()

    def dismiss(self, idx: int) -> None:
        if 0 <= idx < len(self.items):
            self.items.pop(idx)
            self.owner.update()

    def _tick(self) -> None:
        now = time.monotonic()
        before = len(self.items)
        self.items = [t for t in self.items if t["until"] > now]
        if len(self.items) != before:
            self.owner.update()
        if not self.items:
            self._timer.stop()

    def stop(self) -> None:
        self._timer.stop()


STARTING_WINDOW = 90.0                    # a game started this recently may still be on its way (Steam is slow)

# button prompts per controller family ("" = no controller: the keyboard)
PAD_LABELS = {
    "xbox": {"a": "A", "b": "B", "x": "Y", "lb": "LB", "rb": "RB", "lt": "LT", "rt": "RT", "start": "☰",
             "select": "⧉", "home": "Xbox"},
    "playstation": {"a": "✕", "b": "○", "x": "△", "lb": "L1", "rb": "R1", "lt": "L2", "rt": "R2", "start": "OPTIONS",
                    "select": "CREATE", "home": "PS"},
    "steam": {"a": "A", "b": "B", "x": "Y", "lb": "L1", "rb": "R1", "lt": "L2", "rt": "R2", "start": "☰",
              "select": "⧉", "home": "STEAM"},
    "nintendo": {"a": "B", "b": "A", "x": "X", "lb": "L", "rb": "R", "lt": "ZL", "rt": "ZR", "start": "+",
                 "select": "−", "home": "HOME"},
    "": {"a": "Enter", "b": "Esc", "x": "I", "lb": "⇧Tab", "rb": "Tab", "lt": "PgUp", "rt": "PgDn",
         "start": "Enter", "select": "/", "home": "Home"},
}
PAD_LABELS["generic"] = PAD_LABELS["xbox"]
FACE_COLORS = {                           # (background, letter) of the face buttons
    "xbox": {"a": ("#16a34a", "white"), "b": ("#dc2626", "white"), "x": ("#eab308", "#1a1300")},
    "steam": {"a": ("#2a2f3a", "white"), "b": ("#2a2f3a", "white"), "x": ("#2a2f3a", "white")},
    "nintendo": {"a": ("#2a2f3a", "white"), "b": ("#2a2f3a", "white"), "x": ("#2a2f3a", "white")},
}
FACE_COLORS["generic"] = FACE_COLORS["xbox"]
FAMILY_NAMES = {"xbox": "Xbox controller", "playstation": "PlayStation controller", "steam": "Steam controller",
                "nintendo": "Nintendo controller", "generic": "Controller"}


class CouchWindow(QWidget):
    closed = Signal()
    BG_REST = 0.12                        # seconds the focus rests on a game before the background follows it

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
        self.setAttribute(Qt.WA_OpaquePaintEvent)          # every frame paints everything (see the frame pump)
        self.reduce_motion = bool(actions.settings.get("reduce_motion"))
        self.wc = WindowControl(self, "Couch mode")         # keep above other windows, take the focus back
        self._launching = False                             # Play was pressed; the game is not running yet
        self._idle_away = QTimer(self)                      # tabbed out by the user: stop forcing ourselves on top
        self._idle_away.setSingleShot(True)
        self._idle_away.setInterval(1200)
        self._idle_away.timeout.connect(self._user_left)
        self._refocus = QTimer(self)                        # a pop-up took the focus while a game starts
        self._refocus.setSingleShot(True)
        self._refocus.setInterval(120)
        self._refocus.timeout.connect(self._take_focus_back)
        self._dirty = True
        self._pump = QTimer(self)                           # steady frame rate (VRR screens: no flicker)
        self._pump.setTimerType(Qt.PreciseTimer)
        self._pump.timeout.connect(self._pump_tick)
        self.images = Images(self)
        self.entries: list = []
        self.art: dict[int, dict] = {}
        self._art_pending: set[int] = set()
        self._title_apps: dict[str, int | None] = {}      # catalog title key → Steam App ID found by title
        self._title_queue: list[str] = []
        self._title_names: dict[str, str] = {}
        self._title_inflight = 0
        self.running: set[int] = set(running)
        self.launched: set[int] = set()          # games started from couch mode (the ✕ closes these)
        self._launch_at: dict[int, float] = {}   # when each was started (monotonic)
        self.pending_kill: set[int] = set()      # left while these were still starting: the main window closes them
        self.extraction: dict[int, float] = {}
        self.extraction_info: dict[int, dict] = {}
        self.downloads: list[dict] = []
        self.summary: dict = {}
        self.tab = T_HOME
        self.zone = "main"                        # top | buttons | main
        self.top_index = 0
        self.button_index = 0
        self.index = {T_HOME: 0, T_LIBRARY: 0, T_DOWNLOADS: 0}    # focused item per built-in tab
        self.pages: dict[int, Page] = {}
        self.stack: list[Page] = []               # pages opened over the current tab (a game's page)
        self.keyboard: Keyboard | None = None
        self.overlay: Overlay | None = None
        self._away: str | None = None             # stepped aside for: "browser" | "installer" | "steam"
        self._installing: set[int] = set()        # repack installers started from here
        self._exiting = False
        self._closing_watchdog: QTimer | None = None
        self._hit: list[tuple[QRectF, str, int]] = []
        self._scroll = {0: 0.0, 1: 0.0}           # animated: shelf x offset (Home), grid y offset (Library)
        self._scroll_target = {0: 0.0, 1: 0.0}
        self._focus_anim = 1.0
        self._bg_cur: tuple | None = None        # (url, mode) of the background on screen
        self._bg_prev: tuple | None = None       # the one fading out
        self._bg_want: tuple | None = None       # where the focus is now (shown once it rests there)
        self._bg_want_at = 0.0
        self._bg_t = 1.0
        self._fonts: dict[tuple, QFont] = {}
        self._tag_cache: dict[tuple, QPixmap] = {}
        self._shown: dict[tuple, tuple] = {}     # tile → the picture it showed last
        self._tab_fit: dict[tuple, float] = {}
        self._paint_failed = False
        self._frame_pending = False
        self._minimize_timer = QTimer(self)
        self._minimize_timer.setSingleShot(True)
        self._minimize_timer.timeout.connect(self._step_aside)
        self.toasts = CouchToasts(self)
        self.toasts.reduce_motion = self.reduce_motion

        self._tick = QTimer(self)                 # animations (only runs while something moves)
        self._tick.setInterval(16)
        self._tick.timeout.connect(self._animate)
        self._focus_va = QVariantAnimation(self)
        self._focus_va.setDuration(170)
        self._focus_va.setEasingCurve(QEasingCurve.OutCubic)
        self._focus_va.valueChanged.connect(self._focus_value)
        self._bg_va = QVariantAnimation(self)
        self._bg_va.setDuration(200)
        self._bg_va.valueChanged.connect(self._bg_value)
        self._clock = QTimer(self)
        self._clock.setInterval(15_000)
        self._clock.timeout.connect(self.update)
        self._clock.start()
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_downloads)
        self._poll.start()

        self.pages = {T_STORE: StorePage(self), T_WISHLIST: WishlistPage(self), T_SETTINGS: SettingsPage(self)}
        self._known_pads: set[str] = set()
        self._opened = False
        self.pads = Gamepads(self)
        self.pad_family = ""                      # button prompts: the controller used last ("" = keyboard)
        self.browser = None                       # the in-app browser, driven by the controller (couch_browser)
        self.pads.action.connect(self._pad)
        self.pads.button.connect(self._pad_button)
        self.pads.devices_changed.connect(self._pads_changed)
        self.pads.family_changed.connect(self._family_changed)
        self.pads.start()
        self.pad_family = self.pads.family
        self.set_entries(entries)

    # ================================================================== data
    def set_entries(self, entries: list) -> None:
        """Library entries, most recently played first (Home) — the focused game stays focused."""
        cur = self._focused_entry()
        self.entries = sorted(entries, key=lambda e: (-(e.last_played_at or 0), -(e.added_at or 0), e.title.casefold()))
        if cur is not None:
            for tab in (T_HOME, T_LIBRARY):
                items = self._items(tab)
                pos = next((i for i, e in enumerate(items) if e.id == cur.id), None)
                if pos is not None:
                    self.index[tab] = pos
        for tab in (T_HOME, T_LIBRARY):
            n = len(self._items(tab))
            self.index[tab] = max(0, min(self.index[tab], n - 1))
        self.load_art([e.steam_app_id for e in entries if e.steam_app_id])
        if self._installing:                          # a repack installer we stepped aside for has finished
            by_id = {e.id: e for e in self.entries}
            done = {i for i in self._installing if (by_id.get(i) is None or
                                                    (by_id[i].setup or {}).get("state") != "installing")}
            self._installing -= done
            if done and not self._installing:
                self.come_back_from("installer")
                for i in done:
                    e = by_id.get(i)
                    if e is not None and self.ready(e):
                        self.toasts.show("Installed", f"{e.title} is ready to play.", "success")
        self.refresh_bg()
        self.update()

    def load_art(self, app_ids) -> None:
        """Steam artwork (portrait capsule, hero) for these App IDs, fetched once and cached on disk."""
        want = sorted({int(a) for a in app_ids if a} - set(self.art) - self._art_pending)
        for k in range(0, len(want), 50):
            chunk = want[k:k + 50]
            self._art_pending.update(chunk)
            self.bridge.call(self.bridge.core.steam_art(chunk), lambda art, c=chunk: self._art_loaded(art, c),
                             lambda _e, c=chunk: self._art_pending.difference_update(c), owner=self)

    def _art_loaded(self, art: dict, chunk: list | None = None) -> None:
        self.art.update({int(k): v for k, v in art.items()})
        if chunk:
            self._art_pending.difference_update(chunk)
        self.refresh_bg()
        self.update()

    def _items(self, tab: int | None = None) -> list:
        tab = self.tab if tab is None else tab
        if tab == T_HOME:
            return self.entries
        if tab == T_LIBRARY:
            return sorted(self.entries, key=lambda e: e.title.casefold())
        return []

    def _focused_entry(self):
        if self.tab not in (T_HOME, T_LIBRARY) or self.stack:
            return None
        items = self._items()
        i = self.index.get(self.tab, 0)
        return items[i] if 0 <= i < len(items) else None

    def _capsule_for(self, app: int | None, title_key: str | None = None) -> str | None:
        """Steam's portrait cover for a game; ``"wait:…"`` while it is still being found (the tile then waits a
        moment instead of showing the site's cover and swapping it)."""
        if app:
            if app in self.art:
                return (self.art.get(app) or {}).get("capsule")
            return f"wait:app:{app}"
        if title_key is not None and title_key not in self._title_apps:
            return f"wait:title:{title_key}"
        return None

    def capsule_url(self, e) -> str | None:
        return self._capsule_for(e.steam_app_id)

    def hero_url(self, e) -> str | None:
        a = self.art.get(e.steam_app_id) or {} if e.steam_app_id else {}
        return a.get("hero") or a.get("wide") or e.cover_url

    def game_capsule(self, g) -> str | None:
        return self._capsule_for(g.steam_app_id)

    def group_app_id(self, group) -> int | None:
        """The Steam App ID of a game offered by several sites (any of them may know it, else Steam's search by
        the exact title)."""
        app = next((g.steam_app_id for g in group.entries if g.steam_app_id), None)
        return app or self._title_apps.get(group.key)

    def find_apps_by_title(self, groups) -> None:
        """Games whose site does not name their Steam App ID: ask Steam's store search for the exact title, so
        they get proper artwork too (a few at a time; only exact title matches count)."""
        new = []
        for g in groups:
            if g.key and g.key not in self._title_apps and g.key not in new and \
                    not any(x.steam_app_id for x in g.entries):
                new.append(g.key)
                self._title_names[g.key] = g.title
        # what was asked for last is on screen now: it goes first
        self._title_queue = new + [k for k in self._title_queue if k not in new]
        self._next_title_lookup()

    def _next_title_lookup(self) -> None:
        from ezgd.catalog.normalize import dedup_key
        while self._title_queue and self._title_inflight < 3 and not self._exiting:
            key = self._title_queue.pop(0)
            self._title_inflight += 1

            def done(res, key=key) -> None:
                self._title_inflight -= 1
                hit = next((r["appid"] for r in res or [] if _numerals(dedup_key(r.get("name") or ""))
                            == _numerals(key)), None)
                self._title_apps[key] = hit
                if hit:
                    self.load_art([hit])
                self._next_title_lookup()

            def failed(_e, key=key) -> None:
                self._title_inflight -= 1
                self._title_apps[key] = None
                self._next_title_lookup()
            self.bridge.call(self.bridge.core.steam_search(self._title_names.get(key) or key), done, failed,
                             owner=self)

    def group_capsule(self, group) -> str | None:
        return self._capsule_for(self.group_app_id(group), group.key)

    def group_hero(self, group) -> str | None:
        app = self.group_app_id(group)
        a = self.art.get(app) or {} if app else {}
        return a.get("hero") or a.get("wide") or group.primary.cover_url

    def game_hero(self, g) -> str | None:
        a = self.art.get(g.steam_app_id) or {} if g.steam_app_id else {}
        return a.get("hero") or a.get("wide") or g.cover_url

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
        if st == "installing":
            return "Installer running"
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
            if gid in self.launched:
                # the splash stays (and stays on top) until the game's window takes the focus — then couch mode
                # steps aside at once (changeEvent); if that never happens, it does after a while anyway
                self._launching = False
                self._refocus.stop()
                if self.overlay is not None and self.overlay.tag == "starting":
                    self.overlay.text = "Have fun!"
                self._minimize_timer.start(12_000)
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
            self.extraction_info[lid] = dict(state)
        else:
            self.extraction.pop(lid, None)
            self.extraction_info.pop(lid, None)
        self.update()

    def wishlist_changed(self) -> None:
        page = self.pages.get(T_WISHLIST)
        if page is not None and self.tab == T_WISHLIST and not self.stack:
            page.reload()

    def go_to(self, tab: int, entry_id: int | None = None) -> None:
        """Show a tab (from a message's button); with ``entry_id`` the Library focuses that game."""
        self.overlay = None
        self.keyboard = None
        self._switch_tab(tab, force=True)
        if entry_id is not None and tab in (T_HOME, T_LIBRARY):
            items = self._items(tab)
            pos = next((i for i, e in enumerate(items) if e.id == entry_id), None)
            if pos is not None:
                self._move(pos)
        self._come_back()
        self.update()

    def _step_aside(self) -> None:
        """A game started from couch mode is running: get out of its way (couch mode comes back when it ends)."""
        if any(i in self.running for i in self.launched) and not self._exiting and not self.isMinimized():
            self._minimize_timer.stop()
            if self.overlay is not None and self.overlay.tag == "starting":
                self.overlay = None
            self.wc.keep_above(False)
            self.showMinimized()

    def step_away(self, reason: str) -> None:
        """Make room for a window that needs the mouse (in-app browser, installer, Steam)."""
        if self._exiting:
            return
        self._away = reason
        self.wc.keep_above(False)
        self.showMinimized()

    def come_back_from(self, reason: str) -> None:
        if self._away == reason:
            self._away = None
            self._come_back()

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
        self.wc.keep_above(True)
        self.wc.activate()                              # Wayland would not give the focus back by itself
        QTimer.singleShot(250, self, self._fit_screen)

    def _poll_downloads(self) -> None:
        if not self.isVisible() or self.isMinimized():
            return
        self.bridge.call(self.bridge.core.summary(), self._summary, owner=self)
        if self.tab == T_DOWNLOADS:
            self.bridge.call(self.bridge.core.downloads_snapshot(), self._downloads, owner=self)

    def _summary(self, s: dict) -> None:
        self.summary = s
        self.update()

    def _downloads(self, items: list[dict]) -> None:
        order = {"downloading": 0, "metadata": 0, "checking": 1, "verifying": 1, "queued": 2, "paused": 3,
                 "failed": 4, "completed": 5, "cancelled": 6}
        items = [d for d in items if d.get("status") != "cancelled"]
        items.sort(key=lambda d: (order.get(d.get("status"), 9), -(d.get("updated_at") or 0)))
        self.downloads = items[:40]
        self.index[T_DOWNLOADS] = max(0, min(self.index[T_DOWNLOADS], len(self._download_rows()) - 1))
        self.update()

    def _download_rows(self) -> list[tuple[str, object]]:
        installing = [e for e in self.entries if e.id in self.extraction]
        return [("install", e) for e in installing] + [("dl", d) for d in self.downloads]

    # ============================================================== showing
    def open(self, screen=None) -> None:
        self._target_screen = screen or QApplication.primaryScreen()
        self.place_window(self, self.wc)
        screen = self.screen() or self._target_screen
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.ActiveWindowFocusReason)
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._screen_changed)
        if screen is not None:
            screen.geometryChanged.connect(lambda _g: self._fit_screen())
        self._set_pace(screen)
        QTimer.singleShot(250, self, self._fit_screen)    # the window manager has placed it: check it fills it
        QTimer.singleShot(300, self, lambda: (self.wc.keep_above(True), self.wc.activate()))
        if not self.entries:
            self.toasts.show("Your library is empty", "Find a game in the Store tab — it appears here once it is "
                                                      "downloaded.")
        self._opened = True
        n = self.pads.names()
        self.toasts.show("Couch mode", (f"{FAMILY_NAMES.get(self.pad_family, 'Controller')} ready." if n else
                                        "Connect a controller, or use the arrow keys or the mouse.")
                         + " The ✕ in the top-right corner leaves couch mode.")

    # ----------------------------------------------------------- the screen
    def _screen_changed(self, screen) -> None:
        if screen is not None:
            self._target_screen = screen
            screen.geometryChanged.connect(lambda _g: self._fit_screen())
            self._set_pace(screen)
        self._fit_screen()

    def display_target(self):
        """(screen, rectangle, mode) from the display settings: which screen, full screen / borderless / a custom
        size (given in the screen's real pixels — what people know as their resolution)."""
        st = self.actions.settings
        screens = QApplication.screens()
        name = st.get("couch_screen") or ""
        screen = next((x for x in screens if x.name() == name), None) or getattr(self, "_target_screen", None) \
            or self.screen() or QApplication.primaryScreen()
        mode = st.get("couch_window") or "fullscreen"
        geo = QRect(screen.geometry())
        res = str(st.get("couch_resolution") or "")
        if mode == "custom" and "x" in res:
            try:
                pw, ph = (int(v) for v in res.lower().split("x", 1))
                dpr = max(1.0, float(screen.devicePixelRatio()))
                geo = QRect(geo.x(), geo.y(), max(640, round(pw / dpr)), max(400, round(ph / dpr)))
            except ValueError:
                pass
        return screen, geo, mode

    def place_window(self, win, wc=None) -> None:
        """Show ``win`` (couch mode or its browser) the way the display settings say."""
        screen, geo, mode = self.display_target()
        frameless = bool(win.windowFlags() & Qt.FramelessWindowHint)
        if mode == "fullscreen":
            if frameless:
                win.setWindowFlag(Qt.FramelessWindowHint, False)
            win.setScreen(screen)
            win.setGeometry(geo)                      # X11 / Windows: on that monitor (Wayland places it itself)
            win.showFullScreen()
        else:
            if win.isFullScreen():
                win.showNormal()
            if not frameless:
                win.setWindowFlag(Qt.FramelessWindowHint, True)
            win.setScreen(screen)
            win.setGeometry(geo)
            win.show()
            if wc is not None:
                wc.place(geo.x(), geo.y(), geo.width(), geo.height())
        log.info("couch mode display: %s (%dx%d, scale %.2f, %.0f Hz) on %s, %d screen(s); %s %dx%d", screen.name(),
                 screen.geometry().width(), screen.geometry().height(), screen.devicePixelRatio(),
                 screen.refreshRate(), QApplication.platformName(), len(QApplication.screens()), mode, geo.width(),
                 geo.height())

    def _fit_screen(self) -> None:
        """Cover what the display settings ask for — the whole screen by default, whatever its size, shape or
        scaling (a window manager may place a full-screen window short of the edges)."""
        if self._exiting or not self.isVisible() or self.isMinimized():
            return
        target, want, mode = self.display_target()
        on = self.screen()
        if mode == "fullscreen" and on is not None:
            # several monitors: the compositor decides where a full-screen window goes — fill *that* monitor
            # (forcing another monitor's size onto it left part of a bigger screen uncovered); move only when a
            # screen was chosen in Settings
            chosen = bool(self.actions.settings.get("couch_screen"))
            if not (chosen and target is not None and target is not on):
                want = on.geometry()
        if self.geometry() != want or (mode == "fullscreen") != self.isFullScreen():
            log.info("couch mode: the window was %dx%d on %s, should be %dx%d (%s); fixing it", self.width(),
                     self.height(), on.name() if on is not None else "?", want.width(), want.height(), mode)
            if mode == "fullscreen" and want == (on.geometry() if on is not None else None):
                self.setGeometry(want)
                self.showFullScreen()
            else:
                self.place_window(self, self.wc)
        self.images.clear()
        self._dirty = True
        super().update()

    def apply_display(self, revert: dict | None = None) -> None:
        """Display settings changed: show couch mode that way now. With ``revert`` (the previous values) ask
        "Keep these display settings?" and go back by itself after 15 seconds — a setting that does not suit the
        screen can never leave anyone stuck."""
        self.reduce_motion = bool(self.actions.settings.get("reduce_motion"))
        self.place_window(self, self.wc)
        self._set_pace(self.screen())
        QTimer.singleShot(400, self, self._fit_screen)
        if revert is None:
            return
        left = [15]

        def keep() -> None:
            ticker.stop()
            self._close_overlay()

        def undo() -> None:
            ticker.stop()
            self._close_overlay()
            for k, v in revert.items():
                self.actions.settings[k] = v
                self.bridge.call(self.bridge.core.set_setting(k, v), None, lambda _e: None, owner=self)
            self.apply_display(None)
            self.toasts.show("Display settings put back", "", "info")

        def tick() -> None:
            left[0] -= 1
            if self.overlay is None or self.overlay.tag != "display":
                ticker.stop()
            elif left[0] <= 0:
                undo()
            else:
                self.overlay.text = f"Going back to the previous settings in {left[0]} s."
                self.update()
        ticker = QTimer(self)
        ticker.setInterval(1000)
        ticker.timeout.connect(tick)
        ticker.start()
        self.show_overlay("Keep these display settings?", f"Going back to the previous settings in {left[0]} s.",
                          [("Keep", keep, "primary"), ("Go back", undo, "ghost")], cancel=undo, tag="display")

    # ------------------------------------------------------- steady frames
    def _set_pace(self, screen) -> None:
        """Frames go out at one steady rate (60 Hz, or the screen's own rate when that is a multiple). On a VRR /
        FreeSync / G-Sync screen the refresh rate follows the app's frames: uneven frames made it jump around —
        the flicker people saw."""
        if not self.actions.settings.get("couch_steady_frames", True):
            self._pump.stop()                           # switched off in Settings: frames only when something moves
            return
        hz = float(screen.refreshRate()) if screen is not None else 60.0
        if not hz or hz < 30:
            hz = 60.0
        while hz > 75:
            hz /= 2
        self._pump.setInterval(max(8, round(1000 / hz)))
        if not self._pump.isActive() and self.isVisible():
            self._pump.start()

    def update(self, *args) -> None:                    # noqa: D401 — every repaint goes through the frame pump
        if args:
            super().update(*args)
            return
        self._dirty = True
        if not self._pump.isActive():
            super().update()

    def _pump_tick(self) -> None:
        if not self.isVisible() or self.isMinimized() or not self.isActiveWindow():
            if self._dirty and self.isVisible() and not self.isMinimized():
                self._dirty = False
                super().update()
            return
        if self._dirty:
            self._dirty = False
            super().update()
        else:
            super().update(0, 0, 1, 1)                  # nothing moved: a one-pixel frame keeps the rate steady

    def _pads_changed(self, names: list) -> None:
        new = [n for n in names if n not in self._known_pads]
        self._known_pads = set(names)
        if new and self._opened:
            self.toasts.show("Controller connected", ", ".join(new), "success")
        elif self.pad_family:
            self.pad_family = ""
            self.update()

    def _family_changed(self, family: str) -> None:
        self.pad_family = family
        self.update()

    # ============================================================== input
    def _accepts_input(self) -> bool:
        """Never steer couch mode while a game is being played: only when it is the active window, or visible and
        no game is running at all (after a game ends the desktop may not hand focus back right away)."""
        if not self.isVisible() or self.isMinimized():
            return False
        if self.isActiveWindow():
            return True
        return not self.running

    def _pad_button(self, name: str, pressed: bool) -> None:
        if self.browser is not None and self.browser.active:
            self.browser.button(name, pressed)

    def _pad(self, name: str) -> None:
        if self.browser is not None and self.browser.active:
            try:
                self.browser.command(name)                # the browser has the screen: the controller drives it
            except Exception:
                log.exception("couch browser: handling %s failed", name)
            return
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
        if self.pads.family and self.pad_family != self.pads.family:
            self.pad_family = self.pads.family            # back on the controller after using the keyboard
        if self.cursor().shape() != Qt.BlankCursor:
            self.setCursor(Qt.BlankCursor)                # a console look: no mouse pointer while on the pad
        self.command(name)

    def focusNextPrevChild(self, _next: bool) -> bool:        # noqa: N802 — Tab switches tabs here
        return False

    def changeEvent(self, e) -> None:                         # noqa: N802
        super().changeEvent(e)
        if e.type() != e.Type.ActivationChange or self._exiting:
            return
        if self.isActiveWindow():
            self._idle_away.stop()
            self._refocus.stop()
            if not self._playing_here():
                self.wc.keep_above(True)
            self.update()
            return
        if self._launching and not self._playing_here():
            self._refocus.start()                       # Steam's pop-ups while the game starts: stay in front
        elif self._playing_here():
            self._step_aside()                          # the game has the focus now: make room right away
        elif self.isVisible() and not self.isMinimized():
            self._idle_away.start()                     # tabbed out by the user (or a window they opened)

    def _playing_here(self) -> bool:
        return any(i in self.running for i in self.launched)

    def _take_focus_back(self) -> None:
        if self._launching and not self._exiting and not self.isActiveWindow() and not self._playing_here():
            self.wc.keep_above(True)
            self.wc.activate()

    def _user_left(self) -> None:
        """Couch mode stayed in the background: the user chose another window — let it show."""
        if not self.isActiveWindow() and not self._launching and not self._exiting:
            self.wc.keep_above(False)

    def resizeEvent(self, e) -> None:                         # noqa: N802
        super().resizeEvent(e)
        self.images.clear()
        self._tag_cache.clear()
        self._shown.clear()
        self._fonts.clear()
        self._tab_fit.clear()

    def keyPressEvent(self, e) -> None:                       # noqa: N802 (Qt API)
        k = e.key()
        if self.keyboard is not None:                         # typing into the on-screen keyboard
            nav = {Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right"}.get(k)
            try:
                if nav:
                    self.keyboard.command(nav)
                elif k in (Qt.Key_Return, Qt.Key_Enter):
                    self.keyboard.done()
                elif k == Qt.Key_Escape:
                    self.keyboard.cancel()
                elif k == Qt.Key_Backspace:
                    self.keyboard.text = self.keyboard.text[:-1]
                elif e.text() and e.text().isprintable():
                    self.keyboard.type(e.text())
            except Exception:
                log.exception("couch mode: typing failed")
            self.update()
            return
        name = {Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
                Qt.Key_Return: "a", Qt.Key_Enter: "a", Qt.Key_Space: "a", Qt.Key_Escape: "b",
                Qt.Key_Backspace: "b", Qt.Key_Menu: "x", Qt.Key_PageUp: "lt",
                Qt.Key_PageDown: "rt", Qt.Key_Tab: "rb", Qt.Key_Backtab: "lb", Qt.Key_Home: "home",
                Qt.Key_Slash: "select"}.get(k)
        if name is not None and k not in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Return, Qt.Key_Enter) and \
                self.pad_family:
            self.pad_family = ""                  # typing on the keyboard: show keyboard keys
        if name is None:
            page = self.current_page()
            text = e.text()
            if page is not None and self.overlay is None and text and page.type_text(text):
                self.update()
                return
            if k == Qt.Key_I:
                self.command("x")
                return
            super().keyPressEvent(e)
            return
        self.command(name)

    def command(self, name: str) -> None:
        """One input, whatever it came from: up/down/left/right, a (confirm), b (back), x (details), lb/rb (tabs),
        start (go to ✕), select."""
        gcguard.defer()                        # no slow garbage collection in the middle of moving around
        try:
            self._command(name)
        except Exception:
            log.exception("couch mode: handling %s failed", name)
        self.update()

    def current_page(self) -> Page | None:
        """The page that has the screen: an opened game page, or the Store / Wishlist / Settings tab."""
        if self.stack:
            return self.stack[-1]
        return self.pages.get(self.tab)

    def _command(self, name: str) -> None:
        if self.keyboard is not None:
            self.keyboard.command(name)
            return
        if self.overlay is not None:
            self._overlay_command(name)
            return
        if name == "home":                          # the PS / Xbox / Steam button: back to Home
            self._switch_tab(T_HOME, force=True)
            return
        if name == "select":                        # View / Create: search the Store from anywhere
            self._switch_tab(T_STORE)
            self.pages[T_STORE].open_keyboard()
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
                    self._switch_tab(self.top_index, force=True)
                    self.zone = "main"
            return
        page = self.current_page()
        if page is not None:
            self.zone = "main"
            r = page.command(name)
            if r == "top":
                self.zone, self.top_index = "top", self.tab
            elif r == "back":
                if self.stack:
                    self.pop_page()
                else:
                    self.zone, self.top_index = "top", X_INDEX
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
        if self.tab == T_DOWNLOADS:
            rows = self._download_rows()
            n = len(rows)
            i = self.index[T_DOWNLOADS]
            if name in ("lt", "rt") and n:
                self.index[T_DOWNLOADS] = max(0, min(n - 1, i + (5 if name == "rt" else -5)))
            elif name == "up":
                if i == 0 or not n:
                    self.zone, self.top_index = "top", self.tab
                else:
                    self.index[T_DOWNLOADS] -= 1
            elif name == "down" and n:
                self.index[T_DOWNLOADS] = min(n - 1, i + 1)
            elif name == "a" and n:
                self._download_menu(*rows[min(i, n - 1)])
            elif name == "x":
                self._pause_or_resume_all()
            elif name == "b":
                self.zone, self.top_index = "top", X_INDEX
            return
        items = self._items()
        n = len(items)
        i = self.index[self.tab]
        cols = self._grid_cols() if self.tab == T_LIBRARY else n or 1
        if name in ("lt", "rt") and n:              # triggers: jump (two rows in the grid, five games on the shelf)
            step = cols * 2 if self.tab == T_LIBRARY else 5
            self._move(max(0, min(n - 1, i + (step if name == "rt" else -step))))
        elif name == "left" and n:
            self._move(max(0, i - 1))
        elif name == "right" and n:
            self._move(min(n - 1, i + 1))
        elif name == "up":
            if self.tab == T_LIBRARY and i >= cols:
                self._move(i - cols)
            elif self.tab == T_HOME and n:
                self.zone, self.button_index = "buttons", 0
            else:
                self.zone, self.top_index = "top", self.tab
        elif name == "down" and self.tab == T_LIBRARY and n:
            if i + cols < n:
                self._move(i + cols)
            elif i // cols < (n - 1) // cols:
                self._move(n - 1)
        elif name == "a" and n:
            self.primary(items[i])
        elif name == "a" and not n:
            self._switch_tab(T_STORE)
        elif name == "x" and n:
            self._details()
        elif name == "b":
            self.zone, self.top_index = "top", X_INDEX

    def _switch_tab(self, tab: int, force: bool = False) -> None:
        if tab == self.tab and not self.stack and not force:
            return
        self.stack.clear()
        self.tab = tab
        self.top_index = tab
        self.zone = "main"
        if tab == T_DOWNLOADS:
            self.bridge.call(self.bridge.core.downloads_snapshot(), self._downloads, owner=self)
        page = self.pages.get(tab)
        if page is not None:
            try:
                page.activate()
            except Exception:
                log.exception("couch mode: opening %s failed", TABS[tab])
        self.refresh_bg()
        self._kick_focus()

    def _move(self, i: int) -> None:
        if i == self.index[self.tab]:
            return
        self.index[self.tab] = i
        self.refresh_bg()
        self._kick_focus()

    # ------------------------------------------------------------- pages
    def open_game(self, games: list, update: dict | None = None) -> None:
        """A game's page (from the Store, the Wishlist, or the Library's Update)."""
        if not games:
            return
        page = GamePage(self, games, update)
        self.stack.append(page)
        self.zone = "main"
        page.activate()
        self.refresh_bg()
        self.update()

    def pop_page(self) -> None:
        if self.stack:
            self.stack.pop()
        self.zone = "main"
        self.refresh_bg()
        self.update()

    # ----------------------------------------------------------- mouse
    def _hit_at(self, pos) -> tuple[str, int] | None:
        for rect, kind, idx in reversed(self._hit):
            if rect.contains(QPointF(pos)):
                return kind, idx
        return None

    def mouseMoveEvent(self, e) -> None:                      # noqa: N802
        if self.cursor().shape() == Qt.BlankCursor:
            self.setCursor(Qt.ArrowCursor)
        h = self._hit_at(e.position())
        if self.keyboard is not None:
            if h and h[0] == "key":
                self.keyboard.hit(h[1], False)
                self.update()
            return
        if self.overlay is not None:
            if h and h[0] == "overlay" and h[1] != self.overlay.focus:
                self.overlay.focus = h[1]
                self.update()
            return
        if not h:
            return
        kind, idx = h
        if kind == "tab" or kind == "x":
            self.zone, self.top_index = "top", idx
        elif kind == "button":
            self.zone, self.button_index = "buttons", idx
        elif kind == "row":
            self.zone = "main"
            self.index[T_DOWNLOADS] = idx
        self.update()

    def mousePressEvent(self, e) -> None:                     # noqa: N802
        if e.button() == Qt.BackButton:
            self.command("b")
            return
        if e.button() != Qt.LeftButton:
            return
        h = self._hit_at(e.position())
        if h and h[0] == "x":
            self.keyboard = None
            self.request_exit()
            self.update()
            return
        if h and h[0] == "toast":
            t = self.toasts.items[h[1]] if 0 <= h[1] < len(self.toasts.items) else None
            self.toasts.dismiss(h[1])
            if t and t.get("action"):
                try:
                    t["action"][1]()
                except Exception:
                    log.exception("couch mode: message action failed")
            return
        if self.keyboard is not None:
            if h and h[0] == "key":
                self.keyboard.hit(h[1], True)
            self.update()
            return
        if self.overlay is not None:
            if h and h[0] == "overlay":
                self.overlay.focus = h[1]
                self._overlay_command("a")
            self.update()
            return
        if not h:
            return
        kind, idx = h
        try:
            if kind == "tab":
                self._switch_tab(idx, force=True)
            elif kind == "button":
                btns = self._hero_buttons()
                if 0 <= idx < len(btns):
                    btns[idx][1]()
            elif kind == "page":
                page = self.current_page()
                if page is not None:
                    self.zone = "main"
                    page.hit(idx, True)
            elif kind == "row":
                rows = self._download_rows()
                if self.index[T_DOWNLOADS] == idx and 0 <= idx < len(rows):
                    self._download_menu(*rows[idx])
                self.index[T_DOWNLOADS] = idx
                self.zone = "main"
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
            if self.overlay.menu:
                dy = e.angleDelta().y()
                if dy:
                    self._overlay_command("up" if dy > 0 else "down")
                    self.update()
            return
        if self.keyboard is not None:
            return
        dy = e.angleDelta().y() or e.angleDelta().x()
        if not dy:
            return
        if self.current_page() is None and self.tab == T_HOME:
            self.command("left" if dy > 0 else "right")
        else:
            self.command("up" if dy > 0 else "down")

    # ============================================================== overlays
    def show_overlay(self, title: str, text: str = "", buttons=None, lines=None, focus: int = 0,
                     cancel=None, tag: str = "") -> None:
        self.keyboard = None
        self.overlay = Overlay(title, text, list(buttons or [("OK", self.close_overlay, "primary")]), focus,
                               list(lines or []), cancel=cancel or self.close_overlay, tag=tag)
        self.update()

    def busy(self, title: str, text: str = "") -> None:
        self.overlay = Overlay(title, text, busy=True, tag="busy")
        self.update()

    def message(self, title: str, text: str = "") -> None:
        self.show_overlay(title, text, [("OK", self.close_overlay, "primary")])

    def menu(self, title: str, rows: list[tuple], text: str = "", lines=None, focus: int = 0) -> None:
        """A vertical list of choices: rows are (label, action, kind[, note])."""
        self.keyboard = None
        buttons = [(r[0], r[1], r[2]) for r in rows]
        notes = [(r[3] if len(r) > 3 else "") or "" for r in rows]
        self.overlay = Overlay(title, text, buttons, focus, list(lines or []), cancel=self.close_overlay,
                               menu=True, notes=notes, tag="menu")
        self.update()

    def close_overlay(self) -> None:
        self._close_overlay()

    def refresh_bg(self) -> None:
        self._update_bg()
        self.update()

    def _schedule_frame(self, ms: int = 16) -> None:
        if not self._frame_pending:
            self._frame_pending = True

            def go() -> None:
                self._frame_pending = False
                self.update()
            QTimer.singleShot(ms, self, go)              # tied to this window: never runs after it closed

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
            act = self._next_step(e)
            if act is not None:
                first = (act[0].upper(), act[1], "primary")
            else:
                first = ("ⓘ  " + self.state_text(e).upper(), lambda e=e: self.explain(e), "muted")
        return [first, ("MANAGE", self._details, "ghost")]

    def _next_step(self, e) -> tuple[str, Callable[[], None]] | None:
        """What gets a game that is not ready yet closer to Play (None: nothing to do but wait)."""
        st = (e.setup or {}).get("state")
        if e.id in self.extraction or e.status.value == "extracting" or st == "installing":
            return None
        if st == "pending":
            return ("⬇  Install game", lambda e=e: self.install_repack(e))
        if st == "waiting_steam":
            return ("⬇  Install in Steam", lambda e=e: self.install_steam_base(e))
        if e.status.value == "downloaded":
            return ("⬇  Install", lambda e=e: self.extract(e))
        if e.status.value in ("installed", "extracted") and not e.executable:
            return ("⚙  Set up", lambda e=e: self.choose_executable(e))
        return None

    def primary(self, e) -> None:
        if e.id in self.running:
            self.ask_stop(e)
        elif self.ready(e):
            self.play(e)
        else:
            step = self._next_step(e)
            if step is not None:
                step[1]()
            else:
                self.explain(e)

    def explain(self, e) -> None:
        st = self.state_text(e)
        if st.startswith("Installing"):
            text = "The game is being unpacked. It can be played as soon as that has finished."
        elif st == "Installer running":
            text = "The repack's installer is open in its own window. Finish it there; the game is set up for Play " \
                   "when it closes."
        elif st == "Files missing":
            text = "The game's files were not found on disk. Fix it from Manage (choose the game file), or " \
                   "uninstall it and download it again."
        else:
            text = "Open Manage to set it up."
        self.show_overlay(e.title, f"{st}. {text}", [("OK", self._close_overlay, "primary"),
                                                       ("Manage", self._details, "ghost")])

    def play(self, e) -> None:
        if e.id in self.running:
            return
        self._launch_entry = e
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
                self._launching = False
                self._close_overlay()
            self.overlay = Overlay(f"Play {entry.title}?", "", [("▶  PLAY", go, "play"), ("Cancel", cancel, "ghost")],
                                   lines=lines + [("File", entry.executable or "")], cancel=cancel)
            self.update()

        def started(entry) -> None:
            self.launched.add(entry.id)
            self._launch_at[entry.id] = time.monotonic()
            if self._exiting:
                return
            QTimer.singleShot(20_000, self, lambda i=entry.id: self._clear_starting(i))

        def failed(_err) -> None:
            self._launch_at.pop(e.id, None)
            self._launching = False
            if self._exiting:
                return
            if self.overlay is not None and self.overlay.tag == "starting":
                self.overlay = None
            self.update()
        self._launch_at[e.id] = time.monotonic()          # from now on the ✕ treats it as (about to be) running
        self._launching = True
        self.wc.keep_above(True)                         # Steam's windows stay behind the splash
        QTimer.singleShot(int(STARTING_WINDOW * 1000), self, self._launch_over)
        self.actions.launch(self, e, confirm=confirm, couch=True, started=started, failed=failed)
        self.update()

    def _launch_over(self) -> None:
        """Nothing has shown up for a while: stop holding the focus (the user may need Steam's window)."""
        if self._launching and not self._playing_here():
            self._launching = False
            self._refocus.stop()

    def _clear_starting(self, entry_id: int) -> None:
        if self._exiting:
            return
        if self.overlay is not None and self.overlay.tag == "starting":
            self.overlay = None
            if entry_id not in self.running:
                self.toasts.show("The game has not shown up yet", "If nothing opens, try Manage → Fix problems.",
                                 "warning")
            self.update()

    def ask_stop(self, e) -> None:
        self.overlay = Overlay(f"Quit {e.title}?", "The game is closed like with Stop in the Library — progress "
                               "the game has not saved is lost.",
                               [("Quit game", lambda e=e: self._stop([e.id], then_exit=False), "danger"),
                                ("Cancel", self._close_overlay, "ghost")], focus=1, cancel=self._close_overlay)

    # ------------------------------------------------------- manage a game
    def _details(self) -> None:
        e = self._focused_entry()
        if e is None:
            return
        self.manage(e)

    def manage(self, e) -> None:
        """Everything the desktop Library can do with a game, as one menu."""
        lines = [("Status", self.state_text(e)), ("Play time", playtime(e.playtime_seconds).replace(" played", "")),
                 ("Last played", relative_time(e.last_played_at) if e.last_played_at else "Never"),
                 ("From", theme.source_name(e.source) if e.source else "Imported")]
        rel = e.release or {}
        if rel.get("version"):
            v = rel["version"]
            if rel.get("status") == "update" and rel.get("latest"):
                v += f"  →  {rel['latest']} available"
            lines.append(("Version", v))
        rows: list[tuple] = []
        if e.id in self.running:
            rows.append(("■  Stop the game", lambda e=e: self.ask_stop(e), "danger", ""))
        elif self.ready(e):
            rows.append(("▶  Play", lambda e=e: (self._close_overlay(), self.play(e)), "play", ""))
        step = self._next_step(e)
        if step is not None and e.id not in self.running:
            rows.append((step[0], lambda f=step[1]: (self._close_overlay(), f()), "primary", ""))
        if e.id not in self.running:
            if rel.get("updating"):
                rows.append(("Continue the update", lambda e=e: self.update_game(e), "primary",
                             "Download the new version from the game's page"))
                rows.append(("Cancel the update", lambda e=e: self._cancel_update(e), "ghost", ""))
            elif rel.get("status") == "update" and rel.get("latest") and e.game_id:
                rows.append((f"Update to {rel['latest']}", lambda e=e: self.update_game(e), "primary",
                             f"You have {rel.get('version') or 'an older version'} — saves are backed up first"))
            if e.status.value in ("installed", "extracted", "broken") and (e.install_path or e.download_path):
                rows.append(("Choose the game file", lambda e=e: self.choose_executable(e), "ghost",
                             "Which program Play starts"))
            rows.append(("Fix problems", lambda e=e: self.fix_game(e), "ghost",
                         "Finds out why it does not start and repairs it (may test-start the game)"))
        rows.append(("Back up saves now", lambda e=e: self._backup(e), "ghost", ""))
        rows.append(("Restore saves…", lambda e=e: self.restore_saves(e), "ghost", "Put an earlier backup back"))
        if e.game_id:
            rows.append(("Open the game's page", lambda e=e: self._open_entry_page(e), "ghost",
                         "Download options, other sites"))
        if e.id not in self.running:
            rows.append(("Uninstall…", lambda e=e: self.uninstall(e), "danger", ""))
        rows.append(("Close", self._close_overlay, "ghost", ""))
        self.menu(e.title, rows, lines=lines)
        self.overlay.tag = "details"
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
        self._close_overlay()
        self.toasts.show("Backing up saves…", e.title)

        def done(b) -> None:
            self.toasts.show("Saves backed up" if b else "Nothing new to back up",
                             f"{e.title}: {b['files']} file(s)." if b else e.title, "success" if b else "info")
        self.bridge.call(self.bridge.core.library_call("backup_saves", e.id, "manual"), done,
                         lambda err: self.toasts.show("Could not back up", str(err), "error"), owner=self)

    def restore_saves(self, e) -> None:
        self.busy("Looking for save backups…", e.title)

        def got(r: dict) -> None:
            backups = r.get("backups") or []
            if not backups:
                self.message("No save backups yet", "Backups are made after you play and before updates or "
                                                    "uninstalls (if that is switched on in Settings).")
                return
            rows = [(f"{relative_time(b['created'])} — {b['reason_text']}",
                     lambda b=b: self._confirm_restore(e, b), "ghost",
                     f"{b['files']} file(s) · {', '.join(b.get('folders') or [])[:80]}") for b in backups[:20]]
            rows.append(("Close", self._close_overlay, "ghost", ""))
            self.menu(f"Restore saves — {e.title}", rows, text="The saves you have now are backed up first, so this "
                                                               "can be undone.")
        self.bridge.call(self.bridge.core.library_call("save_backups", e.id), got,
                         lambda err: self.message("Could not list the backups", str(err)), owner=self)

    def _confirm_restore(self, e, b: dict) -> None:
        def go() -> None:
            self.busy("Restoring saves…", e.title)
            self.bridge.call(self.bridge.core.library_call("restore_save_backup", e.id, b["path"]),
                             lambda res: (self._close_overlay(), self.toasts.show(
                                 "Saves restored", f"{res['files']} file(s) put back.", "success")),
                             lambda err: self.message("Could not restore", str(err)), owner=self)
        if e.id in self.running:
            self.message("Quit the game first", "Saves cannot be restored while the game is running.")
            return
        self.show_overlay("Restore these saves?", f"{b['reason_text']}, {relative_time(b['created'])}: "
                          f"{b['files']} file(s). Your current saves are backed up first.",
                          [("Restore", go, "primary"), ("Cancel", self._close_overlay, "ghost")])

    def choose_executable(self, e) -> None:
        self.busy("Looking for the game's programs…", e.title)

        def got(cands: list) -> None:
            if not cands:
                self.message("No program found", "No .exe or start script was found in the game's folder. The files "
                                                 "may be missing — uninstall it and download it again.")
                return
            rows = []
            for c in cands[:14]:
                bits = [c.kind] if getattr(c, "kind", "") and c.kind != "game" else []
                bits.append(getattr(c, "reason", "") or "")
                mark = "   ✓" if str(c.path) == (e.executable or "") else ""
                rows.append((c.path.name + mark, lambda c=c: self._set_exe(e, str(c.path)), "ghost",
                             " · ".join(b for b in bits if b)))
            rows.append(("Close", self._close_overlay, "ghost", ""))
            self.menu(f"Which program starts {e.title}?", rows, text="The best guess is first.")
        self.bridge.call(self.bridge.core.library_call("candidates", e.id), got,
                         lambda err: self.message("Could not look for programs", str(err)), owner=self)

    def _set_exe(self, e, path: str) -> None:
        self.bridge.call(self.bridge.core.library_call("set_executable", e.id, path),
                         lambda _r: (self._close_overlay(), self.toasts.show("Ready to play", e.title, "success")),
                         lambda err: self.message("Could not use this program", str(err)), owner=self)

    def extract(self, e) -> None:
        self.busy("Finding the archive…", e.title)

        def got(archives: list) -> None:
            if not archives:
                self.message("No archive found", "The downloaded file was not found. Download the game again.")
                return

            def go() -> None:
                self.bridge.call(self.bridge.core.extract_entry(e.id, archives[0]),
                                 lambda _r: (self._close_overlay(), self.toasts.show("Installing", e.title, "success")),
                                 lambda err: self.message("Could not install", str(err)), owner=self)
            self.show_overlay(f"Install {e.title}?", "The downloaded archive is unpacked into your games folder.",
                              [("Install", go, "primary"), ("Cancel", self._close_overlay, "ghost")],
                              lines=[("Archive", archives[0])])
        self.bridge.call(self.bridge.core.library_archives(e.id), got,
                         lambda err: self.message("Could not find the archive", str(err)), owner=self)

    def install_repack(self, e) -> None:
        self.busy("Getting the installer ready…", e.title)

        def got(info: dict | None) -> None:
            if not info or info.get("state") != "pending":
                self.message("No installer found", "This game does not need installing.")
                return
            lines = [("Installer", info.get("setup", "")), ("Installs to", str(info.get("target") or ""))]
            text = ("The repack's installer opens in its own window (couch mode makes room for it): choose what to "
                    "install and click Install. When it closes, the game is set up for Play. Only run installers "
                    "from sources you trust.")
            buttons = []
            if info.get("md5"):
                buttons.append(("Check files, then install", lambda: self._verify_then_install(e), "primary"))
            buttons.append(("Install now", lambda: self._run_installer(e), "primary" if not info.get("md5")
                            else "ghost"))
            buttons.append(("Cancel", self._close_overlay, "ghost"))
            self.show_overlay(f"Install {e.title}?", text, buttons, lines=lines)
        self.bridge.call(self.bridge.core.library_call("installer_info", e.id), got,
                         lambda err: self.message("Install", str(err)), owner=self)

    def _verify_then_install(self, e) -> None:
        self.busy("Checking the repack files…", "This reads every file once; big repacks take a few minutes.")

        def done(r: dict) -> None:
            broken = (r.get("bad") or []) + (r.get("missing") or [])
            if not broken:
                self.toasts.show("Repack files are OK", f"{r.get('checked', 0)} file(s) match.", "success")
                self._run_installer(e)
                return
            names = ", ".join(broken[:6]) + ("…" if len(broken) > 6 else "")
            self.show_overlay("Repack files damaged", f"{len(r.get('bad') or [])} damaged and "
                              f"{len(r.get('missing') or [])} missing file(s): {names}. Installing would most likely "
                              "fail part-way — download those parts again (a torrent re-checks itself).",
                              [("Install anyway", lambda: self._run_installer(e), "danger"),
                               ("Cancel", self._close_overlay, "ghost")], focus=1)
        self.bridge.call(self.bridge.core.library_call("verify_repack", e.id), done,
                         lambda err: self.message("Could not check the files", str(err)), owner=self)

    def _run_installer(self, e) -> None:
        self.busy("Starting the installer…", e.title)

        def ok(_r) -> None:
            self._close_overlay()
            self._installing.add(e.id)
            self.toasts.show("Installer started", "Finish it in its window — couch mode comes back when it closes.",
                             "success")
            self.step_away("installer")
        self.bridge.call(self.bridge.core.library_call("run_installer", e.id, True), ok,
                         lambda err: self.message("Could not start the installer", str(err)), owner=self)

    def install_steam_base(self, e) -> None:
        if not e.game_id:
            self.explain(e)
            return

        def done(r: dict) -> None:
            if r.get("opened") == "store" and r.get("url"):
                from ezgd.ui.dialogs.misc import open_url
                open_url(r["url"])
            self.toasts.show("Install it in Steam", f"Press “Install” for {r.get('name')} in Steam; the app adds the "
                                                    "fix by itself afterwards.", "info")
            self.step_away("steam")
        self.bridge.call(self.bridge.core.install_steam_base(e.game_id), done,
                         lambda err: self.message("Steam", str(err)), owner=self)

    def update_game(self, e) -> None:
        self.busy("Opening the update…", e.title)

        def begun(info: dict) -> None:
            def got(game) -> None:
                self._close_overlay()
                if game is None:
                    self.message("Game page not found", "The game's page is not in the catalog any more.")
                    return
                self.open_game([game], update=info)
            self.bridge.call(self.bridge.core.game(info["game_id"]), got,
                             lambda err: self.message("Could not open the game page", str(err)), owner=self)
        self.bridge.call(self.bridge.core.begin_game_update(e.id), begun,
                         lambda err: self.message("Could not update", str(err)), owner=self)

    def _cancel_update(self, e) -> None:
        self.bridge.call(self.bridge.core.cancel_game_update(e.id),
                         lambda _r: (self._close_overlay(), self.toasts.show("Update cancelled", e.title)),
                         lambda err: self.message("Could not cancel", str(err)), owner=self)

    def _open_entry_page(self, e) -> None:
        def got(game) -> None:
            self._close_overlay()
            if game is None:
                self.message("Game page not found", "The game's page is not in the catalog any more.")
            else:
                self.open_game([game])
        self.bridge.call(self.bridge.core.game(e.game_id), got,
                         lambda err: self.message("Could not open the game page", str(err)), owner=self)

    def fix_game(self, e) -> None:
        if e.id in self.running:
            self.message("Quit the game first", "The game has to be closed to check it.")
            return

        def go() -> None:
            self.busy(f"Checking {e.title}…", "Looking at the files, the setup and the logs; when the evidence is "
                                              "weak the game is started once for a test. This can take a few "
                                              "minutes.")
            self.bridge.call(self.bridge.core.fix_my_game(e.id, "", True), lambda v: self._fixed(e, v),
                             lambda err: self.message("Could not check the game", str(err)), owner=self)
        self.show_overlay(f"Fix {e.title}?", "The app checks the game and repairs what it is sure about. It may start "
                          "the game for a short test.", [("Fix problems", go, "primary"),
                                                         ("Cancel", self._close_overlay, "ghost")])

    def _fixed(self, e, v: dict) -> None:
        applied = v.get("applied") or []
        lines = [("Changed", a) for a in applied[:6]]
        status = v.get("status")
        if status == "fixed":
            title = f"Fixed — {e.title} ran in the test"
            text = f"It kept running with: {v.get('kept') or 'the repaired setup'}. Play it — does it work now?"

            def answer(worked: bool) -> None:
                self._close_overlay()

                def done(_st) -> None:
                    if worked:
                        self.toasts.show("Great — noted", "The assistant remembers what fixed it.", "success")
                    else:
                        self.bridge.call(self.bridge.core.undo_fix_my_game(e.id, v.get("before", {})),
                                         lambda msg: self.toasts.show("Undone", msg), owner=self)
                self.bridge.call(self.bridge.core.record_outcome(e.id, worked, applied), done, owner=self)
            self.show_overlay(title, text, [("▶  Play now", lambda: (self._close_overlay(), self.play(e)), "play"),
                                            ("It works", lambda: answer(True), "primary"),
                                            ("Still broken — undo", lambda: answer(False), "ghost")], lines=lines)
            return
        if status == "changed":
            self.show_overlay("Repairs applied", "Start the game to see if it works now.",
                              [("▶  Play now", lambda: (self._close_overlay(), self.play(e)), "play"),
                               ("Close", self._close_overlay, "ghost")], lines=lines)
            return
        reason = v.get("reason") or ("Nothing to repair automatically." if status == "nothing" else "")
        self.show_overlay("Not fixed", reason + " The desktop's Diagnose (Library) shows every finding in detail.",
                          [("Close", self._close_overlay, "primary")], lines=lines)

    def uninstall(self, e) -> None:
        if e.id in self.running:
            self.message("Quit the game first", "A running game cannot be uninstalled.")
            return
        self.busy("Checking what belongs to the game…", e.title)

        def got(plan: dict) -> None:
            items = [it for it in plan.get("items") or [] if it.get("default")]
            keys = [it["key"] for it in items]
            lines = [("Removes", it["title"]) for it in items[:8]] or [("Removes", "The library entry")]
            auto = bool(self.actions.settings.get("auto_backup_saves", True))

            def go() -> None:
                self.busy(f"Uninstalling {e.title}…", "")
                self.bridge.call(self.bridge.core.uninstall_game(e.id, keys, auto),
                                 lambda _r: (self._close_overlay(), self.toasts.show(
                                     "Uninstalled", e.title + (" — your saves are backed up." if auto else ""),
                                     "success")),
                                 lambda err: self.message("Could not uninstall completely", str(err)), owner=self)
            self.show_overlay(f"Uninstall {e.title}?", ("Your saves are backed up first. " if auto else "")
                              + "Only what the app put there is removed.",
                              [("Uninstall", go, "danger"), ("Cancel", self._close_overlay, "ghost")], focus=1,
                              lines=lines)
        self.bridge.call(self.bridge.core.uninstall_plan(e.id), got,
                         lambda err: self.message("Could not uninstall", str(err)), owner=self)

    # --------------------------------------------------------- downloads
    def _download_menu(self, kind: str, d) -> None:
        if kind == "install":
            info = self.extraction_info.get(d.id) or {}
            rows = []
            if info.get("id") is not None:
                rows.append(("Cancel installing", lambda: self.bridge.call(
                    self.bridge.core.cancel_extraction(info["id"]),
                    lambda _r: (self._close_overlay(), self.toasts.show("Installing cancelled", d.title)),
                    owner=self), "danger", "The archive is kept; install it again from the Library"))
            rows.append(("Close", self._close_overlay, "ghost", ""))
            self.menu(d.title, rows, text=f"Installing — {int(self.extraction.get(d.id, 0) * 100)}%")
            return
        st = d.get("status")
        tid = d.get("id")
        title = d.get("title") or d.get("filename") or "Download"
        cmd = self._download_command
        rows = []
        if st in ("downloading", "metadata", "checking", "verifying", "queued"):
            rows.append(("Pause", lambda: cmd("pause", tid, "Paused"), "ghost", ""))
        if st == "paused":
            rows.append(("Resume", lambda: cmd("resume", tid, "Resumed"), "primary", ""))
        if st == "failed":
            rows.append(("Try again", lambda: cmd("retry", tid, "Trying again"), "primary",
                         str(d.get("error") or "")[:100]))
        if st in ("completed",) and d.get("kind") == "torrent":
            rows.append(("Check the files", lambda: cmd("recheck", tid, "Checking the files"), "ghost",
                         "Re-downloads only damaged pieces"))
        if st not in ("completed", "failed", "cancelled"):
            rows.append(("Cancel download", lambda: self._confirm_cancel(tid, title), "danger", ""))
        if st in ("completed", "failed"):
            rows.append(("Remove from the list", lambda: cmd("remove", tid, "Removed"), "ghost",
                         "The downloaded files stay"))
        rows.append(("Close", self._close_overlay, "ghost", ""))
        lines = [("Status", str(st or "").capitalize())]
        if d.get("error") and st == "failed":
            lines.append(("Error", str(d["error"])[:200]))
        self.menu(title, rows, lines=lines)

    def _download_command(self, command: str, tid: int, done_text: str) -> None:
        def ok(_r) -> None:
            self._close_overlay()
            self.toasts.show(done_text, "", "success")
            self.bridge.call(self.bridge.core.downloads_snapshot(), self._downloads, owner=self)
        self.bridge.call(self.bridge.core.download_command(command, tid), ok,
                         lambda err: self.message("Could not do that", str(err)), owner=self)

    def _confirm_cancel(self, tid: int, title: str) -> None:
        self.show_overlay(f"Cancel {title}?", "The download stops and what was downloaded so far is deleted.",
                          [("Cancel download", lambda: self._download_command("cancel", tid, "Download cancelled"),
                            "danger"), ("Keep downloading", self._close_overlay, "ghost")], focus=1)

    def _pause_or_resume_all(self) -> None:
        active = any(d.get("status") in ("downloading", "metadata", "queued", "checking") for d in self.downloads)
        coro = self.bridge.core.pause_all() if active else self.bridge.core.resume_all()
        self.bridge.call(coro, lambda _r: (self.toasts.show("All downloads paused" if active else "Downloads resumed"),
                                           self.bridge.call(self.bridge.core.downloads_snapshot(), self._downloads,
                                                            owner=self)), owner=self)

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
        if o.menu:
            if name in ("lt", "rt"):
                o.focus = max(0, min(n - 1, o.focus + (5 if name == "rt" else -5)))
            elif name == "up":
                o.focus = max(0, o.focus - 1)
            elif name == "down":
                o.focus = min(n - 1, o.focus + 1)
            elif name == "a" and n:
                o.buttons[o.focus][1]()
            elif name == "b":
                (o.cancel or self._close_overlay)()
            return
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
        self.keyboard = None
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
        self.keyboard = None
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
        if self.browser is not None and self.browser.active:
            browser, self.browser = self.browser, None
            browser.active = False                       # leaving couch mode closes its browser too
            browser.overlay.hide()
            browser.dlg.close()                          # (a download keeps going: the window only hides)
        for t in (self._tick, self._clock, self._poll, self._minimize_timer, self._pump, self._idle_away,
                  self._refocus):
            t.stop()
        self.wc.forget()
        self.toasts.stop()
        self.images.shutdown()
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

    def _bg_url(self) -> str | None:
        page = self.current_page()
        if page is not None:
            try:
                return page.background()
            except Exception:
                return None
        if self.tab in (T_HOME, T_LIBRARY):
            e = self._focused_entry()
            return self.hero_url(e) if e is not None else None
        return None

    def _bg_mode(self) -> str:
        page = self.current_page()
        if page is None and self.tab == T_HOME:
            return "home"
        return "game" if isinstance(page, GamePage) else "dim"

    def _update_bg(self) -> None:
        """The background follows the focus; it changes (with a short fade) once its picture is ready."""
        self.update()

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
        scale = int(self.actions.settings.get("couch_ui_scale") or 100) / 100
        return max(0.4, min(3.0, max(0.55, min(2.2, self.height() / 1080)) * scale))

    def _font(self, px: float, weight: int = 400) -> QFont:
        key = (max(9, int(px)), weight)
        f = self._fonts.get(key)
        if f is None:
            f = QFont(self.font())
            f.setPixelSize(key[0])
            f.setWeight(QFont.Weight(weight))
            self._fonts[key] = f
        return f

    def paintEvent(self, _e) -> None:                         # noqa: N802
        rect = _e.rect()
        if rect.width() <= 1 and rect.height() <= 1 and not self._dirty:
            return                                      # the frame pump's keep-alive: the picture stays as it is
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setRenderHint(QPainter.TextAntialiasing)
            self._hit = []
            p.setBrush(Qt.NoBrush)
            self._paint_background(p)
            page = self.current_page()
            if page is not None:
                page.paint(p)
                p.setBrush(Qt.NoBrush)
            elif self.tab == T_HOME:
                self._paint_home(p)
            elif self.tab == T_LIBRARY:
                self._paint_library(p)
            else:
                self._paint_downloads(p)
            splash = self.overlay is not None and self.overlay.tag == "starting"
            if not splash:
                self._paint_top(p)
                self._paint_bottom(p)
                self._paint_toasts(p)
            if self.overlay is not None:
                p.setBrush(Qt.NoBrush)
                self._paint_overlay(p)
            if self.keyboard is not None:
                p.setBrush(Qt.NoBrush)
                self.keyboard.paint(p)
                self._hit.append((self._x_rect(), "x", X_INDEX))
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
        """One or two finished full-screen pixmaps (made by worker threads, see Images.layer) — no gradients or
        scaling here, so frames stay fast while the focus moves."""
        W, H = self.width(), self.height()
        want = (self._bg_url(), self._bg_mode())
        now = time.monotonic()
        if want != self._bg_want:                   # the focus moved: wait until it rests before changing
            self._bg_want, self._bg_want_at = want, now
        rested = now - self._bg_want_at >= self.BG_REST or self._bg_cur is None
        if want != self._bg_cur and not rested:
            self._schedule_frame(int((self.BG_REST - (now - self._bg_want_at)) * 1000) + 5)
        if want != self._bg_cur and rested and self.images.layer(want[0], W, H, want[1]) is not None:
            # the new background is ready: fade to it from what is on screen now
            self._bg_prev = self._bg_cur
            self._bg_cur = want
            if self.reduce_motion or self._bg_prev is None:
                self._bg_t = 1.0
            else:
                self._bg_t = 0.0
                self._bg_va.stop()
                self._bg_va.setStartValue(0.0)
                self._bg_va.setEndValue(1.0)
                self._bg_va.start()
        cur = self.images.layer(self._bg_cur[0], W, H, self._bg_cur[1]) if self._bg_cur else None
        prev = self.images.layer(self._bg_prev[0], W, H, self._bg_prev[1]) \
            if self._bg_prev and self._bg_t < 1.0 else None
        if cur is None and prev is None:
            p.fillRect(self.rect(), QColor(theme.CURRENT.bg0))
            return
        if prev is not None:
            p.drawImage(QPointF(0, 0), prev)
            if cur is not None:
                p.setOpacity(self._bg_t)
                p.drawImage(QPointF(0, 0), cur)
                p.setOpacity(1.0)
        else:
            p.drawImage(QPointF(0, 0), cur)

    def _paint_top(self, p: QPainter) -> None:
        s, W = self._s(), self.width()
        h = 96 * s
        bar = QLinearGradient(0, 0, 0, h)
        bar.setColorAt(0, QColor(0, 0, 0, 160))
        bar.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, W, h), QBrush(bar))
        x = 56 * s
        p.setPen(QColor(theme.CURRENT.accent))
        p.setFont(self._font(26 * s, 800))
        p.drawText(QRectF(x, 0, 400 * s, h), Qt.AlignVCenter | Qt.AlignLeft, "EZ")
        x += QFontMetrics(p.font()).horizontalAdvance("EZ ") + 2 * s
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, 0, 400 * s, h), Qt.AlignVCenter | Qt.AlignLeft, "GAMES")
        x += QFontMetrics(p.font()).horizontalAdvance("GAMES") + 56 * s
        right = W - 120 * s
        clock = time.strftime("%H:%M")
        p.setFont(self._font(22 * s, 600))
        cw = QFontMetrics(p.font()).horizontalAdvance(clock)
        active = int(self.summary.get("active") or 0) + int(self.summary.get("extracting") or 0)
        status = ""
        if active:
            from ezgd.core.fsutil import format_speed
            status = f"↓ {format_speed(self.summary.get('speed') or 0)}" if self.summary.get("active") else \
                "Installing…"
        p.setFont(self._font(17 * s, 600))
        sw = QFontMetrics(p.font()).horizontalAdvance(status) + 28 * s if status else 0
        # tabs: as big as fits between the logo and the clock
        avail = right - cw - sw - 30 * s - x
        fit_key = (round(avail), round(s, 3))
        fpx = self._tab_fit.get(fit_key)
        if fpx is None:
            fpx = 21 * s
            while fpx > 11:
                fm = QFontMetrics(self._font(fpx, 700))
                need = sum(fm.horizontalAdvance(n) for n in TABS) + (len(TABS) - 1) * fpx * 1.9
                if need <= avail:
                    break
                fpx -= 1
            self._tab_fit[fit_key] = fpx
        f = self._font(fpx, 700)
        fm = QFontMetrics(f)
        p.setFont(f)
        gap = fpx * 1.9
        for i, name in enumerate(TABS):
            tw = fm.horizontalAdvance(name)
            r = QRectF(x - fpx * 0.75, h / 2 - fpx * 1.25, tw + fpx * 1.5, fpx * 2.5)
            focused = self.zone == "top" and self.top_index == i and self.overlay is None and self.keyboard is None
            selected = self.tab == i
            if focused:
                path = QPainterPath()
                path.addRoundedRect(r, r.height() / 2, r.height() / 2)
                p.fillPath(path, QColor(255, 255, 255, 235))
                p.setPen(QColor(10, 12, 18))
            else:
                p.setPen(QColor(255, 255, 255, 255 if selected else 150))
            p.drawText(r, Qt.AlignCenter, name)
            if selected and not focused:
                p.fillRect(QRectF(x, r.bottom() - 2 * s, tw, 3 * s), QColor(theme.CURRENT.accent))
            self._hit.append((r, "tab", i))
            x += tw + gap
        # right side: downloads, clock, the ✕
        p.setFont(self._font(22 * s, 600))
        p.setPen(QColor(255, 255, 255, 220))
        p.drawText(QRectF(right - cw, 0, cw, h), Qt.AlignVCenter | Qt.AlignRight, clock)
        if status:
            p.setFont(self._font(17 * s, 600))
            tw = QFontMetrics(p.font()).horizontalAdvance(status)
            p.setPen(QColor(theme.CURRENT.success))
            p.drawText(QRectF(right - cw - 28 * s - tw, 0, tw, h), Qt.AlignVCenter | Qt.AlignRight, status)
        self._paint_x(p)

    def _x_rect(self) -> QRectF:
        s = self._s()
        r = 34 * s
        c = QPointF(self.width() - 62 * s, 48 * s)
        return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)

    def _paint_x(self, p: QPainter) -> None:
        s = self._s()
        r = self._x_rect()
        focused = self.zone == "top" and self.top_index == X_INDEX and self.overlay is None and self.keyboard is None
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

    def _glyph(self, p: QPainter, x: float, y: float, action: str, text: str = "") -> float:
        """A button prompt for the controller in use (PlayStation symbols, Xbox/Steam letters, keyboard keys) and its
        meaning. Returns the x after it."""
        s = self._s()
        d = 32 * s
        fam = self.pad_family
        labels = PAD_LABELS.get(fam, PAD_LABELS["generic"])
        label = labels.get(action, action)
        face = action in ("a", "b", "x") and fam != ""
        if face:
            r = QRectF(x, y - d / 2, d, d)
            if fam == "playstation":
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(26, 29, 38))
                p.drawEllipse(r)
                p.setBrush(Qt.NoBrush)
                self._ps_symbol(p, r, action)
            else:
                bg, fg = FACE_COLORS.get(fam, FACE_COLORS["xbox"]).get(action, ("#475569", "white"))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(bg))
                p.drawEllipse(r)
                p.setBrush(Qt.NoBrush)
                p.setPen(QColor(fg))
                p.setFont(self._font(15 * s, 800))
                p.drawText(r, Qt.AlignCenter, label)
            w = d
        elif action in ("lr", "ud", "dpad"):
            w = self._dpad(p, x, y, d, action)
        else:
            f = self._font(13.5 * s, 800)
            w = max(d * 1.25, QFontMetrics(f).horizontalAdvance(label) + 18 * s)
            r = QRectF(x, y - d / 2 + 2 * s, w, d - 4 * s)
            path = QPainterPath()
            radius = r.height() / 2 if action in ("lb", "rb", "lt", "rt") else 7 * s
            path.addRoundedRect(r, radius, radius)
            p.fillPath(path, QColor(71, 85, 105))
            p.setPen(QColor("white"))
            p.setFont(f)
            p.drawText(r, Qt.AlignCenter, label)
        if not text:
            return x + w + 8 * s
        p.setFont(self._font(18 * s, 600))
        p.setPen(QColor(255, 255, 255, 215))
        tw = QFontMetrics(p.font()).horizontalAdvance(text)
        p.drawText(QRectF(x + w + 10 * s, y - d / 2, tw + 4, d), Qt.AlignVCenter | Qt.AlignLeft, text)
        return x + w + 10 * s + tw + 34 * s

    def _ps_symbol(self, p: QPainter, r: QRectF, action: str) -> None:
        """✕ ○ △ drawn as shapes (fonts do not always have them), in PlayStation's colours."""
        s = self._s()
        c = r.center()
        k = r.width() * 0.22
        color = {"a": "#7ca3f0", "b": "#ff6b6b", "x": "#3ddc97"}.get(action, "white")
        pen = QPen(QColor(color), 2.6 * s)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if action == "a":
            p.drawLine(QPointF(c.x() - k, c.y() - k), QPointF(c.x() + k, c.y() + k))
            p.drawLine(QPointF(c.x() + k, c.y() - k), QPointF(c.x() - k, c.y() + k))
        elif action == "b":
            p.drawEllipse(c, k * 1.1, k * 1.1)
        else:
            path = QPainterPath()
            path.moveTo(c.x(), c.y() - k * 1.15)
            path.lineTo(c.x() + k * 1.2, c.y() + k * 0.85)
            path.lineTo(c.x() - k * 1.2, c.y() + k * 0.85)
            path.closeSubpath()
            p.drawPath(path)

    def _dpad(self, p: QPainter, x: float, y: float, d: float, action: str) -> float:
        """A small d-pad with the directions that do something highlighted."""
        arm = d * 0.32
        c = QPointF(x + d / 2, y)
        on = {"lr": ("l", "r"), "ud": ("u", "d"), "dpad": ("l", "r", "u", "d")}[action]
        for dirn, (dx, dy) in {"l": (-1, 0), "r": (1, 0), "u": (0, -1), "d": (0, 1)}.items():
            rr = QRectF(c.x() + dx * arm - arm / 2, c.y() + dy * arm - arm / 2, arm, arm)
            p.fillRect(rr, QColor(255, 255, 255, 235) if dirn in on else QColor(255, 255, 255, 70))
        p.fillRect(QRectF(c.x() - arm / 2, c.y() - arm / 2, arm, arm), QColor(255, 255, 255, 120))
        return d

    def _paint_bottom(self, p: QPainter) -> None:
        s, W, H = self._s(), self.width(), self.height()
        h = 76 * s
        top = H - h
        p.fillRect(QRectF(0, top, W, h), QColor(0, 0, 0, 150))
        p.fillRect(QRectF(0, top, W, 1 * s), QColor(255, 255, 255, 30))
        y = top + h / 2
        x = 56 * s
        if self.keyboard is not None:
            x = self._glyph(p, x, y, "a", "Type")
            x = self._glyph(p, x, y, "b", "Delete")
            x = self._glyph(p, x, y, "start", self.keyboard.done_label)
        elif self.overlay is not None:
            if not self.overlay.busy:
                x = self._glyph(p, x, y, "a", "Select")
                x = self._glyph(p, x, y, "b", "Back")
        else:
            page = self.current_page()
            e = self._focused_entry()
            if self.zone == "top":
                x = self._glyph(p, x, y, "a", "Leave couch mode" if self.top_index == X_INDEX else "Open")
                x = self._glyph(p, x, y, "b", "Back")
            elif page is not None:
                for action, text in page.hints():
                    x = self._glyph(p, x, y, action, text)
            elif self.tab in (T_HOME, T_LIBRARY) and e is not None:
                step = self._next_step(e)
                act = "Stop" if e.id in self.running else "Play" if self.ready(e) else \
                    (step[0].split("  ", 1)[-1] if step else "Why not?")
                x = self._glyph(p, x, y, "a", act if self.zone == "main" else "Select")
                x = self._glyph(p, x, y, "x", "Manage")
                x = self._glyph(p, x, y, "b", "Exit…")
            elif self.tab == T_DOWNLOADS:
                if self._download_rows():
                    x = self._glyph(p, x, y, "a", "Options")
                x = self._glyph(p, x, y, "x", "Pause / resume all")
                x = self._glyph(p, x, y, "b", "Exit…")
            else:
                if self.tab in (T_HOME, T_LIBRARY):
                    x = self._glyph(p, x, y, "a", "Go to the Store")
                x = self._glyph(p, x, y, "b", "Exit…")
            x = self._glyph(p, x, y, "lb")
            x = self._glyph(p, x, y, "rb", "Tabs")
        # right: the controller in use
        fam = self.pad_family
        names = self.pads.names()
        txt = (FAMILY_NAMES.get(fam, "Controller") + (f"  ·  {len(names)} connected" if len(names) > 1 else "")) \
            if names else "Keyboard: arrows · Enter · Esc · Tab"
        p.setFont(self._font(16 * s, 600))
        p.setPen(QColor(255, 255, 255, 150))
        room = W - x - 56 * s
        if room > 120 * s:
            p.drawText(QRectF(x, top, room, h), Qt.AlignVCenter | Qt.AlignRight,
                       QFontMetrics(p.font()).elidedText(txt, Qt.ElideRight, int(room)))

    def _paint_toasts(self, p: QPainter) -> None:
        items = self.toasts.items
        if not items:
            return
        s, W, H = self._s(), self.width(), self.height()
        tw = min(620 * s, W * 0.45)
        y = H - 76 * s - 22 * s
        now = time.monotonic()
        for k in range(len(items) - 1, -1, -1):
            t = items[k]
            f_title, f_text = self._font(20 * s, 800), self._font(17 * s, 500)
            inner = tw - 76 * s
            p.setFont(f_text)
            th = p.boundingRect(QRectF(0, 0, inner, 1000), Qt.TextWordWrap, t["text"]).height() if t["text"] else 0
            th = min(th, 5 * QFontMetrics(f_text).lineSpacing())
            h = 30 * s + 28 * s + (th + 6 * s if th else 0) + (30 * s if t.get("action") else 0) + 14 * s
            y -= h
            r = QRectF(W - tw - 36 * s, y, tw, h)
            fade = 1.0 if self.toasts.reduce_motion else min(1.0, (now - t["born"]) / 0.18,
                                                              max(0.0, (t["until"] - now) / 0.4))
            p.setOpacity(max(0.0, fade))
            path = QPainterPath()
            path.addRoundedRect(r, 18 * s, 18 * s)
            p.fillPath(path, QColor(18, 20, 28, 240))
            color = {"error": theme.CURRENT.danger, "warning": theme.CURRENT.warning,
                     "success": theme.CURRENT.success}.get(t["kind"], theme.CURRENT.accent)
            p.fillRect(QRectF(r.left(), r.top() + 16 * s, 6 * s, r.height() - 32 * s), QColor(color))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 30), 1.5 * s))
            p.drawPath(path)
            p.setFont(f_title)
            p.setPen(QColor("white"))
            p.drawText(QRectF(r.left() + 36 * s, r.top() + 18 * s, inner, 30 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(f_title).elidedText(t["title"], Qt.ElideRight, int(inner)))
            if th:
                p.setFont(f_text)
                p.setPen(QColor(255, 255, 255, 190))
                p.drawText(QRectF(r.left() + 36 * s, r.top() + 54 * s, inner, th), Qt.TextWordWrap, t["text"])
            if t.get("action"):
                p.setFont(self._font(16 * s, 800))
                p.setPen(QColor(theme.CURRENT.accent).lighter(140))
                p.drawText(QRectF(r.left() + 36 * s, r.bottom() - 40 * s, inner, 26 * s),
                           Qt.AlignLeft | Qt.AlignVCenter, t["action"][0] + "  ›")
            p.setOpacity(1.0)
            self._hit.append((r, "toast", k))
            y -= 12 * s
            if fade < 1.0:
                self._schedule_frame(30)

    # ------------------------------------------------------------- tiles
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
        f = self._font(r.height() * 0.36, 800)
        p.setFont(f)
        p.drawText(r.adjusted(14 * s, 0, -14 * s, 0), Qt.AlignCenter,
                   QFontMetrics(f).elidedText(text, Qt.ElideRight, int(r.width() - 28 * s)))

    def art_tile(self, p: QPainter, r: QRectF, focused: bool, capsule: str | None, cover: str | None, title: str,
                 badge: tuple[str, str] | None, dim: bool, hit: tuple[str, int],
                 tags: list[tuple[str, str]] | None = None) -> None:
        """A portrait game tile: Steam's capsule when there is one; otherwise the site's cover shown whole (over a
        blur of itself), never stretched or cut off. The picture is one finished pixmap (rounded corners and all,
        see Images.tile); the focused tile is the same pixmap drawn a little bigger. ``tags``: (text, colour)
        pills along the bottom — which sites have the game."""
        s = self._s()
        radius = 12 * s
        w, h = max(1, int(round(r.width()))), max(1, int(round(r.height())))
        if focused:
            grow = 1.0 + 0.06 * self._focus_anim
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
        pm, needs_title, key = self.images.tile(capsule, cover, w, h, radius, dim)
        ident = (title, cover, w, h, dim)
        prev = self._shown.get(ident)                   # what this tile showed last (never back to blank)
        if pm is None and prev is not None:
            pm, needs_title, key = prev[0], prev[1], None
        fade = 1.0
        if pm is not None and key is not None and not self.reduce_motion:
            fade = min(1.0, (time.monotonic() - self.images.ready_since(key)) / Images.FADE)
        under = prev[0] if prev is not None and prev[0] is not pm else None
        if pm is None or fade < 1.0:
            if under is not None:                       # cross-fade from the picture it had
                p.drawImage(r, under, QRectF(under.rect()))
            else:                                       # a quiet placeholder (no bright flash)
                p.fillPath(path, QColor(255, 255, 255, 16))
                if dim:
                    p.fillPath(path, QColor(0, 0, 0, 90))
            if fade < 1.0:
                self._schedule_frame()
        if pm is not None and key is not None and fade >= 1.0:
            self._shown[ident] = (pm, needs_title)
            if len(self._shown) > 400:
                self._shown.pop(next(iter(self._shown)))
        if pm is not None:
            p.setOpacity(fade)
            if focused:
                p.drawImage(r, pm, QRectF(pm.rect()))
            else:                                       # on whole device pixels: a plain copy, no resampling
                dpr = pm.devicePixelRatio()
                p.drawImage(QPointF(round(r.left() * dpr) / dpr, round(r.top() * dpr) / dpr), pm)
            p.setOpacity(1.0)
        if needs_title:                                  # above the site tags, at most two lines
            f = self._font(max(12, r.width() * 0.075), 800)
            fm = QFontMetrics(f)
            lift = self.tag_height(tags) + 8 * s if tags else 0.0
            th = 2 * fm.lineSpacing()
            tr = QRectF(r.left() + 10 * s, r.bottom() - 10 * s - lift - th, r.width() - 20 * s, th)
            p.setPen(QColor("white"))
            p.setFont(f)
            p.drawText(tr, Qt.AlignHCenter | Qt.AlignBottom | Qt.TextWordWrap, title)
        if badge:
            p.setFont(self._font(12 * s, 800))
            tw = QFontMetrics(p.font()).horizontalAdvance(badge[0]) + 16 * s
            br = QRectF(r.left() + 8 * s, r.top() + 8 * s, min(tw, r.width() - 16 * s), 24 * s)
            bp = QPainterPath()
            bp.addRoundedRect(br, 12 * s, 12 * s)
            p.fillPath(bp, QColor(badge[1]))
            p.setPen(QColor("white"))
            p.drawText(br, Qt.AlignCenter, QFontMetrics(p.font()).elidedText(badge[0], Qt.ElideRight,
                                                                             int(br.width() - 8 * s)))
        if tags:
            self._tags(p, r, tags, w)
        if focused or pm is None:                       # unfocused tiles have their faint border built in
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 255 if focused else 40), (3 if focused else 1) * s))
            p.drawPath(path)
        self._hit.append((r, hit[0], hit[1]))

    def _tags(self, p: QPainter, r: QRectF, tags: list[tuple[str, str]], base_w: int) -> None:
        """Small coloured pills along the bottom of a tile (what does not fit becomes "+N"), drawn once into a
        pixmap and reused."""
        s = self._s()
        key = (tuple(tags), base_w, round(s, 3))
        pm = self._tag_cache.get(key)
        if pm is None:
            pm = self._render_tags(tags, base_w, s)
            if len(self._tag_cache) > 300:
                self._tag_cache.clear()
            self._tag_cache[key] = pm
        dpr = pm.devicePixelRatio()
        h = pm.height() / dpr
        x, y = r.left() + 8 * s, r.bottom() - h - 8 * s
        p.drawPixmap(QPointF(round(x * dpr) / dpr, round(y * dpr) / dpr), pm)

    TAG_ROWS = 4

    def _tag_rows(self, tags: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """The pills shown: one per site, at most four; the last says "+N more" when there are more."""
        if len(tags) <= self.TAG_ROWS:
            return list(tags)
        return list(tags[:self.TAG_ROWS - 1]) + [(f"+{len(tags) - self.TAG_ROWS + 1} more", "#475569")]

    def tag_height(self, tags: list[tuple[str, str]]) -> float:
        s = self._s()
        n = len(self._tag_rows(tags))
        return n * 24 * s + max(0, n - 1) * 5 * s if n else 0.0

    def _render_tags(self, tags: list[tuple[str, str]], base_w: int, s: float) -> QPixmap:
        """One pill per site, stacked from the bottom of the tile — every site's name stays readable."""
        dpr = self.images.dpr()
        f = self._font(11.5 * s, 800)
        fm = QFontMetrics(f)
        h, gap = 24 * s, 5 * s
        room = base_w - 16 * s
        rows = self._tag_rows(tags)
        widths = [min(room, fm.horizontalAdvance(t) + 18 * s) for t, _c in rows]
        total_h = len(rows) * h + max(0, len(rows) - 1) * gap
        pm = QPixmap(max(1, round(max(widths or [1]) * dpr)), max(1, round(total_h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(f)
        y = 0.0
        for (text, color), pw in zip(rows, widths):
            tr = QRectF(0, y, pw, h)
            path = QPainterPath()
            path.addRoundedRect(tr, h / 2, h / 2)
            c = QColor(color)
            c.setAlpha(240)
            p.fillPath(path, c)
            p.setPen(QColor(10, 12, 18) if QColor(color).lightness() > 150 else QColor("white"))
            p.drawText(tr, Qt.AlignCenter, fm.elidedText(text, Qt.ElideRight, int(pw - 12 * s)))
            y += h + gap
        p.end()
        return pm

    def _tile(self, p: QPainter, e, r: QRectF, focused: bool, idx: int) -> None:
        ready = self.ready(e)
        badge = None
        if e.id in self.running:
            badge = ("● PLAYING", theme.CURRENT.success)
        elif (e.release or {}).get("status") == "update":
            badge = ("UPDATE", theme.CURRENT.warning)
        elif not ready:
            badge = (self.state_text(e).upper(), "#64748b")
        self.art_tile(p, r, focused, self.capsule_url(e), e.cover_url, e.title, badge,
                      not ready and e.id not in self.running, ("tile", idx))

    # ------------------------------------------------------------- home
    def _paint_home(self, p: QPainter) -> None:
        s, W, H = self._s(), self.width(), self.height()
        items = self._items(T_HOME)
        top_h, bot_h = 96 * s, 76 * s
        if not items:
            p.setPen(QColor("white"))
            p.setFont(self._font(40 * s, 800))
            p.drawText(QRectF(0, H * 0.32, W, 60 * s), Qt.AlignCenter, "No games yet")
            p.setFont(self._font(22 * s))
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(0, H * 0.32 + 70 * s, W, 40 * s), Qt.AlignCenter,
                       "Find one in the Store tab (press A) — it shows up here, ready to play.")
            return
        e = self._focused_entry()
        tile_h = min(H * 0.30, 330 * s)
        tile_w = tile_h * 2 / 3
        gap = 28 * s
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
                           f"Update available: {rel['latest']} (you have {rel.get('version')}) — Manage → Update")
                y += 34 * s
            y += 10 * s
            bx = left
            for i, (text, _fn, kind) in enumerate(self._hero_buttons()):
                p.setFont(self._font(68 * s * 0.36, 800))
                bw = max((260 if i == 0 else 200) * s, QFontMetrics(p.font()).horizontalAdvance(text) + 70 * s)
                r = QRectF(bx, y, bw, 68 * s)
                self._pill(p, r, text, kind, self.zone == "buttons" and self.button_index == i and
                           self.overlay is None)
                self._hit.append((r, "button", i))
                bx += bw + 22 * s
        # shelf
        p.setFont(self._font(17 * s, 800))
        p.setPen(QColor(255, 255, 255, 190))
        p.drawText(QRectF(left, shelf_y - 56 * s, 600 * s, 28 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   f"YOUR GAMES  ·  {len(items)}")
        i = self.index[T_HOME]
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
        gap = 34 * s
        left = 72 * s
        cols = max(1, int((W - 2 * left + gap) // (tile_w + gap)))
        return tile_w, tile_h, gap, cols

    def _grid_cols(self) -> int:
        return self._grid_geom()[3]

    def _paint_library(self, p: QPainter) -> None:
        s, W, H = self._s(), self.width(), self.height()
        items = self._items(T_LIBRARY)
        tile_w, tile_h, gap, cols = self._grid_geom()
        top = 96 * s + 100 * s                    # room for the heading and a focused tile's glow
        bottom = H - 76 * s
        left = 72 * s
        gx = left + 6 * s
        hgap = gap
        if cols > 1 and len(items) >= cols:              # a full row: spread the columns edge to edge
            hgap = (W - 2 * left - 12 * s - cols * tile_w) / (cols - 1)
        p.setFont(self._font(34 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(left, 96 * s + 8 * s, W, 50 * s), Qt.AlignLeft | Qt.AlignVCenter, "Library")
        hw = QFontMetrics(p.font()).horizontalAdvance("Library")
        p.setFont(self._font(18 * s, 600))
        p.setPen(QColor(255, 255, 255, 160))
        ready = sum(1 for e in items if self.ready(e))
        p.drawText(QRectF(left + hw + 28 * s, 96 * s + 8 * s, W, 50 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{len(items)} games · {ready} ready to play")
        if not items:
            p.setFont(self._font(22 * s))
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(left, top + 20 * s, W - 2 * left, 40 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       "No games yet — find one in the Store tab.")
            return
        i = self.index[T_LIBRARY]
        row = i // cols
        row_h = tile_h + gap + 30 * s
        view = bottom - top - 20 * s
        target = self._scroll_target[1]
        row_top, row_bottom = row * row_h, row * row_h + tile_h + 50 * s
        if row_top < target:
            target = row_top
        elif row_bottom - target > view:
            target = row_bottom - view
        self._scroll_target[1] = max(0.0, target)
        if abs(self._scroll[1] - self._scroll_target[1]) > 0.5 and not self._tick.isActive():
            self._tick.start()
        off = self._scroll[1]
        p.save()
        p.setClipRect(QRectF(0, top - 40 * s, W, bottom - top + 40 * s))
        focused_draw = None
        for k, e in enumerate(items):
            rr, cc = divmod(k, cols)
            x = gx + cc * (tile_w + hgap)
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
            p.drawText(QRectF(r.left() - 30 * s, r.bottom() + 16 * s, r.width() + 60 * s, 26 * s), Qt.AlignCenter,
                       QFontMetrics(p.font()).elidedText(e.title, Qt.ElideRight, int(r.width() + 60 * s)))
        p.restore()

    # ------------------------------------------------------------- downloads
    def _paint_downloads(self, p: QPainter) -> None:
        from ezgd.core.fsutil import format_bytes, format_speed
        s, W, H = self._s(), self.width(), self.height()
        left = 72 * s
        y = 96 * s + 8 * s
        p.setFont(self._font(34 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(left, y, W, 50 * s), Qt.AlignLeft | Qt.AlignVCenter, "Downloads")
        y += 70 * s
        rows = self._download_rows()
        if not rows:
            p.setFont(self._font(22 * s))
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(left, y + 40 * s, W - 2 * left, 40 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       "Nothing is downloading. Find a game in the Store tab — its progress shows here.")
            return
        row_h = 112 * s
        bottom = H - 76 * s - 10 * s
        sel = min(self.index[T_DOWNLOADS], len(rows) - 1)
        view = bottom - y
        target = getattr(self, "_dl_t", 0.0)
        if sel * row_h < target:
            target = sel * row_h
        elif sel * row_h + row_h - target > view:
            target = sel * row_h + row_h - view
        self._dl_t = max(0.0, target)
        cur = getattr(self, "_dl", 0.0)
        cur = self._dl_t if self.reduce_motion or abs(cur - self._dl_t) < 0.6 else cur + (self._dl_t - cur) * 0.25
        if cur != self._dl_t:
            self._schedule_frame()
        self._dl = cur
        p.save()
        p.setClipRect(QRectF(0, y - 10 * s, W, bottom - y + 10 * s))
        for k, (kind, d) in enumerate(rows):
            ry = y + k * row_h - cur
            if ry + row_h < y - 20 * s or ry > bottom:
                continue
            r = QRectF(left, ry, W - 2 * left, row_h - 14 * s)
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
                if d.get("status") == "failed" and d.get("error"):
                    extra = str(d["error"])
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
                       Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(extra, Qt.ElideRight, int(r.width() - 48 * s)))
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
        p.restore()

    # ------------------------------------------------------------- overlay
    def _paint_launch(self, p: QPainter, o: Overlay) -> None:
        """Play was pressed: the whole screen is the game's — its art, its name, a quiet spinner. Steam's windows
        stay behind this until the game itself is up."""
        s, W, H = self._s(), self.width(), self.height()
        e = getattr(self, "_launch_entry", None)
        layer = self.images.layer(self.hero_url(e), W, H, "home") if e is not None else None
        if layer is not None:
            p.drawImage(QPointF(0, 0), layer)
        else:
            p.fillRect(self.rect(), QColor(theme.CURRENT.bg0))
        shade = QLinearGradient(0, 0, 0, H)
        shade.setColorAt(0.0, QColor(0, 0, 0, 90))
        shade.setColorAt(0.55, QColor(0, 0, 0, 150))
        shade.setColorAt(1.0, QColor(0, 0, 0, 235))
        p.fillRect(self.rect(), QBrush(shade))
        title = e.title if e is not None else o.title
        p.setPen(QColor("white"))
        p.setFont(self._font(min(84 * s, W * 0.06), 900))
        tr = QRectF(W * 0.1, H * 0.56, W * 0.8, 120 * s)
        p.drawText(tr, Qt.AlignHCenter | Qt.AlignBottom | Qt.TextWordWrap, title)
        p.setFont(self._font(24 * s, 600))
        p.setPen(QColor(255, 255, 255, 200))
        p.drawText(QRectF(0, H * 0.56 + 132 * s, W, 40 * s), Qt.AlignCenter,
                   "Starting…" if o.text != "Have fun!" else "Have fun!")
        t = time.monotonic()
        for k in range(3):
            a = 0.3 + 0.7 * max(0.0, math.sin(t * 4.5 - k * 0.8))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(255 * a)))
            p.drawEllipse(QPointF(W / 2 - 40 * s + k * 40 * s, H * 0.56 + 200 * s), 10 * s, 10 * s)
        p.setBrush(Qt.NoBrush)
        self._schedule_frame(16)

    def _paint_overlay(self, p: QPainter) -> None:
        o = self.overlay
        if o.tag == "starting":
            self._paint_launch(p, o)
            return
        s, W, H = self._s(), self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 170))
        cw = min(W - 80 * s, (1080 if o.menu else 980) * s)
        f_title = self._font(36 * s, 900)
        f_text = self._font(21 * s, 500)
        f_line = self._font(19 * s, 500)
        inner = cw - 96 * s
        h = 56 * s
        p.setFont(f_title)
        th = p.boundingRect(QRectF(0, 0, inner, 1000), Qt.TextWordWrap, o.title).height()
        h += th + 16 * s
        xh = 0.0
        if o.text:
            p.setFont(f_text)
            xh = p.boundingRect(QRectF(0, 0, inner, 1000), Qt.TextWordWrap, o.text).height()
            h += xh + 18 * s
        lh = []
        p.setFont(f_line)
        for k, v in o.lines:
            vh = p.boundingRect(QRectF(0, 0, inner - 220 * s, 1000), Qt.TextWordWrap, v).height()
            lh.append(max(30 * s, min(vh, 90 * s) + 8 * s))
        h += sum(lh) + (12 * s if lh else 0)
        row_h = [((76 if (o.notes[k] if k < len(o.notes) else "") else 58) * s) for k in range(len(o.buttons))] \
            if o.menu else []
        if o.menu:
            list_h = sum(row_h) + 10 * s * max(0, len(row_h) - 1)
            room = H - 80 * s - h - 40 * s
            h += min(list_h, max(room, 2 * 76 * s)) + 40 * s
        else:
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
            self._schedule_frame(60)
            return
        if o.menu:
            self._paint_menu(p, o, QRectF(x, y + 6 * s, inner, r.bottom() - 30 * s - y - 6 * s), row_h)
            self._hit.append((self._x_rect(), "x", X_INDEX))
            return
        bx = x
        by = r.bottom() - 40 * s - 64 * s
        for k, (text, _fn, kind) in enumerate(o.buttons):
            p.setFont(self._font(64 * s * 0.36, 800))
            bw = max(200 * s, QFontMetrics(p.font()).horizontalAdvance(text) + 80 * s)
            bw = min(bw, r.right() - 48 * s - bx)
            br = QRectF(bx, by, bw, 64 * s)
            self._pill(p, br, text, kind, k == o.focus)
            self._hit.append((br, "overlay", k))
            bx += bw + 20 * s
        self._hit.append((self._x_rect(), "x", X_INDEX))

    def _paint_menu(self, p: QPainter, o: Overlay, rect: QRectF, row_h: list[float]) -> None:
        s = self._s()
        tops, y = [], 0.0
        for hh in row_h:
            tops.append(y)
            y += hh + 10 * s
        total = y - 10 * s
        if 0 <= o.focus < len(tops):
            ft, fb = tops[o.focus], tops[o.focus] + row_h[o.focus]
            if ft < o.scroll:
                o.scroll = ft
            elif fb - o.scroll > rect.height():
                o.scroll = fb - rect.height()
        o.scroll = max(0.0, min(o.scroll, max(0.0, total - rect.height())))
        p.save()
        p.setClipRect(rect.adjusted(-12 * s, -6 * s, 12 * s, 6 * s))
        for k, (text, _fn, kind) in enumerate(o.buttons):
            r = QRectF(rect.left(), rect.top() + tops[k] - o.scroll, rect.width(), row_h[k])
            if r.bottom() < rect.top() - 10 * s or r.top() > rect.bottom() + 10 * s:
                continue
            focused = k == o.focus
            path = QPainterPath()
            path.addRoundedRect(r, 14 * s, 14 * s)
            base = {"play": theme.CURRENT.success, "primary": theme.CURRENT.accent, "danger": theme.CURRENT.danger,
                    "stop": theme.CURRENT.danger}.get(kind)
            if focused:
                p.fillPath(path, QColor(base) if base else QColor(255, 255, 255, 235))
                fg, sub = (QColor("white"), QColor(255, 255, 255, 200)) if base else \
                    (QColor(11, 13, 19), QColor(11, 13, 19, 170))
            else:
                c = QColor(base) if base else QColor(255, 255, 255)
                c.setAlpha(60 if base else 20)
                p.fillPath(path, c)
                fg, sub = QColor("white"), QColor(255, 255, 255, 160)
            note = o.notes[k] if k < len(o.notes) else ""
            p.setFont(self._font(21 * s, 700))
            p.setPen(fg)
            fm = QFontMetrics(p.font())
            if note:
                p.drawText(QRectF(r.left() + 24 * s, r.top() + 8 * s, r.width() - 48 * s, 32 * s),
                           Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(text, Qt.ElideRight, int(r.width() - 48 * s)))
                p.setFont(self._font(16 * s, 500))
                p.setPen(sub)
                p.drawText(QRectF(r.left() + 24 * s, r.top() + 40 * s, r.width() - 48 * s, 26 * s),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           QFontMetrics(p.font()).elidedText(note, Qt.ElideRight, int(r.width() - 48 * s)))
            else:
                p.drawText(r.adjusted(24 * s, 0, -24 * s, 0), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(text, Qt.ElideRight, int(r.width() - 48 * s)))
            self._hit.append((r, "overlay", k))
        p.restore()
        if total > rect.height() + 1:
            frac = rect.height() / total
            bh = max(36 * s, rect.height() * frac)
            by = rect.top() + (rect.height() - bh) * (o.scroll / max(1.0, total - rect.height()))
            bar = QPainterPath()
            bar.addRoundedRect(QRectF(rect.right() + 14 * s, by, 6 * s, bh), 3 * s, 3 * s)
            p.fillPath(bar, QColor(255, 255, 255, 80))
