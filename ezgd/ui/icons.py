"""Small vector icon set painted with QPainter (no image assets to ship)."""

from __future__ import annotations

import math
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _pen(color: str, w: float = 1.8) -> QPen:
    pen = QPen(QColor(color), w)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw(name: str, p: QPainter, c: str) -> None:
    p.setPen(_pen(c))
    p.setBrush(Qt.NoBrush)
    if name == "home":
        path = QPainterPath(QPointF(4, 11))
        path.lineTo(12, 4); path.lineTo(20, 11)
        p.drawPath(path)
        p.drawRoundedRect(QRectF(6.5, 10, 11, 9.5), 1.5, 1.5)
    elif name == "grid":
        for x, y in ((4, 4), (13, 4), (4, 13), (13, 13)):
            p.drawRoundedRect(QRectF(x, y, 7, 7), 1.8, 1.8)
    elif name == "star":
        path = QPainterPath()
        for i in range(10):
            r = 8.5 if i % 2 == 0 else 3.8
            a = -math.pi / 2 + i * math.pi / 5
            pt = QPointF(12 + r * math.cos(a), 12.5 + r * math.sin(a))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "download":
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        path = QPainterPath(QPointF(7.5, 10.5)); path.lineTo(12, 15); path.lineTo(16.5, 10.5)
        p.drawPath(path)
        p.drawLine(QPointF(5, 19.5), QPointF(19, 19.5))
    elif name == "library":
        p.drawRoundedRect(QRectF(3.5, 7, 17, 10.5), 4.5, 4.5)
        p.drawLine(QPointF(7.5, 12.2), QPointF(10.5, 12.2)); p.drawLine(QPointF(9, 10.7), QPointF(9, 13.7))
        p.setBrush(QColor(c)); p.drawEllipse(QPointF(15, 11.2), 0.9, 0.9); p.drawEllipse(QPointF(17, 13.2), 0.9, 0.9)
    elif name == "settings":
        p.drawEllipse(QPointF(12, 12), 3, 3)
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(QPointF(12 + 5.5 * math.cos(a), 12 + 5.5 * math.sin(a)),
                       QPointF(12 + 8 * math.cos(a), 12 + 8 * math.sin(a)))
        p.drawEllipse(QPointF(12, 12), 5.6, 5.6)
    elif name == "search":
        p.drawEllipse(QPointF(10.5, 10.5), 5.5, 5.5)
        p.drawLine(QPointF(14.5, 14.5), QPointF(19.5, 19.5))
    elif name == "plus":
        p.drawLine(QPointF(12, 5), QPointF(12, 19)); p.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "pause":
        p.drawLine(QPointF(9, 6), QPointF(9, 18)); p.drawLine(QPointF(15, 6), QPointF(15, 18))
    elif name == "play":
        path = QPainterPath(QPointF(8, 5.5)); path.lineTo(18.5, 12); path.lineTo(8, 18.5); path.closeSubpath()
        p.setBrush(QColor(c)); p.drawPath(path)
    elif name == "close":
        p.drawLine(QPointF(6.5, 6.5), QPointF(17.5, 17.5)); p.drawLine(QPointF(17.5, 6.5), QPointF(6.5, 17.5))
    elif name == "retry":
        p.drawArc(QRectF(5, 5, 14, 14), 60 * 16, 290 * 16)
        path = QPainterPath(QPointF(15.5, 3.8)); path.lineTo(18.6, 6.6); path.lineTo(15, 8.6)
        p.drawPath(path)
    elif name == "folder":
        path = QPainterPath(QPointF(3.5, 7))
        path.lineTo(9, 7); path.lineTo(11, 9); path.lineTo(20.5, 9); path.lineTo(20.5, 18.5)
        path.lineTo(3.5, 18.5); path.closeSubpath()
        p.drawPath(path)
    elif name == "external":
        p.drawRoundedRect(QRectF(4.5, 7, 12.5, 12.5), 2, 2)
        p.drawLine(QPointF(11, 13), QPointF(19.5, 4.5))
        path = QPainterPath(QPointF(14, 4.5)); path.lineTo(19.5, 4.5); path.lineTo(19.5, 10); p.drawPath(path)
    elif name == "trash":
        p.drawLine(QPointF(5, 7), QPointF(19, 7)); p.drawLine(QPointF(10, 4.5), QPointF(14, 4.5))
        path = QPainterPath(QPointF(6.5, 7)); path.lineTo(7.5, 19.5); path.lineTo(16.5, 19.5); path.lineTo(17.5, 7)
        p.drawPath(path)
    elif name == "more":
        p.setBrush(QColor(c))
        for x in (6, 12, 18):
            p.drawEllipse(QPointF(x, 12), 1.5, 1.5)
    elif name == "check":
        path = QPainterPath(QPointF(5, 12.5)); path.lineTo(10, 17.5); path.lineTo(19, 7); p.drawPath(path)
    elif name == "warning":
        path = QPainterPath(QPointF(12, 4)); path.lineTo(20.5, 19); path.lineTo(3.5, 19); path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(12, 9.5), QPointF(12, 13.5)); p.drawPoint(QPointF(12, 16.3))
    elif name == "info":
        p.drawEllipse(QPointF(12, 12), 8, 8)
        p.drawLine(QPointF(12, 11), QPointF(12, 16)); p.drawPoint(QPointF(12, 8))
    elif name == "refresh":
        p.drawArc(QRectF(5, 5, 14, 14), 30 * 16, 300 * 16)
        path = QPainterPath(QPointF(19, 5)); path.lineTo(19, 9.5); path.lineTo(14.5, 9.5); p.drawPath(path)
    elif name == "link":
        p.drawRoundedRect(QRectF(3.5, 9, 9, 6), 3, 3); p.drawRoundedRect(QRectF(11.5, 9, 9, 6), 3, 3)
    elif name == "magnet":
        p.drawArc(QRectF(5, 5, 14, 14), 180 * 16, 180 * 16)
        p.drawLine(QPointF(5, 12), QPointF(5, 5)); p.drawLine(QPointF(19, 12), QPointF(19, 5))
    elif name == "steam":
        p.drawEllipse(QPointF(15, 9), 4, 4); p.drawEllipse(QPointF(8, 16), 2.8, 2.8)
        p.drawLine(QPointF(10.5, 14.5), QPointF(12.5, 11.5))
    elif name == "wrench":
        p.drawLine(QPointF(5.5, 18.5), QPointF(13, 11))
        p.drawEllipse(QPointF(15.8, 8.2), 3.6, 3.6)
        p.setPen(_pen(c, 2.6))
        p.drawLine(QPointF(5.5, 18.5), QPointF(9, 15))
    elif name == "lock":
        p.drawRoundedRect(QRectF(5.5, 11, 13, 9), 2, 2)
        p.drawArc(QRectF(8, 4.5, 8, 11), 0, 180 * 16)
        p.drawLine(QPointF(8, 10), QPointF(8, 11)); p.drawLine(QPointF(16, 10), QPointF(16, 11))
        p.drawPoint(QPointF(12, 15.5))
    elif name == "bug":
        p.drawRoundedRect(QRectF(7.5, 8, 9, 11), 4.5, 4.5); p.drawLine(QPointF(12, 8), QPointF(12, 19))
        for y in (11, 14.5):
            p.drawLine(QPointF(4, y), QPointF(7.5, y)); p.drawLine(QPointF(16.5, y), QPointF(20, y))
    elif name in ("heart", "heart-filled"):
        path = QPainterPath(QPointF(12, 19.5))
        path.cubicTo(QPointF(4, 14), QPointF(2.5, 9.5), QPointF(5, 6.8))
        path.cubicTo(QPointF(7.4, 4.3), QPointF(10.8, 5), QPointF(12, 8))
        path.cubicTo(QPointF(13.2, 5), QPointF(16.6, 4.3), QPointF(19, 6.8))
        path.cubicTo(QPointF(21.5, 9.5), QPointF(20, 14), QPointF(12, 19.5))
        if name == "heart-filled":
            p.setBrush(QColor(c))
        p.drawPath(path)
    elif name == "gamepad":
        path = QPainterPath(QPointF(7, 7.5))
        path.lineTo(17, 7.5)
        path.cubicTo(QPointF(20.5, 7.5), QPointF(22, 17.5), QPointF(19.2, 18.2))
        path.cubicTo(QPointF(17.4, 18.7), QPointF(16, 15.4), QPointF(14.6, 15.4))
        path.lineTo(9.4, 15.4)
        path.cubicTo(QPointF(8, 15.4), QPointF(6.6, 18.7), QPointF(4.8, 18.2))
        path.cubicTo(QPointF(2, 17.5), QPointF(3.5, 7.5), QPointF(7, 7.5))
        p.drawPath(path)
        p.drawLine(QPointF(6.5, 11.5), QPointF(9.5, 11.5)); p.drawLine(QPointF(8, 10), QPointF(8, 13))
        p.setBrush(QColor(c)); p.drawEllipse(QPointF(15.5, 10.8), 0.9, 0.9); p.drawEllipse(QPointF(17.3, 12.6), 0.9, 0.9)
    elif name == "disk":
        p.drawRoundedRect(QRectF(4, 6, 16, 12), 2.5, 2.5)
        p.drawLine(QPointF(4, 13), QPointF(20, 13))
        p.setBrush(QColor(c)); p.drawEllipse(QPointF(16.5, 15.6), 0.9, 0.9)
    elif name == "save":
        path = QPainterPath(QPointF(5, 4.5))
        path.lineTo(16, 4.5); path.lineTo(19.5, 8); path.lineTo(19.5, 19.5); path.lineTo(5, 19.5); path.closeSubpath()
        p.drawPath(path)
        p.drawRect(QRectF(8, 4.5, 7, 4.5)); p.drawRoundedRect(QRectF(8, 12.5, 8.5, 7), 1, 1)
    elif name == "update":
        p.drawArc(QRectF(4.5, 4.5, 15, 15), 100 * 16, 300 * 16)
        p.drawLine(QPointF(12, 8.5), QPointF(12, 15.5))
        path = QPainterPath(QPointF(9, 11)); path.lineTo(12, 8); path.lineTo(15, 11)
        p.drawPath(path)
    elif name == "tv":
        p.drawRoundedRect(QRectF(3.5, 5, 17, 11.5), 2, 2)
        p.drawLine(QPointF(9, 19.5), QPointF(15, 19.5)); p.drawLine(QPointF(12, 16.5), QPointF(12, 19.5))
    elif name == "clock":
        p.drawEllipse(QPointF(12, 12), 8, 8)
        p.drawLine(QPointF(12, 7.5), QPointF(12, 12)); p.drawLine(QPointF(12, 12), QPointF(15, 14))
    else:
        p.drawEllipse(QPointF(12, 12), 7, 7)


@lru_cache(maxsize=256)
def icon(name: str, color: str = "#c9cfdd", size: int = 24) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size * 2 / 24, size * 2 / 24)
    _draw(name, p, color)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def app_icon() -> QIcon:
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    from ezgd.ui.theme import CURRENT
    p.setBrush(QBrush(QColor(CURRENT.accent)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(16, 16, 224, 224), 56, 56)
    p.setPen(_pen("#ffffff", 22))
    p.drawLine(QPointF(128, 64), QPointF(128, 150))
    path = QPainterPath(QPointF(88, 112)); path.lineTo(128, 152); path.lineTo(168, 112)
    p.drawPath(path)
    p.drawLine(QPointF(76, 190), QPointF(180, 190))
    p.end()
    return QIcon(pm)
