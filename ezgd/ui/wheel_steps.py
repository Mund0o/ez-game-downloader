"""Mouse wheels as whole steps, for couch mode and the quick menu (one notch of the wheel moves the selection once).

A wheel turned by one notch does not always arrive as one event. Mice with a high-resolution wheel (most Logitech
gaming mice, many others) send that notch in several small parts — 8 × 15 instead of one 120 — and the quick menu
moved once per part: one notch went 3-5 rows down (the user, 2026-11-05). So the parts are added up, and the
selection moves once per full notch (120, Qt's "one notch"). A touchpad (its scroll arrives in pixels, with a
begin / update / end phase) moves it once per ``PIXELS`` of finger travel, and its coasting after the fingers left
(``ScrollMomentum``) is left out — it is not something the player did.

What is left over from a gesture is dropped after a short pause and when the direction turns, so a small leftover
never adds a step to the next turn of the wheel.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt

NOTCH = 120              # one notch of a wheel (QWheelEvent.angleDelta, 1/8 degree units)
PIXELS = 50              # a touchpad: finger travel (logical pixels) per step
GAP = 0.35               # seconds without wheel events: the next event starts a new gesture


class WheelSteps:
    """Feed every QWheelEvent to ``steps``: 1 when it completes a step with the wheel turned away from the player
    (up / back), -1 towards (down / on), 0 while a notch is still on its way."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._acc = 0.0
        self._last = -1e9

    def reset(self) -> None:
        self._acc = 0.0

    def steps(self, e) -> int:
        if e.phase() == Qt.ScrollMomentum:              # a touchpad coasting on by itself: not the player
            return 0
        pixels = e.pixelDelta()
        if e.phase() != Qt.NoScrollPhase and not pixels.isNull():
            d, size = float(pixels.y() or pixels.x()), PIXELS        # a touchpad: its travel in pixels
        else:
            angle = e.angleDelta()
            d, size = float(angle.y() or angle.x()), NOTCH            # a wheel: notches, maybe in parts
        if not d:
            return 0
        now = self._clock()
        if now - self._last > GAP or (d > 0) != (self._acc > 0):
            self._acc = 0.0                             # a new gesture, or the wheel turned back
        self._last = now
        self._acc += d
        if abs(self._acc) < size:
            return 0
        # one step per event at most: a desktop set to scroll faster sends one notch as a single bigger event
        # (KDE's scroll speed 3: 360), which moved once before and must not move three times now
        self._acc = 0.0
        return 1 if d > 0 else -1
