"""Small building blocks shared by pages and dialogs."""

from __future__ import annotations

import time

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy,
                               QSpinBox, QToolButton, QVBoxLayout, QWidget)

from ezgd.ui import theme
from ezgd.ui.icons import icon


def button(text: str = "", icon_name: str | None = None, variant: str | None = None, tooltip: str | None = None,
           on_click=None) -> QPushButton:
    b = QPushButton(text)
    if icon_name:
        b.setIcon(icon(icon_name, "#ffffff" if variant == "primary" else theme.CURRENT.text))
        b.setIconSize(QSize(16, 16))
    if variant:
        b.setProperty("variant", variant)
    if tooltip:
        b.setToolTip(tooltip)
    if on_click:
        # call without Qt's "checked" flag: handlers written as lambda k=value: … would otherwise get False as k
        b.clicked.connect(lambda _checked=False: on_click())
    b.setCursor(Qt.PointingHandCursor)
    return b


def tool(icon_name: str, tooltip: str, on_click=None, color: str | None = None) -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(icon_name, color or theme.CURRENT.text))
    b.setIconSize(QSize(18, 18))
    b.setToolTip(tooltip)
    b.setAutoRaise(True)
    b.setCursor(Qt.PointingHandCursor)
    if on_click:
        b.clicked.connect(lambda _checked=False: on_click())
    return b


def spin_box() -> QSpinBox:
    """A number box with its text inset like the other fields (Qt ignores a spin box's horizontal padding)."""
    w = QSpinBox()
    w.lineEdit().setTextMargins(8, 0, 0, 0)
    return w


def label(text: str = "", role: str | None = None, wrap: bool = False, selectable: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    return lab


class PathLabel(QLabel):
    """A file path that never forces its layout wider: shortened in the middle ("/home/…/Game.exe") when there is
    no room, the full path in its tooltip; right-click copies it."""

    def __init__(self, path: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._full = path or "—"
        self.setToolTip(self._full if path else "")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        if path:
            from PySide6.QtGui import QAction, QGuiApplication
            act = QAction("Copy path", self)
            act.triggered.connect(lambda: QGuiApplication.clipboard().setText(self._full))
            self.addAction(act)
        self._update()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._update()

    def _update(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, max(40, self.width())))

    def sizeHint(self) -> QSize:
        return QSize(min(self.fontMetrics().horizontalAdvance(self._full), 520), super().sizeHint().height())


class FlowLayout(QLayout):
    """Lays its items out left to right and wraps to the next line when the width runs out (Qt's flow layout)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:                    # noqa: N802 (Qt API)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):                                # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):                                # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):                      # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:                # noqa: N802
        return True

    def heightForWidth(self, w: int) -> int:            # noqa: N802
        return self._layout(w, False)

    def setGeometry(self, rect) -> None:                # noqa: N802
        super().setGeometry(rect)
        self._layout(rect.width(), True, rect.x(), rect.y())

    def sizeHint(self) -> QSize:                        # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:                     # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, width: int, apply: bool, x0: int = 0, y0: int = 0) -> int:
        from PySide6.QtCore import QPoint, QRect
        x, y, line = x0, y0, 0
        for it in self._items:
            if it.widget() is not None and not it.widget().isVisible() and it.widget().isHidden():
                continue
            hint = it.sizeHint()
            if x > x0 and x + hint.width() > x0 + width:
                x, y, line = x0, y + line + self._spacing, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - y0


def chip(text: str, color: str) -> QLabel:
    c = QLabel(text)
    c.setProperty("role", "chip")
    c.setStyleSheet(theme.chip_style(color))
    c.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return c


def set_chip(c: QLabel, text: str, color: str) -> None:
    c.setText(text)
    c.setStyleSheet(theme.chip_style(color))


def card(layout_cls=QVBoxLayout, margins=(16, 14, 16, 14), spacing=10) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = layout_cls(f)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return f, lay


def hline() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {theme.CURRENT.border};")
    return f


class Banner(QFrame):
    """Inline message with optional action buttons."""

    def __init__(self, kind: str = "info", text: str = "", parent=None):
        super().__init__(parent)
        self.setProperty("banner", kind)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(10)
        self.icon = QLabel()
        self._kind = kind
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.icon, 0, Qt.AlignTop)
        lay.addWidget(self.text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        lay.addLayout(self.actions)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.setProperty("banner", kind)
        color = {"info": theme.CURRENT.info, "warning": theme.CURRENT.warning, "error": theme.CURRENT.danger,
                 "success": theme.CURRENT.success}.get(kind, theme.CURRENT.info)
        name = {"warning": "warning", "error": "warning", "success": "check"}.get(kind, "info")
        self.icon.setPixmap(icon(name, color, 18).pixmap(18, 18))
        self.style().unpolish(self)
        self.style().polish(self)

    def set_text(self, text: str, kind: str | None = None) -> None:
        if kind:
            self.set_kind(kind)
        self.text.setText(text)

    def add_action(self, text: str, on_click, icon_name: str | None = None) -> QPushButton:
        b = button(text, icon_name, "ghost", on_click=on_click)
        self.actions.addWidget(b)
        return b

    def clear_actions(self) -> None:
        while self.actions.count():
            w = self.actions.takeAt(0).widget()
            if w:
                w.deleteLater()


class EmptyState(QWidget):
    def __init__(self, icon_name: str, title: str, text: str = "", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(8)
        ic = QLabel()
        ic.setPixmap(icon(icon_name, theme.CURRENT.muted, 48).pixmap(48, 48))
        ic.setAlignment(Qt.AlignCenter)
        self.title = label(title, "h2")
        self.title.setAlignment(Qt.AlignCenter)
        self.text = label(text, "muted", wrap=True)
        self.text.setAlignment(Qt.AlignCenter)
        self.text.setMaximumWidth(520)
        lay.addWidget(ic)
        lay.addWidget(self.title)
        lay.addWidget(self.text, 0, Qt.AlignCenter)
        self.buttons = QHBoxLayout()
        self.buttons.setAlignment(Qt.AlignCenter)
        lay.addLayout(self.buttons)

    def set(self, title: str, text: str = "") -> None:
        self.title.setText(title)
        self.text.setText(text)


class SectionHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label(title, "h2")
        col.addWidget(self.title)
        self.subtitle = label(subtitle, "muted", wrap=True)
        if subtitle:
            col.addWidget(self.subtitle)
        lay.addLayout(col, 1)
        self.right = QHBoxLayout()
        self.right.setSpacing(6)
        lay.addLayout(self.right)


class Toast(QFrame):
    closed = Signal()

    def __init__(self, title: str, text: str, kind: str, parent: QWidget, reduce_motion: bool = False,
                 action: tuple | None = None):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setFixedWidth(400)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 10, 12)
        color = {"error": theme.CURRENT.danger, "warning": theme.CURRENT.warning,
                 "success": theme.CURRENT.success}.get(kind, theme.CURRENT.accent)
        ic = QLabel()
        ic.setPixmap(icon({"error": "warning", "warning": "warning", "success": "check"}.get(kind, "info"),
                          color, 20).pixmap(20, 20))
        lay.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = label(title)
        t.setStyleSheet("font-weight: 700;")
        col.addWidget(t)
        if text:
            body = label(text, "muted", wrap=True, selectable=True)
            col.addWidget(body)
        if action:                                  # (button text, callback), e.g. "Open Library"
            text_, cb = action
            b = button(text_, variant="ghost")

            def run(_=False, cb=cb):
                self.dismiss()
                cb()
            b.clicked.connect(run)
            col.addWidget(b, 0, Qt.AlignLeft)
        lay.addLayout(col, 1)
        lay.addWidget(tool("close", "Dismiss", self.dismiss), 0, Qt.AlignTop)
        # wrapped text needs the real height for this width (adjustSize alone cut long messages off)
        self.setFixedHeight(max(56, lay.heightForWidth(400) if lay.hasHeightForWidth() else self.sizeHint().height()))
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(1.0)
        self._reduce = reduce_motion
        self._hover = False
        # long messages stay longer; errors longest; a hovered toast waits until the mouse leaves
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: None if self._hover else self.dismiss())
        self._timer.start((10000 if kind == "error" else 6000) + min(20000, 45 * len(text)))

    def enterEvent(self, e) -> None:
        self._hover = True
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        if not self._timer.isActive():
            self._timer.start(2500)
        super().leaveEvent(e)

    def dismiss(self) -> None:
        if self._reduce:
            self._finish()
            return
        anim = QPropertyAnimation(self._effect, b"opacity", self)
        anim.setDuration(250)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self._finish)
        anim.start()

    def _finish(self) -> None:
        self.hide()
        self.closed.emit()
        self.deleteLater()


class ToastHost:
    """Stacks toasts in the bottom-right corner of a window."""

    def __init__(self, window: QWidget):
        self.window = window
        self.toasts: list[Toast] = []
        self.reduce_motion = False
        self._recent: dict[str, float] = {}

    history: list[dict] = []                     # every notification of this session (Notifications list)
    history_changed = None                        # set by the main window: refreshes the bell's count

    def show(self, title: str, text: str = "", kind: str = "info", action: tuple | None = None) -> None:
        key = f"{title}|{text}"
        now = time.monotonic()
        if now - self._recent.get(key, 0) < 3:          # collapse duplicates
            return
        self._recent[key] = now
        ToastHost.history.append({"time": time.time(), "title": title, "text": text, "kind": kind})
        del ToastHost.history[:-200]
        if ToastHost.history_changed:
            ToastHost.history_changed()
        t = Toast(title, text, kind, self.window, self.reduce_motion, action)
        t.closed.connect(lambda t=t: self._remove(t))
        self.toasts.append(t)
        if len(self.toasts) > 4:
            self.toasts[0].dismiss()
        t.show()
        t.raise_()
        self.relayout()

    def _remove(self, t: Toast) -> None:
        if t in self.toasts:
            self.toasts.remove(t)
        self.relayout()

    def relayout(self) -> None:
        y = self.window.height() - 18
        for t in reversed(self.toasts):
            t.adjustSize()
            y -= t.height()
            t.move(self.window.width() - t.width() - 18, y)
            y -= 10


class PasswordBanner(Banner):
    """'From <source> · archives are password-protected · password [Copy]'."""

    def __init__(self, source: str | None, password: str, auto: bool = True, parent=None, browser: bool = False):
        super().__init__("warning", "", parent)
        self.icon.setPixmap(icon("lock", theme.CURRENT.warning, 18).pixmap(18, 18))
        if browser:       # downloaded in the browser: the user may well unpack it themselves
            tail = (" You need it to unpack the file you download in your browser (WinRAR / 7-Zip / Ark). "
                    "If you use “Import downloaded file” instead, the app enters it for you.")
        elif auto:
            tail = (" The app enters it automatically when it extracts the download; you only need it if "
                    "you unpack the archive yourself.")
        else:
            tail = ""
        self.text.setTextFormat(Qt.RichText)
        self.text.setText(f"<b>Every {theme.source_name(source)} archive is password-protected.</b> "
                          f"Password: <code style='font-size:11pt'>{password}</code>.{tail}")
        self.password = password
        self.add_action("Copy password", self._copy, "link")

    def _copy(self) -> None:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(self.password)


def source_chip(source_id: str | None) -> QLabel:
    return chip(theme.source_name(source_id), theme.SOURCE_COLORS.get(source_id or "", theme.CURRENT.accent))


def relative_time(ts: float | None) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 86400 * 2:
        return "yesterday"
    if d < 86400 * 30:
        return f"{int(d // 86400)} days ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())
