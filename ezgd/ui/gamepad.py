"""Game controllers for couch mode (Linux evdev, no extra libraries).

Only devices that are game controllers are opened — recognised from ``/sys`` without opening anything else
(keyboards and mice are never read). Linux gives the person at the computer access to controllers (systemd's
``uaccess``); a controller that cannot be opened is skipped. Controllers can be plugged in and out at any time.

Buttons are reported by position, the way Linux names them: the bottom face button is "a" (confirm), the right one
"b" (back), the left/top ones "x" (details). D-pad and left stick move; holding a direction repeats (faster the
longer it is held). Shoulder buttons are "lb"/"rb", triggers "lt"/"rt", the guide button (PS / Xbox / Steam) "home".

The controller's family (PlayStation, Xbox, Steam, Nintendo) is recognised from its USB/Bluetooth vendor, so couch
mode can show the right button symbols (✕ ○ △ □ and L1/R1 on a DualSense, A B Y and LB/RB on an Xbox pad).
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

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
EVIOCGRAB = 0x40044590                              # _IOW('E', 0x90, int): this reader alone gets the device's events
EVIOCSCLOCKID = 0x400445A0                          # _IOW('E', 0xa0, int): the clock the event times are in
CLOCK_MONOTONIC = 1
KEY_BYTES = 96                                      # (KEY_MAX + 1) / 8
EVIOCGKEY = (2 << 30) | (KEY_BYTES << 16) | (ord("E") << 8) | 0x18      # the buttons held now
BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x132, 0x133, 0x134
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE = 0x13A, 0x13B, 0x13C
BTN_JOYSTICK, BTN_GAMEPAD = 0x120, 0x130
BTN_DPAD = {0x220: "up", 0x221: "down", 0x222: "left", 0x223: "right"}
ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_HAT0X, ABS_HAT0Y = 0x00, 0x01, 0x02, 0x05, 0x10, 0x11
ABS_RX, ABS_RY = 0x03, 0x04                         # the right stick
STICK_DEADZONE = 0.16
BUTTONS = {BTN_SOUTH: "a", BTN_EAST: "b", BTN_NORTH: "x", BTN_WEST: "x", BTN_C: "x", BTN_TL: "lb", BTN_TR: "rb",
           BTN_TL2: "lt", BTN_TR2: "rt", BTN_SELECT: "select", BTN_START: "start", BTN_MODE: "home"}
EVENT = struct.Struct("llHHi")                      # struct input_event (64-bit: timeval, type, code, value)
ABSINFO = struct.Struct("iiiiii")                   # value, minimum, maximum, fuzz, flat, resolution
REPEAT_DELAY, REPEAT_EVERY, REPEAT_FAST, FAST_AFTER = 0.36, 0.11, 0.055, 1.4

_pad_logged: dict[tuple[str, str], float] = {}     # (event, controller) → when it was last written to the log
PAD_LOG_QUIET = 300.0


def _log_pad(fmt: str, name: str, *args) -> None:
    """Controllers coming and going, in the log — once per controller and event every five minutes: Steam's
    virtual pad comes and goes every few seconds during a game, and a friend's log was mostly those lines."""
    now = time.monotonic()
    key = (fmt, name)
    quiet = now - _pad_logged.get(key, -PAD_LOG_QUIET) < PAD_LOG_QUIET
    if not quiet:
        _pad_logged[key] = now
    (log.debug if quiet else log.info)(fmt, name, *args)
_steam_hw_logged = False                            # what the log last said about a Valve controller
HOME_HOLD = 0.8                                     # seconds the guide button is held for the quick menu
DUPLICATE_WINDOW = 0.25                             # the same press from two devices (Steam's virtual pad + the real):
#                                                     Steam's copy can come late — even after the real button is up
STEAM_VIRTUAL = (0x28DE, 0x11FF)                    # Steam Input's virtual pad (mirrors a real controller)


def family_of(vendor: int, product: int, name: str) -> str:
    """playstation | xbox | steam | nintendo | generic — which button symbols the controller has."""
    n = (name or "").lower()
    if vendor == 0x054C or any(w in n for w in ("dualsense", "dualshock", "playstation", "ps5", "ps4")):
        return "playstation"
    if vendor == 0x28DE or "steam" in n:
        return "steam"
    if vendor == 0x045E or "xbox" in n or "x-box" in n:
        return "xbox"
    if vendor == 0x057E or any(w in n for w in ("nintendo", "pro controller", "joy-con")):
        return "nintendo"
    return "generic"


def steam_products(base: Path | None = None) -> set[int]:
    """Product ids of the connected Valve controllers (Steam Controller, Steam Deck, the new Steam Controller).
    While Steam runs it takes them over and, outside games, turns them into keyboard and mouse input — couch mode
    then only sees keys."""
    from ezgd.platform.steam import valve_controllers
    return valve_controllers(base)


def steam_hardware(base: Path | None = None) -> bool:
    return bool(steam_products(base))


def _sys_id(sys_event_dir: Path) -> tuple[int, int]:
    try:
        d = sys_event_dir / "device" / "id"
        return int((d / "vendor").read_text(), 16), int((d / "product").read_text(), 16)
    except (OSError, ValueError):
        return 0, 0


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
    return [(path, name) for path, name, _v, _p, _k in gamepad_info()]


def gamepad_info() -> list[tuple[str, str, int, int, int]]:
    """(/dev/input/eventN, name, vendor, product, key bits) of every game controller."""
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
            try:
                keys = _bits((d / "device" / "capabilities" / "key").read_text())
            except (OSError, ValueError):
                keys = 0
            vendor, product = _sys_id(d)
            out.append((f"/dev/input/{d.name}", name, vendor, product, keys))
    return out


class _Device:
    def __init__(self, path: str, name: str, fd: int, vendor: int = 0, product: int = 0, keys: int = 0):
        self.path, self.name, self.fd = path, name, fd
        self.vendor, self.product = vendor, product
        self.family = family_of(vendor, product, name)
        self.virtual = (vendor, product) == STEAM_VIRTUAL
        # analog triggers (ABS_Z / ABS_RZ) only where they are known to be triggers and there are no trigger buttons;
        # on other pads those axes can be the right stick
        self.analog_triggers = not (keys >> BTN_TL2 & 1) and (vendor in (0x045E, 0x28DE) or self.family == "xbox")
        self.trigger = {ABS_Z: False, ABS_RZ: False}
        self.ranges: dict[int, tuple[int, int]] = {}
        self.axes = {ABS_X: 0.0, ABS_Y: 0.0, ABS_RX: 0.0, ABS_RY: 0.0}   # -1 … 1 (the pointer in the browser)
        self.level = {ABS_Z: 0.0, ABS_RZ: 0.0, ABS_HAT0X: 0, ABS_HAT0Y: 0}   # triggers 0 … 1, d-pad axes
        for code in (ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_RX, ABS_RY, ABS_HAT0X, ABS_HAT0Y):
            try:
                buf = fcntl.ioctl(fd, _eviocgabs(code), bytes(ABSINFO.size))
                v, lo, hi, _f, _flat, _r = ABSINFO.unpack(buf)
                if hi > lo:
                    self.ranges[code] = (lo, hi)
                    if code in self.axes:                # where it is now (a stick held while it was opened)
                        self.axes[code] = max(-1.0, min(1.0, (v - (lo + hi) / 2) / ((hi - lo) / 2)))
                    elif code in (ABS_Z, ABS_RZ):
                        self.level[code] = (v - lo) / (hi - lo)
                    elif max(abs(lo), abs(hi)) <= 1:
                        self.level[code] = v
            except (OSError, ValueError):               # not an input device (or no such axis)
                pass
        self.stick = {ABS_X: 0, ABS_Y: 0}          # -1 / 0 / 1 per axis
        self.notifier: QSocketNotifier | None = None
        self.down: set[int] = set()                # buttons held now (any code)
        try:
            bits = int.from_bytes(fcntl.ioctl(fd, EVIOCGKEY, bytes(KEY_BYTES)), "little")
            self.down = {c for c in range(KEY_BYTES * 8) if bits >> c & 1}
        except (OSError, ValueError):
            pass
        self.grabbed = 0.0                         # when the app took the pad for itself (0: it has not)
        try:                                       # event times on the monotonic clock (see Gamepads.hold_virtual)
            fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
            self.mono = True
        except (OSError, ValueError):
            self.mono = False


class Gamepads(QObject):
    """Emits ``action`` ("up", "down", "left", "right", "a", "b", "x", "lb", "rb", "lt", "rt", "start", "select",
    "home"), ``devices_changed`` (names of the connected controllers) and ``family_changed`` (the button symbols to
    show: playstation | xbox | steam | nintendo | generic — of the controller used last)."""

    action = Signal(str)
    devices_changed = Signal(list)
    family_changed = Signal(str)
    button = Signal(str, bool)                       # a button went down / up (holding A holds a click)
    home_held = Signal()                             # the guide button (PS / Xbox / Steam) held down HOME_HOLD s

    def __init__(self, parent=None):
        super().__init__(parent)
        self.devices: dict[str, _Device] = {}
        self._denied: set[str] = set()
        self._held: str | None = None
        self._held_by: str | None = None                 # the device holding that direction
        self._held_since = 0.0
        self._last: dict[str, tuple[float, str]] = {}
        self._raw_owner: dict[str, tuple[str, float, bool]] = {}    # button → (device, pressed at, still down)
        self._raw_ignored: set[tuple[str, str]] = set()             # (button, device) presses dropped as copies
        self._home_timer = QTimer(self)
        self._home_timer.setSingleShot(True)
        self._home_timer.setInterval(int(HOME_HOLD * 1000))
        self._home_timer.timeout.connect(self.home_held.emit)
        self.family = ""                                 # "" until a controller is connected
        self.steam_hw = False                            # a Valve controller is plugged in (see steam_products)
        self.steam_up = False                            # … and Steam runs (only then can keys come from it)
        from ezgd.ui.steam_input import DesktopKeys, GuideWatch
        self.desktop = DesktopKeys()                     # … the keys Steam turns its buttons into (steam_input)
        self.guide = GuideWatch(self)                    # … and its Steam button, from Steam's log
        self.guide.tapped.connect(lambda: self._steam_guide(False))
        self.guide.held.connect(lambda: self._steam_guide(True))
        self._home_seen = 0.0                            # the guide button read from a controller, last
        self.pointer_mode = False                        # the left stick moves a pointer: no stick → directions
        self._holding = False                            # Steam's virtual pads are the app's alone (hold_virtual)
        self._repeat = QTimer(self)
        self._repeat.setInterval(int(REPEAT_EVERY * 1000))
        self._repeat.timeout.connect(self._tick)
        self._scan = QTimer(self)
        self._scan.setInterval(2000)
        self._scan.timeout.connect(self.rescan)
        self.enabled = os.name != "nt" and Path("/sys/class/input").is_dir() and \
            os.environ.get("EZGD_NO_GAMEPADS") != "1"          # (the tests: never the computer's real controllers)

    def start(self) -> None:
        if not self.enabled:
            return
        self.rescan()
        self._scan.start()

    def stop(self) -> None:
        self.guide.stop()
        self._scan.stop()
        self._repeat.stop()
        self._home_timer.stop()
        self._held = None
        for path in list(self.devices):
            self._close(path)

    def sticks(self) -> tuple[float, float, float, float]:
        """(left x, left y, right x, right y), each -1 … 1 with a dead zone — the strongest across controllers."""
        out = [0.0, 0.0, 0.0, 0.0]
        for d in self.devices.values():
            for k, code in enumerate((ABS_X, ABS_Y, ABS_RX, ABS_RY)):
                v = getattr(d, "axes", {}).get(code, 0.0)
                if abs(v) > abs(out[k]):
                    out[k] = v
        return tuple(0.0 if abs(v) < STICK_DEADZONE else (v - STICK_DEADZONE * (1 if v > 0 else -1))
                     / (1 - STICK_DEADZONE) for v in out)

    def names(self) -> list[str]:
        return [d.name for d in self.devices.values() if not d.virtual] or \
            [d.name for d in self.devices.values()]

    def _family_for(self, dev) -> str:
        """Steam's virtual pad mirrors a real controller: show that controller's buttons when one is connected."""
        if getattr(dev, "virtual", False):
            real = [d for d in self.devices.values() if not d.virtual]
            if real:
                return self.family if self.family and self.family != "steam" else real[0].family
            return "steam"
        return getattr(dev, "family", "generic")

    def _set_family(self, family: str) -> None:
        if family != self.family:
            self.family = family
            self.family_changed.emit(family)

    # ------------------------------------------------------------- the game's pad, the app's alone for a moment
    def hold_virtual(self, on: bool) -> None:
        """While the quick menu is open over a game: Steam's virtual pads are read by the app alone (EVIOCGRAB).
        The game reads the same pad (Steam's layout for it is kept while the menu is open, see steaminput), so every
        press in the menu reached the game too. A pad is only taken at rest — the game has to have seen every
        button, stick and trigger let go, or it keeps one held that it never sees released. Closing the device
        (the app quitting, Steam taking the pad away) gives it back by itself."""
        self._holding = on
        for dev in list(self.devices.values()):
            if dev.virtual:
                self._take(dev, on and self._at_rest(dev))

    @staticmethod
    def _at_rest(dev) -> bool:
        level = getattr(dev, "level", {})
        return not getattr(dev, "down", None) and \
            all(abs(v) < STICK_DEADZONE for v in getattr(dev, "axes", {}).values()) and \
            level.get(ABS_Z, 0.0) < 0.1 and level.get(ABS_RZ, 0.0) < 0.1 and \
            not level.get(ABS_HAT0X) and not level.get(ABS_HAT0Y)

    def _take(self, dev, on: bool) -> None:
        if bool(dev.grabbed) == on:
            return
        try:
            fcntl.ioctl(dev.fd, EVIOCGRAB, 1 if on else 0)
        except OSError as exc:                           # (EBUSY: something else has it — the game reads it as before)
            log.debug("controller %s: %s it for the quick menu failed: %s", dev.name, "taking" if on else "giving back",
                      exc)
            if not on:
                dev.grabbed = 0.0
            return
        dev.grabbed = (time.monotonic() if dev.mono else time.time()) if on else 0.0

    def _held_while_taken(self, dev, sec: int, usec: int) -> None:
        """After an input report of a virtual pad while the menu holds it. Not taken yet: taken once at rest.
        Taken, but the report is from before that (it reached the game as well) and leaves something held: given
        back, so the game also sees it let go — taken again at rest."""
        if dev.grabbed:
            if sec + usec / 1e6 <= dev.grabbed and not self._at_rest(dev):
                self._take(dev, False)
        elif self._at_rest(dev):
            self._take(dev, True)

    # ------------------------------------------------------------- devices
    def _steam_direct(self) -> bool:
        """The Valve controller is read directly (Steam does not have it: no keys come from it)."""
        return any(not d.virtual and d.family == "steam" for d in self.devices.values())

    def steam_key(self, qt_key: int) -> str | None:
        """A key that Steam sent for its controller's button → the couch action of that button (None: not one).
        Only while Steam runs: without Steam no key comes from the controller (a Steam Controller's receiver is
        always plugged in), and the keyboard's own Space, Tab or Page Down meant B, ✕ and Manage."""
        if not self.steam_as_keys():
            return None
        return self.desktop.action(qt_key)

    def steam_as_keys(self) -> bool:
        """Steam has the Valve controller and turns it into keys and mouse input (outside games)."""
        return self.steam_hw and self.steam_up and not self._steam_direct()

    def reachable(self, action: str) -> bool:
        """Can the controller in use do ``action`` here? Always — except a Steam controller sent as keys, whose
        Desktop Configuration may leave buttons without a key (the triggers are mouse clicks in Steam's defaults)."""
        if not self.steam_as_keys() or action not in ("a", "b", "x", "lb", "rb", "lt", "rt", "start", "select"):
            return True
        return action in self.desktop.keys.values()

    def steam_labels(self) -> dict[str, str]:
        """What the Steam controller's buttons are called (LB / RB on the original, L1 / R1 on the others; Y or X for
        Manage — whichever sends a key)."""
        return self.desktop.labels

    def _steam_guide(self, held: bool) -> None:
        """Steam logged its Steam button: a tap means "home" (over a game: the quick menu) — unless the button was read
        from the controller itself just now (the same press). A long press is Steam's own: Steam skips it ("Guide
        button skipped due to length") because it was its chord — the right pad moving the mouse, the triggers
        clicking, … —, and so does the app now: opening the quick menu when it ended took the mouse away from a
        friend's Steam Controller in games (2026-10-29)."""
        if time.monotonic() - self._home_seen < 1.5 or self._steam_direct():
            return
        self._set_family("steam")
        if held:
            log.info("Steam button held: Steam's own (its chord — the right pad moves the mouse), not the quick menu")
            return
        self.action.emit("home")

    def rescan(self) -> None:
        products = steam_products()
        steam_hw = bool(products)
        self.steam_up = False
        if steam_hw and not self._steam_direct():
            from ezgd.ui import steam_bpm
            self.steam_up = steam_bpm.steam_running()
            if self.steam_up:
                self.desktop.update(products)
                self.guide.start()
            else:
                self.guide.stop()
        else:
            self.guide.stop()
        if steam_hw != self.steam_hw:
            self.steam_hw = steam_hw
            global _steam_hw_logged
            if steam_hw != _steam_hw_logged:             # (once per change, not per listener: the desktop's and each
                _steam_hw_logged = steam_hw              #  quick menu's controllers start "not connected")
                log.info("Steam controller %s", "connected (Steam may hand it over as keyboard and mouse)"
                         if steam_hw else "gone")
            if steam_hw and not self.devices:
                self._set_family("steam")
            elif not steam_hw and self.family == "steam" and not self.devices:
                self._set_family("")
        info = {path: (name, v, pr, k) for path, name, v, pr, k in gamepad_info()}
        found = {path: i[0] for path, i in info.items()}
        changed = False
        for path in list(self.devices):
            if path not in found:
                self._close(path)
                changed = True
        # A controller that cannot be opened is tried again at every scan (said once): Steam's new virtual pad can
        # show up before Linux has given the person at the computer access to it — a friend's quick menu found it
        # "Permission denied" once and never read it again (2026-10-29), so it did not answer the controller.
        self._denied &= set(found)
        for path, name in found.items():
            if path in self.devices:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM) and path not in self._denied:
                    self._denied.add(path)
                    log.info("controller %s (%s) cannot be opened: %s", name, path, exc.strerror)
                    QTimer.singleShot(250, self, self.rescan)     # (access usually comes a moment later)
                continue
            self._denied.discard(path)
            _n, vendor, product, keys = info[path]
            dev = _Device(path, name, fd, vendor, product, keys)
            dev.notifier = QSocketNotifier(fd, QSocketNotifier.Read, self)
            # activated(socket, type): take any arguments, so ``p`` is never overwritten (that silently dropped
            # every press of every controller before 2026-10-13)
            dev.notifier.activated.connect(lambda *_args, p=path: self._read(p))
            self.devices[path] = dev
            _log_pad("controller connected: %s (%s)", name, path)
            if self._holding and dev.virtual:
                self._take(dev, self._at_rest(dev))      # (Steam made a new pad while the quick menu is open)
            changed = True
        if changed:
            if self.devices and (not self.family or not any(self._family_for(d) == self.family
                                                            for d in self.devices.values())):
                real = [d for d in self.devices.values() if not d.virtual]
                self._set_family(real[0].family if real else "steam")
            elif not self.devices:
                self._set_family("steam" if self.steam_hw else "")
            self.devices_changed.emit(self.names())

    def _close(self, path: str) -> None:
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        self._let_go(path)
        if dev.notifier is not None:
            dev.notifier.setEnabled(False)
            try:
                dev.notifier.activated.disconnect()      # drops the handler (and its reference to us)
            except (RuntimeError, TypeError):
                pass
            dev.notifier.deleteLater()
            dev.notifier = None
        try:
            os.close(dev.fd)
        except OSError:
            pass
        _log_pad("controller disconnected: %s", dev.name)

    def _let_go(self, path: str) -> None:
        """A controller went away with a direction or buttons held (unplugged — or Steam took its virtual pad away to
        switch a Steam controller's layout): they are let go. Its release never comes: a held stick kept scrolling
        forever, a held guide button opened the quick menu by itself, and a held button stayed "down" — the same
        button from the pad Steam made next was dropped as a copy."""
        if self._held is not None and self._held_by == path:
            self._held = self._held_by = None
            self._repeat.stop()
        for name, (owner, _at, down) in list(self._raw_owner.items()):
            if owner != path:
                continue
            del self._raw_owner[name]
            if down:
                if name == "home":
                    self._home_timer.stop()
                self.button.emit(name, False)            # (a held click in the couch browser is let go)
        self._raw_ignored = {(n, p) for n, p in self._raw_ignored if p != path}

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
                sec, usec, typ, code, value = EVENT.unpack_from(data, off)
                self._event(dev, typ, code, value)
                if typ == EV_SYN and code == 0 and self._holding and dev.virtual and path in self.devices:
                    self._held_while_taken(dev, sec, usec)         # (a whole report read: its state is complete)

    def _event(self, dev: _Device, typ: int, code: int, value: int) -> None:
        if typ == EV_KEY and value in (0, 1) and hasattr(dev, "down"):
            (dev.down.add if value else dev.down.discard)(code)
        elif typ == EV_ABS and code in (ABS_Z, ABS_RZ, ABS_HAT0X, ABS_HAT0Y) and hasattr(dev, "level"):
            lo, hi = dev.ranges.get(code, (-1, 1))
            if code in (ABS_Z, ABS_RZ):
                dev.level[code] = (value - lo) / max(1, hi - lo)
            elif max(abs(lo), abs(hi)) <= 1:                   # (not a trackpad's touch position)
                dev.level[code] = value
        if typ == EV_KEY:
            name = BUTTONS.get(code) or BTN_DPAD.get(code)
            if name is None:
                return
            if code in BTN_DPAD:
                if value == 2:
                    return                       # the kernel's own key repeat: holding repeats here (as buttons do)
                self._direction(dev, name if value else None, name)
            else:
                if value in (0, 1):
                    self._raw(dev, name, bool(value))
                if value == 1:
                    self._emit(dev, name)
        elif typ == EV_ABS:
            if code in (ABS_HAT0X, ABS_HAT0Y):
                lo, hi = dev.ranges.get(code, (-1, 1))
                if max(abs(lo), abs(hi)) > 1:
                    return    # a trackpad's touch position (Steam Controller / Deck, kernel hid-steam): its click
                    #           comes as the d-pad buttons — resting a thumb on it must not move anything
                neg, pos = ("left", "right") if code == ABS_HAT0X else ("up", "down")
                self._direction(dev, neg if value < 0 else pos if value > 0 else None, neg, pos)
            elif code in (ABS_Z, ABS_RZ) and getattr(dev, "analog_triggers", False) and code in dev.ranges:
                lo, hi = dev.ranges[code]
                x = (value - lo) / max(1, hi - lo)
                was = dev.trigger[code]
                now = x > 0.55 if not was else x > 0.3
                if now != was:
                    dev.trigger[code] = now
                    if now:
                        self._emit(dev, "lt" if code == ABS_Z else "rt")
            elif code in (ABS_RX, ABS_RY) and code in dev.ranges:
                lo, hi = dev.ranges[code]
                dev.axes[code] = max(-1.0, min(1.0, (value - (lo + hi) / 2) / ((hi - lo) / 2)))
            elif code in (ABS_X, ABS_Y) and code in dev.ranges:
                lo, hi = dev.ranges[code]
                mid = (lo + hi) / 2
                x = (value - mid) / ((hi - lo) / 2)
                if hasattr(dev, "axes"):
                    dev.axes[code] = max(-1.0, min(1.0, x))
                if self.pointer_mode:                    # the stick is the pointer now, not up/down/left/right
                    if dev.stick[code]:
                        dev.stick[code] = 0
                        neg, pos = ("left", "right") if code == ABS_X else ("up", "down")
                        self._direction(dev, None, neg, pos)
                    return
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
        self._held, self._held_by = pressed, dev.path
        self._held_since = time.monotonic()
        self._repeat.setInterval(int(REPEAT_EVERY * 1000))
        self._repeat.start()

    def _tick(self) -> None:
        if self._held is None:
            self._repeat.stop()
            return
        held = time.monotonic() - self._held_since
        if held >= REPEAT_DELAY:
            self.action.emit(self._held)
        if held >= FAST_AFTER and self._repeat.interval() != int(REPEAT_FAST * 1000):
            self._repeat.setInterval(int(REPEAT_FAST * 1000))     # held a while: scroll faster

    def _raw(self, dev: _Device, name: str, down: bool) -> None:
        """Button down / up, once per press however many devices report it. Steam's virtual pad repeats every press
        of the real controller, sometimes after the real button was already let go: in the couch browser (A = the
        mouse button) that was a second click — a file host's download button started the download twice."""
        now = time.monotonic()
        owner = self._raw_owner.get(name)
        if down:
            if owner is not None and owner[0] != dev.path and (owner[2] or now - owner[1] < DUPLICATE_WINDOW):
                self._raw_ignored.add((name, dev.path))
                return
            self._raw_owner[name] = (dev.path, now, True)
            if name == "home":
                self._home_seen = now
                self._home_timer.start()
            self.button.emit(name, True)
            return
        if (name, dev.path) in self._raw_ignored:
            self._raw_ignored.discard((name, dev.path))
            return
        if owner is not None:
            if owner[0] != dev.path:
                return                            # not the device that pressed it
            self._raw_owner[name] = (owner[0], owner[1], False)
        if name == "home":
            self._home_seen = now
            self._home_timer.stop()
        self.button.emit(name, False)

    def _emit(self, dev: _Device, name: str) -> None:
        now = time.monotonic()
        last = self._last.get(name)
        if last is not None and last[1] != dev.path and now - last[0] < DUPLICATE_WINDOW:
            return                                # the same press reported by a second (virtual) device
        self._last[name] = (now, dev.path)
        self._set_family(self._family_for(dev))
        self.action.emit(name)
