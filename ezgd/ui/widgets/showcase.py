"""Auto-rotating, scrollbar-free game showcase (Spotlight Stage + centered card shelf) for Home and OnlineFix Picks."""

from __future__ import annotations

import html
import math
import re
import time
from collections import OrderedDict

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ezgd.catalog.models import GameGroup
from ezgd.core.fsutil import format_bytes
from ezgd.ui import ratings, theme
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import button, label, relative_time
from ezgd.ui.widgets.game_grid import GameCardDelegate, GameListModel, _top_rounded

SHELF_WIDTHS = {"compact": 182, "comfortable": 216, "large": 258}
ROTATE_MS = 3000
USER_HOLD_MS = 4800
MAX_SHOWCASE = 24
_TAG_RE = re.compile(r"<[^>]+>")


def _cover_url(group: GameGroup) -> str | None:
    return group.primary.cover_url or next((e.cover_url for e in group.entries if e.cover_url), None)


def _clean_blurb(text: str | None) -> str:
    if not text:
        return ""
    plain = html.unescape(_TAG_RE.sub(" ", text))
    return " ".join(plain.split())


def _group_tooltip(g: GameGroup) -> str:
    lines = [g.title, "Available from: " + ", ".join(theme.source_name(s) for s in g.sources)]
    for s in g.sources:
        pw = theme.source_password(s)
        if pw:
            lines.append(f"🔒 {theme.source_name(s)} archives are password-protected (password: {pw})")
    return "\n".join(lines)


def clear_now(layout) -> None:
    """Remove and hide layout items immediately so replaced rows never overlap for a frame."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_now(item.layout())


class ElidedLabel(QLabel):
    """Single-line label that elides smoothly instead of forcing its parent layout wider."""

    def __init__(self, text: str = "", role: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        if role:
            self.setProperty("role", role)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(20)
        self.setText(text)

    def full_text(self) -> str:
        return self._full

    def setText(self, text: str) -> None:                   # noqa: N802 (Qt API)
        self._full = text or ""
        self._apply_elide()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_elide()

    def _apply_elide(self) -> None:
        avail = max(20, self.width())
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full, Qt.ElideRight, avail)
        super().setText(elided)
        if elided != self._full and not self.toolTip():
            super().setToolTip(self._full)
        elif elided == self._full and self.toolTip() == self._full:
            super().setToolTip("")


class _PageDots(QWidget):
    """Clickable capsule indicator pills for the auto-rotating showcase pages."""

    page_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._count = 1
        self._current = 0
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(20)
        self._sync_width()

    def set_pages(self, count: int, current: int) -> None:
        count = max(1, count)
        current = max(0, min(count - 1, current))
        if (count, current) == (self._count, self._current):
            return
        self._count = count
        self._current = current
        self._sync_width()
        self.update()

    def _sync_width(self) -> None:
        w = 24 + max(0, self._count - 1) * 8 + max(0, self._count - 1) * 6 + 4
        self.setFixedWidth(max(28, w))
        self.setVisible(self._count > 1)

    def _rects(self) -> list[QRectF]:
        rects: list[QRectF] = []
        x = 2.0
        y = (self.height() - 7.0) / 2.0
        for i in range(self._count):
            w = 22.0 if i == self._current else 7.0
            rects.append(QRectF(x, y, w, 7.0))
            x += w + 6.0
        return rects

    def _hit(self, pos) -> int:
        pt = QPointF(pos)
        for i, r in enumerate(self._rects()):
            if r.adjusted(-3, -5, 3, 5).contains(pt):
                return i
        return -1

    def mouseMoveEvent(self, e) -> None:
        h = self._hit(e.position())
        if h != self._hover:
            self._hover = h
            self.update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            h = self._hit(e.position())
            if h >= 0:
                self.page_clicked.emit(h)
                e.accept()
                return
        super().mousePressEvent(e)

    def paintEvent(self, _e) -> None:
        pal = theme.CURRENT
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        for i, r in enumerate(self._rects()):
            if i == self._current:
                c = QColor(pal.accent)
            elif i == self._hover:
                c = QColor(pal.muted)
            else:
                c = QColor(pal.border)
                c.setAlpha(210)
            p.setBrush(c)
            p.drawRoundedRect(r, 3.5, 3.5)
        p.end()


class _SpotlightStage(QWidget):
    """Large featured stage card showing the active game."""

    activated = Signal(object)
    STAGE_H = 252

    def __init__(self, covers, installed_fn, reduce_motion_fn, parent: QWidget | None = None):
        super().__init__(parent)
        self.covers = covers
        self._installed = installed_fn
        self._reduce_motion = reduce_motion_fn
        self.ratings = ratings.store(covers.bridge)
        self._group: GameGroup | None = None
        self._prev_group: GameGroup | None = None
        self._blend = 1.0
        self._hovered = False
        self._cta_rect = QRectF()
        self._cta_hovered = False
        self._lock_dark: QPixmap | None = None
        self._dl_icon: tuple[str, QPixmap] | None = None
        self._chk_icon: QPixmap | None = None
        self._cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self.setFixedHeight(self.STAGE_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._anim.finished.connect(self._on_anim_done)

    def _on_anim(self, val) -> None:
        self._blend = float(val)
        self.update()

    def _on_anim_done(self) -> None:
        self._prev_group = None
        self._blend = 1.0
        self.update()

    def invalidate(self) -> None:
        self._cache.clear()
        self.update()

    def set_group(self, group: GameGroup | None, *, animate: bool = True) -> None:
        old = self._group
        if old is group or (old is not None and group is not None and old.key == group.key
                            and len(old.entries) == len(group.entries)):
            self._group = group
            self.update()
            return
        self._anim.stop()
        if animate and not self._reduce_motion() and old is not None and group is not None and self.isVisible():
            self._prev_group = old
            self._group = group
            self._blend = 0.0
            self._anim.start()
        else:
            self._prev_group = None
            self._group = group
            self._blend = 1.0
            self.update()
        self.setToolTip(_group_tooltip(group) if group is not None else "")

    @staticmethod
    def _finish_cover(img: QImage, w: int, h: int, dpr: float) -> QImage:
        """Build the crisp rounded 16:9 spotlight cover in a worker thread (preserving portrait box art cleanly)."""
        pw, ph = max(1, round(w * dpr)), max(1, round(h * dpr))
        out = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
        out.fill(Qt.transparent)
        q = QPainter(out)
        q.setRenderHint(QPainter.Antialiasing)
        q.setRenderHint(QPainter.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0.5 * dpr, 0.5 * dpr, pw - dpr, ph - dpr), 13 * dpr, 13 * dpr)
        q.setClipPath(clip)

        bg = img.scaled(QSize(pw, ph), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        q.drawImage(0, 0, bg, (bg.width() - pw) // 2, (bg.height() - ph) // 2, pw, ph)

        if img.width() < img.height() * 1.12:
            q.fillRect(QRectF(0, 0, pw, ph), QColor(8, 10, 16, 190))
            pad = round(10 * dpr)
            fg = img.scaled(QSize(max(1, pw - pad * 2), max(1, ph - pad * 2)),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
            fx, fy = (pw - fg.width()) / 2.0, (ph - fg.height()) / 2.0
            inner = QPainterPath()
            inner.addRoundedRect(QRectF(fx, fy, fg.width(), fg.height()), 8 * dpr, 8 * dpr)
            q.save()
            q.setClipPath(inner)
            q.drawImage(QPointF(fx, fy), fg)
            q.restore()
            q.setPen(QPen(QColor(255, 255, 255, 42), max(1.0, dpr)))
            q.setBrush(Qt.NoBrush)
            q.drawRoundedRect(QRectF(fx, fy, fg.width(), fg.height()), 8 * dpr, 8 * dpr)
        else:
            shade = QLinearGradient(0, ph * 0.62, 0, ph)
            shade.setColorAt(0, QColor(0, 0, 0, 0))
            shade.setColorAt(1, QColor(0, 0, 0, 105))
            q.fillRect(QRectF(0, 0, pw, ph), QBrush(shade))

        q.end()
        out.setDevicePixelRatio(dpr)
        return out

    def _lock_pm(self) -> QPixmap:
        if self._lock_dark is None:
            self._lock_dark = icon("lock", "#0b0d12", 13).pixmap(26, 26)
        return self._lock_dark

    def _cta_icon(self, installed: bool) -> QPixmap:
        if installed:
            if self._chk_icon is None:
                self._chk_icon = icon("check", "#ffffff", 16).pixmap(32, 32)
            return self._chk_icon
        key = theme.CURRENT.accent
        if self._dl_icon is None or self._dl_icon[0] != key:
            self._dl_icon = (key, icon("download", "#ffffff", 16).pixmap(32, 32))
        return self._dl_icon[1]

    def _render_slide(self, group: GameGroup, w: int, h: int, dpr: float, cta_hover: bool) -> tuple[QPixmap, QRectF]:
        pal = theme.CURRENT
        installed = group.key in self._installed()
        url = _cover_url(group)
        ch = h - 32
        cw = min(round(ch * 16 / 9), max(240, round(w * 0.44)))
        pm = self.covers.variant(url, ("home_stage", url, cw, ch, dpr),
                                 lambda img, cw=cw, ch=ch, dpr=dpr: self._finish_cover(img, cw, ch, dpr))
        rating = self.ratings.get(group)
        if rating is None:
            self.ratings.want((group,))

        rx = 16 + cw + 26
        cta_label = "In Library — View Details" if installed else "View Game & Download"
        bf = QFont(self.font())
        bf.setPointSizeF(10.0)
        bf.setBold(True)
        cta_w = QFontMetrics(bf).horizontalAdvance(cta_label) + 46
        cta_rect = QRectF(rx, h - 54, cta_w, 38)

        key = (group.key, group.title, tuple(group.sources), w, h, dpr, pm.cacheKey() if pm else None,
               installed, cta_hover, pal.accent, pal.bg2, rating and rating["label"])
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached, cta_rect

        out = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        stage_path = QPainterPath()
        stage_path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 18, 18)
        p.setClipPath(stage_path)

        aura_hex = theme.SOURCE_COLORS.get(group.primary.source, pal.accent)
        aura_col = QColor(aura_hex)
        aura_col.setAlpha(34)
        glow = QLinearGradient(0, 0, w * 0.72, h)
        glow.setColorAt(0.0, aura_col)
        mid = QColor(pal.accent)
        mid.setAlpha(16)
        glow.setColorAt(0.45, mid)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, w, h), QBrush(glow))

        cover = QRectF(16, 16, cw, ch)
        if pm is not None:
            p.drawPixmap(QPointF(16, 16), pm)
        else:
            p.save()
            cpath = QPainterPath()
            cpath.addRoundedRect(cover, 13, 13)
            p.setClipPath(cpath)
            grad = QLinearGradient(cover.topLeft(), cover.bottomRight())
            grad.setColorAt(0, QColor(pal.accent).darker(250))
            grad.setColorAt(1, QColor(pal.bg3))
            p.fillRect(cover, QBrush(grad))
            ifont = QFont(self.font())
            ifont.setPointSize(28)
            ifont.setBold(True)
            p.setFont(ifont)
            p.setPen(QColor(255, 255, 255, 68))
            initials = "".join(x[0] for x in group.title.split()[:2] if x).upper() or "?"
            p.drawText(cover, Qt.AlignCenter, initials)
            p.restore()

        p.setPen(QPen(QColor(255, 255, 255, 24), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(cover.adjusted(0.5, 0.5, -0.5, -0.5), 13, 13)

        rw = max(140, w - rx - 24)
        bx = float(rx)
        by = 20.0
        cf = QFont(self.font())
        cf.setPointSizeF(8.2)
        cf.setBold(True)
        p.setFont(cf)
        cfm = QFontMetrics(cf)

        for src in group.sources[:4]:
            sc = QColor(theme.SOURCE_COLORS.get(src, pal.accent))
            stext = theme.source_name(src)
            locked = bool(theme.source_password(src))
            bw = cfm.horizontalAdvance(stext) + 16 + (14 if locked else 0)
            if bx + bw > rx + rw - 90:
                break
            pill = QRectF(bx, by, bw, 21)
            bg = QColor(sc)
            bg.setAlpha(232)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(pill, 6.5, 6.5)
            p.setPen(QColor("#0b0d12"))
            p.drawText(pill.adjusted(8, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, stext)
            if locked:
                p.drawPixmap(QRectF(pill.right() - 18, pill.top() + 4, 13, 13).toRect(), self._lock_pm())
            bx += bw + 6

        if installed and bx + 92 <= rx + rw:
            itxt = "✓ In Library"
            iw = cfm.horizontalAdvance(itxt) + 16
            pill = QRectF(bx, by, iw, 21)
            ic = QColor(pal.success)
            ic.setAlpha(45)
            p.setPen(QPen(QColor(pal.success), 1))
            p.setBrush(ic)
            p.drawRoundedRect(pill, 6.5, 6.5)
            p.setPen(QColor(pal.success))
            p.drawText(pill, Qt.AlignCenter, itxt)
            bx += iw + 6

        if rating is not None and rating["level"] != "few":
            from ezgd.ui.ratings import LEVEL_COLORS
            rtxt = rating["label"]
            rw_pill = cfm.horizontalAdvance(rtxt) + 24
            if bx + rw_pill <= rx + rw:
                pill = QRectF(bx, by, rw_pill, 21)
                p.setPen(QPen(QColor(pal.border), 1))
                p.setBrush(QColor(11, 13, 18, 195))
                p.drawRoundedRect(pill, 6.5, 6.5)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(LEVEL_COLORS[rating["level"]]))
                p.drawEllipse(QRectF(pill.left() + 7, pill.center().y() - 3, 6, 6))
                p.setPen(QColor(pal.text))
                p.drawText(pill.adjusted(17, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, rtxt)
                bx += rw_pill + 6

        if group.sort_time > 0:
            fresh = relative_time(group.sort_time)
            ff = QFont(self.font())
            ff.setPointSizeF(8.5)
            p.setFont(ff)
            ffm = QFontMetrics(ff)
            fw = ffm.horizontalAdvance(fresh)
            if bx + fw + 10 < rx + rw:
                p.setPen(QColor(pal.muted))
                p.drawText(QRectF(rx + rw - fw, by, fw, 21), Qt.AlignRight | Qt.AlignVCenter, fresh)

        tf = QFont(self.font())
        tf.setPointSizeF(18.5)
        tf.setBold(True)
        p.setFont(tf)
        tfm = QFontMetrics(tf)
        max_title_h = tfm.lineSpacing() * 2 + 4
        elided_title = tfm.elidedText(group.title, Qt.ElideRight, int(rw * 1.92))
        tbox = tfm.boundingRect(QRectF(rx, 50, rw, max_title_h).toRect(),
                                Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, elided_title)
        th = min(max_title_h, max(tfm.lineSpacing(), tbox.height()))
        p.setPen(QColor(pal.text))
        p.drawText(QRectF(rx, 50, rw, th + 2), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, elided_title)

        g = group.primary
        meta_parts: list[str] = []
        if group.genres:
            meta_parts.append(" · ".join(group.genres[:3]))
        if g.version:
            meta_parts.append(g.version if len(g.version) <= 28 else g.version[:26] + "…")
        elif g.release_date:
            meta_parts.append(g.release_date)
        if g.size_text:
            meta_parts.append(g.size_text)
        elif g.size_bytes:
            meta_parts.append(format_bytes(g.size_bytes))
        if g.extra.get("official_servers"):
            meta_parts.append("Official servers")
        elif g.extra.get("network"):
            meta_parts.append(f"via {g.extra['network']}")
        meta_line = ("   •   ".join(meta_parts) if meta_parts
                     else f"From {', '.join(theme.source_name(s) for s in group.sources)}")

        my = 50 + th + 8
        mf = QFont(self.font())
        mf.setPointSizeF(9.6)
        mf.setWeight(QFont.DemiBold)
        p.setFont(mf)
        mfm = QFontMetrics(mf)
        p.setPen(QColor(pal.accent_hover))
        p.drawText(QRectF(rx, my, rw, mfm.lineSpacing()), Qt.AlignLeft | Qt.AlignVCenter,
                   mfm.elidedText(meta_line, Qt.ElideRight, int(rw)))

        dy = my + mfm.lineSpacing() + 8
        avail_h = cta_rect.top() - dy - 10
        if avail_h >= 18:
            blurb = _clean_blurb(g.description)
            if not blurb:
                srcs = ", ".join(theme.source_name(s) for s in group.sources)
                has_pw = any(theme.source_password(s) for s in group.sources)
                tail = "Archive password is entered automatically during extraction." if has_pw else (
                    "Inspect download mirrors, torrents, and compatibility details.")
                blurb = f"Listed on {srcs}. {tail}"
            df = QFont(self.font())
            df.setPointSizeF(9.3)
            p.setFont(df)
            dfm = QFontMetrics(df)
            lines = max(1, min(2, int(avail_h // max(1, dfm.lineSpacing()))))
            elided_desc = dfm.elidedText(blurb, Qt.ElideRight, int(rw * (lines - 0.08)))
            mc = QColor(pal.muted)
            mc.setAlpha(225)
            p.setPen(mc)
            p.drawText(QRectF(rx, dy, rw, lines * dfm.lineSpacing() + 4),
                       Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, elided_desc)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal.accent_hover if cta_hover else pal.accent))
        p.drawRoundedRect(cta_rect, 10, 10)
        ic_pm = self._cta_icon(installed)
        p.drawPixmap(QRectF(cta_rect.left() + 14, cta_rect.center().y() - 8, 16, 16).toRect(), ic_pm)
        p.setFont(bf)
        p.setPen(QColor("#ffffff"))
        p.drawText(cta_rect.adjusted(36, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, cta_label)

        if group.is_duplicate:
            sf = QFont(self.font())
            sf.setPointSizeF(8.8)
            p.setFont(sf)
            p.setPen(QColor(pal.muted))
            note = f"{len(group.entries)} sources available"
            nx = cta_rect.right() + 14
            if nx + 80 < rx + rw:
                p.drawText(QRectF(nx, cta_rect.top(), rx + rw - nx, cta_rect.height()),
                           Qt.AlignLeft | Qt.AlignVCenter, note)

        p.end()
        self._cache[key] = out
        while len(self._cache) > 8:
            self._cache.popitem(last=False)
        return out, cta_rect

    def enterEvent(self, e) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hovered = False
        if self._cta_hovered:
            self._cta_hovered = False
        self.update()
        super().leaveEvent(e)

    def mouseMoveEvent(self, e) -> None:
        over = self._cta_rect.contains(e.position())
        if over != self._cta_hovered:
            self._cta_hovered = over
            self.update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()) and self._group is not None:
            self.activated.emit(self._group)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and self._group is not None:
            self.activated.emit(self._group)
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e) -> None:
        pal = theme.CURRENT
        w, h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, w - 1, h - 1)

        shell = QLinearGradient(0, 0, w, h)
        shell.setColorAt(0.0, QColor(pal.bg2))
        shell.setColorAt(1.0, QColor(pal.bg1))
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        p.fillPath(path, QBrush(shell))

        if self._group is None:
            f = QFont(self.font())
            f.setPointSizeF(10.5)
            p.setFont(f)
            p.setPen(QColor(pal.muted))
            p.drawText(r, Qt.AlignCenter, "Loading games…")
        else:
            if self._prev_group is not None and self._blend < 0.999:
                prev_pm, _ = self._render_slide(self._prev_group, w, h, dpr, False)
                p.save()
                p.setOpacity(max(0.0, 1.0 - self._blend))
                p.drawPixmap(QPointF(0, 0), prev_pm)
                p.restore()
            curr_pm, self._cta_rect = self._render_slide(self._group, w, h, dpr, self._cta_hovered or self._hovered)
            p.save()
            p.setOpacity(self._blend)
            p.drawPixmap(QPointF(0, 0), curr_pm)
            p.restore()

        border = QColor(pal.accent if (self._hovered or self.hasFocus()) else pal.border)
        if self._hovered and not self.hasFocus():
            border.setAlpha(155)
        p.setPen(QPen(border, 1.3 if (self._hovered or self.hasFocus()) else 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 18, 18)
        p.end()


class _ShelfStrip(QWidget):
    """Centered, scrollbar-free row of complete game cards.

    Hovering a card NEVER changes the highlighted/spotlighted game; only clicking another card selects it,
    and clicking the already-highlighted card opens its details dialog.
    """

    card_clicked = Signal(int)
    layout_metrics_changed = Signal()

    GAP = 14
    BODY_H = 68

    def __init__(self, covers, installed_fn, parent: QWidget | None = None):
        super().__init__(parent)
        self.covers = covers
        self._installed = installed_fn
        self.ratings = ratings.store(covers.bridge)
        self._items: list[GameGroup] = []
        self._active = 0
        self._hover_slot = -1
        self._card_key = "comfortable"
        self._lock_pm: QPixmap | None = None
        self._cards: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._sync_height()

    def invalidate(self) -> None:
        self._cards.clear()
        self.update()

    def set_card_size(self, key: str) -> None:
        if key not in SHELF_WIDTHS:
            key = "comfortable"
        if key != self._card_key:
            self._card_key = key
            self._sync_height()
            self.layout_metrics_changed.emit()
            self.update()

    def set_state(self, items: list[GameGroup], active: int) -> None:
        self._items = items
        self._active = max(0, min(active, len(items) - 1)) if items else 0
        self._sync_tooltip()
        self.update()

    def cols(self) -> int:
        avail = max(240, self.width() - 4)
        base = SHELF_WIDTHS[self._card_key]
        return max(1, min(5, (avail + self.GAP) // (base + self.GAP)))

    def _card_dims(self) -> tuple[int, int, int]:
        c = self.cols()
        avail = max(240, self.width() - 4)
        base = SHELF_WIDTHS[self._card_key]
        w = max(150, min(int(base * 1.14), (avail - (c - 1) * self.GAP) // c))
        ch = round(w * 0.5625)
        return w, ch + self.BODY_H, ch

    def _sync_height(self) -> None:
        _w, h, _ch = self._card_dims()
        self.setFixedHeight(h + 18)

    def page_slice(self) -> tuple[int, list[GameGroup]]:
        total = len(self._items)
        if total == 0:
            return 0, []
        c = self.cols()
        if total <= c:
            return 0, self._items
        start = min((self._active // c) * c, total - c)
        return start, self._items[start:start + c]

    def _slot_rects(self) -> list[tuple[int, QRectF]]:
        start, vis = self.page_slice()
        if not vis:
            return []
        w, h, _ch = self._card_dims()
        n = len(vis)
        row_w = n * w + (n - 1) * self.GAP
        x0 = (self.width() - row_w) / 2.0
        out: list[tuple[int, QRectF]] = []
        for s in range(n):
            gidx = start + s
            y = 2.0 if gidx == self._active else 6.0
            out.append((gidx, QRectF(x0 + s * (w + self.GAP), y, w, h)))
        return out

    def _hit_slot(self, pos) -> tuple[int, int]:
        pt = QPointF(pos)
        for s, (gidx, r) in enumerate(self._slot_rects()):
            if r.adjusted(-2, -4, 2, 10).contains(pt):
                return s, gidx
        return -1, -1

    def _sync_tooltip(self) -> None:
        rects = self._slot_rects()
        if 0 <= self._hover_slot < len(rects):
            gidx = rects[self._hover_slot][0]
            if 0 <= gidx < len(self._items):
                self.setToolTip(_group_tooltip(self._items[gidx]))
                return
        self.setToolTip("")

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._sync_height()
        self.layout_metrics_changed.emit()

    def mouseMoveEvent(self, e) -> None:
        slot, _gidx = self._hit_slot(e.position())
        if slot != self._hover_slot:
            self._hover_slot = slot
            self._sync_tooltip()
            self.update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:
        if self._hover_slot != -1:
            self._hover_slot = -1
            self.setToolTip("")
            self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            _slot, gidx = self._hit_slot(e.position())
            if gidx >= 0:
                self.card_clicked.emit(gidx)
                e.accept()
                return
        super().mouseReleaseEvent(e)

    def _lock_pixmap(self) -> QPixmap:
        if self._lock_pm is None:
            self._lock_pm = icon("lock", "#0b0d12", 12).pixmap(24, 24)
        return self._lock_pm

    def _render_card(self, group: GameGroup, w: int, h: int, ch: int, dpr: float) -> QPixmap:
        pal = theme.CURRENT
        installed = group.key in self._installed()
        url = _cover_url(group)
        pm = self.covers.variant(url, ("card", url, w, ch, dpr),
                                 lambda img, w=w, ch=ch, dpr=dpr: GameCardDelegate._finish(img, w, ch, dpr))
        rating = self.ratings.get(group)
        if rating is None:
            self.ratings.want((group,))
        meta = GameCardDelegate._meta(group)
        key = (group.key, group.title, tuple(group.sources), meta, w, h, dpr, pm.cacheKey() if pm else None,
               installed, pal.accent, pal.bg2, rating and rating["label"])
        cached = self._cards.get(key)
        if cached is not None:
            self._cards.move_to_end(key)
            return cached

        out = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        r = QRectF(0, 0, w, h)
        cpath = QPainterPath()
        cpath.addRoundedRect(r, 12, 12)
        p.fillPath(cpath, QColor(pal.bg2))

        cover = QRectF(0, 0, w, ch)
        if pm is not None:
            p.drawPixmap(QPointF(0, 0), pm)
        else:
            p.save()
            p.setClipPath(_top_rounded(cover.adjusted(0.5, 0.5, -0.5, 0), 11.5))
            grad = QLinearGradient(cover.topLeft(), cover.bottomRight())
            grad.setColorAt(0, QColor(pal.accent).darker(260))
            grad.setColorAt(1, QColor(pal.bg3))
            p.fillRect(cover, QBrush(grad))
            f = QFont(self.font())
            f.setPointSize(18)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 68))
            initials = "".join(x[0] for x in group.title.split()[:2] if x).upper() or "?"
            p.drawText(cover, Qt.AlignCenter, initials)
            p.restore()

        f = QFont(self.font())
        f.setPointSizeF(7.5)
        f.setBold(True)
        p.setFont(f)
        bfm = QFontMetrics(f)
        x = 7.0
        limit = w - (34 if installed else 7)
        sources = list(group.sources)
        for k, src in enumerate(sources):
            color = QColor(theme.SOURCE_COLORS.get(src, pal.accent))
            text = theme.source_name(src)
            locked = bool(theme.source_password(src))
            bw = bfm.horizontalAdvance(text) + 12 + (13 if locked else 0)
            more = bfm.horizontalAdvance(f"+{len(sources) - k - 1}") + 12 if k < len(sources) - 1 else 0
            if x + bw + (more + 4 if more else 0) > limit:
                text, locked, color = f"+{len(sources) - k}", False, QColor(pal.muted)
                bw = bfm.horizontalAdvance(text) + 12
                if x + bw > limit:
                    break
            badge = QRectF(x, 7, bw, 18)
            bg = QColor(color)
            bg.setAlpha(232)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(badge, 5.5, 5.5)
            p.setPen(QColor("#0b0d12"))
            p.drawText(badge.adjusted(6, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
            if locked:
                p.drawPixmap(QRectF(badge.right() - 16, badge.top() + 3, 12, 12).toRect(), self._lock_pixmap())
            x += bw + 4
            if text.startswith("+"):
                break

        if installed:
            ib = QRectF(w - 28, 7, 20, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.success))
            p.drawEllipse(ib)
            p.setPen(QPen(QColor("white"), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            c = ib.center()
            chk = QPainterPath()
            chk.moveTo(c.x() - 4.5, c.y())
            chk.lineTo(c.x() - 1.2, c.y() + 3.2)
            chk.lineTo(c.x() + 4.5, c.y() - 3.2)
            p.setBrush(Qt.NoBrush)
            p.drawPath(chk)

        if rating is not None and rating["level"] != "few":
            from ezgd.ui.ratings import LEVEL_COLORS
            text = rating["label"]
            bw = bfm.horizontalAdvance(text) + 22
            pill = QRectF(w - bw - 7, ch - 25, bw, 18)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(11, 13, 18, 215))
            p.drawRoundedRect(pill, 5.5, 5.5)
            p.setBrush(QColor(LEVEL_COLORS[rating["level"]]))
            p.drawEllipse(QRectF(pill.left() + 6, pill.center().y() - 2.8, 5.6, 5.6))
            p.setPen(QColor("white"))
            p.drawText(pill.adjusted(15, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)

        body = QRectF(10, ch + 7, w - 20, h - ch - 13)
        tf = QFont(self.font())
        tf.setPointSizeF(9.6)
        tf.setBold(True)
        p.setFont(tf)
        p.setPen(QColor(pal.text))
        tfm = QFontMetrics(tf)
        title = tfm.elidedText(group.title, Qt.ElideRight, int(body.width() * 1.9))
        p.drawText(QRectF(body.left(), body.top(), body.width(), tfm.lineSpacing() * 2 + 2),
                   Qt.TextWordWrap | Qt.AlignTop | Qt.AlignLeft, title)

        mf = QFont(self.font())
        mf.setPointSizeF(8.2)
        p.setFont(mf)
        p.setPen(QColor(pal.muted))
        mfm = QFontMetrics(mf)
        p.drawText(QRectF(body.left(), body.bottom() - mfm.lineSpacing(), body.width(), mfm.lineSpacing()),
                   Qt.AlignLeft | Qt.AlignVCenter, mfm.elidedText(meta, Qt.ElideRight, int(body.width())))
        p.end()

        self._cards[key] = out
        while len(self._cards) > 64:
            self._cards.popitem(last=False)
        return out

    def paintEvent(self, _e) -> None:
        pal = theme.CURRENT
        w, h, ch = self._card_dims()
        dpr = self.devicePixelRatioF()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        slots = self._slot_rects()
        if not slots:
            p.end()
            return

        for s, (gidx, rect) in enumerate(slots):
            group = self._items[gidx]
            active = gidx == self._active
            hovered = s == self._hover_slot
            pm = self._render_card(group, w, h, ch, dpr)

            if active:
                glow = QColor(pal.accent)
                glow.setAlpha(44)
                p.setPen(QPen(glow, 4.0))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 13, 13)

            p.save()
            if not active and not hovered:
                p.setOpacity(0.88)
            p.drawPixmap(QPointF(round(rect.left() * dpr) / dpr, round(rect.top() * dpr) / dpr), pm)
            p.restore()

            if active:
                pen_col = QColor(pal.accent)
                pen_w = 2.0
            elif hovered:
                # Subtle neutral border cue only — never steals the active accent highlight
                pen_col = QColor(pal.muted)
                pen_col.setAlpha(165)
                pen_w = 1.2
            else:
                pen_col = QColor(pal.border)
                pen_w = 1.0
            p.setPen(QPen(pen_col, pen_w))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, 12, 12)

            if active:
                bar_w = min(40.0, rect.width() * 0.28)
                bar = QRectF(rect.center().x() - bar_w / 2.0, rect.bottom() + 5.0, bar_w, 3.5)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(pal.accent))
                p.drawRoundedRect(bar, 1.8, 1.8)

        p.end()


class GameShowcase(QWidget):
    """Auto-rotating, scrollbar-free centerpiece (Spotlight Stage + centered card row)."""

    activated_group = Signal(object)

    def __init__(self, actions, title_text: str = "Recently added",
                 trailing_button: tuple[str, str, object] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.actions = actions
        self.covers = actions.covers
        self.model_ = GameListModel(self)
        self._installed_keys: set[str] = set()
        self.index = 0
        self._pinned = False
        self._last_wheel = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(2, 0, 2, 0)
        hdr.setSpacing(10)
        accent_bar = QFrame()
        accent_bar.setFixedSize(4, 20)
        accent_bar.setStyleSheet(f"background: {theme.CURRENT.accent}; border-radius: 2px;")
        self._accent_bar = accent_bar
        hdr.addWidget(accent_bar, 0, Qt.AlignVCenter)

        self.title_label = label(title_text)
        self.title_label.setStyleSheet("font-size: 15pt; font-weight: 750;")
        hdr.addWidget(self.title_label, 0, Qt.AlignVCenter)

        self.note_label = ElidedLabel("", "small", self)
        hdr.addWidget(self.note_label, 1, Qt.AlignVCenter)

        self.counter = label("", "small")
        hdr.addWidget(self.counter, 0, Qt.AlignVCenter)

        self.dots = _PageDots(self)
        self.dots.page_clicked.connect(self._jump_page)
        hdr.addWidget(self.dots, 0, Qt.AlignVCenter)

        self.prev_btn = self._step_btn("‹", "Previous game (or Shift + mouse wheel)", -1)
        self.next_btn = self._step_btn("›", "Next game (or Shift + mouse wheel)", 1)
        hdr.addWidget(self.prev_btn, 0, Qt.AlignVCenter)
        hdr.addWidget(self.next_btn, 0, Qt.AlignVCenter)
        if trailing_button is not None:
            b_text, b_icon, b_cb = trailing_button
            hdr.addWidget(button(b_text, b_icon, "ghost", on_click=b_cb), 0, Qt.AlignVCenter)
        lay.addLayout(hdr)

        self.stage = _SpotlightStage(self.covers, lambda: self._installed_keys, self._reduce_motion, self)
        self.stage.activated.connect(self.activated_group.emit)
        lay.addWidget(self.stage)

        self.shelf = _ShelfStrip(self.covers, lambda: self._installed_keys, self)
        self.shelf.card_clicked.connect(self._on_card_clicked)
        self.shelf.layout_metrics_changed.connect(self._sync_controls)
        lay.addWidget(self.shelf)

        self.auto_timer = QTimer(self)
        self.auto_timer.setInterval(ROTATE_MS)
        self.auto_timer.timeout.connect(self._auto_step)

        self.covers.ready.connect(self._on_media_updated)
        self.stage.ratings.changed.connect(self._on_media_updated)
        self._sync_view(animate=False)

    def _step_btn(self, glyph: str, tip: str, delta: int) -> QToolButton:
        b = QToolButton(self)
        b.setText(glyph)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(30, 30)
        b.setStyleSheet("QToolButton { font-size: 15pt; font-weight: 700; padding: 0px 0px 2px 0px; border-radius: 8px; }")
        b.clicked.connect(lambda _=False, d=delta: self.step(d, by_user=True))
        return b

    def _reduce_motion(self) -> bool:
        return bool(self.actions.settings.get("reduce_motion"))

    def visible_items(self) -> list[GameGroup]:
        return self.model_.groups[:MAX_SHOWCASE]

    def _on_media_updated(self, *_args) -> None:
        self.stage.invalidate()
        self.shelf.invalidate()

    def _prefetch_neighbors(self) -> None:
        items = self.visible_items()
        if not items:
            return
        for off in (0, 1, 2):
            u = _cover_url(items[(self.index + off) % len(items)])
            if u:
                self.covers.get(u)

    def _sync_timer(self, *, hold_user: bool = False) -> None:
        win = self.window()
        minimized = bool(win is not None and win.isMinimized())
        should_run = (self.isVisible() and not minimized
                      and not self._reduce_motion() and len(self.visible_items()) > 1)
        if should_run:
            if hold_user:
                self.auto_timer.start(USER_HOLD_MS)
            elif not self.auto_timer.isActive():
                self.auto_timer.start(ROTATE_MS)
        else:
            self.auto_timer.stop()

    def _auto_step(self) -> None:
        if not self.isVisible() or (self.window() is not None and self.window().isMinimized()):
            self.auto_timer.stop()
            return
        if self.auto_timer.interval() != ROTATE_MS:
            self.auto_timer.setInterval(ROTATE_MS)
        self.step(1, by_user=False)

    def step(self, delta: int, *, by_user: bool = True) -> None:
        items = self.visible_items()
        if not items:
            return
        self.set_index((self.index + delta) % len(items), by_user=by_user)

    def _jump_page(self, page: int) -> None:
        items = self.visible_items()
        if not items:
            return
        c = self.shelf.cols()
        self.set_index(min(len(items) - 1, page * c), by_user=True)

    def _on_card_clicked(self, idx: int) -> None:
        """Clicking another card selects/spotlights it; clicking the already-highlighted card opens details."""
        items = self.visible_items()
        if not (0 <= idx < len(items)):
            return
        if idx != self.index:
            self.set_index(idx, by_user=True, animate=True)
        else:
            self.activated_group.emit(items[idx])

    def set_index(self, idx: int, *, by_user: bool = False, animate: bool = True) -> None:
        items = self.visible_items()
        if not items:
            self.index = 0
            self._sync_view(animate=False)
            return
        idx = max(0, min(len(items) - 1, idx))
        self._pinned = True
        changed = idx != self.index
        self.index = idx
        self._sync_view(animate=animate and changed, hold_user=by_user)

    def _sync_controls(self) -> None:
        self._accent_bar.setStyleSheet(f"background: {theme.CURRENT.accent}; border-radius: 2px;")
        items = self.visible_items()
        n = len(items)
        c = self.shelf.cols()
        pages = max(1, math.ceil(n / c)) if n else 1
        cur_page = min(pages - 1, self.index // c) if n else 0
        self.dots.set_pages(pages, cur_page)
        self.counter.setText(f"{self.index + 1} / {n}" if n > 1 else "")
        many = n > 1
        self.prev_btn.setEnabled(many)
        self.next_btn.setEnabled(many)

    def _sync_view(self, *, animate: bool = True, hold_user: bool = False) -> None:
        items = self.visible_items()
        group = items[self.index] if items and 0 <= self.index < len(items) else None
        self.stage.set_group(group, animate=animate)
        self.shelf.set_state(items, self.index)
        self._sync_controls()
        self._prefetch_neighbors()
        self._sync_timer(hold_user=hold_user)

    def set_groups(self, groups: list[GameGroup]) -> None:
        prev = self.visible_items()
        keep_key = prev[self.index].key if (self._pinned and prev and 0 <= self.index < len(prev)) else None
        self.model_.set_groups(groups)
        items = self.visible_items()
        self.stage.ratings.want(items)
        if keep_key is not None:
            found = next((i for i, g in enumerate(items) if g.key == keep_key), None)
            self.index = found if found is not None else min(self.index, max(0, len(items) - 1))
        else:
            self.index = 0
        self._sync_view(animate=False)

    def set_card_size(self, key: str) -> None:
        self.shelf.set_card_size(key)
        self._sync_timer()

    def hide_ids(self, ids: set[str]) -> None:
        groups = []
        for g in self.model_.groups:
            entries = [e for e in g.entries if e.id not in ids]
            if entries:
                groups.append(type(g)(g.key, entries))
        if len(groups) != len(self.model_.groups) or any(
                len(a.entries) != len(b.entries) for a, b in zip(groups, self.model_.groups)):
            self.set_groups(groups)

    def set_installed(self, keys: set[str]) -> None:
        self._installed_keys = set(keys)
        self.stage.invalidate()
        self.shelf.invalidate()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._sync_controls()
        self._sync_timer()

    def hideEvent(self, e) -> None:
        self.auto_timer.stop()
        super().hideEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Left:
            self.step(-1, by_user=True)
            e.accept()
            return
        if e.key() == Qt.Key_Right:
            self.step(1, by_user=True)
            e.accept()
            return
        super().keyPressEvent(e)

    def wheelEvent(self, event) -> None:
        """Plain vertical wheel scrolls the page; Shift+wheel or sideways swipe steps the showcase."""
        dx, dy = event.angleDelta().x(), event.angleDelta().y()
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if shift or abs(dx) > abs(dy):
            delta = dy if (shift and abs(dy) > abs(dx)) else dx
            now = time.monotonic()
            if delta != 0 and now - self._last_wheel >= 0.16:
                self._last_wheel = now
                self.step(-1 if delta > 0 else 1, by_user=True)
            event.accept()
            return
        event.ignore()


RecentShowcase = GameShowcase
