"""Game card grid (model/delegate based, so thousands of items stay smooth)."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QApplication, QListView, QStyle,
                               QStyledItemDelegate)

from ezgd.catalog.models import Game, GameGroup
from ezgd.ui import theme
from ezgd.ui.icons import icon

CARD_WIDTHS = {"compact": 196, "comfortable": 234, "large": 284}
GroupRole = Qt.UserRole + 1


class GameListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups: list[GameGroup] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.groups)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.groups):
            return None
        g = self.groups[index.row()]
        if role == Qt.DisplayRole:
            return g.title
        if role == GroupRole:
            return g
        if role == Qt.ToolTipRole:
            lines = [g.title, "Available from: " + ", ".join(theme.source_name(s) for s in g.sources)]
            for s in g.sources:
                pw = theme.source_password(s)
                if pw:
                    lines.append(f"🔒 {theme.source_name(s)} archives are password-protected (password: {pw})")
            return "\n".join(lines)
        return None

    def set_groups(self, groups: list[GameGroup]) -> None:
        self.beginResetModel()
        self.groups = list(groups)
        self.endResetModel()

    def set_games(self, games: list[Game]) -> None:
        self.set_groups([GameGroup(g.dedup_key, [g]) for g in games])


class GameCardDelegate(QStyledItemDelegate):
    def __init__(self, covers, parent=None):
        super().__init__(parent)
        self.covers = covers
        self.installed: set[str] = set()
        self.card_width = CARD_WIDTHS["comfortable"]

    def card_size(self) -> QSize:
        w = self.card_width
        return QSize(w + 14, int(w * 0.5625) + 84)

    def sizeHint(self, option, index) -> QSize:
        return self.card_size()

    def paint(self, p: QPainter, option, index) -> None:
        group: GameGroup = index.data(GroupRole)
        if group is None:
            return
        pal = theme.CURRENT
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(option.rect).adjusted(7, 7, -7, -7)
        hover = bool(option.state & QStyle.State_MouseOver)
        selected = bool(option.state & QStyle.State_Selected)
        if hover:
            r.translate(0, -2)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        p.fillPath(path, QColor(pal.bg2))
        p.setPen(QPen(QColor(pal.accent if (hover or selected) else pal.border), 1.4 if hover else 1))
        p.drawPath(path)

        cover_h = r.width() * 0.5625
        cover = QRectF(r.left(), r.top(), r.width(), cover_h)
        # rounded top corners, square bottom edge (union — not odd-even — of the two shapes)
        rounded = QPainterPath()
        rounded.addRoundedRect(cover.adjusted(0.5, 0.5, -0.5, 0), 11.5, 11.5)
        bottom = QPainterPath()
        bottom.addRect(cover.adjusted(0.5, cover_h / 2, -0.5, 0))
        p.save()
        p.setClipPath(rounded.united(bottom))
        url = group.primary.cover_url or next((e.cover_url for e in group.entries if e.cover_url), None)
        pm = self.covers.get(url)
        if pm is not None and not pm.isNull():
            scaled = pm.scaled(cover.size().toSize(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            sx = (scaled.width() - cover.width()) / 2
            sy = (scaled.height() - cover.height()) / 2
            p.drawPixmap(cover.toRect(), scaled, QRect(int(sx), int(sy), int(cover.width()), int(cover.height())))
        else:
            grad = QLinearGradient(cover.topLeft(), cover.bottomRight())
            grad.setColorAt(0, QColor(pal.accent).darker(260))
            grad.setColorAt(1, QColor(pal.bg3))
            p.fillRect(cover, QBrush(grad))
            f = QFont(option.font)
            f.setPointSize(22)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 70))
            initials = "".join(w[0] for w in group.title.split()[:2] if w).upper() or "?"
            p.drawText(cover, Qt.AlignCenter, initials)
        shade = QLinearGradient(cover.bottomLeft() - (cover.bottomLeft() - cover.topLeft()) * 0.35, cover.bottomLeft())
        shade.setColorAt(0, QColor(0, 0, 0, 0))
        shade.setColorAt(1, QColor(0, 0, 0, 120))
        p.fillRect(cover, QBrush(shade))
        p.restore()

        # source badges: full site names, plus a lock for sources whose archives are password-protected
        f = QFont(option.font)
        f.setPointSizeF(7.8)
        f.setBold(True)
        p.setFont(f)
        bfm = QFontMetrics(f)
        x = cover.left() + 8
        for src in group.sources:
            color = QColor(theme.SOURCE_COLORS.get(src, pal.accent))
            text = theme.source_name(src)
            locked = bool(theme.source_password(src))
            w = bfm.horizontalAdvance(text) + 14 + (14 if locked else 0)
            badge = QRectF(x, cover.top() + 8, w, 19)
            bg = QColor(color)
            bg.setAlpha(232)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(badge, 6, 6)
            p.setPen(QColor("#0b0d12"))
            p.drawText(badge.adjusted(7, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
            if locked:
                lock_rect = QRectF(badge.right() - 18, badge.top() + 3, 13, 13)
                p.drawPixmap(lock_rect.toRect(), icon("lock", "#0b0d12", 13).pixmap(26, 26))
            x += w + 5
        if group.key in self.installed:
            badge = QRectF(cover.right() - 30, cover.top() + 8, 22, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.success))
            p.drawEllipse(badge)
            p.setPen(QPen(QColor("white"), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            c = badge.center()
            check = QPainterPath()
            check.moveTo(c.x() - 5, c.y())
            check.lineTo(c.x() - 1.5, c.y() + 3.5)
            check.lineTo(c.x() + 5, c.y() - 3.5)
            p.setBrush(Qt.NoBrush)
            p.drawPath(check)

        # text
        body = QRectF(r.left() + 12, cover.bottom() + 9, r.width() - 24, r.bottom() - cover.bottom() - 14)
        tf = QFont(option.font)
        tf.setPointSizeF(10.2)
        tf.setBold(True)
        p.setFont(tf)
        p.setPen(QColor(pal.text))
        fm = QFontMetrics(tf)
        title = fm.elidedText(group.title, Qt.ElideRight, int(body.width() * 2 - 20))
        title_rect = QRectF(body.left(), body.top(), body.width(), fm.lineSpacing() * 2 + 2)
        p.drawText(title_rect, Qt.TextWordWrap | Qt.AlignTop | Qt.AlignLeft, title)
        mf = QFont(option.font)
        mf.setPointSizeF(8.6)
        p.setFont(mf)
        p.setPen(QColor(pal.muted))
        meta = self._meta(group)
        mfm = QFontMetrics(mf)
        p.drawText(QRectF(body.left(), body.bottom() - mfm.lineSpacing(), body.width(), mfm.lineSpacing()),
                   Qt.AlignLeft | Qt.AlignVCenter, mfm.elidedText(meta, Qt.ElideRight, int(body.width())))
        p.restore()

    @staticmethod
    def _meta(group: GameGroup) -> str:
        g = group.primary
        parts = []
        if group.genres:
            parts.append(", ".join(group.genres[:2]))
        if g.version:
            parts.append(g.version if len(g.version) < 22 else g.version[:20] + "…")
        elif g.release_date:
            parts.append(g.release_date)
        if not parts:
            parts.append(theme.source_name(g.source))
            if g.extra.get("official_servers"):
                parts.append("official servers")
            elif g.extra.get("network"):
                parts.append(f"via {g.extra['network']}")
        return " · ".join(parts)


class GameGridView(QListView):
    activated_group = Signal(object)
    near_bottom = Signal()

    def __init__(self, covers, horizontal: bool = False, parent=None):
        super().__init__(parent)
        self.model_ = GameListModel(self)
        self.setModel(self.model_)
        self.delegate = GameCardDelegate(covers, self)
        self.setItemDelegate(self.delegate)
        self.horizontal = horizontal
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setSpacing(0)
        self.setCursor(Qt.PointingHandCursor)
        if horizontal:
            self.setFlow(QListView.LeftToRight)
            self.setWrapping(False)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self._fit_height()
            self._arrows = [self._arrow("‹", -1), self._arrow("›", 1)]
            self.horizontalScrollBar().valueChanged.connect(lambda _v: self._place_arrows())
            self.horizontalScrollBar().rangeChanged.connect(lambda _a, _b: self._place_arrows())
            # the row's scroll bar is for clicking and dragging only: the wheel over it scrolls the page
            self.horizontalScrollBar().installEventFilter(self)
            self._style_row_bar()
        self.clicked.connect(self._emit)
        self.activated.connect(self._emit)
        self.verticalScrollBar().valueChanged.connect(self._check_bottom)
        covers.ready.connect(lambda _url: self.viewport().update())

    ROW_BAR = 14                                 # height of a row's drag bar (px)

    def _fit_height(self) -> None:
        # cards + room for the row's drag bar below them, so the bar never covers the cards
        self.setFixedHeight(self.delegate.card_size().height() + 16 + (self.ROW_BAR - 6 if self.horizontal else 0))

    def _style_row_bar(self) -> None:
        """A row's scroll bar you can see and grab: wider handle, lighter colour, accent while hovered/dragged."""
        p = theme.CURRENT
        handle = QColor(p.muted)
        handle.setAlpha(120)
        rgba = f"rgba({handle.red()},{handle.green()},{handle.blue()},{handle.alpha()})"
        sb = self.horizontalScrollBar()
        sb.setCursor(Qt.ArrowCursor)
        sb.setToolTip("Drag to scroll this row (the mouse wheel scrolls the page)")
        sb.setStyleSheet(
            f"QScrollBar:horizontal {{ background: {p.bg2}; height: {self.ROW_BAR}px; margin: 4px 0 0 0; "
            f"border-radius: 6px; }}"
            f"QScrollBar::handle:horizontal {{ background: {rgba}; border-radius: 5px; min-width: 80px; "
            f"margin: 2px; }}"
            f"QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{ "
            f"background: {p.accent}; }}"
            "QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }")

    def set_card_size(self, key: str) -> None:
        self.delegate.card_width = CARD_WIDTHS.get(key, CARD_WIDTHS["comfortable"])
        if self.horizontal:
            self._fit_height()
        self.model_.layoutChanged.emit()

    def set_groups(self, groups) -> None:
        """Replace the items but keep the scroll position (incremental loading appends pages)."""
        sb = self.horizontalScrollBar() if self.horizontal else self.verticalScrollBar()
        pos = sb.value()
        self.model_.set_groups(groups)
        if pos:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: sb.setValue(min(pos, sb.maximum())))

    def hide_ids(self, ids: set[str]) -> None:
        """Drop games (by id) that turned out to be withdrawn by their source."""
        groups = []
        for g in self.model_.groups:
            entries = [e for e in g.entries if e.id not in ids]
            if entries:
                groups.append(type(g)(g.key, entries))
        if len(groups) != len(self.model_.groups) or any(
                len(a.entries) != len(b.entries) for a, b in zip(groups, self.model_.groups)):
            self.set_groups(groups)

    def set_installed(self, keys: set[str]) -> None:
        self.delegate.installed = keys
        self.viewport().update()

    def _emit(self, index) -> None:
        g = index.data(GroupRole)
        if g is not None:
            self.activated_group.emit(g)

    def _check_bottom(self, value: int) -> None:
        sb = self.verticalScrollBar()
        if sb.maximum() > 0 and value >= sb.maximum() - 300:
            self.near_bottom.emit()

    def wheelEvent(self, event) -> None:
        """Rows never take the page's vertical scrolling: the wheel scrolls the page; Shift+wheel or a sideways
        touchpad swipe scrolls the row (2026-10-11: the wheel used to scroll rows sideways, so scrolling down
        Home kept getting stuck on them)."""
        if self.horizontal:
            dx, dy = event.angleDelta().x(), event.angleDelta().y()
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            if shift or abs(dx) > abs(dy):
                sb = self.horizontalScrollBar()
                sb.setValue(sb.value() - (dy if shift and abs(dy) > abs(dx) else dx))
                event.accept()
            else:
                event.ignore()                   # let the page's scroll area handle it
            return
        super().wheelEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if self.horizontal and event.type() == QEvent.Wheel and obj is self.horizontalScrollBar():
            dx, dy = event.angleDelta().x(), event.angleDelta().y()
            if event.modifiers() & Qt.ShiftModifier or abs(dx) > abs(dy):
                self.wheelEvent(event)           # deliberate sideways scrolling still moves the row
                return True
            page = self._page_scroll_area()
            if page is not None:                 # plain wheel: straight to the page, never the bar
                QApplication.sendEvent(page.viewport(), event)
            return True
        return super().eventFilter(obj, event)

    def _page_scroll_area(self) -> QAbstractScrollArea | None:
        w = self.parentWidget()
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                return w
            w = w.parentWidget()
        return None

    # ------------------------------------------------------------------ row arrows
    def _arrow(self, text: str, direction: int):
        from PySide6.QtWidgets import QToolButton
        from ezgd.ui import theme
        b = QToolButton(self)
        b.setText(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip("Scroll this row (or Shift + mouse wheel)")
        b.setFixedSize(36, 64)
        b.setStyleSheet(f"QToolButton {{ background: rgba(10,12,18,0.82); color: {theme.CURRENT.text}; "
                        f"border: 1px solid {theme.CURRENT.border}; border-radius: 10px; font-size: 22pt; }}"
                        f"QToolButton:hover {{ background: {theme.CURRENT.accent}; color: white; }}")
        b.clicked.connect(lambda: self._scroll_by(direction))
        b.hide()
        return b

    def _scroll_by(self, direction: int) -> None:
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        sb = self.horizontalScrollBar()
        target = max(0, min(sb.maximum(), sb.value() + direction * int(self.viewport().width() * 0.8)))
        anim = QPropertyAnimation(sb, b"value", self)
        anim.setDuration(260)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(sb.value())
        anim.setEndValue(target)
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _place_arrows(self) -> None:
        if not getattr(self, "_arrows", None):
            return
        sb = self.horizontalScrollBar()
        y = max(0, (self.viewport().height() - 64) // 2)
        left, right = self._arrows
        left.move(4, y)
        right.move(self.width() - right.width() - 4, y)
        left.setVisible(sb.maximum() > 0 and sb.value() > 0)
        right.setVisible(sb.maximum() > 0 and sb.value() < sb.maximum())
        left.raise_()
        right.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_arrows()
