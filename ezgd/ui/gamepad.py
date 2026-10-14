"""Game controllers for couch mode (Linux evdev, no extra libraries).

Only devices that are game controllers are opened — recognised from ``/sys`` without opening anything else
(keyboards and mice are never read). Linux gives the person at the computer access to controllers (systemd's
``uaccess``); a controller that cannot be opened is skipped. Controllers can be plugged in and out at any time.

Buttons are reported by position, the way Linux names them: the bottom face button is "a" (confirm), the right one
"b" (back), the left/top ones "x" (details). D-pad and left stick move; holding a direction repeats.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import struct
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

log = logging.getLogger(__name__)

EV_KEY, EV_ABS = 0x01, 0x03
BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x132, 0x133, 0x134
BTN_TL, BTN_TR, BTN_SELECT, BTN_START, BTN_MODE = 0x136, 0x137, 0x13A, 0x13B, 0x13C
BTN_JOYSTICK, BTN_GAMEPAD = 0x120, 0x130
BTN_DPAD = {0x220: "up", 0x221: "down", 0x222: "left", 0x223: "right"}
ABS_X, ABS_Y, ABS_HAT0X, ABS_HAT0Y = 0x00, 0x01, 0x10, 0x11
BUTTONS = {BTN_SOUTH: "a", BTN_EAST: "b", BTN_NORTH: "x", BTN_WEST: "x", BTN_C: "x", BTN_TL: "lb", BTN_TR: "rb",
           BTN_SELECT: "select", BTN_START: "start"}
EVENT = struct.Struct("llHHi")                      # struct input_event (64-bit: timeval, type, code, value)
ABSINFO = struct.Struct("iiiiii")                   # value, minimum, maximum, fuzz, flat, resolution
REPEAT_DELAY, REPEAT_EVERY = 0.38, 0.11
DUPLICATE_WINDOW = 0.06                             # the same press from two devices (Steam's virtual pad + the real)


def _eviocgabs(code: int) -> int:
    return (2 << 30) | (ABSINFO.size << 16) | (ord("E") << 8) | (0x40 + code)


def _bits(text: str) -> int:
    """A /sys capabilities bitmap ("hex words, most significant first") as one integer."""
    n = 0
    for word in text.split():
        n = (n << (8 * struct.calcsize("l"))) | int(word, 16)
    return n


def is_gamepad(sys_event_dir: Path) -> bool:
    """A controller has gamepad/joystick buttons *and* a stick or d-pad axis (virtual keyboards that claim every key
    have no axes, so they are never opened)."""
    try:
        caps = sys_event_dir / "device" / "capabilities"
        keys = _bits((caps / "key").read_text())
        axes = _bits((caps / "abs").read_text())
    except (OSError, ValueError):
        return False
    buttons = bool(keys >> BTN_GAMEPAD & 1 or keys >> BTN_JOYSTICK & 1)
    return buttons and bool(axes >> ABS_X & 1 or axes >> ABS_HAT0X & 1)


def gamepad_nodes() -> list[tuple[str, str]]:
    """(/dev/input/eventN, name) of every game controller, found through /sys."""
    out = []
    base = Path("/sys/class/input")
    if not base.is_dir():
        return out
    for d in sorted(base.glob("event*")):
        if is_gamepad(d):
            try:
                name = (d / "device" / "name").read_text().strip()
            except OSError:
                name = d.name
            out.append((f"/dev/input/{d.name}", name))
    return out


class _Device:
    def __init__(self, path: str, name: str, fd: int):
        self.path, self.name, self.fd = path, name, fd
        self.ranges: dict[int, tuple[int, int]] = {}
        for code in (ABS_X, ABS_Y):
            try:
                buf = fcntl.ioctl(fd, _eviocgabs(code), bytes(ABSINFO.size))
                _v, lo, hi, _f, _flat, _r = ABSINFO.unpack(buf)
                if hi > lo:
                    self.ranges[code] = (lo, hi)
            except OSError:
                pass
        self.stick = {ABS_X: 0, ABS_Y: 0}          # -1 / 0 / 1 per axis
        self.notifier: QSocketNotifier | None = None


class Gamepads(QObject):
    """Emits ``action`` ("up", "down", "left", "right", "a", "b", "x", "lb", "rb", "start", "select") and
    ``devices_changed`` (names of the connected controllers)."""

    action = Signal(str)
    devices_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.devices: dict[str, _Device] = {}
        self._denied: set[str] = set()
        self._held: str | None = None
        self._held_since = 0.0
        self._last: dict[str, tuple[float, str]] = {}
        self._repeat = QTimer(self)
        self._repeat.setInterval(int(REPEAT_EVERY * 1000))
        self._repeat.timeout.connect(self._tick)
        self._scan = QTimer(self)
        self._scan.setInterval(2000)
        self._scan.timeout.connect(self.rescan)
        self.enabled = os.name != "nt" and Path("/sys/class/input").is_dir()

    def start(self) -> None:
        if not self.enabled:
            return
        self.rescan()
        self._scan.start()

    def stop(self) -> None:
        self._scan.stop()
        self._repeat.stop()
        self._held = None
        for path in list(self.devices):
            self._close(path)

    def names(self) -> list[str]:
        return [d.name for d in self.devices.values()]

    # ------------------------------------------------------------- devices
    def rescan(self) -> None:
        found = dict(gamepad_nodes())
        changed = False
        for path in list(self.devices):
            if path not in found:
                self._close(path)
                changed = True
        for path, name in found.items():
            if path in self.devices or path in self._denied:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    self._denied.add(path)
                    log.info("controller %s (%s) cannot be opened: %s", name, path, exc.strerror)
                continue
            dev = _Device(path, name, fd)
            dev.notifier = QSocketNotifier(fd, QSocketNotifier.Read, self)
            dev.notifier.activated.connect(lambda _s, p=path: self._read(p))
            self.devices[path] = dev
            log.info("controller connected: %s (%s)", name, path)
            changed = True
        if changed:
            self.devices_changed.emit(self.names())

    def _close(self, path: str) -> None:
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        if dev.notifier is not None:
            dev.notifier.setEnabled(False)
            dev.notifier.deleteLater()
        try:
            os.close(dev.fd)
        except OSError:
            pass
        log.info("controller disconnected: %s", dev.name)

    # --------------------------------------------------------------- input
    def _read(self, path: str) -> None:
        dev = self.devices.get(path)
        if dev is None:
            return
        while True:
            try:
                data = os.read(dev.fd, EVENT.size * 64)
            except BlockingIOError:
                return
            except OSError:                       # unplugged
                self._close(path)
                self.devices_changed.emit(self.names())
                return
            if not data:
                return
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _s, _us, typ, code, value = EVENT.unpack_from(data, off)
                self._event(dev, typ, code, value)

    def _event(self, dev: _Device, typ: int, code: int, value: int) -> None:
        if typ == EV_KEY:
            name = BUTTONS.get(code) or BTN_DPAD.get(code)
            if name is None:
                return
            if code in BTN_DPAD:
                self._direction(dev, name if value else None, name)
            elif value == 1:
                self._emit(dev, name)
        elif typ == EV_ABS:
            if code in (ABS_HAT0X, ABS_HAT0Y):
                neg, pos = ("left", "right") if code == ABS_HAT0X else ("up", "down")
                self._direction(dev, neg if value < 0 else pos if value > 0 else None, neg, pos)
            elif code in (ABS_X, ABS_Y) and code in dev.ranges:
                lo, hi = dev.ranges[code]
                mid = (lo + hi) / 2
                x = (value - mid) / ((hi - lo) / 2)
                prev = dev.stick[code]
                state = -1 if x < -0.6 else 1 if x > 0.6 else (prev if prev * x > 0.35 else 0)
                if state != prev:
                    dev.stick[code] = state
                    neg, pos = ("left", "right") if code == ABS_X else ("up", "down")
                    self._direction(dev, neg if state < 0 else pos if state > 0 else None, neg, pos)

    def _direction(self, dev: _Device, pressed: str | None, *releases: str) -> None:
        if pressed is None:
            if self._held in releases:
                self._held = None
                self._repeat.stop()
            return
        self._emit(dev, pressed)
        self._held = pressed
        self._held_since = time.monotonic()
        self._repeat.start()

    def _tick(self) -> None:
        if self._held is None:
            self._repeat.stop()
            return
        if time.monotonic() - self._held_since >= REPEAT_DELAY:
            self.action.emit(self._held)

    def _emit(self, dev: _Device, name: str) -> None:
        now = time.monotonic()
        last = self._last.get(name)
        if last is not None and last[1] != dev.path and now - last[0] < DUPLICATE_WINDOW:
            return                                # the same press reported by a second (virtual) device
        self._last[name] = (now, dev.path)
        self.action.emit(name)
