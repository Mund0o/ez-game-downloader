"""Couch mode's interface sounds: a soft tick when the selector moves, a blip when pressing into something, and a
mouse click for the in-app browser's pointer. Couch mode only, quiet, switchable and with its own volume
(Settings → couch_sounds, couch_sounds_volume).

The sounds are short WAV files (made by packaging/make_sounds.py) played with QSoundEffect, which keeps them
decoded and ready: a press is heard at once. Each sound has a few players so quick presses overlap instead of
cutting each other off."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QUrl

log = logging.getLogger(__name__)

SOUNDS = Path(__file__).resolve().parent.parent / "resources" / "couch" / "sounds"
NAMES = ("move", "select", "click")
VOLUME = 0.15            # the default (Settings → Sound volume: 15 %); the files peak at -12 dBFS
VOICES = 3               # players per sound


def _effect_class():
    """QSoundEffect, or None (no Qt Multimedia, or tests: never through the real speakers)."""
    if os.environ.get("EZGD_NO_SOUNDS") == "1":
        return None
    for k in ("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"):
        os.environ.setdefault(k, "none")      # Qt's hardware-decoder probe froze couch mode's opening ~0.5 s
    try:
        from PySide6.QtMultimedia import QSoundEffect
        return QSoundEffect
    except ImportError:
        log.info("couch sounds: Qt Multimedia is not available")
        return None


class CouchSounds(QObject):
    def __init__(self, parent, enabled, volume=lambda: VOLUME) -> None:
        """``enabled`` / ``volume``: callables, read at every sound (the settings can change while open)."""
        super().__init__(parent)
        self._enabled = enabled
        self._volume = volume
        self._voices: dict[str, list] = {}
        self._next: dict[str, int] = {}
        self.played: list[str] = []          # what was asked for, newest last (tests; the last 50)
        effect = _effect_class()
        if effect is None:
            return
        for name in NAMES:
            path = SOUNDS / f"{name}.wav"
            if not path.is_file():
                continue
            voices = []
            for _ in range(VOICES):
                fx = effect(self)
                fx.setSource(QUrl.fromLocalFile(str(path)))
                voices.append(fx)
            self._voices[name] = voices
            self._next[name] = 0

    def play(self, name: str, volume: float | None = None) -> None:
        """Play a sound; ``volume`` (0–1) only for a preview of a new volume setting."""
        try:
            if volume is None and not self._enabled():
                return
            vol = max(0.0, min(1.0, float(self._volume() if volume is None else volume)))
        except Exception:
            return
        self.played.append(name)
        del self.played[:-50]
        voices = self._voices.get(name)
        if not voices or vol <= 0:
            return
        i = self._next[name]
        self._next[name] = (i + 1) % len(voices)
        fx = voices[i]
        if fx.isPlaying():
            fx.stop()
        fx.setVolume(vol)
        fx.play()

    def stop(self) -> None:
        """Couch mode closes: nothing more may sound."""
        for voices in self._voices.values():
            for fx in voices:
                fx.stop()
        self._enabled = lambda: False


_preview: CouchSounds | None = None


def preview(volume_percent: int) -> None:
    """The desktop Settings' volume slider: one blip at the new volume."""
    global _preview
    if _preview is None:
        from PySide6.QtWidgets import QApplication
        _preview = CouchSounds(QApplication.instance(), lambda: True)
    _preview.play("select", volume_percent / 100)
