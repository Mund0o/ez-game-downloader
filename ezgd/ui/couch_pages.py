"""Couch mode's other pages: Store (browse, search, game pages, downloads), Wishlist, Settings and the on-screen
keyboard — everything the desktop window can do, made for a controller and painted by :class:`CouchWindow`.

A page draws itself into the couch window and gets the controller's commands while it has focus. ``command``
returns ``"top"`` to hand focus to the top bar (up from the first row) or ``"back"`` when B is not used by the page.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QTextLayout

from ezgd.core.fsutil import format_bytes, free_space, safe_dirname
from ezgd.ui import theme

log = logging.getLogger(__name__)



# ====================================================================== shared
@dataclass
class Item:
    """One focusable row of a list page."""
    label: str
    action: Callable[[], None] | None = None
    note: str = ""
    kind: str = "row"              # row | primary | danger | toggle | choice | info | header
    value: str = ""                # right-hand text (toggle state, chosen value)
    left: Callable[[], None] | None = None
    right: Callable[[], None] | None = None

    @property
    def focusable(self) -> bool:
        return self.kind not in ("info", "header")


class Page:
    """Base class: a full-screen page inside couch mode."""

    def __init__(self, w):
        self.w = w

    @property
    def s(self) -> float:
        return self.w._s()

    def activate(self) -> None:
        pass

    def paint(self, p: QPainter) -> None:
        pass

    def command(self, name: str) -> str | None:
        return "back" if name == "b" else None

    def hit(self, idx: int, click: bool) -> None:
        pass

    def background(self) -> str | None:
        return None

    def hints(self) -> list[tuple[str, str]]:
        return [("a", "Select"), ("b", "Back")]

    def type_text(self, text: str) -> bool:
        """A key typed on a real keyboard (not an arrow/Enter/Esc). True when the page used it."""
        return False

    # ---------------------------------------------------------- helpers
    def _smooth(self, attr: str, target: float) -> float:
        cur = getattr(self, attr, 0.0)
        if self.w.reduce_motion or abs(cur - target) < 0.6:
            cur = target
        else:
            cur += (target - cur) * 0.25
            self.w._schedule_frame()
        setattr(self, attr, cur)
        return cur

    def _heading(self, p: QPainter, text: str, sub: str = "", y: float | None = None) -> float:
        s, W = self.s, self.w.width()
        y = 96 * s + 8 * s if y is None else y
        left = 72 * s
        p.setFont(self.w._font(34 * s, 900))
        p.setPen(QColor("white"))
        p.drawText(QRectF(left, y, W, 50 * s), Qt.AlignLeft | Qt.AlignVCenter, text)
        if sub:
            tw = QFontMetrics(p.font()).horizontalAdvance(text)
            p.setFont(self.w._font(18 * s, 600))
            p.setPen(QColor(255, 255, 255, 160))
            p.drawText(QRectF(left + tw + 28 * s, y, W - left - tw - 100 * s, 50 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(sub, Qt.ElideRight, int(W - left - tw - 100 * s)))
        return y + 60 * s

    def _list(self, p: QPainter, items: list[Item], focus: int, rect: QRectF, scroll_attr: str,
              focused: bool = True, row_h: float | None = None) -> None:
        """A vertical list of rows (with notes, toggles and values) that scrolls to keep the focus in view."""
        s = self.s
        heights = []
        for it in items:
            if it.kind == "header":
                heights.append(58 * s)
            else:
                heights.append(row_h or ((86 if it.note else 62) * s))
        tops, y = [], 0.0
        for h in heights:
            tops.append(y)
            y += h + 10 * s
        total = y
        target = getattr(self, scroll_attr + "_t", 0.0)
        if 0 <= focus < len(items):
            ft, fb = tops[focus], tops[focus] + heights[focus]
            if ft - target < 0:
                target = ft - (70 * s if focus > 0 and items[focus - 1].kind == "header" else 0)
            elif fb - target > rect.height():
                target = fb - rect.height()
        target = max(0.0, min(target, max(0.0, total - rect.height())))
        setattr(self, scroll_attr + "_t", target)
        off = self._smooth(scroll_attr, target)
        p.save()
        p.setClipRect(rect.adjusted(-30 * s, -14 * s, 30 * s, 14 * s))
        for k, it in enumerate(items):
            y = rect.top() + tops[k] - off
            h = heights[k]
            if y + h < rect.top() - 20 * s or y > rect.bottom() + 20 * s:
                continue
            r = QRectF(rect.left(), y, rect.width(), h)
            if it.kind == "header":
                p.setFont(self.w._font(17 * s, 800))
                p.setPen(QColor(theme.CURRENT.accent).lighter(135))
                p.drawText(r.adjusted(4 * s, 0, 0, -8 * s), Qt.AlignLeft | Qt.AlignBottom, it.label.upper())
                continue
            self._cached_row(p, it, r, focused and k == focus)
            if it.focusable:
                self.w._hit.append((r, "page", k))
        p.restore()
        if total > rect.height() + 1:                       # scroll bar
            frac = rect.height() / total
            bh = max(40 * s, rect.height() * frac)
            by = rect.top() + (rect.height() - bh) * (off / max(1.0, total - rect.height()))
            bar = QPainterPath()
            bar.addRoundedRect(QRectF(rect.right() + 18 * s, by, 6 * s, bh), 3 * s, 3 * s)
            p.fillPath(bar, QColor(255, 255, 255, 70))

    def _cached_row(self, p: QPainter, it: Item, r: QRectF, focused: bool) -> None:
        """A row drawn once into a layer (with room for the focus glow) and copied while the list moves."""
        m = 10 * self.s
        c = theme.CURRENT
        key = ("row", it.label, it.note, it.kind, it.value, focused, round(r.width(), 1), round(r.height(), 1),
               round(self.s, 3), c.success, c.danger)
        self.w.blit(p, r.left() - m, r.top() - m, key, r.width() + 2 * m, r.height() + 2 * m,
                    lambda q: self._row(q, it, QRectF(m, m, r.width(), r.height()), focused))

    def _row(self, p: QPainter, it: Item, r: QRectF, focused: bool) -> None:
        s = self.s
        path = QPainterPath()
        path.addRoundedRect(r, 16 * s, 16 * s)
        if it.kind == "info":
            p.fillPath(path, QColor(255, 255, 255, 10))
        elif focused:
            for k in range(3, 0, -1):
                g = QColor(255, 255, 255, 30)
                gp = QPainterPath()
                rr = r.adjusted(-k * 3 * s, -k * 3 * s, k * 3 * s, k * 3 * s)
                gp.addRoundedRect(rr, 16 * s + k * 3 * s, 16 * s + k * 3 * s)
                p.fillPath(gp, g)
            fill = {"primary": QColor(theme.CURRENT.success), "danger": QColor(theme.CURRENT.danger)}.get(
                it.kind, QColor(255, 255, 255, 235))
            p.fillPath(path, fill)
        else:
            fill = {"primary": QColor(22, 163, 74, 70), "danger": QColor(220, 38, 38, 55)}.get(
                it.kind, QColor(255, 255, 255, 22))
            p.fillPath(path, fill)
        light = focused and it.kind not in ("primary", "danger", "info")
        fg = QColor(11, 13, 19) if light else QColor("white")
        sub = QColor(11, 13, 19, 170) if light else QColor(255, 255, 255, 165)
        pad = 26 * s
        right_w = 0.0
        if it.kind == "toggle":
            on = it.value == "on"
            tw, th = 70 * s, 36 * s
            tr = QRectF(r.right() - pad - tw, r.center().y() - th / 2, tw, th)
            tp = QPainterPath()
            tp.addRoundedRect(tr, th / 2, th / 2)
            p.fillPath(tp, QColor(theme.CURRENT.success) if on else QColor(120, 125, 140, 160))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("white"))
            d = th - 8 * s
            p.drawEllipse(QRectF(tr.right() - d - 4 * s if on else tr.left() + 4 * s, tr.top() + 4 * s, d, d))
            p.setBrush(Qt.NoBrush)
            right_w = tw + 20 * s
        elif it.value:
            p.setFont(self.w._font(19 * s, 700))
            text = (f"◂  {it.value}  ▸" if it.kind == "choice" else it.value)
            vw = min(r.width() * 0.45, QFontMetrics(p.font()).horizontalAdvance(text) + 8 * s)
            p.setPen(fg)
            p.drawText(QRectF(r.right() - pad - vw, r.top(), vw, r.height()), Qt.AlignRight | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(text, Qt.ElideLeft, int(vw)))
            right_w = vw + 20 * s
        inner = r.adjusted(pad, 0, -pad - right_w, 0)
        if it.note:
            p.setFont(self.w._font(21 * s, 700))
            p.setPen(fg)
            p.drawText(QRectF(inner.left(), r.top() + 12 * s, inner.width(), 32 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(it.label, Qt.ElideRight, int(inner.width())))
            p.setFont(self.w._font(16 * s, 500))
            p.setPen(sub)
            p.drawText(QRectF(inner.left(), r.top() + 44 * s, inner.width(), 28 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(it.note, Qt.ElideRight, int(inner.width())))
        else:
            p.setFont(self.w._font(21 * s, 700))
            p.setPen(fg)
            p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(it.label, Qt.ElideRight, int(inner.width())))

    @staticmethod
    def _step(items: list[Item], i: int, d: int) -> int:
        """The next focusable row from ``i`` in direction ``d`` (or ``i`` when there is none)."""
        j = i + d
        while 0 <= j < len(items):
            if items[j].focusable:
                return j
            j += d
        return i

    @staticmethod
    def _first(items: list[Item]) -> int:
        return next((k for k, it in enumerate(items) if it.focusable), 0)

    def _list_command(self, items: list[Item], attr: str, name: str) -> str | None:
        i = getattr(self, attr)
        if name in ("lt", "rt"):                         # triggers: a page at a time
            j = i
            for _ in range(5):
                j = self._step(items, j, 1 if name == "rt" else -1)
            setattr(self, attr, j)
        elif name == "up":
            j = self._step(items, i, -1)
            if j == i:
                return "top"
            setattr(self, attr, j)
        elif name == "down":
            setattr(self, attr, self._step(items, i, 1))
        elif name in ("left", "right") and 0 <= i < len(items):
            fn = items[i].left if name == "left" else items[i].right
            if fn:
                fn()
        elif name == "a" and 0 <= i < len(items):
            if items[i].action:
                items[i].action()
        elif name == "b":
            return "back"
        return None


# =================================================================== keyboard
KEY_ROWS = ["1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm-.:"]
SPECIAL = [("SPACE", 4), ("⌫", 2), ("CLEAR", 2), ("DONE", 2)]


class Keyboard:
    """On-screen keyboard: D-pad to move, A types, B deletes (closes when empty), Start is Done. A real keyboard
    types into it too."""

    def __init__(self, w, title: str, on_done: Callable[[str], None], text: str = "", done_label: str = "Search",
                 on_cancel: Callable[[], None] | None = None):
        self.w = w
        self.title = title
        self.text = text
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.done_label = done_label
        self.row, self.col = 1, 0

    def _row_len(self, r: int) -> int:
        return len(SPECIAL) if r == len(KEY_ROWS) else len(KEY_ROWS[r])

    def command(self, name: str) -> None:
        n = len(KEY_ROWS) + 1
        if name == "up":
            self.row = max(0, self.row - 1)
            self.col = self._map_col(self.row + 1, self.row)
        elif name == "down":
            self.row = min(n - 1, self.row + 1)
            self.col = self._map_col(self.row - 1, self.row)
        elif name == "left":
            self.col = (self.col - 1) % self._row_len(self.row)
        elif name == "right":
            self.col = (self.col + 1) % self._row_len(self.row)
        elif name == "a":
            self.press(self.row, self.col)
        elif name in ("b", "x"):
            if self.text:
                self.text = self.text[:-1]
            elif name == "b":
                self.cancel()
        elif name == "start":
            self.done()
        # lt / rt / select / home: nothing while typing

    def _map_col(self, frm: int, to: int) -> int:
        """Keep the column roughly where it was when moving between the letter rows and the wide bottom row."""
        if frm == to:
            return self.col
        if to == len(KEY_ROWS):                          # into the bottom row
            unit = 0
            for k, (_lbl, wdt) in enumerate(SPECIAL):
                unit += wdt
                if self.col < unit:
                    return k
            return len(SPECIAL) - 1
        if frm == len(KEY_ROWS):
            return min(len(KEY_ROWS[to]) - 1, sum(wdt for _l, wdt in SPECIAL[:self.col]))
        return min(self.col, len(KEY_ROWS[to]) - 1)

    def press(self, r: int, c: int) -> None:
        if r < len(KEY_ROWS):
            self.type(KEY_ROWS[r][c])
            return
        key = SPECIAL[c][0]
        if key == "SPACE":
            self.type(" ")
        elif key == "⌫":
            self.text = self.text[:-1]
        elif key == "CLEAR":
            self.text = ""
        else:
            self.done()

    def type(self, ch: str) -> None:
        if len(self.text) < 80:
            self.text += ch

    def done(self) -> None:
        self.w.keyboard = None
        self.on_done(self.text.strip())

    def cancel(self) -> None:
        self.w.keyboard = None
        if self.on_cancel:
            self.on_cancel()

    def paint(self, p: QPainter) -> None:
        w = self.w
        s, W, H = w._s(), w.width(), w.height()
        p.fillRect(w.rect(), QColor(0, 0, 0, 165))
        unit = min(96 * s, (W - 160 * s) / 10.6)
        gap = unit * 0.12
        kw = unit - gap
        pw = 10 * unit + 80 * s
        ph = 5 * unit + 250 * s
        panel = QRectF((W - pw) / 2, max(110 * s, H - 76 * s - ph - 30 * s), pw, ph)
        path = QPainterPath()
        path.addRoundedRect(panel, 26 * s, 26 * s)
        p.fillPath(path, QColor(theme.CURRENT.bg2))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.5 * s))
        p.drawPath(path)
        x0 = panel.left() + 40 * s
        y = panel.top() + 30 * s
        p.setFont(w._font(24 * s, 800))
        p.setPen(QColor("white"))
        p.drawText(QRectF(x0, y, pw - 80 * s, 36 * s), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        y += 48 * s
        field = QRectF(x0, y, pw - 80 * s, 64 * s)
        fp = QPainterPath()
        fp.addRoundedRect(field, 14 * s, 14 * s)
        p.fillPath(fp, QColor(0, 0, 0, 120))
        p.setPen(QPen(QColor(theme.CURRENT.accent), 2 * s))
        p.drawPath(fp)
        p.setFont(w._font(28 * s, 600))
        fm = QFontMetrics(p.font())
        shown = fm.elidedText(self.text, Qt.ElideLeft, int(field.width() - 60 * s))
        p.setPen(QColor("white"))
        p.drawText(field.adjusted(22 * s, 0, -22 * s, 0), Qt.AlignLeft | Qt.AlignVCenter, shown)
        if int(time.monotonic() * 2) % 2 == 0:                    # caret
            cx = field.left() + 22 * s + fm.horizontalAdvance(shown) + 3 * s
            p.fillRect(QRectF(cx, field.top() + 16 * s, 3 * s, field.height() - 32 * s), QColor(theme.CURRENT.accent))
        w._schedule_frame(250)
        y += 64 * s + 26 * s
        p.setFont(w._font(kw * 0.36, 700))
        for r, keys in enumerate(KEY_ROWS):
            for c, ch in enumerate(keys):
                kr = QRectF(x0 + c * unit, y + r * unit, kw, kw)
                self._key(p, kr, ch.upper(), r == self.row and c == self.col, r * 100 + c)
        r = len(KEY_ROWS)
        x = x0
        for c, (lbl, wdt) in enumerate(SPECIAL):
            kr = QRectF(x, y + r * unit, wdt * unit - gap, kw)
            text = self.done_label.upper() if lbl == "DONE" else lbl
            self._key(p, kr, text, r == self.row and c == self.col, r * 100 + c, primary=lbl == "DONE")
            x += wdt * unit

    def _key(self, p: QPainter, r: QRectF, text: str, focused: bool, idx: int, primary: bool = False) -> None:
        s = self.w._s()
        path = QPainterPath()
        path.addRoundedRect(r, 12 * s, 12 * s)
        if focused:
            p.fillPath(path, QColor(255, 255, 255, 240) if not primary else QColor(theme.CURRENT.success))
            p.setPen(QColor(11, 13, 19) if not primary else QColor("white"))
        else:
            p.fillPath(path, QColor(22, 163, 74, 150) if primary else QColor(255, 255, 255, 26))
            p.setPen(QColor("white"))
        f = self.w._font(min(r.height() * 0.36, 30 * s), 800)
        p.setFont(f)
        p.drawText(r, Qt.AlignCenter, text)
        self.w._hit.append((r, "key", idx))

    def hit(self, idx: int, click: bool) -> None:
        self.row, self.col = divmod(idx, 100)
        if click:
            self.press(self.row, self.col)


# ====================================================================== store
SORT_NAMES = {"relevance": "Best match", "recent": "Newest", "title": "A – Z", "sources": "Most sites"}


def source_tags(group) -> list[tuple[str, str]]:
    """(site name, site colour) for every site that offers this game — shown on its tile."""
    seen: list[str] = []
    for g in group.entries:
        if g.source not in seen:
            seen.append(g.source)
    return [(theme.source_name(src), theme.SOURCE_COLORS.get(src, theme.CURRENT.accent)) for src in seen]


class SeeAll:
    """The last tile of a Discover shelf: opens that shelf as a full, scrolling grid."""

    def __init__(self, filter_key: str, title: str, count: int):
        self.filter_key, self.title, self.count = filter_key, title, count
        self.key = f"seeall:{filter_key}"
        self.entries: list = []


class Feed:
    """Games from the sites, page after page as you scroll down (like the desktop's All games page): the next page
    of every site that has more is fetched when the focus nears the end."""

    def __init__(self, w, sids: list[str], query: str = "", genre: str | None = None, on_change=None, sort=None):
        from ezgd.catalog.search import SortKey
        self.w = w
        self.sids = list(sids)
        self.query = query
        self.genre = genre
        self.sort = sort or (SortKey.RELEVANCE if query else SortKey.RECENT)
        self.games: dict[str, object] = {}
        self.groups: list = []
        self.pages: dict[str, int] = {}
        self.has_more: dict[str, bool] = {}
        self.inflight: set[str] = set()
        self.errors: dict[str, str] = {}
        self.on_change = on_change
        self.closed = False

    @property
    def busy(self) -> bool:
        return bool(self.inflight)

    @property
    def more(self) -> bool:
        return any(self.has_more.get(s, True) and s not in self.errors for s in self.sids)

    def load_more(self) -> None:
        b = self.w.bridge
        for sid in self.sids:
            if sid in self.inflight or sid in self.errors or not self.has_more.get(sid, True):
                continue
            n = self.pages.get(sid, 0) + 1
            self.pages[sid] = n
            self.inflight.add(sid)
            coro = b.core.search(self.query, n, [sid]) if self.query else b.core.latest(n, [sid], self.genre)
            b.call(coro, lambda res, s=sid: self._loaded(s, res), lambda e, s=sid: self._failed(s, e), owner=self.w)

    def seed(self, games) -> None:
        """Games the app already knows (its catalog): shown at once, while the sites are asked for more."""
        for g in games:
            if not self.sids or g.source in self.sids:
                self.games.setdefault(g.id, g)
        self._regroup()

    def set_sort(self, sort) -> None:
        self.sort = sort
        self._regroup()

    def _loaded(self, sid: str, res) -> None:
        if self.closed:
            return
        self.inflight.discard(sid)
        for g in res.games:
            self.games.setdefault(g.id, g)
        self.has_more[sid] = bool(res.has_more.get(sid, False)) and bool(res.games)
        if sid in (res.errors or {}):
            self.errors[sid] = res.errors[sid]
        self._regroup()

    def _failed(self, sid: str, err) -> None:
        if self.closed:
            return
        self.inflight.discard(sid)
        self.errors[sid] = str(err) or type(err).__name__
        self._regroup()

    def _regroup(self) -> None:
        from ezgd.catalog.search import Filters, filter_and_group
        self.groups = filter_and_group(self.games.values(), Filters(query=self.query), self.w.actions.installed_keys,
                                       self.sort)
        if self.on_change:
            self.on_change(self)


class StorePage(Page):
    """The Store: search every site, filters (Discover, New on the sites, OnlineFix's picks, one per site, genre)
    and grids that keep loading as you scroll. A on a game opens its page.

    Rows: 0 = search bar, 1 = filter chips, 2 = the content (Discover's shelves or a grid)."""

    def __init__(self, w):
        super().__init__(w)
        self.loaded = False
        self.loading = False
        self._all_shelves: list[tuple[str, list, str | None]] = []   # (title, [GameGroup | SeeAll], filter key)
        self._vis: dict = {}                         # see _candidates
        self._views: dict[str, dict] = {}            # see _stable
        self._shelf_games: dict = {}
        self._cands_n = 0
        self.sections: dict[str, tuple[str, list]] = {}         # featured section key → (title, games)
        self.query = ""
        self.genre: str | None = None
        self.filter = "discover"                     # the active filter (chip) key
        self.chip = 0                                # the focused chip
        self.feed: Feed | None = None
        self.row = 2
        self.shelf = 0                               # Discover: the focused shelf
        self.cols: dict[int, int] = {}               # Discover: focused tile per shelf
        self.gi = 0                                  # grids: the focused tile
        self._all_sort = None                        # "All games": the order chosen last
        self.hscroll: dict[int, float] = {}
        self._gen = 0
        from ezgd.ui import ratings
        self.ratings = ratings.store(w.bridge)          # (the window repaints when answers come: see _Couch)

    # ------------------------------------------------------------ filters
    def chips(self) -> list[tuple[str, str]]:
        """(key, label) of every filter chip, in order."""
        out = []
        if self.query:
            out.append(("search", f"“{self.query}”"))
        out += [("discover", "Discover"), ("all", "All games"), ("new", "New on the sites")]
        out += [(f"sec:{k}", title) for k, (title, _g) in self.sections.items()]
        out += [(f"site:{sid}", theme.source_name(sid)) for sid in self._sources()]
        out.append(("genre", f"Genre: {self.genre}" if self.genre else "Genre ▾"))
        if self.feed is not None:
            out.append(("sort", f"Sort: {SORT_NAMES.get(self.feed.sort, 'Newest')} ▾"))
        return out

    def _sources(self) -> list[str]:
        enabled = self.w.actions.settings.get("sources_enabled") or {}
        return [s for s in theme.SOURCE_NAMES if enabled.get(s, True)]

    def set_filter(self, key: str) -> None:
        if key == "genre":
            self._choose_genre()
            return
        if key == "sort":
            self._choose_sort()
            return
        if key != "search" and self.query:
            self.query = ""
        if self.feed is not None:
            self.feed.closed = True
            self.feed = None
        self._views = {k: v for k, v in self._views.items() if not k.startswith("grid|")}   # a fresh grid
        self.filter = key
        self.gi = 0
        self._gs_t = self._gs = 0.0
        if key.startswith("site:"):
            self.feed = Feed(self.w, [key[5:]], on_change=self._feed_changed)
        elif key == "new":
            self.feed = Feed(self.w, self._sources(), on_change=self._feed_changed)
        elif key == "all":
            # every game from every site: what the app already knows at once, then page after page from all sites
            self.feed = Feed(self.w, self._sources(), on_change=self._feed_changed, sort=self._all_sort)
            feed = self.feed
            b = self.w.bridge
            b.call(b.core.cached_recent(5000), lambda games, f=feed: f.seed(games) if not f.closed else None,
                   lambda _e: None, owner=self.w)
        elif key == "genre:set":
            self.feed = Feed(self.w, self._sources(), genre=self.genre, on_change=self._feed_changed)
            self.filter = "genre"
        elif key == "search":
            self.feed = Feed(self.w, self._sources(), query=self.query, on_change=self._feed_changed)
        if self.feed is not None:
            self.feed.load_more()
        keys = [k for k, _l in self.chips()]
        self.chip = keys.index(self.filter) if self.filter in keys else 0
        self.w.refresh_bg()
        self.w.update()

    def _choose_sort(self) -> None:
        from ezgd.catalog.search import SortKey
        options = [(SortKey.RECENT, "Newest first"), (SortKey.TITLE, "A – Z"),
                   (SortKey.SOURCES, "On the most sites")]
        if self.query:
            options.insert(0, (SortKey.RELEVANCE, "Best match"))

        def pick(sort) -> None:
            self.w.close_overlay()
            if self.feed is not None:
                if self.filter == "all":
                    self._all_sort = sort
                self.gi = 0
                self._focus_key = None
                self.feed.set_sort(sort)
            self.row = 2
        rows = [(("✓  " if self.feed is not None and self.feed.sort == k else "") + t, lambda k=k: pick(k), "ghost",
                 "") for k, t in options]
        rows.append(("Close", self.w.close_overlay, "ghost", ""))
        self.w.menu("Sort", rows)

    def _choose_genre(self) -> None:
        b = self.w.bridge

        def got(genres: list[str]) -> None:
            if not genres:
                self.w.message("No genres yet", "The sites have not listed genres yet — try again in a moment.")
                return
            rows = [(g, lambda g=g: self._pick_genre(g), "ghost", "") for g in genres[:80]]
            rows.append(("Close", self.w.close_overlay, "ghost", ""))
            self.w.menu("Genre", rows, text="Show only games of one genre.")
        b.call(b.core.genres(), got, lambda e: self.w.message("Could not load the genres", str(e)), owner=self.w)

    def _pick_genre(self, genre: str) -> None:
        self.w.close_overlay()
        self.genre = genre
        self.set_filter("genre:set")
        self.row = 2

    def _feed_changed(self, feed: Feed) -> None:
        if feed is not self.feed:
            return
        self.items_changed()

    def keep_focus(self) -> None:
        """New pages (or controller checks) changed the grid: the focused game stays focused."""
        old = getattr(self, "_focus_key", None)
        items = self._grid_items()
        if old is not None and self._grid():
            pos = next((i for i, g in enumerate(items) if g.key == old), None)
            if pos is not None:
                self.gi = pos
        self.gi = max(0, min(self.gi, len(items) - 1))
        self._art(items[max(0, self.gi - 6):self.gi + 30])
        self.w.refresh_bg()
        self.w.update()

    # --------------------------------------------------------------- data
    def activate(self) -> None:
        if not self.loaded and not self.loading:
            self.reload()

    def reload(self) -> None:
        from ezgd.catalog.search import group_duplicates
        self.loading = True
        b = self.w.bridge
        state = {"featured": None, "recent": [], "live": []}

        def build() -> None:
            shelves = []
            sections = {}
            for sec in (state["featured"] or []):
                if sec.games:
                    sections[sec.key] = (sec.title, list(sec.games))
            new = list(state["live"]) + [g for g in state["recent"] if g.id not in {x.id for x in state["live"]}]
            new.sort(key=lambda g: -(g.published_at or g.updated_at or g.cached_at or 0))
            if new:
                groups = group_duplicates(new[:40])
                shelves.append(("New on the sites", groups + [SeeAll("new", "New on the sites", len(groups))], "new"))
            for key, (title, games) in sections.items():
                groups = group_duplicates(games)
                shelves.append((title, groups + [SeeAll(f"sec:{key}", title, len(groups))], f"sec:{key}"))
            self.sections = sections
            self._all_shelves = shelves
            self.loaded = True
            self._art(g for _t, gs, _k in shelves for g in gs[:14])
            self.shelf = min(self.shelf, max(0, len(shelves) - 1))
            self.w.refresh_bg()
            self.w.update()

        def featured(res) -> None:
            sections, _at, _err = res
            state["featured"] = sections
            build()

        def cached(games) -> None:
            state["recent"] = list(games)
            build()

        def live(res) -> None:
            state["live"] = list(res.games)
            self.loading = False
            build()

        def failed(_e) -> None:
            self.loading = False
            build()
        b.call(b.core.featured(), featured, lambda _e: build(), owner=self.w)
        b.call(b.core.cached_recent(40), cached, lambda _e: None, owner=self.w)
        b.call(b.core.latest(1), live, failed, owner=self.w)

    def _art(self, groups) -> None:
        groups = [g for g in groups if not isinstance(g, SeeAll)]
        self.w.load_art([self.w.group_app_id(g) for g in groups])
        if not self.controller_only():               # (otherwise the controller check finds their App IDs)
            self.w.find_apps_by_title(groups)

    def search(self, text: str) -> None:
        text = text.strip()
        if not text:
            if self.query:
                self.clear_search()
            return
        self.query = text
        self.row = 2
        self.set_filter("search")

    def clear_search(self) -> None:
        self.query = ""
        self.set_filter("discover")

    def open_keyboard(self, first: str = "") -> None:
        self.w.keyboard = Keyboard(self.w, "Search every site", self.search,
                                   text=(self.query + first) if first else self.query, done_label="Search")

    def type_text(self, text: str) -> bool:
        if text.isprintable() and text.strip() and text != "/":
            self.open_keyboard(text)
            return True
        return False

    # ----------------------------------------------------------- geometry
    def _tile_size(self) -> tuple[float, float, float]:
        s = self.s
        th = min(self.w.height() * 0.25, 270 * s)
        return th * 2 / 3, th, 26 * s

    def _grid_cols(self) -> int:
        tw, _th, gap = self._tile_size()
        left = 72 * self.s
        return max(1, int((self.w.width() - 2 * left + gap) // (tw + gap)))

    # ------------------------------------------------- controller games only
    def controller_only(self) -> bool:
        return bool(self.w.actions.settings.get("couch_controller_only", True))

    PAGE = 50                                        # games added to a grid at a time

    def _candidates(self, groups: list, need: int) -> list:
        """The games that may be shown, in the chosen order. With "controller games only" (the default): those
        Steam lists with full or partial controller support — games that are not on Steam, or need a mouse and
        keyboard, stay hidden. Only as many games are checked as the screen needs (``need``), plus some ahead."""
        if not self.controller_only():
            return groups
        w = self.w
        key = id(groups)
        hit = self._vis.get(key)
        if hit is not None and hit[0] is groups and hit[1] == (len(groups), w.ctl_version, need):
            return hit[2]
        ok = ("full", "partial")
        out, unknown = [], []
        for g in groups:
            if isinstance(g, SeeAll):
                out.append(g)
                continue
            info = w.controller.get(g.key)
            if info is not None and not info.get("pending"):
                if info.get("controller") in ok:
                    out.append(g)
            elif len(out) + len(unknown) < need + 60:
                unknown.append(g)
        w.want_controller(unknown)
        if len(self._vis) > 12:
            self._vis.clear()
        self._vis[key] = (groups, (len(groups), w.ctl_version, need), out)
        return out

    def _stable(self, view: str, cands: list, cap: int | None = None) -> list:
        """What a grid or shelf shows: games are only ever added at the end — never moved or taken away while you
        look around (answers and new pages from the sites used to re-sort the screen all the time). A grid shows
        ``PAGE`` games; the next ``PAGE`` come when you reach the bottom."""
        st = self._views.get(view)
        if st is None:
            st = self._views[view] = {"keys": [], "limit": self.PAGE, "out": None}
        keys = st["keys"]
        limit = cap if cap is not None else st["limit"]
        if st["out"] is not None and st["out"][0] is cands and st["out"][1] == (len(cands), limit):
            return st["out"][2]
        games = [g for g in cands if not isinstance(g, SeeAll)]
        if len(keys) < limit:
            seen = set(keys)
            for g in games:
                if len(keys) >= limit:
                    break
                if g.key not in seen:
                    keys.append(g.key)
                    seen.add(g.key)
        by_key = {g.key: g for g in games}
        out = [by_key[k] for k in keys if k in by_key]
        st["out"] = (cands, (len(cands), limit), out)
        st["more"] = len(games) > len(keys)
        return out

    def _view(self) -> str:
        sort = self.feed.sort if self.feed is not None else None
        return f"grid|{self.filter}|{self.query}|{self.genre}|{sort}|{self.controller_only()}"

    @property
    def shelves(self) -> list[tuple[str, list, str | None]]:
        out = []
        for t, gs, k in self._all_shelves:
            cached = self._shelf_games.get(k)
            if cached is None or cached[0] is not gs:
                cached = self._shelf_games[k] = (gs, [g for g in gs if not isinstance(g, SeeAll)])
            games = cached[1]
            shown = self._stable(f"shelf|{k}|{self.controller_only()}", self._candidates(games, len(games)),
                                 cap=len(games))
            out.append((t, shown + [g for g in gs if isinstance(g, SeeAll)], k))
        self.shelf = max(0, min(self.shelf, len(out) - 1))
        return out

    def _raw_grid(self) -> list:
        if self.filter.startswith("sec:"):
            from ezgd.catalog.search import group_duplicates
            sec = self.sections.get(self.filter[4:])
            if sec is None:
                return []
            cache = getattr(self, "_sec_cache", None)
            if cache is None or cache[0] != self.filter:
                self._sec_cache = (self.filter, group_duplicates(sec[1]))
            return self._sec_cache[1]
        return self.feed.groups if self.feed is not None else []

    def _grid_items(self) -> list:
        view = self._view()
        st = self._views.get(view) or {}
        limit = st.get("limit", self.PAGE)
        cands = self._candidates(self._raw_grid(), limit + self.PAGE)     # the next page is checked ahead
        self._cands_n = len(cands)
        return self._stable(view, cands)

    def more_games(self) -> bool:
        """Is there a next page (already found, or the sites / the controller check may still bring one)?"""
        st = self._views.get(self._view()) or {}
        feed = self.feed
        return bool(st.get("more")) or (feed is not None and (feed.more or feed.busy)) or \
            (self.controller_only() and self.w.checking_controllers())

    def next_page(self) -> bool:
        """At the bottom: the next PAGE games. True when there were more to show right away."""
        st = self._views.get(self._view())
        if st is None:
            return False
        n = len(self._grid_items())
        if n < st["limit"]:
            return False                             # this page is not full yet: what comes, comes here
        st["limit"] += self.PAGE
        feed = self.feed
        if feed is not None and not feed.busy and feed.more and self._cands_n < st["limit"] + self.PAGE:
            feed.load_more()
        return len(self._grid_items()) > n

    def unload_far(self) -> None:
        """Only the page of 50 you are on and its neighbours keep their pictures; the others are unloaded (their
        games stay where they were — just their pictures load again when you come back)."""
        if not self._grid():
            return
        items = self._grid_items()
        page = self.gi // self.PAGE
        if getattr(self, "_loaded_page", None) == (self._view(), page):
            return
        self._loaded_page = (self._view(), page)
        self.w.unload_except(items[max(0, (page - 1) * self.PAGE):(page + 2) * self.PAGE])

    def items_changed(self) -> bool:
        """Answers came in (pages, controller checks): repaint only when what is on screen changed."""
        items = self._grid_items() if self._grid() else [g for _t, gs, _k in self.shelves for g in gs]
        sig = tuple(id(g) for g in items)
        if sig == getattr(self, "_shown_sig", None):
            return False
        self._shown_sig = sig
        self.keep_focus()
        return True

    def _grid(self) -> bool:
        return self.filter != "discover"

    def _focused_group(self):
        if self.row != 2:
            return None
        if self._grid():
            items = self._grid_items()
            return items[min(self.gi, len(items) - 1)] if items else None
        if not self.shelves:
            return None
        _t, groups, _k = self.shelves[min(self.shelf, len(self.shelves) - 1)]
        c = min(self.cols.get(self.shelf, 0), len(groups) - 1)
        return groups[c] if groups else None

    def background(self) -> str | None:
        g = self._focused_group()
        if g is None or isinstance(g, SeeAll):
            return None
        return self.w.group_hero(g)

    # ------------------------------------------------------------- input
    def _cycle(self, d: int) -> None:
        """LT / RT: the previous / next filter, right away (the genre chip is chosen with A)."""
        keys = [k for k, _l in self.chips() if k not in ("genre", "sort", "search")]
        cur = self.filter if self.filter in keys else "discover"
        nxt = keys[(keys.index(cur) + d) % len(keys)]
        self.set_filter(nxt)
        self.row = 2

    def _activate(self, g) -> None:
        if isinstance(g, SeeAll):
            self.set_filter(g.filter_key)
            self.row = 2
        elif g is not None:
            self.w.open_game(g.entries)

    def command(self, name: str) -> str | None:
        chips = self.chips()
        if name in ("lt", "rt"):
            self._cycle(1 if name == "rt" else -1)
            return None
        if name == "x":
            self.open_keyboard()
            return None
        if name == "b":
            if self.row == 1 or (self.row == 2 and self.filter != "discover"):
                if self.query:
                    self.clear_search()
                elif self.filter != "discover":
                    self.set_filter("discover")
                self.row = 2
                return None
            if self.row == 0:
                self.row = 2
                return None
            return "back"
        if self.row == 0:
            if name == "a":
                self.open_keyboard()
            elif name == "down":
                self.row = 1
            elif name == "up":
                return "top"
            return None
        if self.row == 1:
            if name in ("left", "right"):
                self.chip = max(0, min(len(chips) - 1, self.chip + (1 if name == "right" else -1)))
            elif name == "a":
                self.set_filter(chips[self.chip][0])
                if chips[self.chip][0] not in ("genre", "sort"):
                    self.row = 2
            elif name == "down":
                self.row = 2
            elif name == "up":
                self.row = 0
            return None
        # the content
        if self._grid():
            items = self._grid_items()
            n = len(items)
            cols = self._grid_cols()
            if name == "up":
                if self.gi >= cols:
                    self.gi -= cols
                else:
                    self.row = 1
            elif name == "down" and n:
                if self.gi + cols < n:
                    self.gi += cols
                elif self.gi // cols < (n - 1) // cols:
                    self.gi = n - 1
                elif self.next_page():                   # at the bottom: the next 50 games
                    items = self._grid_items()
                    n = len(items)
                    self.gi = min(n - 1, self.gi + cols)
            elif name in ("left", "right") and n:
                self.gi = max(0, min(n - 1, self.gi + (1 if name == "right" else -1)))
            elif name == "a" and n:
                self._activate(items[min(self.gi, n - 1)])
            if n:
                self._focus_key = items[min(self.gi, n - 1)].key
                self._art(items[max(0, self.gi - 6):self.gi + 24])
                self.unload_far()
        else:
            if name == "up":
                if self.shelf > 0:
                    self.shelf -= 1
                else:
                    self.row = 1
            elif name == "down" and self.shelf < len(self.shelves) - 1:
                self.shelf += 1
            elif name in ("left", "right") and self.shelves:
                groups = self.shelves[self.shelf][1]
                c = self.cols.get(self.shelf, 0) + (1 if name == "right" else -1)
                self.cols[self.shelf] = max(0, min(len(groups) - 1, c))
                c = self.cols[self.shelf]
                if c >= 8:
                    self._art(groups[c - 2:c + 10])
            elif name == "a":
                self._activate(self._focused_group())
        self.w.refresh_bg()
        return None

    def hit(self, idx: int, click: bool) -> None:
        if idx == -1:
            self.row = 0
            if click:
                self.open_keyboard()
            return
        if idx <= -100:                                  # a filter chip
            self.row, self.chip = 1, -100 - idx
            if click:
                key = self.chips()[self.chip][0]
                self.set_filter(key)
                if key not in ("genre", "sort"):
                    self.row = 2
            return
        was = (self.row, self.shelf, self.cols.get(self.shelf, 0), self.gi)
        self.row = 2
        if self._grid():
            self.gi = idx
        else:
            self.shelf, col = divmod(idx, 10000)
            self.cols[self.shelf] = col
        self.w.refresh_bg()
        if click and was == (self.row, self.shelf, self.cols.get(self.shelf, 0), self.gi):
            self._activate(self._focused_group())

    def hints(self) -> list[tuple[str, str]]:
        if self.row == 0:
            return [("a", "Type to search"), ("b", "Back")]
        if self.row == 1:
            return [("lr", "Choose"), ("a", "Show"), ("b", "Discover" if self.filter != "discover" else "Back")]
        h = [("a", "Open"), ("x", "Search"), ("lt", ""), ("rt", "Filter")]
        h.append(("b", "Clear search" if self.query else "Discover" if self.filter != "discover" else "Back"))
        return h

    # ------------------------------------------------------------- paint
    def paint(self, p: QPainter) -> None:
        w = self.w
        s, W, H = self.s, w.width(), w.height()
        left = 72 * s
        active = w.zone == "main" and w.overlay is None and w.keyboard is None
        y = 96 * s + 10 * s
        # search bar
        bar = QRectF(left, y, min(720 * s, W - 2 * left), 60 * s)
        focused = active and self.row == 0
        path = QPainterPath()
        path.addRoundedRect(bar, 30 * s, 30 * s)
        p.fillPath(path, QColor(255, 255, 255, 235) if focused else QColor(0, 0, 0, 130))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 255 if focused else 60), (3 if focused else 1.5) * s))
        p.drawPath(path)
        p.setFont(w._font(22 * s, 600))
        p.setPen(QColor(11, 13, 19) if focused else QColor(255, 255, 255, 230 if self.query else 150))
        p.drawText(bar.adjusted(28 * s, 0, -28 * s, 0), Qt.AlignLeft | Qt.AlignVCenter,
                   "🔍   " + (self.query if self.query else "Search games on every site"))
        w._hit.append((bar, "page", -1))
        p.setFont(w._font(17 * s, 600))
        p.setPen(QColor(255, 255, 255, 150))
        p.drawText(QRectF(bar.right() + 28 * s, y, W - bar.right() - 140 * s, 60 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   self._status())
        # filter chips
        y += 60 * s + 16 * s
        self._paint_chips(p, QRectF(left, y, W - 2 * left, 46 * s), active)
        top = y + 46 * s + 28 * s
        bottom = H - 76 * s - 10 * s
        if self._grid():
            self._paint_grid(p, top, bottom, active)
        else:
            self._paint_shelves(p, top, bottom, active)

    def _status(self) -> str:
        if self._grid():
            items = self._grid_items()
            n = len(items)
            feed = self.feed
            if feed is not None and feed.busy and not n:
                return "Searching every site…" if self.query else "Loading…"
            text = f"{n} game{'s' if n != 1 else ''}" + (f" for “{self.query}”" if self.query else "")
            if self.controller_only():
                text += " with controller support"
            filling = n < (self._views.get(self._view()) or {}).get("limit", self.PAGE)
            if filling and self.controller_only() and self.w.checking_controllers():
                text += " · checking more…"
            elif filling and feed is not None and feed.busy:
                text += " · loading more…"
            elif feed is not None and feed.errors and len(feed.errors) == len(feed.sids):
                text += " · the site did not answer"
            elif feed is not None and feed.errors:
                text += f" · {len(feed.errors)} site(s) did not answer"
            return text
        if self.loading and not self.shelves:
            return "Loading what the sites offer…"
        if self.controller_only():
            return "Games with controller support" + (" · checking more…" if self.w.checking_controllers() else "")
        return ""

    def _paint_chips(self, p: QPainter, rect: QRectF, active: bool) -> None:
        w, s = self.w, self.s
        chips = self.chips()
        f = w._font(18 * s, 700)
        wkey = (tuple(chips), round(s, 3))
        if getattr(self, "_chip_w", (None,))[0] != wkey:
            fm = QFontMetrics(f)
            self._chip_w = (wkey, [fm.horizontalAdvance(label) + 44 * s for _k, label in chips])
        widths = self._chip_w[1]
        xs, x = [], 0.0
        for cw in widths:
            xs.append(x)
            x += cw + 12 * s
        total = x - 12 * s
        self.chip = max(0, min(self.chip, len(chips) - 1))
        target = getattr(self, "_cs_t", 0.0)
        cl, cr = xs[self.chip], xs[self.chip] + widths[self.chip]
        if cl - target < 0:
            target = max(0.0, cl - 60 * s)
        elif cr - target > rect.width():
            target = cr - rect.width() + 60 * s
        self._cs_t = max(0.0, min(target, max(0.0, total - rect.width())))
        off = self._smooth("_cs", self._cs_t)
        focus = self.chip if active and self.row == 1 else -1
        m = 12 * s                                       # room for the focus glow around the strip
        clip = rect.adjusted(-10 * s, -10 * s, 10 * s, 10 * s)
        p.save()
        p.setClipRect(clip)
        # the whole strip is drawn once and then only copied (text and glows every frame were slow at 4K)
        w.blit(p, rect.left() - off - m, rect.top() - m,
               ("chips", tuple(chips), self.filter, focus, round(s, 3), theme.CURRENT.accent),
               total + 2 * m, rect.height() + 2 * m,
               lambda q: self._draw_chips(q, chips, xs, widths, QRectF(m, m, total, rect.height()), focus, f),
               clip=clip)
        p.restore()

    def _draw_chips(self, p: QPainter, chips, xs, widths, rect: QRectF, focus: int, f) -> None:
        w, s = self.w, self.s
        p.setFont(f)
        for i, (key, label) in enumerate(chips):
            r = QRectF(rect.left() + xs[i], rect.top(), widths[i], rect.height())
            selected = key == self.filter
            focused = i == focus
            path = QPainterPath()
            path.addRoundedRect(r, r.height() / 2, r.height() / 2)
            if focused:
                for k in range(3, 0, -1):
                    gp = QPainterPath()
                    rr = r.adjusted(-k * 3 * s, -k * 3 * s, k * 3 * s, k * 3 * s)
                    gp.addRoundedRect(rr, rr.height() / 2, rr.height() / 2)
                    p.fillPath(gp, QColor(255, 255, 255, 34))
                p.fillPath(path, QColor(255, 255, 255, 240))
                p.setPen(QColor(11, 13, 19))
            elif selected:
                p.fillPath(path, QColor(theme.CURRENT.accent))
                p.setPen(QColor("white"))
            else:
                p.fillPath(path, QColor(255, 255, 255, 26))
                p.setPen(QColor(255, 255, 255, 210))
            if key.startswith("site:"):                  # the site's colour, like the tags on the tiles
                dot = QRectF(r.left() + 14 * s, r.center().y() - 5 * s, 10 * s, 10 * s)
                p.save()
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.SOURCE_COLORS.get(key[5:], theme.CURRENT.accent)))
                p.drawEllipse(dot)
                p.restore()
                p.drawText(r.adjusted(18 * s, 0, 0, 0), Qt.AlignCenter, label)
            else:
                p.drawText(r, Qt.AlignCenter, label)
            w._hit.append((r, "page", -100 - i))

    def _group_tile(self, p: QPainter, g, r: QRectF, focused: bool, idx: int) -> None:
        if isinstance(g, SeeAll):
            self._see_all_tile(p, g, r, focused, idx)
            return
        game = g.primary
        badge = ("IN LIBRARY", theme.CURRENT.success) if game.dedup_key in self.w.actions.installed_keys else None
        self.w.art_tile(p, r, focused, self.w.group_capsule(g), game.cover_url, g.title, badge, False,
                        ("page", idx), tags=source_tags(g) + self._linux_tag(g))

    def _linux_tag(self, g) -> list[tuple[str, str]]:
        """How well the game runs on Linux (ProtonDB players) as one more pill on its tile."""
        from ezgd.ui.ratings import LEVEL_COLORS
        r = self.ratings.get(g)
        if r is None:
            app = self.w.group_app_id(g)
            self.ratings.want((g,), app_ids={g.key: app} if app else None)
            return []
        return [(r["label"], LEVEL_COLORS[r["level"]])] if r["level"] != "few" else []

    def _see_all_tile(self, p: QPainter, g: SeeAll, r: QRectF, focused: bool, idx: int) -> None:
        w, s = self.w, self.s
        if focused:
            w._focus_rect = r.adjusted(-r.width() * 0.04, -r.height() * 0.04, r.width() * 0.04, r.height() * 0.04)
            grow = 1.0 + 0.06 * w._focus_anim
            c = r.center()
            r = QRectF(c.x() - r.width() * grow / 2, c.y() - r.height() * grow / 2, r.width() * grow,
                       r.height() * grow)
        path = QPainterPath()
        path.addRoundedRect(r, 12 * s, 12 * s)
        p.fillPath(path, QColor(255, 255, 255, 235) if focused else QColor(255, 255, 255, 22))
        p.setPen(QColor(11, 13, 19) if focused else QColor("white"))
        p.setFont(w._font(24 * s, 800))
        p.drawText(r.adjusted(0, 0, 0, -30 * s), Qt.AlignCenter, "See all  ›")
        p.setFont(w._font(16 * s, 600))
        p.setPen(QColor(11, 13, 19, 170) if focused else QColor(255, 255, 255, 160))
        p.drawText(r.adjusted(14 * s, 40 * s, -14 * s, 0), Qt.AlignCenter | Qt.TextWordWrap, g.title)
        w._hit.append((r, "page", idx))

    def _paint_shelves(self, p: QPainter, top: float, bottom: float, active: bool) -> None:
        w = self.w
        s, W = self.s, w.width()
        left = 72 * s
        tw, th, gap = self._tile_size()
        block = 44 * s + th + 92 * s
        if not self.shelves:
            p.setFont(w._font(22 * s))
            p.setPen(QColor(255, 255, 255, 170))
            text = "Loading…" if self.loading else "The sites could not be reached. Search still tries them."
            if self._all_shelves and self.controller_only():
                text = "Checking which games work with a controller…" if self.w.checking_controllers() else \
                    "No games with controller support here. (Settings → Store shows every game.)"
            p.drawText(QRectF(left, top + 20 * s, W - 2 * left, 40 * s), Qt.AlignLeft | Qt.AlignVCenter, text)
            return
        view = bottom - top
        target = getattr(self, "_vs_t", 0.0)
        fr = self.shelf
        if fr * block < target:
            target = fr * block
        elif fr * block + block - target > view:
            target = fr * block + block - view
        self._vs_t = max(0.0, target)
        off = self._smooth("_vs", self._vs_t)
        p.save()
        p.setClipRect(QRectF(0, top - 20 * s, W, bottom - top + 20 * s))
        focused_draw = None
        for k, (title, groups, _key) in enumerate(self.shelves):
            y = top + k * block - off
            if y > bottom or y + block < top - 40 * s:
                continue
            p.setFont(w._font(19 * s, 800))
            p.setPen(QColor(255, 255, 255, 215 if k == self.shelf else 150))
            p.drawText(QRectF(left, y, W, 32 * s), Qt.AlignLeft | Qt.AlignVCenter, title.upper())
            c = min(self.cols.get(k, 0), len(groups) - 1)
            tgt = max(0.0, c * (tw + gap) - (W * 0.33 - left))
            tgt = min(tgt, max(0.0, len(groups) * (tw + gap) - gap - (W - 2 * left)))
            cur = self.hscroll.get(k, 0.0)
            cur = tgt if w.reduce_motion or abs(cur - tgt) < 0.6 else cur + (tgt - cur) * 0.25
            if cur != tgt:
                w._schedule_frame()
            self.hscroll[k] = cur
            ty = y + 44 * s
            for j, g in enumerate(groups):
                x = left + j * (tw + gap) - cur
                if x > W or x + tw < 0:
                    continue
                r = QRectF(x, ty, tw, th)
                if active and self.row == 2 and k == self.shelf and j == c:
                    focused_draw = (g, r, k * 10000 + j)
                    continue
                self._group_tile(p, g, r, False, k * 10000 + j)
        if focused_draw is not None:
            g, r, idx = focused_draw
            self._group_tile(p, g, r, True, idx)
            if not isinstance(g, SeeAll):
                self._caption(p, g, r)
        p.restore()

    def _caption(self, p: QPainter, g, r: QRectF) -> None:
        s = self.s
        p.setFont(self.w._font(17 * s, 700))
        p.setPen(QColor("white"))
        p.drawText(QRectF(r.left() - 40 * s, r.bottom() + 12 * s, r.width() + 80 * s, 26 * s), Qt.AlignCenter,
                   QFontMetrics(p.font()).elidedText(g.primary.title, Qt.ElideRight, int(r.width() + 80 * s)))
        bits = [g.primary.version, g.primary.size_text]
        sub = " · ".join(b for b in bits if b)
        if sub:
            p.setFont(self.w._font(14 * s, 600))
            p.setPen(QColor(255, 255, 255, 160))
            p.drawText(QRectF(r.left() - 40 * s, r.bottom() + 38 * s, r.width() + 80 * s, 22 * s), Qt.AlignCenter,
                       QFontMetrics(p.font()).elidedText(sub, Qt.ElideRight, int(r.width() + 80 * s)))

    def _paint_grid(self, p: QPainter, top: float, bottom: float, active: bool) -> None:
        w = self.w
        s, W = self.s, w.width()
        tw, th, gap = self._tile_size()
        cols = self._grid_cols()
        left = 72 * s + 6 * s
        hgap = (W - 2 * left - cols * tw) / (cols - 1) if cols > 1 else gap    # columns spread edge to edge
        items = self._grid_items()
        feed = self.feed
        if not items:
            p.setFont(w._font(22 * s))
            p.setPen(QColor(255, 255, 255, 170))
            busy = feed is not None and (feed.busy or not feed.pages)
            text = ("Searching every site…" if self.query else "Loading games…") if busy else \
                ("Nothing found. Try another name (Y), or fewer words." if self.query else
                 "No games here right now — the site may be down.")
            if self.controller_only() and not busy:
                text = ("Checking which games work with a controller…" if self.w.checking_controllers() else
                        "No games with controller support here. (Settings → Store shows every game.)")
            p.drawText(QRectF(72 * s, top + 20 * s, W - 144 * s, 40 * s), Qt.AlignLeft | Qt.AlignVCenter, text)
            return
        n = len(items)
        i = min(self.gi, n - 1)
        rows = (n - 1) // cols + 1
        row_h = th + gap + 64 * s
        view = bottom - top
        # fetch pages from the sites until the next 50 games are ready (they are shown when you get there)
        limit = (self._views.get(self._view()) or {}).get("limit", self.PAGE)
        if feed is not None and not feed.busy and feed.more and self._cands_n < limit + self.PAGE:
            feed.load_more()
        target = getattr(self, "_gs_t", 0.0)
        rt = (i // cols) * row_h
        if rt < target:
            target = rt
        elif rt + row_h - target > view:
            target = rt + row_h - view
        self._gs_t = max(0.0, target)
        off = self._smooth("_gs", self._gs_t)
        p.save()
        p.setClipRect(QRectF(0, top - 24 * s, W, bottom - top + 24 * s))
        focused_draw = None
        first = max(0, int((off - 40 * s - th) // row_h))           # only the rows on screen (not every game)
        last = min(rows - 1, int((off + view + 24 * s) // row_h) + 1)
        for k in range(first * cols, min(n, (last + 1) * cols)):
            g = items[k]
            rr, cc = divmod(k, cols)
            r = QRectF(left + cc * (tw + hgap), top + rr * row_h - off, tw, th)
            if r.top() > bottom or r.bottom() < top - 40 * s:
                continue
            if k == i and active and self.row == 2:
                focused_draw = (g, r, k)
                continue
            self._group_tile(p, g, r, False, k)
        # the end of the grid: more is loading, or that is everything
        fy = top + rows * row_h - off
        if fy < bottom:
            p.setFont(w._font(19 * s, 600))
            p.setPen(QColor(255, 255, 255, 150))
            st = self._views.get(self._view()) or {}
            if n >= st.get("limit", self.PAGE) and self.more_games():
                p.drawText(QRectF(0, fy, W, 40 * s), Qt.AlignCenter, "▼   Down for the next 50 games")
            elif self.more_games():                      # this page is still filling up
                t = time.monotonic()
                for k in range(3):
                    a = 0.35 + 0.65 * max(0.0, math.sin(t * 5 - k * 0.7))
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(255, 255, 255, int(255 * a)))
                    p.drawEllipse(QPointF(W / 2 - 34 * s + k * 34 * s, fy + 20 * s), 8 * s, 8 * s)
                p.setBrush(Qt.NoBrush)
                w.update_area(QRectF(0, fy, W, 44 * s), 60)          # only the dots move
            else:
                p.drawText(QRectF(0, fy, W, 40 * s), Qt.AlignCenter, f"That's everything — {n} games")
        if focused_draw is not None:
            g, r, idx = focused_draw
            self._group_tile(p, g, r, True, idx)
            if not isinstance(g, SeeAll):
                self._caption(p, g, r)
        p.restore()


# ================================================================== game page
class GamePage(Page):
    """A game from the sites: what it is, where it comes from, and every way to download it."""

    def __init__(self, w, games: list, update: dict | None = None):
        super().__init__(w)
        self.games = list(games)
        self.cur = 0
        self.game = self.games[0] if self.games else None
        self.update_info = update
        self.loading = False
        self.error = ""
        self.items: list[Item] = []
        self.focus = 0
        self.wishlisted: bool | None = None
        self.steam_base: dict | None = None
        self.linux: dict | None = None
        self._settled = False
        self._gen = 0

    def activate(self) -> None:
        if self.game is not None and not self.loading and not self.items and not self.error:
            self.load(self.game)
            b = self.w.bridge
            b.call(b.core.related(self.games[0].id), self._related, lambda _e: None, owner=self.w)

    def _related(self, related: list) -> None:
        known = {g.id for g in self.games}
        new = [g for g in related if g.id not in known]
        if new:
            self.games += new
            self.w.load_art([g.steam_app_id for g in new if g.steam_app_id])
            self._build()
            self.w.refresh_bg()
            self.w.update()

    def load(self, game, force: bool = False) -> None:
        self.game = game
        self.loading = True
        self.error = ""
        self.steam_base = None
        self.wishlisted = None
        self.linux = None
        self._settled = False
        self._gen += 1
        gen = self._gen
        b = self.w.bridge
        self._build()
        self.w.load_art([game.steam_app_id] if game.steam_app_id else [])

        def loaded(g) -> None:
            if gen != self._gen:
                return
            self.loading = False
            self.game = g
            self.games[self.cur] = g
            self.w.load_art([g.steam_app_id] if g.steam_app_id else [])
            b.call(b.core.linux_ratings([(self._app_id(), g.title)]),
                   lambda r: gen == self._gen and (setattr(self, "linux", r[0] if r else None), self.w.update()),
                   lambda _e: None, owner=self.w)
            if g.extra.get("steam_base_game"):
                b.call(b.core.steam_base_info(g.id), lambda info: self._base(gen, info), lambda _e: None,
                       owner=self.w)
            self._build()
            self.w.refresh_bg()
            self.w.update()

        def failed(e) -> None:
            if gen != self._gen:
                return
            self.loading = False
            self.error = str(e) or type(e).__name__
            self._build()
            self.w.update()
        b.call(b.core.details(game.id, force), loaded, failed, owner=self.w)
        b.call(b.core.wishlist_item_for(game.id), lambda it: self._wish(gen, it is not None), lambda _e: None,
               owner=self.w)

    def _base(self, gen: int, info) -> None:
        if gen == self._gen:
            self.steam_base = info
            self._build()
            self.w.update()

    def _wish(self, gen: int, on: bool) -> None:
        if gen == self._gen:
            self.wishlisted = on
            self._build()
            self.w.update()

    def _app_id(self) -> int | None:
        if self.game is not None and self.game.steam_app_id:
            return self.game.steam_app_id
        found = next((g.steam_app_id for g in self.games if g.steam_app_id), None)
        return found or (self.w._title_apps.get(self.games[0].dedup_key) if self.games else None)

    def background(self) -> str | None:
        if self.game is None:
            return None
        a = self.w.art.get(self._app_id()) or {}
        return a.get("hero") or a.get("wide") or self.game.cover_url

    def switch_source(self, d: int) -> None:
        if len(self.games) < 2:
            return
        self.cur = (self.cur + d) % len(self.games)
        self.load(self.games[self.cur])

    # ------------------------------------------------------------- items
    def _build(self) -> None:
        from ezgd.catalog.models import MethodKind
        from ezgd.installation.volumes import volume_info
        from ezgd.sources.hosts import browser_url
        g = self.game
        items: list[Item] = []
        if g is None:
            self.items = items
            return
        if len(self.games) > 1:
            items.append(Item(f"From {theme.source_name(g.source)}", lambda: self.switch_source(1),
                              f"{len(self.games)} sites have this game — left / right to switch", "choice",
                              f"{self.cur + 1} of {len(self.games)}", left=lambda: self.switch_source(-1),
                              right=lambda: self.switch_source(1)))
        methods = [] if self.loading else (g.download_methods or [])
        if not self.loading and not self.error and not g.withdrawn:
            if self.steam_base and not self.steam_base.get("installed"):
                items.append(Item(f"Install “{self.steam_base['name']}” in Steam (free)", self._install_base,
                                  "Only the fix is offered here — install the game in Steam first", "primary"))
            groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
            for i, m in enumerate(methods):
                v = volume_info(m.file_name or m.label.split("—")[-1].strip())
                if v is not None:
                    groups.setdefault((m.label.split("—")[0].strip(), v.set_key), []).append((v.index, i))
            in_sets: set[int] = set()
            for (host, _key), parts in groups.items():
                if len(parts) < 2:
                    continue
                idx = [i for _, i in sorted(parts)]
                if all(methods[i].supported for i in idx):
                    in_sets.update(idx)
                    total = sum(methods[i].size_bytes or 0 for i in idx)
                    items.append(Item(f"Download all {len(idx)} parts — {host}",
                                      lambda idx=idx, host=host: start_download_set(self.w, g, idx, host),
                                      "In the app" + (f" · {format_bytes(total)}" if total else "")
                                      + " · installed when the last part is in", "primary"))
            for i, m in enumerate(methods):
                if i in in_sets:
                    continue
                size = f" · {format_bytes(m.size_bytes)}" if m.size_bytes else ""
                if m.supported and m.kind in (MethodKind.TORRENT, MethodKind.DIRECT):
                    kind = "Torrent" if m.kind == MethodKind.TORRENT else "Direct download"
                    items.append(Item(f"Download — {m.label}", lambda i=i: start_download(self.w, g, i),
                                      f"{kind} in the app{size}" + (f" · {m.note}" if m.note else ""), "primary"))
                else:
                    target = browser_url(m, g.url)
                    items.append(Item(f"Download in the browser — {m.label}",
                                      lambda m=m, t=target: open_browser(self.w, g, m, t),
                                      f"Needs a browser (captcha or sign-in){size} — the file then joins your Library"))
        if not self.loading and not self.error:
            if self.wishlisted is not None:
                items.append(Item("♥  On your wishlist" if self.wishlisted else "♡  Add to wishlist",
                                  self._toggle_wish, "A removes it" if self.wishlisted else
                                  "The app tells you when a site has it or a newer version", "toggle",
                                  "on" if self.wishlisted else "off"))
        if self.error:
            items.append(Item("Try again", lambda: self.load(self.game, True), self.error[:120], "primary"))
        items.append(Item("Back", self.w.pop_page))
        old_item = self.items[self.focus] if 0 <= self.focus < len(self.items) else None
        old = old_item.label if old_item is not None else None
        settled = not self.loading
        self.items = items
        same = next((k for k, it in enumerate(items) if it.label == old), None)
        if old_item is not None and old_item.kind == "choice":     # switching sites: stay on the switch
            same = next((k for k, it in enumerate(items) if it.kind == "choice"), same)
        elif same is None or old == "Back" or (settled and not self._settled):
            # a page that just loaded starts on its best download, not on "Back"
            same = next((k for k, it in enumerate(items) if it.kind == "primary"), self._first(items))
        self._settled = settled
        self.focus = same

    def _install_base(self) -> None:
        g = self.game
        b = self.w.bridge

        def done(r: dict) -> None:
            if r.get("opened") == "store" and r.get("url"):
                from ezgd.ui.dialogs.misc import open_url
                open_url(r["url"])
            self.w.toasts.show("Install it in Steam", f"Press “Install” for {r.get('name')} in Steam — then download "
                                                      "the fix here.", "info")
            self.w.step_away("steam")
        b.call(b.core.install_steam_base(g.id), done, lambda e: self.w.actions.fail("Steam", e), owner=self.w)

    def _toggle_wish(self) -> None:
        g = self.game
        b = self.w.bridge

        def done(on: bool) -> None:
            self.wishlisted = on
            self.w.toasts.show("Added to your wishlist" if on else "Removed from your wishlist", g.title,
                               "success" if on else "info")
            self._build()
            self.w.update()
        b.call(b.core.wishlist_toggle(g.id), done, lambda e: self.w.actions.fail("Wishlist", e), owner=self.w)

    # ------------------------------------------------------------- input
    def command(self, name: str) -> str | None:
        if name in ("lt", "rt") and len(self.games) > 1:      # triggers: the other sites' pages
            self.switch_source(1 if name == "rt" else -1)
            return None
        r = self._list_command(self.items, "focus", name)
        if r == "back":
            self.w.pop_page()
            return None
        return r

    def hit(self, idx: int, click: bool) -> None:
        if 0 <= idx < len(self.items) and self.items[idx].focusable:
            self.focus = idx
            if click and self.items[idx].action:
                self.items[idx].action()

    def hints(self) -> list[tuple[str, str]]:
        h = [("a", "Select")]
        if len(self.games) > 1:
            h += [("lt", ""), ("rt", "Other sites")]
        h.append(("b", "Back"))
        return h

    # ------------------------------------------------------------- paint
    def _notes(self, g) -> list[tuple[str, str]]:
        notes = []
        if self.update_info:
            u = self.update_info
            ver = f" from {u.get('installed')} to {u.get('latest')}" if u.get("installed") and u.get("latest") else ""
            notes.append((theme.CURRENT.success, f"Updating your game{ver}: the download goes over the installed "
                                                 "game. Your saves are backed up first."))
        if g.withdrawn:
            notes.append((theme.CURRENT.danger, f"{theme.source_name(g.source)} has closed this guide, so it is not "
                                                "offered for download."))
        if g.extra.get("warning"):
            notes.append((theme.CURRENT.warning, g.extra["warning"]))
        pw = g.extra.get("archive_password") or theme.source_password(g.source)
        if pw and not self.loading:
            notes.append((theme.CURRENT.warning, f"🔒 Archive password: {pw} — the app enters it for you."))
        if g.extra.get("installer"):
            notes.append((theme.CURRENT.accent, "A repack: after downloading, “Install game” runs its installer."))
        return notes

    def paint(self, p: QPainter) -> None:
        w = self.w
        g = self.game
        s, W, H = self.s, w.width(), w.height()
        if g is None:
            return
        left = 72 * s
        top = 96 * s + 20 * s
        bottom = H - 76 * s - 20 * s
        col_w = W * 0.46 - left
        # left column: what the game is — the same until the page loads, so it is drawn once and then copied
        source = ("UPDATE  ·  " if self.update_info else "") + theme.source_name(g.source).upper()
        facts = [x for x in (g.version and f"Version {g.version}", g.size_text, g.release_date,
                             ", ".join(g.genres[:3])) if x]
        if g.dedup_key in w.actions.installed_keys:
            facts.insert(0, "In your library")
        if g.size_bytes:
            from ezgd.core.fsutil import install_estimate
            est = install_estimate(g, g.size_bytes)
            if est["install_size"]:
                facts.append(f"needs {'~' if est['install_size_guessed'] else ''}"
                             f"{format_bytes(g.size_bytes + est['install_size'])} free")
        notes = self._notes(g)
        if self.linux is not None:
            from ezgd.ui.ratings import LEVEL_COLORS
            notes.insert(0, (LEVEL_COLORS[self.linux["level"]], self.linux["text"] + "."))
        desc = " ".join(g.description.split()) if g.description and not self.loading else ""
        key = ("gameinfo", W, H, round(s, 3), theme.CURRENT.accent, source, g.title, "  ·  ".join(facts),
               tuple(notes), hash(desc))
        w.blit(p, left, top, key, col_w, bottom - top,
               lambda q: self._paint_info(q, g.title, source, facts, notes, desc, col_w, bottom - top))
        # right column: what you can do
        rx = W * 0.52
        rect = QRectF(rx, top + 10 * s, W - rx - 90 * s, bottom - top - 10 * s)
        p.setFont(w._font(17 * s, 800))
        p.setPen(QColor(255, 255, 255, 190))
        head = "LOADING THE GAME PAGE…" if self.loading else "DOWNLOAD" if not self.error else "COULD NOT LOAD THE PAGE"
        if not self.loading and not self.error and not (g.download_methods or []) and not g.withdrawn:
            head = "NO DOWNLOAD LINKS ON THIS PAGE"
        p.drawText(QRectF(rx, top + 10 * s, rect.width(), 28 * s), Qt.AlignLeft | Qt.AlignVCenter, head)
        rect.setTop(top + 52 * s)
        self._list(p, self.items, self.focus, rect, "_ls", focused=w.zone == "main" and w.overlay is None
                   and w.keyboard is None)
        if self.loading:
            t = time.monotonic()
            for k in range(3):
                a = 0.35 + 0.65 * max(0.0, math.sin(t * 5 - k * 0.7))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, int(255 * a)))
                p.drawEllipse(QPointF(rx + 14 * s + k * 34 * s, rect.top() + (len(self.items) * 72 + 40) * s),
                              9 * s, 9 * s)
            p.setBrush(Qt.NoBrush)
            w.update_area(QRectF(rx, rect.top() + (len(self.items) * 72 + 25) * s, 120 * s, 30 * s), 60)

    def _paint_info(self, p: QPainter, title: str, source: str, facts: list[str], notes: list[tuple[str, str]],
                    desc: str, col_w: float, height: float) -> None:
        """The left column at (0, 0): source, title, facts, notes and as much of the description as fits."""
        w, s = self.w, self.s
        y = 10 * s
        p.setFont(w._font(15 * s, 800))
        p.setPen(QColor(theme.CURRENT.accent).lighter(130))
        p.drawText(QRectF(0, y, col_w, 24 * s), Qt.AlignLeft | Qt.AlignVCenter, source)
        y += 32 * s
        p.setFont(w._font(min(60 * s, w.width() * 0.04), 900))
        p.setPen(QColor("white"))
        tr = QRectF(0, y, col_w, 170 * s)
        bound = p.boundingRect(tr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, title)
        p.drawText(tr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, title)
        y += min(bound.height(), tr.height()) + 16 * s
        p.setFont(w._font(19 * s, 600))
        p.setPen(QColor(255, 255, 255, 205))
        fr = QRectF(0, y, col_w, 60 * s)
        fb = p.boundingRect(fr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, "  ·  ".join(facts))
        p.drawText(fr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, "  ·  ".join(facts))
        y += min(fb.height(), fr.height()) + 22 * s
        for color, text in notes:
            p.setFont(w._font(17 * s, 600))
            nr = QRectF(18 * s, y, col_w - 18 * s, 200 * s)
            nb = p.boundingRect(nr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, text)
            if y + nb.height() > height:
                break
            p.fillRect(QRectF(0, y + 2 * s, 5 * s, nb.height() - 4 * s), QColor(color))
            p.setPen(QColor(255, 255, 255, 225))
            p.drawText(nr, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, text)
            y += nb.height() + 14 * s
        if desc:
            font = w._font(18 * s, 500)
            p.setFont(font)
            p.setPen(QColor(255, 255, 255, 185))
            dr = QRectF(0, y + 6 * s, col_w, max(0.0, height - y - 6 * s))
            lines = int(dr.height() // QFontMetrics(font).lineSpacing())
            if lines > 0:
                p.drawText(dr, Qt.AlignLeft | Qt.AlignTop, "\n".join(wrap_lines(desc, font, dr.width(), lines)))


def wrap_lines(text: str, font: QFont, width: float, max_lines: int) -> list[str]:
    """``text`` broken into at most ``max_lines`` lines of ``width``; the last one ends with "…" when text is left
    over. Only the lines shown are laid out (a long Steam description measured word by word took half a second)."""
    layout = QTextLayout(text, font)
    layout.beginLayout()
    spans = []
    while len(spans) < max_lines:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        spans.append((line.textStart(), line.textLength()))
    layout.endLayout()
    out = [text[a:a + n].rstrip() for a, n in spans]
    if spans and spans[-1][0] + spans[-1][1] < len(text):
        out[-1] = QFontMetrics(font).elidedText(text[spans[-1][0]:spans[-1][0] + spans[-1][1] + 200], Qt.ElideRight,
                                                int(width))
    return out


# ================================================================ downloading
def _defaults(w, title: str) -> dict:
    st = w.actions.settings
    game_dir = str(Path(st.get("install_dir") or Path.home() / "Games") / safe_dirname(title))
    one = bool(st.get("downloads_in_game_folder", True))
    auto = bool(st.get("auto_extract", True))
    return {"dest_dir": game_dir if one else (st.get("download_dir") or str(Path.home() / "Downloads")),
            "auto_extract": auto, "extract_dest": game_dir if auto else None, "selected_files": None}


def _confirm_lines(prep: dict, v: dict) -> tuple[list[tuple[str, str]], str | None]:
    size = prep.get("size")
    try:
        free = free_space(Path(v["dest_dir"]))
    except OSError:
        free = None
    lines = [("File", prep.get("name") or prep.get("title") or ""),
             ("Size", format_bytes(size) if size else "known once it starts"),
             ("How", ("Torrent" if prep.get("kind") == "torrent" else "Direct download") + f" · {prep.get('method')}"),
             ("Saved to", v["dest_dir"]),
             ("Free space", format_bytes(free) if free is not None else "unknown")]
    install = prep.get("install_size") if v["auto_extract"] else None
    if size and install:
        approx = "about " if prep.get("install_size_guessed") else ""
        need = f"{approx}{format_bytes(size + install)} (download + installed game)"
        if free is not None and size <= free < size + install:
            need += " — installing it may run out of space"
        lines.append(("Needs", need))
    if prep.get("password"):
        lines.append(("Password", f"{prep['password']} (entered for you)"))
    lines.append(("Then", "Installed automatically when it finishes" if v["auto_extract"] else
                  "Kept as an archive (install it from the Library)"))
    problem = None
    if free is not None and size and size > free:
        problem = "There is not enough free space for this download."
    return lines, problem


def start_download(w, game, index: int, on_done=None) -> None:
    """Prepare → confirm (full screen) → start. Browser-only links offer the in-app browser instead."""
    from ezgd.sources.base import ManualActionRequired
    b = w.bridge
    w.busy("Preparing the download…", f"Asking {theme.source_name(game.source)} for the file.")

    def prepared(prep: dict) -> None:
        v = _defaults(w, prep.get("title") or game.title)
        lines, problem = _confirm_lines(prep, v)

        def go() -> None:
            w.busy("Starting the download…", prep.get("title") or game.title)
            b.call(b.core.start_prepared(prep["token"], v["dest_dir"], selected_files=None,
                                         auto_extract=v["auto_extract"], extract_dest=v["extract_dest"]),
                   lambda snap: _added(w, snap, on_done), lambda e: w.message("Could not start the download", str(e)),
                   owner=w)
        buttons = [] if problem else [("Download", go, "play")]
        w.show_overlay(f"Download {prep.get('title') or game.title}?", problem or "", buttons + [
            ("Cancel", w.close_overlay, "ghost")], lines=lines)

    def failed(err) -> None:
        if isinstance(err, ManualActionRequired):
            m = (game.download_methods or [None] * (index + 1))[index] if game.download_methods else None
            target = getattr(err, "url", None) or (m.url if m is not None else game.url)
            w.show_overlay("This download needs a browser", str(err), [
                ("Open in-app browser", lambda: (w.close_overlay(), open_browser(w, game, m, target)), "primary"),
                ("Cancel", w.close_overlay, "ghost")])
        else:
            w.message("Could not prepare the download", str(err) or type(err).__name__)
    b.call(b.core.prepare_download(game.id, index), prepared, failed, owner=w)


def start_download_set(w, game, indices: list[int], host: str) -> None:
    """Every part of a multi-part archive: confirm once, then queue the parts one after another."""
    b = w.bridge
    w.busy("Preparing the download…", f"{game.title}: {len(indices)} parts from {host}.")

    def queue(v: dict, rest: list[int], done: int) -> None:
        if not rest:
            w.close_overlay()
            w.toasts.show("All parts queued", f"{game.title}: {done} parts — installed once the last part is in.",
                          "success")
            return
        w.busy(f"Queuing part {done + 1} of {len(indices)}…", game.title)

        def prepared(prep: dict) -> None:
            b.call(b.core.start_prepared(prep["token"], v["dest_dir"], auto_extract=v["auto_extract"],
                                         extract_dest=v["extract_dest"]),
                   lambda snap: (w.actions.download_added.emit(snap), queue(v, rest[1:], done + 1)),
                   lambda e: w.message(f"Could not queue part {done + 1}", str(e)), owner=w)
        b.call(b.core.prepare_download(game.id, rest[0]), prepared,
               lambda e: w.message(f"Could not prepare part {done + 1}", str(e)), owner=w)

    def first(prep: dict) -> None:
        v = _defaults(w, prep.get("title") or game.title)
        lines, problem = _confirm_lines(prep, v)
        lines[0] = ("Parts", f"{len(indices)} from {host}")
        total = sum((game.download_methods[i].size_bytes or 0) for i in indices)
        if total:
            lines[1] = ("Size", format_bytes(total))

        def go() -> None:
            w.busy("Starting the download…", game.title)
            b.call(b.core.start_prepared(prep["token"], v["dest_dir"], auto_extract=v["auto_extract"],
                                         extract_dest=v["extract_dest"]),
                   lambda snap: (w.actions.download_added.emit(snap), queue(v, indices[1:], 1)),
                   lambda e: w.message("Could not start the download", str(e)), owner=w)
        w.show_overlay(f"Download {game.title}?", problem or "", ([] if problem else [("Download all parts", go,
                                                                                      "play")])
                       + [("Cancel", w.close_overlay, "ghost")], lines=lines)
    b.call(b.core.prepare_download(game.id, indices[0]), first,
           lambda e: w.message("Could not prepare the download", str(e)), owner=w)


def _added(w, snap: dict, on_done) -> None:
    w.close_overlay()
    w.toasts.show("Added to downloads", f"{snap.get('title')} — follow it in Downloads.", "success")
    w.actions.download_added.emit(snap)
    if on_done:
        on_done(snap)


def open_browser(w, game, method, target: str) -> None:
    """Browser-only links: the in-app browser (couch mode steps aside until it is closed)."""
    from ezgd.ui.dialogs import browser
    label = getattr(method, "label", "") or theme.source_name(game.source)
    if not browser.AVAILABLE:
        from ezgd.ui.dialogs.misc import open_url
        open_url(target)
        w.toasts.show("Opened in your browser", "Download it there; then import the file in the Library.", "info")
        w.step_away("browser")
        return
    dlg = browser.open_browser_download(w.actions, game, target, label, w)
    if dlg is None:
        return
    from ezgd.ui.couch_browser import CouchBrowser
    w.browser = CouchBrowser(w, dlg)                # full screen over couch mode, the controller drives it
    w.browser.start()
    if target != getattr(method, "url", target):
        w.toasts.show(f"Click “{label}” on the page", f"{label} only accepts visits from the game's page.", "info")


# ================================================================== wishlist
STATUS_TEXT = {"available": "Available", "waiting": "Not on any site yet", "closed": "Guide closed",
               "library": "In your library"}


class WishlistPage(Page):
    def __init__(self, w):
        super().__init__(w)
        self.items: list[dict] = []
        self.loaded = False
        self.focus = 0

    def activate(self) -> None:
        self.reload()

    def reload(self) -> None:
        b = self.w.bridge

        def got(items: list[dict]) -> None:
            order = {"available": 0, "library": 2, "waiting": 1, "closed": 3}
            self.items = sorted(items, key=lambda it: (order.get(it.get("status"), 9), it["title"].casefold()))
            self.loaded = True
            self.focus = max(0, min(self.focus, len(self.items)))
            self.w.load_art([it["steam_app_id"] for it in self.items if it.get("steam_app_id")])
            self.w.refresh_bg()
            self.w.update()
        b.call(b.core.wishlist(), got, lambda e: self.w.actions.fail("Wishlist", e), owner=self.w)

    def _item(self) -> dict | None:
        return self.items[self.focus - 1] if 1 <= self.focus <= len(self.items) else None

    def background(self) -> str | None:
        it = self._item()
        if it is None:
            return None
        a = self.w.art.get(it.get("steam_app_id")) or {}
        return a.get("hero") or it.get("hero_url") or it.get("cover_url")

    def command(self, name: str) -> str | None:
        n = len(self.items)
        if name in ("lt", "rt"):
            self.focus = max(0, min(n, self.focus + (5 if name == "rt" else -5)))
        elif name == "up":
            if self.focus == 0:
                return "top"
            self.focus -= 1
        elif name == "down":
            self.focus = min(n, self.focus + 1)
        elif name == "a":
            self.activate_row()
        elif name == "x" and self._item() is not None:
            self._remove(self._item())
        elif name == "b":
            return "back"
        self.w.refresh_bg()
        return None

    def hit(self, idx: int, click: bool) -> None:
        self.focus = idx
        self.w.refresh_bg()
        if click:
            self.activate_row()

    def hints(self) -> list[tuple[str, str]]:
        if self.focus == 0:
            return [("a", "Find a game"), ("b", "Back")]
        return [("a", "Options"), ("x", "Remove"), ("b", "Back")]

    def activate_row(self) -> None:
        if self.focus == 0:
            self.w.keyboard = Keyboard(self.w, "Find a game on Steam to wish for", self._steam_search,
                                       done_label="Find")
            return
        it = self._item()
        if it is None:
            return
        rows = []
        for m in [m for m in it.get("matches") or [] if not m.get("withdrawn")][:4]:
            rows.append((f"Open the game page — {theme.source_name(m['source'])}",
                         lambda m=m: self._open(m["game_id"]), "primary",
                         (f"Version {m['version']}" if m.get("version") else m.get("title") or "")))
        rows.append(("Check the sites now", lambda: self._check(it), "ghost", "Looks for it on every site"))
        rows.append(("Remove from wishlist", lambda: self._remove(it), "danger", ""))
        rows.append(("Close", self.w.close_overlay, "ghost", ""))
        self.w.menu(it["title"], rows, text=self._status(it))

    def _status(self, it: dict) -> str:
        st = it.get("status")
        if st == "available":
            names = sorted({theme.source_name(m["source"]) for m in it.get("matches") or [] if not m.get("withdrawn")})
            return "Available from " + ", ".join(names)
        if st == "waiting" and it.get("coming_soon"):
            return "Coming soon" + (f" — {it['release_text']}" if it.get("release_text") else "")
        return STATUS_TEXT.get(st, st or "")

    def _open(self, game_id: str) -> None:
        b = self.w.bridge

        def got(games: list) -> None:
            self.w.close_overlay()
            if games:
                self.w.open_game(games)
            else:
                self.w.message("Game page not found", "Check the sites again, then try once more.")
        b.call(b.core.games_by_ids([game_id]), got, lambda e: self.w.message("Could not open it", str(e)),
               owner=self.w)

    def _check(self, it: dict) -> None:
        b = self.w.bridge
        self.w.busy("Checking every site…", it["title"])

        def done(_r) -> None:
            self.w.close_overlay()
            self.reload()
        b.call(b.core.wishlist_check(it["id"], manual=True), done,
               lambda e: self.w.message("Could not check", str(e)), owner=self.w)

    def _remove(self, it: dict) -> None:
        b = self.w.bridge

        def done(_r) -> None:
            self.w.close_overlay()
            self.w.toasts.show("Removed from your wishlist", it["title"])
            self.reload()
        b.call(b.core.wishlist_remove(it["id"]), done, lambda e: self.w.message("Could not remove it", str(e)),
               owner=self.w)

    def _steam_search(self, term: str) -> None:
        if not term:
            return
        b = self.w.bridge
        self.w.busy("Searching Steam…", term)

        def got(res: list[dict]) -> None:
            if not res:
                self.w.message("Nothing found on Steam", f"No game called “{term}”. Try a shorter name.")
                return
            rows = []
            for r in res[:12]:
                price = f"{r['price']:.2f} {r.get('currency') or ''}".strip() if r.get("price") is not None else ""
                rows.append((("♥ " if r.get("wishlisted") else "") + r["name"],
                             lambda r=r: self._add(r), "ghost", "Already on your wishlist" if r.get("wishlisted")
                             else price))
            rows.append(("Close", self.w.close_overlay, "ghost", ""))
            self.w.menu(f"Steam: “{term}”", rows, text="Choose the game to add to your wishlist.")
        b.call(b.core.steam_search(term), got, lambda e: self.w.message("Steam search failed", str(e)), owner=self.w)

    def _add(self, r: dict) -> None:
        b = self.w.bridge
        self.w.busy("Adding…", r["name"])

        def done(_it) -> None:
            self.w.close_overlay()
            self.w.toasts.show("Added to your wishlist", r["name"] + " — the app checks the sites for it.", "success")
            self.reload()
        b.call(b.core.wishlist_add(steam_app_id=r["appid"], title=r["name"]), done,
               lambda e: self.w.message("Could not add it", str(e)), owner=self.w)

    def paint(self, p: QPainter) -> None:
        w = self.w
        s, W, H = self.s, w.width(), w.height()
        y = self._heading(p, "Wishlist", f"{len(self.items)} game{'s' if len(self.items) != 1 else ''} · "
                                         "the app checks the sites for them")
        left = 72 * s
        rect = QRectF(left, y + 10 * s, W - 2 * left - 30 * s, H - 76 * s - 20 * s - y - 10 * s)
        row_h = 112 * s
        n = len(self.items) + 1
        target = getattr(self, "_ws_t", 0.0)
        ft = self.focus * (row_h + 12 * s)
        if ft < target:
            target = ft
        elif ft + row_h - target > rect.height():
            target = ft + row_h - rect.height()
        self._ws_t = max(0.0, target)
        off = self._smooth("_ws", self._ws_t)
        p.save()
        p.setClipRect(rect.adjusted(-20 * s, -10 * s, 20 * s, 10 * s))
        active = w.zone == "main" and w.overlay is None and w.keyboard is None
        for k in range(n):
            r = QRectF(rect.left(), rect.top() + k * (row_h + 12 * s) - off, rect.width(), row_h)
            if r.bottom() < rect.top() - 20 * s or r.top() > rect.bottom() + 20 * s:
                continue
            focused = active and k == self.focus
            w._hit.append((r, "page", k))
            it = self.items[k - 1] if k else None
            pm, img = None, None
            if it is not None:
                a = w.art.get(it.get("steam_app_id")) or {}
                img = a.get("header") or a.get("wide") or it.get("cover_url")
                th = row_h - 28 * s
                pm = w.images.fill(img, int(th * 460 / 215), int(th), radius=10 * s)
            key = ("wish", k == 0, it and (it["title"], self._status(it), it.get("status")), img if pm else None,
                   focused, round(r.width(), 1), round(row_h, 1), round(s, 3), theme.CURRENT.success,
                   theme.CURRENT.accent)
            m = 4 * s
            w.blit(p, r.left() - m, r.top() - m, key, r.width() + 2 * m, row_h + 2 * m,
                   lambda q, it=it, pm=pm, focused=focused: self._wish_row(
                       q, it, QRectF(m, m, r.width(), row_h), focused, pm))
        p.restore()
        if self.loaded and not self.items:
            p.setFont(w._font(20 * s))
            p.setPen(QColor(255, 255, 255, 160))
            p.drawText(QRectF(left, rect.top() + row_h + 40 * s, W - 2 * left, 40 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       "Nothing here yet. Add a game, or press ♡ on a game's page in the Store.")


    def _wish_row(self, p: QPainter, it: dict | None, r: QRectF, focused: bool, pm) -> None:
        w, s = self.w, self.s
        row_h = r.height()
        path = QPainterPath()
        path.addRoundedRect(r, 18 * s, 18 * s)
        p.fillPath(path, QColor(255, 255, 255, 44 if focused else 18))
        if focused:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("white"), 3 * s))
            p.drawPath(path)
        if it is None:
            p.setFont(w._font(26 * s, 800))
            p.setPen(QColor("white"))
            p.drawText(r.adjusted(32 * s, 0, -32 * s, 0), Qt.AlignLeft | Qt.AlignVCenter, "＋   Find a game to add")
            p.setFont(w._font(17 * s, 500))
            p.setPen(QColor(255, 255, 255, 160))
            p.drawText(r.adjusted(32 * s, 0, -32 * s, 0), Qt.AlignRight | Qt.AlignVCenter,
                       "Any game on Steam — also ones not released yet")
            return
        tr = QRectF(r.left() + 14 * s, r.top() + 14 * s, (row_h - 28 * s) * 460 / 215, row_h - 28 * s)
        if pm is not None:
            p.drawImage(tr, pm, QRectF(pm.rect()))
        else:
            tp = QPainterPath()
            tp.addRoundedRect(tr, 10 * s, 10 * s)
            p.fillPath(tp, QColor(255, 255, 255, 20))
        x = tr.right() + 26 * s
        p.setFont(w._font(24 * s, 800))
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, r.top() + 18 * s, r.right() - x - 300 * s, 36 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(it["title"], Qt.ElideRight, int(r.right() - x - 300 * s)))
        p.setFont(w._font(18 * s, 600))
        st = it.get("status")
        color = {"available": theme.CURRENT.success, "library": theme.CURRENT.accent}.get(st)
        p.setPen(QColor(color) if color else QColor(255, 255, 255, 165))
        p.drawText(QRectF(x, r.top() + 58 * s, r.right() - x - 40 * s, 30 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(self._status(it), Qt.ElideRight, int(r.right() - x - 40 * s)))
        if st == "available":
            p.setFont(w._font(14 * s, 800))
            badge = "AVAILABLE"
            bw = QFontMetrics(p.font()).horizontalAdvance(badge) + 28 * s
            br = QRectF(r.right() - bw - 24 * s, r.top() + 20 * s, bw, 30 * s)
            bp = QPainterPath()
            bp.addRoundedRect(br, 15 * s, 15 * s)
            p.fillPath(bp, QColor(theme.CURRENT.success))
            p.setPen(QColor("white"))
            p.drawText(br, Qt.AlignCenter, badge)


# ================================================================== settings
TOGGLES = [
    ("Downloads", [("auto_extract", "Install games automatically when the download finishes"),
                   ("delete_archive_after_extract", "Delete the archive after installing"),
                   ("download_hours", "Only download during the download hours (set on the desktop)")]),
    ("Playing", [("confirm_before_launch", "Ask before starting a game"),
                 ("auto_backup_saves", "Back up saves automatically (after playing, before updates)"),
                 ("check_game_updates", "Check installed games for new versions"),
                 ("auto_update_games", "Install game updates automatically"),
                 ("steam_overlay", "Steam overlay in games (friends list)"),
                 ("launch_through_steam", "Full Steam overlay: start games through Steam")]),
    ("Store", [("couch_controller_only", "Only show games that work with a controller (Steam's data)"),
               ("hide_withdrawn_games", "Hide games whose guide was closed"),
               ("hide_adult_games", "Hide games marked 18+")]),
    ("App", [("couch_intro", "Intro video when couch mode opens"),
             ("couch_intro_sound", "Intro sound"),
             ("couch_gpu", "Show couch mode through the graphics card (next time it opens)"),
             ("reduce_motion", "Reduce animations"),
             ("notifications_enabled", "Desktop notifications when downloads finish"),
             ("start_with_session", "Start with the computer (in the tray)"),
             ("check_app_updates", "Look for a new version of the app once a day")]),
]
SPEEDS = [0, 1024, 2560, 5120, 10240, 25600, 51200, 102400]


def _speed_text(kbps: int) -> str:
    return "No limit" if not kbps else f"{kbps / 1024:g} MB/s"


def _mount_points() -> list[Path]:
    user = os.environ.get("USER") or ""
    out = []
    for base in (Path("/run/media") / user, Path("/media") / user, Path("/mnt"), Path("/media")):
        try:
            for p in sorted(base.iterdir()):
                if p.is_dir() and os.path.ismount(p) and os.access(p, os.W_OK):
                    out.append(p)
        except OSError:
            continue
    seen: set[str] = set()
    return [p for p in out if not (str(p) in seen or seen.add(str(p)))]


class SettingsPage(Page):
    def __init__(self, w):
        super().__init__(w)
        self.focus = 0
        self.items: list[Item] = []
        self.app: dict = {}

    def activate(self) -> None:
        b = self.w.bridge
        b.call(b.core.app_update_status(), self._app_status, lambda _e: None, owner=self.w)
        self._build()

    def _app_status(self, st: dict) -> None:
        self.app = st
        self._build()
        self.w.update()

    def _get(self, key: str):
        return self.w.actions.settings.get(key)

    def _set(self, key: str, value) -> None:
        self.w.actions.settings[key] = value
        b = self.w.bridge
        b.call(b.core.set_setting(key, value), lambda _v: None, lambda e: self.w.actions.fail("Settings", e),
               owner=self.w)
        if key == "reduce_motion":
            self.w.reduce_motion = bool(value)
        self._build()

    def _build(self) -> None:
        items: list[Item] = []
        st = self.w.actions.settings
        items.append(Item("Downloads", kind="header"))
        n = int(st.get("max_concurrent_downloads") or 3)
        items.append(Item("Downloads at the same time", lambda: self._cycle_num(1), "", "choice", str(n),
                          left=lambda: self._cycle_num(-1), right=lambda: self._cycle_num(1)))
        sp = int(st.get("http_speed_limit_kbps") or 0)
        items.append(Item("Download speed limit", lambda: self._cycle_speed(1), "", "choice", _speed_text(sp),
                          left=lambda: self._cycle_speed(-1), right=lambda: self._cycle_speed(1)))
        items.append(Item("Games folder", lambda: self._choose_folder("install_dir"), "", "row",
                          str(st.get("install_dir") or "")))
        if not st.get("downloads_in_game_folder", True):
            items.append(Item("Downloads folder", lambda: self._choose_folder("download_dir"), "", "row",
                              str(st.get("download_dir") or "")))
        items.append(Item("Pause all downloads", self._pause_all))
        items.append(Item("Resume all downloads", self._resume_all))
        self._display_rows(items, st)
        for section, keys in TOGGLES:
            if section != "Downloads":
                items.append(Item(section, kind="header"))
            for key, text in keys:
                on = bool(st.get(key))
                items.append(Item(text, lambda k=key: self._set(k, not bool(self._get(k))), "", "toggle",
                                  "on" if on else "off"))
        from ezgd import __version__
        latest = self.app.get("latest") or {}
        if self.app.get("staged") and self.app.get("can_install"):
            items.append(Item("Restart and install the update", self._install_update,
                              f"Version {latest.get('version') or ''} is downloaded and checked", "primary"))
        elif latest.get("newer") and self.app.get("can_install"):
            items.append(Item(f"Download version {latest['version']}", self._download_update,
                              f"You have {__version__}", "primary"))
        else:
            src = " (runs from source code)" if self.app and not self.app.get("packaged") else ""
            items.append(Item("Check for an app update", self._check_update, f"You have version {__version__}{src}"))
        items.append(Item(f"EZ Game Downloader {__version__}", kind="info",
                          note="Respect copyright and the terms of the sites you use."))
        old = self.items[self.focus].label if 0 <= self.focus < len(self.items) else None
        self.items = items
        same = next((k for k, it in enumerate(items) if it.label == old), None)
        self.focus = same if same is not None else self._first(items)

    # ------------------------------------------------------------ display
    def _screens(self) -> list[tuple[str, str]]:
        from PySide6.QtWidgets import QApplication
        from ezgd.ui.window_control import real_size
        out = [("", "The app's screen")]
        for sc in QApplication.screens():
            pw, ph = real_size(sc)
            out.append((sc.name(), f"{sc.name()} · {pw}×{ph}"))
        return out

    def _resolutions(self) -> list[tuple[str, str]]:
        from ezgd.ui.window_control import real_size
        screen, _geo, _mode = self.w.display_target()
        own = "%dx%d" % real_size(screen)
        common = ["1280x720", "1600x900", "1920x1080", "2560x1080", "2560x1440", "3440x1440", "3840x1600",
                  "3840x2160", "5120x1440"]
        vals = [own] + [c for c in common if c != own]
        cur = str(self._get("couch_resolution") or "")
        if cur and cur not in vals:
            vals.insert(1, cur)
        return [(v, v.replace("x", " × ") + ("  (this screen)" if v == own else "")) for v in vals]

    def _display_rows(self, items: list, st: dict) -> None:
        items.append(Item("Couch mode display", kind="header"))
        screens = self._screens()
        cur = str(st.get("couch_screen") or "")
        label = next((t for v, t in screens if v == cur), screens[0][1])
        items.append(Item("Screen", lambda: self._cycle_display("couch_screen", screens, 1), "", "choice", label,
                          left=lambda: self._cycle_display("couch_screen", screens, -1),
                          right=lambda: self._cycle_display("couch_screen", screens, 1)))
        modes = [("fullscreen", "Full screen"), ("borderless", "Borderless window"), ("custom", "Custom size")]
        mode = str(st.get("couch_window") or "fullscreen")
        items.append(Item("Window", lambda: self._cycle_display("couch_window", modes, 1),
                          "Try “Borderless window” or a custom size if couch mode does not fill your screen",
                          "choice", dict(modes).get(mode, "Full screen"),
                          left=lambda: self._cycle_display("couch_window", modes, -1),
                          right=lambda: self._cycle_display("couch_window", modes, 1)))
        if mode == "custom":
            res = self._resolutions()
            if not st.get("couch_resolution"):
                st["couch_resolution"] = res[0][0]
            cur_res = str(st.get("couch_resolution"))
            items.append(Item("Resolution", lambda: self._cycle_display("couch_resolution", res, 1), "", "choice",
                              dict(res).get(cur_res, cur_res),
                              left=lambda: self._cycle_display("couch_resolution", res, -1),
                              right=lambda: self._cycle_display("couch_resolution", res, 1)))
        sizes = [75, 90, 100, 110, 125, 150, 175, 200]
        size = int(st.get("couch_ui_scale") or 100)
        items.append(Item("Interface size", lambda: self._cycle_scale(sizes, 1), "", "choice", f"{size} %",
                          left=lambda: self._cycle_scale(sizes, -1), right=lambda: self._cycle_scale(sizes, 1)))
        on = bool(st.get("couch_steady_frames", True))
        items.append(Item("Steady frame rate (no flicker on VRR / FreeSync / G-Sync screens)",
                          lambda: (self._set("couch_steady_frames", not bool(self._get("couch_steady_frames"))),
                                   self.w._set_pace(self.w.screen())), "", "toggle", "on" if on else "off"))

    def _cycle_display(self, key: str, options: list[tuple[str, str]], d: int) -> None:
        values = [v for v, _t in options]
        cur = str(self._get(key) or values[0])
        i = values.index(cur) if cur in values else 0
        new = values[(i + d) % len(values)]
        if new == cur:
            return
        before = {k: self._get(k) for k in ("couch_screen", "couch_window", "couch_resolution")}
        self._set(key, new)
        self.w.apply_display(revert=before)           # asks "Keep these display settings?" (back in 15 s)

    def _cycle_scale(self, sizes: list[int], d: int) -> None:
        cur = int(self._get("couch_ui_scale") or 100)
        i = min(range(len(sizes)), key=lambda k: abs(sizes[k] - cur))
        self._set("couch_ui_scale", sizes[max(0, min(len(sizes) - 1, i + d))])
        self.w._fonts.clear()
        self.w._tab_fit.clear()
        self.w.images.clear()
        self.w.update()

    def _cycle_num(self, d: int) -> None:
        n = int(self._get("max_concurrent_downloads") or 3) + d
        self._set("max_concurrent_downloads", max(1, min(16, n)))

    def _cycle_speed(self, d: int) -> None:
        cur = int(self._get("http_speed_limit_kbps") or 0)
        i = min(range(len(SPEEDS)), key=lambda k: abs(SPEEDS[k] - cur))
        v = SPEEDS[(i + d) % len(SPEEDS)]
        self._set("http_speed_limit_kbps", v)
        self._set("torrent_download_limit_kbps", v)

    def _choose_folder(self, key: str) -> None:
        cur = Path(str(self._get(key) or Path.home()))
        name = "Games" if key == "install_dir" else "EZ Games"
        options = [cur, Path.home() / "Games" if key == "install_dir" else Path.home() / "Downloads" / "EZ Games"]
        options += [m / name for m in _mount_points()]
        seen: set[str] = set()
        rows = []
        for o in options:
            if str(o) in seen:
                continue
            seen.add(str(o))
            try:
                free = format_bytes(free_space(o)) + " free"
            except OSError:
                free = ""
            rows.append((str(o) + ("   ✓" if o == cur else ""), lambda o=o: self._pick(key, o), "ghost", free))
        rows.append(("Close", self.w.close_overlay, "ghost", ""))
        self.w.menu("Games folder" if key == "install_dir" else "Downloads folder", rows,
                    text="Where new games go. Games you already have stay where they are. Other drives show up here "
                         "when they are mounted.")

    def _pick(self, key: str, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.w.message("Cannot use this folder", str(exc))
            return
        self.w.close_overlay()
        self._set(key, str(path))
        self.w.toasts.show("Folder changed", str(path), "success")

    def _pause_all(self) -> None:
        b = self.w.bridge
        b.call(b.core.pause_all(), lambda _r: self.w.toasts.show("All downloads paused"), owner=self.w)

    def _resume_all(self) -> None:
        b = self.w.bridge
        b.call(b.core.resume_all(), lambda _r: self.w.toasts.show("Downloads resumed", kind="success"), owner=self.w)

    def _check_update(self) -> None:
        b = self.w.bridge
        self.w.busy("Looking for a new version…", "")

        def done(info: dict) -> None:
            self.w.close_overlay()
            if info.get("newer"):
                self.w.toasts.show("App update available", f"Version {info['version']}", "success")
            else:
                self.w.toasts.show("You have the newest version", info.get("current") or "")
            b.call(b.core.app_update_status(), self._app_status, lambda _e: None, owner=self.w)
        b.call(b.core.check_app_update(True), done, lambda e: self.w.message("Could not check for an update", str(e)),
               owner=self.w)

    def _download_update(self) -> None:
        b = self.w.bridge
        self.w.busy("Downloading the update…", "It is checked against its published SHA-256 before anything is "
                                               "installed.")

        def done(_r) -> None:
            self.w.close_overlay()
            b.call(b.core.app_update_status(), self._app_status, lambda _e: None, owner=self.w)
        b.call(b.core.download_app_update(), done, lambda e: self.w.message("Could not download the update", str(e)),
               owner=self.w)

    def _install_update(self) -> None:
        def go() -> None:
            b = self.w.bridge
            self.w.busy("Restarting…", "The app closes, swaps in the new version and starts again.")

            def done(_r) -> None:
                win = self.w.actions.parent()
                if hasattr(win, "force_quit"):
                    win.force_quit()
            b.call(b.core.install_app_update(sys.argv[1:]), done,
                   lambda e: self.w.message("Could not install the update", str(e)), owner=self.w)
        self.w.show_overlay("Restart and update?", "The app closes, swaps in the new version and starts again. "
                            "Downloads pause and continue after the restart. Games keep running.",
                            [("Restart and update", go, "primary"), ("Cancel", self.w.close_overlay, "ghost")])

    def command(self, name: str) -> str | None:
        r = self._list_command(self.items, "focus", name)
        return r

    def hit(self, idx: int, click: bool) -> None:
        if 0 <= idx < len(self.items) and self.items[idx].focusable:
            self.focus = idx
            if click and self.items[idx].action:
                self.items[idx].action()

    def hints(self) -> list[tuple[str, str]]:
        it = self.items[self.focus] if 0 <= self.focus < len(self.items) else None
        if it is not None and it.kind == "choice":
            return [("lr", "Change"), ("b", "Back")]
        if it is not None and it.kind == "toggle":
            return [("a", "Switch on/off"), ("b", "Back")]
        return [("a", "Select"), ("b", "Back")]

    def paint(self, p: QPainter) -> None:
        w = self.w
        s, W, H = self.s, w.width(), w.height()
        if not self.items:
            self._build()
        y = self._heading(p, "Settings", "Changes apply right away")
        cw = min(W - 144 * s, 1500 * s)
        rect = QRectF((W - cw) / 2, y + 6 * s, cw, H - 76 * s - 24 * s - y - 6 * s)
        self._list(p, self.items, self.focus, rect, "_ss", focused=w.zone == "main" and w.overlay is None
                   and w.keyboard is None, row_h=None)
