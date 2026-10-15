"""Make couch mode's interface sounds (ezgd/resources/couch/sounds/*.wav), synthesised here like the intro's sound.

    move    the selector moves: a short, soft wooden tick
    select  pressing into something (A / Enter / a click on a tile): a quick rising two-note blip
    click   the in-app browser's mouse button: a small mechanical click

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
    """A fast rise, then an exponential fall."""
    if t < attack:
        return t / attack
    return math.exp(-(t - attack) / decay)


def move(n_sec: float = 0.045) -> list[float]:
    out = []
    for i in range(int(RATE * n_sec)):
        t = i / RATE
        e = env(t, 0.0015, 0.009)
        s = 0.75 * math.sin(2 * math.pi * 1400 * t) + 0.25 * math.sin(2 * math.pi * 2800 * t + 0.6)
        out.append(s * e)
    return out


def select(n_sec: float = 0.15) -> list[float]:
    out = []
    for i in range(int(RATE * n_sec)):
        t = i / RATE
        s = 0.0
        for start, freq in ((0.0, 740.0), (0.045, 1110.0)):        # F#5 then C#6: up a fifth
            if t >= start:
                u = t - start
                e = env(u, 0.003, 0.035)
                s += e * (0.8 * math.sin(2 * math.pi * freq * u) + 0.2 * math.sin(4 * math.pi * freq * u))
        out.append(s * 0.7)
    return out


def click(n_sec: float = 0.03) -> list[float]:
    rnd = random.Random(7)
    out, lp = [], 0.0
    for i in range(int(RATE * n_sec)):
        t = i / RATE
        noise = rnd.uniform(-1, 1)
        lp += 0.35 * (noise - lp)                                  # take the hiss off the noise
        body = math.sin(2 * math.pi * 2000 * t) * env(t, 0.0004, 0.004)
        out.append(0.6 * lp * env(t, 0.0003, 0.0025) + 0.5 * body)
    return out


def write(name: str, samples: list[float]) -> None:
    peak = max(abs(s) for s in samples) or 1.0
    fade = int(RATE * 0.004)                                       # no click at the very end
    data = bytearray()
    for i, s in enumerate(samples):
        if i >= len(samples) - fade:
            s *= (len(samples) - i) / fade
        data += struct.pack("<h", round(max(-1.0, min(1.0, s / peak * PEAK)) * 32767))
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(data))
    print(f"{OUT / name}.wav  {len(samples) / RATE * 1000:.0f} ms")


if __name__ == "__main__":
    write("move", move())
    write("select", select())
    write("click", click())
