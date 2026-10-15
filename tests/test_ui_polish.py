"""Small things that stood out on screen: wording, alignment, nothing overlapping or hanging."""

import time

from PySide6.QtWidgets import QApplication

from ezgd.ui.widgets.common import relative_time, spin_box


def test_relative_time_says_yesterday_not_1_days():
    now = time.time()
    assert relative_time(now - 86400 * 1.2) == "yesterday"
    assert relative_time(now - 86400 * 3.5) == "3 days ago"


def test_number_boxes_inset_their_text(qapp):
    w = spin_box()
    assert w.lineEdit().textMargins().left() >= 6


def test_quick_menu_never_shows_a_heading_without_its_row(qapp):
    from types import SimpleNamespace
    from ezgd.ui.couch_quick import QuickMenu, Row

    drawn = []

    class Fake:
        _s = lambda self: 1.0                                   # noqa: E731
        _row_height = QuickMenu._row_height
        _rows_height = QuickMenu._rows_height
        _paint_rows = QuickMenu._paint_rows

        def height(self):
            return 600

        def _row(self, p, row, rect, focused):
            drawn.append(row.key)

    m = Fake()
    m.focus, m._hits = 0, []
    m.couch = SimpleNamespace(_font=lambda *a: QApplication.font())
    m.rows = [Row("a", "A"), Row("h", "Suspended", kind="header"), Row("g", "Game", "note", "game")]
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    class Painter(QPainter):
        def drawText(self, *a):                                 # noqa: N802
            drawn.append("heading")
            return super().drawText(*a)

    img = QImage(400, 600, QImage.Format_ARGB32)
    p = Painter(img)
    m._paint_rows(p, QRectF(0, 0, 400, 600), 600 - 96 - 62 - 40)   # room for "a" and the heading, not the game
    p.end()
    assert drawn == ["a"]
