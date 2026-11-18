"""Seamless desktop window bar & frameless resize grip for MainWindow.

Replaces the desktop environment's separate title bar so the sidebar (``bg1``) and page canvas (``bg0``) run all the
way to the top edge as one unified window, while keeping native compositor dragging, double-click maximize, right-click
window menu, and 8-edge / corner resizing (``QWindow.startSystemMove`` / ``startSystemResize``).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMenu, QPushButton, QWidget

from ezgd.ui import theme
from ezgd.ui.icons import icon

BAR_HEIGHT = 32
BTN_WIDTH = 44
EDGE_PX = 5
CORNER_PX = 10
DRAG_THRESHOLD = 4


def toggle_maximize(win: QWidget) -> None:
    if win.isFullScreen():
        win.showNormal()
    elif win.isMaximized():
        win.showNormal()
    else:
        win.showMaximized()


class CaptionButton(QPushButton):
    """Minimal top-right window control button (minimize, maximize/restore, close)."""

    def __init__(self, obj_name: str, icon_name: str, tooltip: str, on_click, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._hovered = False
        self.setObjectName(obj_name)
        self.setFixedSize(BTN_WIDTH, BAR_HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setIconSize(QSize(16, 16))
        self.setToolTip(tooltip)
        self.clicked.connect(lambda _checked=False: on_click())
        self.refresh_icon()

    def set_icon_name(self, name: str, tooltip: str) -> None:
        self._icon_name = name
        self.setToolTip(tooltip)
        self.refresh_icon()

    def refresh_icon(self) -> None:
        color = "#ffffff" if self._hovered else theme.CURRENT.muted
        self.setIcon(icon(self._icon_name, color, 18))

    def enterEvent(self, e) -> None:                          # noqa: N802
        self._hovered = True
        self.refresh_icon()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:                          # noqa: N802
        self._hovered = False
        self.refresh_icon()
        super().leaveEvent(e)


class DragStrip(QWidget):
    """Top strip that drags the window (native compositor move, with manual fallback), toggles maximize on
    double-click, and opens a window menu on right-click."""

    def __init__(self, win: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        self._press_global: QPoint | None = None
        self._win_origin: QPoint | None = None
        self._manual_drag = False

    def mousePressEvent(self, e) -> None:                     # noqa: N802
        if e.button() == Qt.LeftButton:
            self._press_global = e.globalPosition().toPoint()
            self._win_origin = self.win.frameGeometry().topLeft()
            self._manual_drag = False
            e.accept()
            return
        if e.button() == Qt.RightButton:
            self._show_menu(e.globalPosition().toPoint())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:                      # noqa: N802
        if not (e.buttons() & Qt.LeftButton):
            self._press_global = None
            self._manual_drag = False
            return
        gp = e.globalPosition().toPoint()
        if self._manual_drag and self._win_origin is not None and self._press_global is not None:
            self.win.move(self._win_origin + (gp - self._press_global))
            e.accept()
            return
        if self._press_global is not None and (gp - self._press_global).manhattanLength() >= DRAG_THRESHOLD:
            wh = self.win.windowHandle()
            if wh is not None and wh.startSystemMove():
                self._press_global = None
            else:
                if self.win.isMaximized():
                    self.win.showNormal()
                    self._win_origin = gp - QPoint(int(self.win.width() * 0.5), BAR_HEIGHT // 2)
                    self.win.move(self._win_origin)
                self._manual_drag = True
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:                   # noqa: N802
        self._press_global = None
        self._manual_drag = False
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:               # noqa: N802
        if e.button() == Qt.LeftButton:
            self._press_global = None
            self._manual_drag = False
            toggle_maximize(self.win)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def _show_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self.win)
        menu.addAction("Minimize", self.win.showMinimized)
        menu.addAction("Restore" if self.win.isMaximized() else "Maximize", lambda: toggle_maximize(self.win))
        if hasattr(self.win, "open_couch"):
            menu.addSeparator()
            menu.addAction("Couch mode (F11)", self.win.open_couch)
        menu.addSeparator()
        menu.addAction("Close", self.win.close)
        menu.exec(global_pos)


class WindowBar(DragStrip):
    """Transparent top bar blended into the right-hand page canvas, holding the window controls on the far right."""

    def __init__(self, win: QWidget, parent: QWidget | None = None):
        super().__init__(win, parent)
        self.setObjectName("windowBar")
        self.setFixedHeight(BAR_HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addStretch(1)
        self.min_btn = CaptionButton("winBtn", "minimize", "Minimize", win.showMinimized, self)
        self.max_btn = CaptionButton("winBtn", "maximize", "Maximize", lambda: toggle_maximize(win), self)
        self.close_btn = CaptionButton("winClose", "close", "Close", win.close, self)
        lay.addWidget(self.min_btn)
        lay.addWidget(self.max_btn)
        lay.addWidget(self.close_btn)

    def sync_maximized(self, maximized: bool) -> None:
        self.max_btn.set_icon_name("restore" if maximized else "maximize", "Restore" if maximized else "Maximize")

    def refresh_icons(self) -> None:
        for b in (self.min_btn, self.max_btn, self.close_btn):
            b.refresh_icon()


class FramelessResizeFilter(QObject):
    """8-edge and 4-corner interactive resize for a frameless MainWindow via QWindow.startSystemResize."""

    def __init__(self, win: QWidget):
        super().__init__(win)
        self.win = win
        self._override = False
        self._handle = None
        # Install ONLY on MainWindow and its QWindow handle — NEVER on QApplication!
        # An app-wide event filter causes Shiboken 6.12 to wrap internal C++ QtWebEngineCore QObjects mid-construction,
        # triggering __dynamic_cast SIGSEGV when opening the in-app browser.
        win.installEventFilter(self)
        self._attach_handle()

    def _on_handle_destroyed(self, *_a) -> None:
        self._handle = None

    def _attach_handle(self) -> None:
        import shiboken6
        wh = self.win.windowHandle()
        if wh is not None and wh is not self._handle:
            if self._handle is not None and shiboken6.isValid(self._handle):
                try:
                    self._handle.removeEventFilter(self)
                except RuntimeError:
                    pass
            self._handle = wh
            wh.destroyed.connect(self._on_handle_destroyed)
            wh.installEventFilter(self)

    def cleanup(self) -> None:
        import shiboken6
        self._clear_cursor()
        if self._handle is not None:
            if shiboken6.isValid(self._handle):
                try:
                    self._handle.removeEventFilter(self)
                except RuntimeError:
                    pass
            self._handle = None
        if shiboken6.isValid(self.win):
            try:
                self.win.removeEventFilter(self)
            except RuntimeError:
                pass

    def _clear_cursor(self) -> None:
        if self._override:
            QApplication.restoreOverrideCursor()
            self._override = False

    def _set_cursor(self, shape: Qt.CursorShape) -> None:
        if self._override:
            QApplication.changeOverrideCursor(shape)
        else:
            QApplication.setOverrideCursor(shape)
            self._override = True

    def edges_at(self, pos: QPoint) -> Qt.Edges:
        w = self.win
        if not w.isVisible() or w.isMaximized() or w.isFullScreen():
            return Qt.Edges(0)
        W, H = w.width(), w.height()
        x, y = pos.x(), pos.y()
        if x < 0 or y < 0 or x >= W or y >= H:
            return Qt.Edges(0)
        # Keep caption buttons easy to click while still allowing a 4px rim resize at top-right
        in_buttons = x >= W - 3 * BTN_WIDTH and y < BAR_HEIGHT
        rim = 4 if in_buttons else EDGE_PX
        cnr = 4 if in_buttons else CORNER_PX
        on_l, on_r = x < rim, x >= W - rim
        on_t, on_b = y < rim, y >= H - rim
        if not (on_l or on_r or on_t or on_b):
            return Qt.Edges(0)
        edges = Qt.Edges(0)
        if x < cnr and (on_t or on_b or on_l):
            edges |= Qt.LeftEdge
        elif x >= W - cnr and (on_t or on_b or on_r):
            edges |= Qt.RightEdge
        if y < cnr and (on_l or on_r or on_t):
            edges |= Qt.TopEdge
        elif y >= H - cnr and (on_l or on_r or on_b):
            edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Qt.Edges) -> Qt.CursorShape:
        if edges in (Qt.TopEdge | Qt.LeftEdge, Qt.BottomEdge | Qt.RightEdge):
            return Qt.SizeFDiagCursor
        if edges in (Qt.TopEdge | Qt.RightEdge, Qt.BottomEdge | Qt.LeftEdge):
            return Qt.SizeBDiagCursor
        if edges & (Qt.LeftEdge | Qt.RightEdge):
            return Qt.SizeHorCursor
        return Qt.SizeVerCursor

    def eventFilter(self, obj, e) -> bool:                    # noqa: N802
        t = e.type()
        if obj is self.win:
            if t in (QEvent.Show, QEvent.WinIdChange):
                self._attach_handle()
            elif t in (QEvent.Leave, QEvent.WindowDeactivate, QEvent.Hide, QEvent.WindowStateChange):
                self._clear_cursor()
            return False
        if obj is not self._handle:
            return False
        if t == QEvent.Leave:
            self._clear_cursor()
            return False
        if t not in (QEvent.MouseMove, QEvent.MouseButtonPress):
            return False
        if QApplication.activePopupWidget() is not None:
            self._clear_cursor()
            return False
        pos = e.position().toPoint()
        edges = self.edges_at(pos)
        if t == QEvent.MouseMove:
            if e.buttons() == Qt.NoButton and edges:
                self._set_cursor(self._cursor_for(edges))
            elif not edges or e.buttons() != Qt.NoButton:
                self._clear_cursor()
            return False
        if t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton and edges:
            self._clear_cursor()
            if self._handle is not None and self._handle.startSystemResize(edges):
                return True
        return False
