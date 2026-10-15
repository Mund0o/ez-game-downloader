"""Couch mode's artwork, made in a helper process: tiles (rounded covers) and whole page backgrounds.

Qt's image calls (decoding a JPEG, ``QImage.scaled``, ``convertToFormat``, ``QPainter.drawImage``) keep Python's
global lock for as long as they run — 5 to 30 ms each at 4K. In a worker *thread* that still froze the window:
couch mode's Store stuttered every time a new background or a row of tiles was prepared. A helper process has its
own interpreter, so the window never waits for it; only the finished pixels come back.

Everything here is plain QImage/QPainter work (no fonts, no windows), which needs no QGuiApplication. If the helper
cannot be started or breaks, the same functions run in a thread for the rest of the session (as before).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient

log = logging.getLogger(__name__)

WORKERS = 2
DECODED_BYTES = 160 * 1024 * 1024        # decoded originals kept in each helper
FORMAT = QImage.Format_ARGB32_Premultiplied
_pool: ProcessPoolExecutor | None = None
_threads: ThreadPoolExecutor | None = None
_disabled = os.environ.get("EZGD_ART_IN_PROCESS", "1") == "0"
_decoded: OrderedDict = OrderedDict()            # in each helper: url → decoded QImage (several sizes reuse it)


# ================================================================== pictures
def _rounded(img: QImage, radius: float, border: float = 0.0) -> QImage:
    """The image with rounded corners (transparent outside) and, optionally, a faint border — so a tile is drawn
    with one plain blit."""
    out = QImage(img.size(), FORMAT)
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


def decode(url: str, data: bytes) -> QImage | None:
    img = _decoded.get(url)
    if img is not None:
        _decoded.move_to_end(url)
        return img
    img = QImage.fromData(data)
    if img.isNull():
        return None
    # wide art may fill a 4K screen; portrait covers only ever fill a tile
    limit = QSize(3840, 2400) if img.width() >= img.height() * 1.45 else QSize(1600, 1200)
    if img.width() > limit.width() or img.height() > limit.height():
        img = img.scaled(limit, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    _decoded[url] = img
    # by size, not count: one wide backdrop decodes to ~36 MB — 40 of them made each helper 1.4 GB (2026-10-14)
    while len(_decoded) > 1 and sum(i.sizeInBytes() for i in _decoded.values()) > DECODED_BYTES:
        _decoded.popitem(last=False)
    return img


def fill(img: QImage, pw: int, ph: int, top: bool, radius: float) -> QImage:
    """The image scaled to cover pw×ph pixels (cropped around its centre)."""
    out = _cover(img, pw, ph, top)
    return _rounded(out, radius) if radius else out


def tile(art: QImage, pw: int, ph: int, radius: float, dim: bool, portrait: bool, dpr: float) -> QImage:
    """A game tile: Steam's portrait capsule, or the site's cover shown whole over a blur of itself."""
    if portrait:
        out = _cover(art, pw, ph)
    else:
        out = _blur(art, pw, ph).convertToFormat(FORMAT)
        p = QPainter(out)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(out.rect(), QColor(0, 0, 0, 70))
        front = art.scaled(QSize(int(pw - 12 * dpr), int(ph * 0.62)), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawImage(int((pw - front.width()) / 2), int((ph * 0.72 - front.height()) / 2), front)
        band = QLinearGradient(0, ph * 0.6, 0, ph)
        band.setColorAt(0, QColor(0, 0, 0, 0))
        band.setColorAt(1, QColor(0, 0, 0, 215))
        p.fillRect(QRectF(0, ph * 0.6, pw, ph * 0.4), QBrush(band))
        p.end()
    if out.hasAlphaChannel():                   # solid inside (tiles are copied, not blended: CouchWindow._copy_tile)
        solid = QImage(out.size(), QImage.Format_RGB32)
        solid.fill(QColor(20, 22, 30))
        p = QPainter(solid)
        p.drawImage(0, 0, out)
        p.end()
        out = solid
    if dim:
        p = QPainter(out)
        p.fillRect(out.rect(), QColor(0, 0, 0, 110))
        p.end()
    return _rounded(out, radius, border=max(1.0, radius / 12))


def layer(img: QImage | None, PW: int, PH: int, mode: str, bg0: str, accent: str) -> QImage:
    """The whole page background (colours, glow, the artwork, shading) as one opaque image. A wide hero image spans
    the full width at its own shape (fading out at the bottom), anything else sits on the right; both over a soft
    blur of the same picture."""
    canvas = QImage(PW, PH, FORMAT)              # opaque; copies to the screen fastest in this format
    canvas.fill(QColor(bg0))
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
            hero = hero.copy(0, 0, PW, min(hh, PH)).convertToFormat(FORMAT)
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
            art = art.convertToFormat(FORMAT)
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


# ==================================================================== jobs
def render(kind: str, url: str | None, data: bytes | None, args: tuple) -> tuple[int, int, bytes] | None:
    """One picture (``kind``: fill | tile | layer) from the encoded source image; returns its pixels
    (width, height, ARGB32-premultiplied bytes) — or None when the source cannot be decoded."""
    img = decode(url, data) if url and data else None
    if img is None and kind != "layer":
        return None
    if kind == "fill":
        out = fill(img, *args)
    elif kind == "tile":
        out = tile(img, *args)
    else:
        out = layer(img, *args)
    out = out.convertToFormat(FORMAT)
    return out.width(), out.height(), bytes(out.constBits())


def to_image(res: tuple[int, int, bytes] | None) -> QImage | None:
    if res is None:
        return None
    w, h, raw = res
    return QImage(raw, w, h, w * 4, FORMAT).copy()      # own its pixels (``raw`` goes away)


def _follow_parent() -> None:
    """In the helper: exit as soon as the app is gone — also when it crashed or was killed."""
    from ezgd.sources.parsing import _follow_parent as follow
    follow()


def submit(kind: str, url: str | None, data: bytes | None, args: tuple) -> Future:
    """Make a picture in the helper process (or, if it is unavailable, in a thread). The future's result is what
    ``render`` returns."""
    global _pool, _disabled, _threads
    if not _disabled:
        try:
            if _pool is None:
                _pool = ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn"),
                                            initializer=_follow_parent)
            return _pool.submit(render, kind, url, data, args)
        except (BrokenProcessPool, OSError, EOFError, pickle.PicklingError, RuntimeError) as exc:
            log.warning("the artwork helper process failed (%s); making artwork in threads from now on", exc)
            _disabled = True
            shutdown()
    if _threads is None:
        _threads = ThreadPoolExecutor(max_workers=3, thread_name_prefix="couch-art")
    return _threads.submit(render, kind, url, data, args)


def broken(exc: BaseException) -> None:
    """A job failed because the helper died: use threads from now on."""
    global _disabled
    if isinstance(exc, BrokenProcessPool) and not _disabled:
        log.warning("the artwork helper process stopped (%s); making artwork in threads from now on", exc)
        _disabled = True
        shutdown()


def warm_up() -> None:
    """Start the helper before the first picture needs it (it imports Qt once, ~0.3 s)."""
    if _disabled:
        return
    try:
        submit("layer", None, None, (8, 8, "dim", "#000000", "#000000"))
    except Exception:
        log.debug("artwork helper warm-up failed", exc_info=True)


def shutdown() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
