"""Make couch mode's 2-second intro video (ezgd/resources/couch/intro.webm).

The app's logo — the violet tile with the download arrow — lands, the arrow drops in, the base line "finishes the
download", a ring and sparks go out and the name appears; it then dims so couch mode can fade in from it. Frames are
drawn with Qt at 2560x1440 / 60 fps from the same shapes as the app icon, the sound (a whoosh, a soft thump and a
two-note chime) is synthesised here, and ffmpeg encodes both as VP9 + Opus.

    python packaging/make_intro.py            # needs ffmpeg with libvpx-vp9 and libopus
"""

from __future__ import annotations

import math
import os
import random
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient,  # noqa: E402
                           QPainter, QPainterPath, QPen, QRadialGradient)

W, H, FPS, SECONDS = 2560, 1440, 60, 2.0
RATE = 48000
OUT = Path(__file__).resolve().parent.parent / "ezgd" / "resources" / "couch" / "intro.webm"

ACCENT = QColor("#8b5cf6")
BG = QColor("#07080d")
LOGO = 330.0                     # tile size in pixels
CX, CY = W / 2, H / 2 - 90


# ------------------------------------------------------------------ easing
def clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def span(t: float, a: float, b: float) -> float:
    return clamp((t - a) / (b - a))


def out_cubic(x: float) -> float:
    return 1 - (1 - x) ** 3


def out_back(x: float, k: float = 1.70158) -> float:
    x -= 1
    return 1 + (k + 1) * x ** 3 + k * x ** 2


def in_out(x: float) -> float:
    return x * x * (3 - 2 * x)


def mix(c: QColor, f: float) -> QColor:
    """``c`` made lighter (f > 1) or darker (f < 1)."""
    return QColor(*(max(0, min(255, round(v * f))) for v in (c.red(), c.green(), c.blue())))


# ------------------------------------------------------------------ frames
SPARKS = [(random.Random(k).uniform(0, 2 * math.pi), random.Random(k + 99).uniform(0.6, 1.0),
           random.Random(k + 7).uniform(5.0, 11.0)) for k in range(22)]


def noise_tile() -> QImage:
    """Fine static grain: keeps the dark glow from banding once encoded at 8 bits."""
    rnd = random.Random(1)
    img = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    for y in range(256):
        for x in range(0, 256, 2):
            v = rnd.randint(0, 255)
            p.setPen(QColor(v, v, v, 10))
            p.drawPoint(x + (y & 1), y)
    p.end()
    return img


def frame(t: float, grain: QImage) -> QImage:
    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(BG)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.setRenderHint(QPainter.TextAntialiasing)

    outro = in_out(span(t, 1.62, 2.0))
    zoom = 1.0 + 0.035 * outro

    # the room lights up: a violet glow behind where the logo lands
    glow = out_cubic(span(t, 0.0, 0.7)) * (1 - 0.35 * span(t, 0.9, 1.6))
    g = QRadialGradient(QPointF(CX, CY + 40), 1100 * (0.55 + 0.45 * glow))
    c0 = QColor(ACCENT)
    c0.setAlphaF(0.34 * glow)
    c1 = QColor(ACCENT)
    c1.setAlphaF(0.10 * glow)
    g.setColorAt(0.0, c0)
    g.setColorAt(0.45, c1)
    g.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(QRectF(0, 0, W, H), g)

    p.translate(CX, CY)
    p.scale(zoom, zoom)

    # pulse ring when the "download" finishes
    rt = span(t, 0.86, 1.3)
    if 0 < rt < 1:
        r = LOGO * (0.62 + 0.9 * out_cubic(rt))
        ring = QColor(mix(ACCENT, 1.35))
        ring.setAlphaF(0.5 * (1 - rt) ** 2)
        p.setPen(QPen(ring, 10 * (1 - rt) + 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(-r, -r, 2 * r, 2 * r), r * 0.44, r * 0.44)

    # sparks
    st = span(t, 0.86, 1.5)
    if 0 < st < 1:
        p.setPen(Qt.NoPen)
        for ang, speed, size in SPARKS:
            d = LOGO * (0.55 + 1.25 * speed * out_cubic(st))
            col = QColor(mix(ACCENT, 1.6))
            col.setAlphaF(0.9 * (1 - st) ** 1.3)
            p.setBrush(col)
            p.drawEllipse(QPointF(math.cos(ang) * d, math.sin(ang) * d), size * (1 - 0.6 * st), size * (1 - 0.6 * st))

    # the tile lands (with a little overshoot), then pops when the base line completes
    land = span(t, 0.08, 0.58)
    pop = math.sin(math.pi * span(t, 0.84, 1.06)) * 0.055
    scale = (0.42 + 0.58 * out_back(land, 2.2)) * (1 + pop) if land > 0 else 0.0
    alpha = out_cubic(span(t, 0.08, 0.3))
    if scale > 0 and alpha > 0:
        p.save()
        p.rotate(-9 * (1 - out_cubic(land)))
        p.scale(scale, scale)
        p.setOpacity(alpha)
        half = LOGO / 2
        tile = QRectF(-half, -half, LOGO, LOGO)
        # soft bloom behind the tile
        bloom = QRadialGradient(QPointF(0, 10), LOGO * 1.05)
        b0 = QColor(ACCENT)
        b0.setAlphaF(0.55)
        bloom.setColorAt(0.0, b0)
        bloom.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(bloom)
        p.drawEllipse(QPointF(0, 10), LOGO * 1.05, LOGO * 1.05)
        path = QPainterPath()
        path.addRoundedRect(tile, LOGO * 0.25, LOGO * 0.25)
        fill = QLinearGradient(tile.topLeft(), tile.bottomRight())
        fill.setColorAt(0.0, mix(ACCENT, 1.22))
        fill.setColorAt(1.0, mix(ACCENT, 0.78))
        p.fillPath(path, fill)
        # top highlight
        hl = QLinearGradient(0, -half, 0, 0)
        hl.setColorAt(0.0, QColor(255, 255, 255, 46))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, hl)

        # the arrow, in the icon's 256-unit space
        u = LOGO / 224                          # the icon's tile is 224 units wide
        p.save()
        p.setClipPath(path, Qt.IntersectClip)
        drop = out_back(span(t, 0.34, 0.74), 2.6)
        p.translate(0, -60 * u * (1 - drop))
        pen = QPen(QColor("white"), 22 * u, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        o = 128                                  # icon centre
        stem = out_cubic(span(t, 0.34, 0.62))
        if stem > 0:
            p.drawLine(QPointF(0, (150 - o) * u - (150 - 64) * u * stem), QPointF(0, (150 - o) * u))
        arms = out_cubic(span(t, 0.5, 0.72))
        if arms > 0:
            tip = QPointF(0, (152 - o) * u)
            p.drawLine(tip, QPointF(-40 * u * arms, (152 - o - 40 * arms) * u))
            p.drawLine(tip, QPointF(40 * u * arms, (152 - o - 40 * arms) * u))
        p.restore()
        base = out_cubic(span(t, 0.62, 0.86))
        if base > 0:
            p.setPen(QPen(QColor("white"), 22 * u, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(-52 * u * base, (190 - o) * u), QPointF(52 * u * base, (190 - o) * u))
        # a shine sweeps across
        sh = span(t, 0.98, 1.4)
        if 0 < sh < 1:
            p.save()
            p.setClipPath(path, Qt.IntersectClip)
            x = -LOGO * 1.2 + LOGO * 2.4 * in_out(sh)
            band = QLinearGradient(QPointF(x - LOGO * 0.3, -half), QPointF(x + LOGO * 0.3, half))
            band.setColorAt(0.0, QColor(255, 255, 255, 0))
            band.setColorAt(0.5, QColor(255, 255, 255, 70))
            band.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(tile, band)
            p.restore()
        p.restore()

    # the name
    nt = span(t, 1.04, 1.46)
    if nt > 0:
        f = QFont("Noto Sans")
        f.setPixelSize(70)
        f.setWeight(QFont.Black)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 6 + 22 * (1 - out_cubic(nt)))
        p.setFont(f)
        p.setOpacity(out_cubic(nt))
        text = "EZ GAME DOWNLOADER"
        y = LOGO / 2 + 120 + 28 * (1 - out_cubic(nt))
        p.setPen(QColor("white"))
        p.drawText(QRectF(-W / 2, y - 50, W, 100), Qt.AlignCenter, text)
        sub = span(t, 1.18, 1.52)
        if sub > 0:
            f2 = QFont("Noto Sans")
            f2.setPixelSize(28)
            f2.setWeight(QFont.Bold)
            f2.setLetterSpacing(QFont.AbsoluteSpacing, 14)
            p.setFont(f2)
            p.setOpacity(0.9 * out_cubic(sub))
            p.setPen(mix(ACCENT, 1.55))
            p.drawText(QRectF(-W / 2, y + 52, W, 50), Qt.AlignCenter, "COUCH MODE")
        p.setOpacity(1.0)
    p.resetTransform()

    for yy in range(0, H, 256):                  # grain
        for xx in range(0, W, 256):
            p.drawImage(QPointF(xx, yy), grain)
    if outro > 0:                                # dims so couch mode can fade in from it
        dim = QColor(BG)
        dim.setAlphaF(0.72 * outro)
        p.fillRect(QRectF(0, 0, W, H), dim)
    p.end()
    return img


# ------------------------------------------------------------------ sound
def sound(path: Path) -> None:
    n = int(RATE * SECONDS)
    rnd = random.Random(5)
    left = [0.0] * n
    right = [0.0] * n

    def add(i: int, l: float, r: float | None = None) -> None:
        if 0 <= i < n:
            left[i] += l
            right[i] += l if r is None else r

    # whoosh: noise through a low-pass that opens up, swelling into the landing
    lp_l = lp_r = 0.0
    for i in range(int(0.75 * RATE)):
        t = i / RATE
        x = span(t, 0.0, 0.5)
        env = (x ** 2.2) * (1 - span(t, 0.48, 0.75)) ** 2
        cut = 0.012 + 0.16 * x ** 1.5
        lp_l += cut * (rnd.uniform(-1, 1) - lp_l)
        lp_r += cut * (rnd.uniform(-1, 1) - lp_r)
        add(i, 1.5 * env * lp_l, 1.5 * env * lp_r)
    # thump when the tile lands
    start = int(0.42 * RATE)
    phase = 0.0
    for k in range(int(0.45 * RATE)):
        t = k / RATE
        f = 38 + 34 * math.exp(-t * 18)
        phase += 2 * math.pi * f / RATE
        add(start + k, 0.55 * math.sin(phase) * math.exp(-t * 9) * min(1.0, t * 400))
    # chime when the download "finishes": two bell notes, a fifth apart
    for at, f0, gain, pan in ((0.86, 659.25, 0.20, -0.25), (0.97, 987.77, 0.16, 0.25)):
        s0 = int(at * RATE)
        for k in range(int((SECONDS - at) * RATE)):
            t = k / RATE
            v = (math.sin(2 * math.pi * f0 * t) * math.exp(-t * 3.2)
                 + 0.45 * math.sin(2 * math.pi * f0 * 2.0 * t) * math.exp(-t * 5.5)
                 + 0.20 * math.sin(2 * math.pi * f0 * 3.01 * t) * math.exp(-t * 8.0)
                 + 0.08 * math.sin(2 * math.pi * f0 * 4.2 * t) * math.exp(-t * 12.0))
            v *= gain * min(1.0, t * 300)
            add(s0 + k, v * (1 - pan), v * (1 + pan))
    # a little room: a few decaying echoes, crossed between the channels
    for delay, g in ((0.031, 0.32), (0.047, 0.26), (0.071, 0.2), (0.113, 0.14), (0.167, 0.09)):
        d = int(delay * RATE)
        for i in range(n - 1, d - 1, -1):
            left[i] += g * right[i - d]
            right[i] += g * left[i - d]
    # fade the tail, normalise to -3 dBFS
    for i in range(int(1.75 * RATE), n):
        f = 1 - span(i / RATE, 1.75, SECONDS)
        left[i] *= f
        right[i] *= f
    peak = max(max(map(abs, left)), max(map(abs, right))) or 1.0
    k = 0.707 / peak
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<hh", int(l * k * 32767), int(r * k * 32767))
                               for l, r in zip(left, right)))


def main() -> int:
    app = QGuiApplication(sys.argv[:1])          # noqa: F841 — fonts need it
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "intro.wav"
        sound(wav)
        cmd = ["ffmpeg", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
               "-i", str(wav),
               "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
               "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
               "-c:v", "libvpx-vp9", "-crf", "28", "-b:v", "0", "-row-mt", "1", "-deadline", "good",
               "-cpu-used", "1", "-g", "120",
               "-c:a", "libopus", "-b:a", "128k", "-shortest", str(OUT)]
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        grain = noise_tile()
        for k in range(int(FPS * SECONDS)):
            img = frame(k / FPS, grain)
            enc.stdin.write(bytes(img.constBits()))
            if os.environ.get("INTRO_STILLS") and k % 12 == 0:
                img.save(str(Path(os.environ["INTRO_STILLS"]) / f"intro-{k:03d}.png"))
        enc.stdin.close()
        if enc.wait() != 0:
            print("ffmpeg failed", file=sys.stderr)
            return 1
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
