"""Couch mode's intro: a 2-second video (the logo landing, with a short sound) played when couch mode opens.

The video is decoded by Qt Multimedia and its frames are drawn by the couch window itself (no extra native window
over it — that would fight the window manager rules couch mode relies on). Any button, key or click skips it; when
it ends it fades out over couch mode, which has been loading underneath all along. If anything goes wrong — no
Qt Multimedia, a missing file, no first frame in time — couch mode simply appears.

The video is made by ``packaging/make_intro.py``.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QPainter

log = logging.getLogger(__name__)

VIDEO = Path(__file__).resolve().parent.parent / "resources" / "couch" / "intro.webm"
FIRST_FRAME_WAIT = 1.5          # seconds: no picture by then → skip it
LONGEST = 4.0                   # seconds: whatever happens, couch mode is shown by then
FADE = 0.35                     # the fade into couch mode
SKIP_FADE = 0.2
VOLUME = 0.7
BG = QColor("#07080d")          # the video's own background


def available() -> bool:
    return VIDEO.is_file() and os.environ.get("EZGD_NO_INTRO") != "1"      # (the tests: straight into couch mode)


class CouchIntro(QObject):
    """States: ``loading`` (dark screen until the first frame) → ``playing`` → ``fading`` (over couch) → ``done``."""

    finished = Signal()

    def __init__(self, owner, sound: bool = True):
        super().__init__(owner)
        self.owner = owner
        self.state = "done"
        self.sound = sound
        self._player = self._sink = self._audio = None
        self._frame = None                  # the newest QVideoFrame
        self._image: QImage | None = None   # … converted, when it was drawn
        self._converted = None
        self._started = 0.0
        self._fade_from = 0.0
        self._fade_len = FADE
        self._watch = QTimer(self)
        self._watch.setInterval(100)
        self._watch.timeout.connect(self._check)

    # ------------------------------------------------------------ control
    def start(self) -> bool:
        if not available():
            return False
        try:
            # Qt opens CUDA / VA-API to look for hardware decoders first: half a second with the window frozen,
            # for a 2-second clip the CPU decodes easily
            for k in ("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"):
                os.environ.setdefault(k, "none")
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
            self._player = QMediaPlayer(self)
            self._sink = QVideoSink(self)
            self._player.setVideoSink(self._sink)
            if self.sound:
                self._audio = QAudioOutput(self)
                self._audio.setVolume(VOLUME)
                self._player.setAudioOutput(self._audio)
            self._sink.videoFrameChanged.connect(self._new_frame)
            self._player.mediaStatusChanged.connect(self._status)
            self._player.errorOccurred.connect(self._error)
            self._player.setSource(QUrl.fromLocalFile(str(VIDEO)))
            self._player.play()
        except Exception:
            log.exception("couch intro: could not play it")
            self._release()
            return False
        self.state = "loading"
        self._started = time.monotonic()
        self._watch.start()
        return True

    @property
    def active(self) -> bool:
        """Still in front of couch mode (input skips it instead of steering couch mode)."""
        return self.state in ("loading", "playing")

    @property
    def showing(self) -> bool:
        return self.state != "done"

    def skip(self) -> None:
        if self.state == "loading":
            self._finish()
        elif self.state == "playing":
            self._fade(SKIP_FADE)

    def stop(self) -> None:
        """Couch mode closes: no sound may go on."""
        if self.state != "done":
            self._finish()

    # ------------------------------------------------------------ media events
    def _new_frame(self, frame) -> None:
        if self.state == "loading":
            self.state = "playing"
            log.info("couch intro: playing (first frame after %.0f ms)", (time.monotonic() - self._started) * 1000)
        if self.state == "playing":
            self._frame = frame
            self.owner.update()

    def _status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.EndOfMedia:
            self._fade(FADE)
        elif status == QMediaPlayer.InvalidMedia:
            self._error(None, "invalid media")

    def _error(self, _err, text: str = "") -> None:
        log.warning("couch intro: %s", text or "playback failed")
        if self.state == "playing":
            self._fade(SKIP_FADE)
        elif self.state == "loading":
            self._finish()

    def _check(self) -> None:
        age = time.monotonic() - self._started
        if self.state == "loading" and age > FIRST_FRAME_WAIT:
            log.warning("couch intro: no picture after %.1f s, skipped", age)
            self._finish()
        elif self.state == "playing" and age > LONGEST:
            self._fade(SKIP_FADE)
        elif self.state == "fading":
            self.owner.update()                 # (the fade is drawn frame by frame; this is a safety net)

    def _fade(self, seconds: float) -> None:
        if self.state != "playing":
            return
        self._current_image()                   # the last picture stays while it fades
        self.state = "fading"
        self._fade_from = time.monotonic()
        self._fade_len = seconds
        self.owner.update()

    def _finish(self) -> None:
        self.state = "done"
        self._watch.stop()
        self._release()
        self._image = None
        self.owner.update()
        self.finished.emit()

    def _release(self) -> None:
        player, self._player = self._player, None
        if player is not None:
            try:
                player.stop()
                player.setVideoSink(None)
                player.deleteLater()
            except RuntimeError:
                pass
        for obj in (self._sink, self._audio):
            if obj is not None:
                try:
                    obj.deleteLater()
                except RuntimeError:
                    pass
        self._sink = self._audio = None
        self._frame = None

    # ------------------------------------------------------------ drawing
    def _current_image(self) -> QImage | None:
        if self._frame is not None and self._frame is not self._converted:
            img = self._frame.toImage()
            if not img.isNull():
                self._image = img
            self._converted = self._frame
        return self._image

    def paint(self, p: QPainter, w: float, h: float) -> bool:
        """Draw the intro over the window. True when it covers everything (couch mode need not be drawn)."""
        if self.state == "loading":
            p.fillRect(QRectF(0, 0, w, h), BG)
            return True
        if self.state == "playing":
            img = self._current_image()
            if img is None:
                p.fillRect(QRectF(0, 0, w, h), BG)
            else:
                self._cover(p, img, w, h)
            return True
        if self.state == "fading":
            t = (time.monotonic() - self._fade_from) / max(0.01, self._fade_len)
            if t >= 1.0:
                self._finish()
                return False
            if self._audio is not None:
                self._audio.setVolume(VOLUME * (1.0 - t))
            if self._image is not None:
                p.save()
                p.setOpacity(1.0 - t * t * (3 - 2 * t))
                self._cover(p, self._image, w, h)
                p.restore()
            QTimer.singleShot(12, self.owner, self.owner.update)   # next step of the fade
        return False

    @staticmethod
    def _cover(p: QPainter, img: QImage, w: float, h: float) -> None:
        """Scaled to fill the window (cropped around the centre on other aspect ratios)."""
        iw, ih = img.width(), img.height()
        scale = max(w / iw, h / ih)
        sw, sh = w / scale, h / scale
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing, False)        # a whole-screen picture: 3 ms less per frame at 4K
        p.drawImage(QRectF(0, 0, w, h), img, QRectF((iw - sw) / 2, (ih - sh) / 2, sw, sh))
        p.restore()
