"""Make couch mode's interface sounds (ezgd/resources/couch/sounds/*.wav), synthesised here like the intro's sound.

    move    the selector moves: a soft, rounded low "pop" with a breath of felt
    select  pressing into something (A / Enter / a click on a tile): two smooth notes rising a fifth, in a soft haze
    click   the in-app browser's mouse button: a muffled tap

Creamy rather than bright: smooth sine notes with only a quiet octave above them (the one overtone that sounds
round), rounded attacks of several milliseconds (an instant start is itself a click), a smooth diffuse tail instead of
a dry beep, and everything low-passed so nothing shrill is left above ~2 kHz. What made the first warm set (0.6.6)
still feel "off": tanh saturation (it adds odd harmonics — a faint buzz), three separate echoes (they ring hollow,
like a comb), a marimba overtone at ~4× (a plink) and a 10 % pitch drop (a boing). The 0.5 set was 740–2800 Hz pure
tones with instant attacks — "rough on the ears".

They are kept short and quiet in the files (peaks around -12 dBFS); couch_sounds.VOLUME sets how loud they play.

    python packaging/make_sounds.py
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

RATE = 48000
OUT = Path(__file__).resolve().parent.parent / "ezgd" / "resources" / "couch" / "sounds"
PEAK = 0.25                       # -12 dBFS


def env(t: float, attack: float, decay: float) -> float:
    """A rounded rise (half a cosine: no corner, so no click of its own), then an exponential fall."""
    if t < attack:
        return 0.5 - 0.5 * math.cos(math.pi * t / attack)
    return math.exp(-(t - attack) / decay)


def lowpass(samples: list[float], cutoff: float, q: float = 0.6) -> list[float]:
    """A gentle 12 dB/octave low-pass (RBJ biquad): takes the edge off everything above ``cutoff``."""
    w = 2 * math.pi * cutoff / RATE
    alpha = math.sin(w) / (2 * q)
    cw = math.cos(w)
    b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
    a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0
    out, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
    for x in samples:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out.append(y)
        x2, x1, y2, y1 = x1, x, y1, y
    return out


def haze(samples: list[float], wet: float, cutoff: float = 1500) -> list[float]:
    """A smooth tail: the sound smeared by a chain of all-pass diffusers (dense, so no separate echoes and no
    hollow ring), darkened and mixed in quietly — a soft space around the sound instead of a dry beep."""
    tail = list(samples)
    for ms, g in ((4.7, 0.62), (6.1, 0.6), (8.9, 0.58), (12.3, 0.55), (17.9, 0.5)):
        d = int(RATE * ms / 1000)
        buf = [0.0] * d                               # y[n] = -g·x[n] + x[n-d] + g·y[n-d]
        xs = [0.0] * d
        out = []
        for i, x in enumerate(tail):
            y = -g * x + xs[i % d] + g * buf[i % d]
            xs[i % d], buf[i % d] = x, y
            out.append(y)
        tail = out
    tail = lowpass(tail, cutoff)
    return [s + wet * t for s, t in zip(samples, tail)]


def note(t: float, freq: float, attack: float, decay: float, octave: float = 0.12) -> float:
    """One smooth note: a sine settling from 2 % above (just enough to feel struck, not bent), and a quiet octave that
    fades faster than the note."""
    f = freq * (1 + 0.02 * math.exp(-t / 0.008))
    s = math.sin(2 * math.pi * f * t) + octave * math.sin(4 * math.pi * f * t) * math.exp(-t / 0.012)
    return s * env(t, attack, decay)


def breath(n: int, cutoff: float, seed: int) -> list[float]:
    """Soft, dark noise: the felt of a mallet touching, under the note."""
    rnd = random.Random(seed)
    return lowpass(lowpass([rnd.uniform(-1, 1) for _ in range(n)], cutoff), cutoff)


def move(n_sec: float = 0.09) -> list[float]:
    n = int(RATE * n_sec)
    felt = breath(n, 900, 3)
    out = []
    for i in range(n):
        t = i / RATE
        out.append(note(t, 370.0, 0.005, 0.020) + 0.9 * felt[i] * env(t, 0.003, 0.008))     # F#4
    return lowpass(haze(out, 0.18), 1700)


def select(n_sec: float = 0.19) -> list[float]:
    n = int(RATE * n_sec)
    felt = breath(n, 1000, 5)
    out = []
    for i in range(n):
        t = i / RATE
        s = 0.8 * felt[i] * env(t, 0.003, 0.008)
        for start, freq, amp in ((0.0, 392.0, 0.8), (0.055, 587.33, 1.0)):     # G4 then D5: up a fifth
            if t >= start:
                s += amp * note(t - start, freq, 0.006, 0.050)
        out.append(s)
    return lowpass(haze(out, 0.25), 2000)


def click(n_sec: float = 0.045) -> list[float]:
    n = int(RATE * n_sec)
    felt = breath(n, 800, 7)
    out = []
    for i in range(n):
        t = i / RATE
        out.append(2.2 * felt[i] * env(t, 0.0015, 0.006) + 0.5 * note(t, 520.0, 0.0015, 0.008, octave=0.0))
    return lowpass(haze(out, 0.12), 1600)


def write(name: str, samples: list[float]) -> None:
    peak = max(abs(s) for s in samples) or 1.0
    fade = int(RATE * 0.015)                                       # no click at the very end: a smooth fade
    data = bytearray()
    for i, s in enumerate(samples):
        if i >= len(samples) - fade:
            s *= 0.5 - 0.5 * math.cos(math.pi * (len(samples) - i) / fade)
        data += struct.pack("<h", round(max(-1.0, min(1.0, s / peak * PEAK)) * 32767)) * 2   # left, right
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(2)                                          # stereo: a mono file can play in one ear only
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(data))
    print(f"{OUT / name}.wav  {len(samples) / RATE * 1000:.0f} ms")


if __name__ == "__main__":
    write("move", move())
    write("select", select())
    write("click", click())
