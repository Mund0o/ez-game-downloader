"""Couch mode's interface sounds: a soft tick when the selector moves, a blip when pressing into something, and a
mouse click for the in-app browser's pointer. Couch mode only, quiet, switchable and with its own volume
(Settings → couch_sounds, couch_sounds_volume).

The sounds are short WAV files (made by packaging/make_sounds.py). They are mixed into **one audio stream that stays
open while couch mode is** (silence when nothing plays): a sound starts at once, sounds overlap, and none is lost.
(Each sound as its own stream — QSoundEffect — was sometimes not heard: a stream that has to start, or a sound card /
headset that PipeWire had put to sleep after a quiet moment, takes longer to wake up than a 40 ms tick lasts.)
QSoundEffect stays as the fallback when no audio stream can be opened."""

from __future__ import annotations

import logging
import os
import threading
import wave
from array import array
from pathlib import Path

from PySide6.QtCore import QIODevice, QObject, QUrl

log = logging.getLogger(__name__)

SOUNDS = Path(__file__).resolve().parent.parent / "resources" / "couch" / "sounds"
NAMES = ("move", "select", "click")
VOLUME = 0.15            # the default (Settings → Sound volume: 15 %); the files peak at -12 dBFS
VOICES = 3               # fallback players per sound
RATE = 48000
LATENCY = 0.06           # seconds of audio the stream buffers (a sound starts within this)


def load(name: str, rate: int = RATE) -> array | None:
    """A sound's samples (mono, 16-bit) at ``rate``."""
    try:
        with wave.open(str(SOUNDS / f"{name}.wav")) as w:
            if w.getsampwidth() != 2:
                return None
            data = array("h", w.readframes(w.getnframes()))
            ch, src = w.getnchannels(), w.getframerate()
    except (OSError, wave.Error, EOFError):
        return None
    if ch > 1:
        data = array("h", data[::ch])
    if src != rate and data:                            # (linear, once at load)
        n = int(len(data) * rate / src)
        out = array("h", bytes(2 * n))
        for i in range(n):
            x = i * src / rate
            j = int(x)
            f = x - j
            a = data[j]
            b = data[j + 1] if j + 1 < len(data) else a
            out[i] = int(a + (b - a) * f)
        data = out
    return data


def mix(voices: list[list], n: int) -> bytes:
    """The next ``n`` samples of every playing sound added up (16-bit, clipped); finished sounds are dropped from
    ``voices`` ([samples, position] each). Silence when nothing plays."""
    if not voices:
        return bytes(2 * n)
    if len(voices) == 1:
        smp, pos = voices[0]
        chunk = smp[pos:pos + n]
        voices[0][1] = pos + len(chunk)
        if voices[0][1] >= len(smp):
            voices.clear()
        out = array("h", chunk)
        if len(out) < n:
            out.extend(array("h", bytes(2 * (n - len(out)))))
        return out.tobytes()
    acc = [0] * n
    for v in voices:
        smp, pos = v
        chunk = smp[pos:pos + n]
        for i, x in enumerate(chunk):
            acc[i] += x
        v[1] = pos + len(chunk)
    voices[:] = [v for v in voices if v[1] < len(v[0])]
    return array("h", (32767 if x > 32767 else -32768 if x < -32768 else x for x in acc)).tobytes()


class _Stream(QIODevice):
    """What the audio output reads from: the playing sounds, mixed, or silence."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.voices: list[list] = []
        self.lock = threading.Lock()

    def add(self, samples: array) -> None:
        with self.lock:
            self.voices.append([samples, 0])
            del self.voices[:-8]                       # (never more than a few at once)

    def readData(self, maxlen):                           # noqa: N802 (Qt API)
        n = max(0, int(maxlen) // 2)
        with self.lock:
            return mix(self.voices, n)

    def writeData(self, _data):                           # noqa: N802
        return 0

    def bytesAvailable(self):                             # noqa: N802
        return 1 << 16

    def isSequential(self):                               # noqa: N802
        return True


def _multimedia():
    """QtMultimedia, or None (not installed, or tests: never through the real speakers)."""
    if os.environ.get("EZGD_NO_SOUNDS") == "1":
        return None
    for k in ("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"):
        os.environ.setdefault(k, "none")      # Qt's hardware-decoder probe froze couch mode's opening ~0.5 s
    try:
        from PySide6 import QtMultimedia
        return QtMultimedia
    except ImportError:
        log.info("couch sounds: Qt Multimedia is not available")
        return None


class CouchSounds(QObject):
    def __init__(self, parent, enabled, volume=lambda: VOLUME) -> None:
        """``enabled`` / ``volume``: callables, read at every sound (the settings can change while open)."""
        super().__init__(parent)
        self._enabled = enabled
        self._volume = volume
        self.played: list[str] = []          # what was asked for, newest last (tests; the last 50)
        self._sink = self._stream = None
        self._samples: dict[str, array] = {}
        self._scaled: dict[tuple[str, int], array] = {}
        self._voices: dict[str, list] = {}   # (fallback) QSoundEffect players per sound
        self._next: dict[str, int] = {}
        self._stopped = False
        mm = _multimedia()
        if mm is None:
            return
        from PySide6.QtCore import QTimer                # (opening a stream takes ~70 ms: not while couch opens)
        QTimer.singleShot(200, self, lambda: None if self._stopped else
                          (self._open_stream(mm) or self._open_effects(mm)))

    def _open_stream(self, mm) -> bool:
        try:
            device = mm.QMediaDevices.defaultAudioOutput()
            if device.isNull():
                return False
            fmt = mm.QAudioFormat()
            fmt.setChannelCount(1)
            fmt.setSampleFormat(mm.QAudioFormat.Int16)
            rate = RATE
            for r in (RATE, 44100, device.preferredFormat().sampleRate()):
                fmt.setSampleRate(r)
                if device.isFormatSupported(fmt):
                    rate = r
                    break
            else:
                return False
            for name in NAMES:
                data = load(name, rate)
                if data:
                    self._samples[name] = data
            self._stream = _Stream(self)
            self._stream.open(QIODevice.ReadOnly)
            self._sink = mm.QAudioSink(device, fmt, self)
            self._sink.setBufferSize(int(rate * 2 * LATENCY))
            self._sink.start(self._stream)
            log.info("couch sounds: one stream at %d Hz on %s", rate, device.description())
            return True
        except Exception:
            log.info("couch sounds: no audio stream; one player per sound instead", exc_info=True)
            self._sink = self._stream = None
            return False

    def _open_effects(self, mm) -> None:
        for name in NAMES:
            path = SOUNDS / f"{name}.wav"
            if not path.is_file():
                continue
            voices = []
            for _ in range(VOICES):
                fx = mm.QSoundEffect(self)
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
        if vol <= 0:
            return
        if self._stream is not None:
            base = self._samples.get(name)
            if base is None:
                return
            key = (name, round(vol * 100))
            smp = self._scaled.get(key)
            if smp is None:
                g = key[1] / 100
                smp = self._scaled[key] = array("h", (int(x * g) for x in base))
            self._stream.add(smp)
            return
        voices = self._voices.get(name)
        if not voices:
            return
        i = self._next[name]
        self._next[name] = (i + 1) % len(voices)
        fx = voices[i]
        if fx.isPlaying():
            fx.stop()
        fx.setVolume(vol)
        fx.play()

    def stop(self) -> None:
        """Couch mode closes: nothing more may sound (and the stream is closed)."""
        self._stopped = True
        self._enabled = lambda: False
        if self._sink is not None:
            try:
                self._sink.stop()
            except RuntimeError:
                pass
            self._sink = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        for voices in self._voices.values():
            for fx in voices:
                fx.stop()


_preview: CouchSounds | None = None


def preview(volume_percent: int) -> None:
    """The desktop Settings' volume slider: one blip at the new volume."""
    global _preview
    from PySide6.QtCore import QTimer
    if _preview is None:
        from PySide6.QtWidgets import QApplication
        _preview = CouchSounds(QApplication.instance(), lambda: True)
        _preview._closer = QTimer(_preview)
        _preview._closer.setSingleShot(True)
        _preview._closer.setInterval(4000)
        _preview._closer.timeout.connect(_close_preview)     # (no audio stream left open on the desktop)
    _preview.play("select", volume_percent / 100)
    _preview._closer.start()


def _close_preview() -> None:
    global _preview
    if _preview is not None:
        _preview.stop()
        _preview.deleteLater()
        _preview = None
