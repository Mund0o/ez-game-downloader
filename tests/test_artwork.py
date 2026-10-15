"""Couch mode's artwork is made in a helper process (Qt's image calls hold Python's lock: in a thread they froze the
window)."""

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PySide6.QtGui import QColor, QImage


def _png(w: int, h: int, color: QColor) -> bytes:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(color)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def test_tiles_and_backgrounds_come_from_the_helper_process(qapp):
    from ezgd.ui import artwork
    data = _png(300, 450, QColor(200, 30, 30))
    tile = artwork.to_image(artwork.submit("tile", "red", data, (150, 225, 12.0, False, True, 1.0)).result(60))
    assert tile.size() == QSize(150, 225)
    assert tile.pixelColor(75, 112).red() > 150                # the picture
    assert tile.pixelColor(0, 0).alpha() == 0                  # a rounded corner
    assert tile.pixelColor(75, 112).alpha() == 255             # solid inside (copied, not blended)
    layer = artwork.to_image(artwork.submit("layer", "red", data, (64, 36, "dim", "#0b0d13", "#7c3aed")).result(60))
    assert layer.size() == QSize(64, 36) and layer.pixelColor(32, 18).alpha() == 255
    assert artwork.submit("tile", "bad", b"not a picture", (10, 10, 0.0, False, True, 1.0)).result(60) is None


def test_artwork_falls_back_to_threads(qapp, monkeypatch):
    from ezgd.ui import artwork
    monkeypatch.setattr(artwork, "_disabled", True)
    data = _png(40, 20, QColor(10, 200, 10))
    img = artwork.to_image(artwork.submit("fill", "green", data, (20, 10, False, 0.0)).result(60))
    assert img.size() == QSize(20, 10) and img.pixelColor(10, 5).green() > 150
