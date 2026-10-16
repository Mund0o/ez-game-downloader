"""What a Steam controller's buttons send while Steam runs — read from Steam itself, not assumed.

While Steam runs it owns Valve's controllers (Steam Controller, Steam Deck, the new Steam Controller). Outside games it
drives them with the **Desktop Configuration** (Steam → Settings → Controller; Steam's app 413080): buttons become keys
and mouse input. Couch mode turns those keys back into the buttons that sent them.

Which configuration that is, Steam writes to its own log each time it loads one::

    Loaded Config for Local Selection Path for App ID 413080, Controller 0: …/controller_base/desktop_neptune.vdf

— Steam's default for that controller, or the user's own (changed in Steam's settings). That file is read (Steam's
text format, versions 2 and 3) and every key a button sends is mapped to that button. Without a log line, Steam's
default file for the controller is used (``controller_base/desktop_*.vdf``), and without Steam's files the defaults
below (copied from those files).

Steam also logs the Steam button itself — ``Guide button sent to JS`` for a tap, ``Guide button skipped due to
length`` for a long hold — which ``GuideWatch`` reports, so the Steam button works like the guide button of any other
controller (the app cannot read the controller itself while Steam has it).

Nothing of Steam's is ever changed: its files and logs are only read.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal

log = logging.getLogger(__name__)

DESKTOP_APP = 413080          # Steam's "Desktop Configuration" (Steam's UI code: Settings_Controller_DesktopConfig)
LOGS = ("controller_ui.txt", "controller.txt")
_LOADED = re.compile(r"Loaded Config for [A-Za-z ]+ Path for App ID %d, Controller (\d+): (.+?)\s*$" % DESKTOP_APP)
GUIDE_TAP = "Guide button sent to JS"
GUIDE_HOLD = "Guide button skipped due to length"

# Valve's product ids (USB / Bluetooth). The original Steam Controller ("gordon": kernel hid-steam 1102 / 1142, its
# Bluetooth ids 1105 / 1106 and the 1201 / 1202 variant in SDL's mappings) and the Steam Deck ("neptune", 1205).
GORDON = {0x1101, 0x1102, 0x1105, 0x1106, 0x1142, 0x1201, 0x1202}
NEPTUNE = {0x1205}
VIRTUAL = {0x11FF, 0x11FC}    # Steam Input's virtual pads (not hardware)

# Steam's default desktop layouts (controller_base/desktop_steamcontroller_gordon.vdf and desktop_neptune.vdf) — only
# used when Steam's own files cannot be read.
DEFAULT_KEYS = {
    "gordon": {"ENTER": "a", "SPACE": "b", "PAGE_UP": "x", "PAGE_DOWN": "x", "LEFT_CONTROL": "lb",
               "LEFT_ALT": "rb", "ESCAPE": "b", "TAB": "start"},
    "neptune": {"RETURN": "a", "ESCAPE": "b", "SPACE": "x", "LEFT_CONTROL": "lb", "LEFT_ALT": "rb", "TAB": "start"},
}

# a physical input → what it means in couch mode
INPUTS = {"button_a": "a", "button_b": "b", "button_y": "x", "button_x": "x",
          "left_bumper": "lb", "right_bumper": "rb", "button_menu": "start", "button_escape": "b",
          "dpad_north": "up", "dpad_south": "down", "dpad_west": "left", "dpad_east": "right"}
SOURCES = {"left_trigger": "lt", "right_trigger": "rt"}
ORDER = ("button_a", "button_b", "button_y", "button_x", "left_bumper", "right_bumper", "button_menu",
         "button_escape")          # when two buttons send the same key, the first of these counts


# ------------------------------------------------------------------ Steam's text format (repeated keys kept)
def parse(text: str) -> list:
    """Steam's text KeyValues as nested [(key, value)] lists (values: str or a list) — keys may repeat."""
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    out: list = []
    stack = [out]
    i = 0
    while i < len(toks):
        s, br = toks[i]
        if br == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        if br == "{":                                    # (a block without a key: skip its brace)
            i += 1
            continue
        if i + 1 < len(toks) and toks[i + 1][1] == "{":
            child: list = []
            stack[-1].append((s, child))
            stack.append(child)
            i += 2
        elif i + 1 < len(toks):
            stack[-1].append((s, toks[i + 1][0]))
            i += 2
        else:
            break
    return out


def _get(node: list, key: str) -> list:
    return [v for k, v in node if k.lower() == key.lower()]


def _first(node: list, key: str, default=None):
    v = _get(node, key)
    return v[0] if v else default


def binding(value: str) -> str:
    """A binding without the label Steam's configurator adds to layouts saved by the user: ``key_press TAB, , `` and
    ``key_press LEFT_CONTROL, Left Control, , `` are ``key_press TAB`` / ``key_press LEFT_CONTROL`` (Steam's own
    default files have no label)."""
    return value.split(",", 1)[0].strip()


def _bindings(node) -> list[str]:
    """Every "binding" below ``node`` (v3: activators → Full_Press → bindings → binding)."""
    out = []
    if not isinstance(node, list):
        return out
    for k, v in node:
        if k.lower() == "binding" and isinstance(v, str):
            out.append(binding(v))
        elif isinstance(v, list):
            out += _bindings(v)
    return out


def bindings_of(text: str, modes: dict | None = None) -> dict[str, list[str]]:
    """``source.input`` → its bindings (e.g. ``button_diamond.button_a`` → ["key_press RETURN"]), for the groups
    active in the configuration's first preset (what Steam uses outside games). ``modes`` (optional) gets each
    source's mode (``right_trackpad`` → ``absolute_mouse``, …)."""
    try:
        root = parse(text)
        cm = _first(root, "controller_mappings") or []
    except Exception:
        return {}
    groups = {str(_first(g, "id")): g for g in _get(cm, "group") if isinstance(g, list)}
    presets = [p for p in _get(cm, "preset") if isinstance(p, list)]
    preset = next((p for p in presets if str(_first(p, "id")) == "0"), presets[0] if presets else None)
    out: dict[str, list[str]] = {}
    if preset is None:
        return out

    def add(source: str, group: list) -> None:
        if modes is not None and isinstance(_first(group, "mode"), str):
            modes[source] = _first(group, "mode")
        flat = _first(group, "bindings")                 # version 2: input → binding
        if isinstance(flat, list):
            for k, v in flat:
                if isinstance(v, str):
                    out.setdefault(f"{source}.{k.lower()}", []).append(binding(v))
        inputs = _first(group, "inputs")                 # version 3: input → activators → … → binding
        if isinstance(inputs, list):
            for k, v in inputs:
                b = _bindings(v)
                if b:
                    out.setdefault(f"{source}.{k.lower()}", []).extend(b)

    gsb = _first(preset, "group_source_bindings") or []
    for gid, src in gsb if isinstance(gsb, list) else []:
        parts = str(src).split()
        if len(parts) > 1 and parts[1] != "active":
            continue
        g = groups.get(str(gid))
        if g is not None:
            add(parts[0] if parts else "", g)
    sw = _first(preset, "switch_bindings")                # version 2 keeps the plain buttons here
    if isinstance(sw, list):
        add("switch", sw)
    return out


def keys_of(text: str) -> dict[str, str]:
    """Steam key name (``RETURN``, ``PAGE_DOWN``, …) → couch action, for the keys the configuration's buttons send."""
    binds = bindings_of(text)
    out: dict[str, str] = {}

    def rank(item):
        _src, _, inp = item[0].partition(".")
        return ORDER.index(inp) if inp in ORDER else len(ORDER)

    for name, bs in sorted(binds.items(), key=rank):
        source, _, inp = name.partition(".")
        action = SOURCES.get(source) or INPUTS.get(inp)
        if source in ("left_trackpad", "right_trackpad") and inp.startswith("dpad_"):
            action = INPUTS.get(inp)
        if action is None:
            continue
        for b in bs:
            parts = b.split()
            if len(parts) >= 2 and parts[0] == "key_press":
                out.setdefault(parts[1].upper(), action)
    return out


def labels_of(text: str, kind: str) -> dict[str, str]:
    """Which button to show for an action: the one whose key means it (e.g. "x" → "Y" when Y sends that key)."""
    binds = bindings_of(text)
    labels = dict(LABELS.get(kind, LABELS["neptune"]))
    for inp, letter in (("button_y", "Y"), ("button_x", "X")):
        bs = [b for n, v in binds.items() if n.endswith("." + inp) for b in v]
        if any(b.startswith("key_press") for b in bs):
            labels["x"] = letter                         # the first of Y, X that sends a key: Manage
            break
    return labels


POINTER_MODES = ("absolute_mouse", "mouse_joystick", "joystick_mouse", "mouse_region", "relative_mouse")


def browser_hints(text: str, labels: dict[str, str]) -> list[tuple[str, str]]:
    """What the controller does in a web page under this configuration (keys and mouse go to the page itself):
    (button name, meaning) — the pointer, the click, scrolling, Steam's keyboard and Esc (closes the browser)."""
    modes: dict[str, str] = {}
    binds = bindings_of(text, modes)
    names = {"right_trackpad": "R-PAD", "left_trackpad": "L-PAD", "joystick": "STICK", "right_joystick": "R-STICK",
             "dpad": "D-PAD", "right_trigger": labels.get("rt", "RT"), "left_trigger": labels.get("lt", "LT"),
             "button_a": labels.get("a", "A"), "button_b": "B", "button_x": "X", "button_y": "Y",
             "button_escape": labels.get("select", "◀"), "button_menu": labels.get("start", "▶"),
             "left_bumper": labels.get("lb", "LB"), "right_bumper": labels.get("rb", "RB")}

    def name(key: str) -> str:
        source, _, inp = key.partition(".")
        if source in ("switch", "button_diamond") or inp in names:
            return names.get(inp, inp.upper())
        base = names.get(source, source.upper())
        return base + (" click" if inp == "click" and source not in ("right_trigger", "left_trigger") else "")

    def first(pred, prefer: tuple = ()) -> str | None:
        hits = [key for key, bs in binds.items() if any(pred(b) for b in bs)]
        hits.sort(key=lambda k: next((i for i, p in enumerate(prefer) if k.startswith(p) or k.endswith(p)),
                                     len(prefer)))
        return name(hits[0]) if hits else None
    out = []
    pointer = next((names.get(src, src.upper()) for src, m in modes.items() if m in POINTER_MODES), None)
    if pointer:
        out.append((pointer, "Pointer"))
    click = first(lambda b: b == "mouse_button LEFT", ("right_trigger", "left_trigger"))
    if click:
        out.append((click, "Click"))
    scroll = next((names.get(src, src.upper()) for src, m in modes.items() if m == "scrollwheel"), None)
    if scroll:
        out.append((scroll, "Scroll"))
    kb = first(lambda b: b.upper().startswith("CONTROLLER_ACTION SHOW_KEYBOARD"))
    if kb:
        out.append((kb, "Keyboard"))
    esc = first(lambda b: b.upper() == "KEY_PRESS ESCAPE", (".button_b", ".button_escape"))
    if esc:
        out.append((esc, "Close"))
    return out


# what the buttons are called on each (Steam's own glyphs: sc_lb … for the Steam Controller, sd_l1 … for the Deck
# and sc_l1 … for the new Steam Controller)
LABELS = {
    "gordon": {"a": "A", "b": "B", "x": "Y", "lb": "LB", "rb": "RB", "lt": "LT", "rt": "RT", "start": "▶",
               "select": "◀", "home": "STEAM"},
    "neptune": {"a": "A", "b": "B", "x": "Y", "lb": "L1", "rb": "R1", "lt": "L2", "rt": "R2", "start": "☰",
                "select": "⧉", "home": "STEAM"},
}


def kind_of(products: set[int]) -> str:
    """gordon (the original Steam Controller) | neptune (Steam Deck) | "" (another Valve controller: the new Steam
    Controller, whose layout Steam names in its log)."""
    if products & GORDON:
        return "gordon"
    if products & NEPTUNE:
        return "neptune"
    return ""


# ------------------------------------------------------------------ where Steam is, what it loaded
def steam_root() -> Path | None:
    try:
        from ezgd.platform.steam import find_steam_root
        return find_steam_root()
    except Exception:
        return None


def _tail(path: Path, size: int = 256 * 1024) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            n = f.tell()
            f.seek(max(0, n - size))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


_log_cache: dict[str, tuple[tuple, list]] = {}


def _loaded_lines(path: Path) -> list[tuple[str, str, str]]:
    """(timestamp, controller, path) of every desktop-configuration load in one log — read again only when it
    changed."""
    try:
        st = path.stat()
        key = (st.st_size, st.st_mtime_ns, st.st_ino)
    except OSError:
        return []
    hit = _log_cache.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1]
    found = []
    for line in _tail(path).splitlines():
        m = _LOADED.search(line)
        if m:
            found.append((line[1:20] if line.startswith("[") else "", m.group(1), m.group(2)))
    _log_cache[str(path)] = (key, found)
    return found


def logged_desktop_configs(root: Path) -> dict[str, Path]:
    """Controller index → the desktop configuration file Steam loaded for it last (from Steam's logs)."""
    found: list[tuple[str, str, str]] = []                      # (timestamp, controller, path)
    for name in LOGS:
        found += _loaded_lines(root / "logs" / name)
    out: dict[str, Path] = {}
    for _stamp, ctl, p in sorted(found, key=lambda t: t[0]):    # (stable: the later line wins)
        path = Path(p)
        if not path.is_absolute():
            path = root / path
        out[ctl] = path
    return out


class DesktopKeys:
    """The keys the connected Steam controller's buttons send outside games, kept up to date (cached by file)."""

    def __init__(self):
        self.keys: dict[str, str] = {}
        self.labels: dict[str, str] = dict(LABELS["neptune"])
        self.browser: list[tuple[str, str]] = []         # what the buttons do in a web page (browser_hints)
        self.source = ""
        self._seen: tuple = ()

    def update(self, products: set[int]) -> None:
        root = steam_root()
        kind = kind_of(products)
        files: list[Path] = []
        if root is not None:
            files = [p for p in logged_desktop_configs(root).values() if p.name != "empty.vdf"]
            if not files:
                name = "steamcontroller_gordon" if kind == "gordon" else "neptune"    # (another Valve controller: the
                files = [root / "controller_base" / f"desktop_{name}.vdf"]            # Deck's, the same buttons)
        stamp = tuple((str(p), _mtime(p)) for p in files) + (kind,)
        if stamp == self._seen:
            return
        self._seen = stamp
        keys: dict[str, str] = {}
        labels = dict(LABELS.get(kind or "neptune"))
        browser: list[tuple[str, str]] = []
        used = ""
        for p in files:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            k = keys_of(text)
            if not k:
                continue
            for key, action in k.items():
                keys.setdefault(key, action)
            file_kind = kind or ("gordon" if "steamcontroller_gordon" in text else "neptune")
            labels = labels_of(text, file_kind)
            browser = browser or browser_hints(text, labels)
            used = used or str(p)
        if not keys:
            keys = dict(DEFAULT_KEYS["gordon" if kind == "gordon" else "neptune"])
            used = f"built-in {kind or 'neptune'} defaults"
        keys = normalise(keys)
        if keys != self.keys or used != self.source:
            log.info("Steam controller: buttons read from %s — %s", used,
                     ", ".join(f"{k}={v}" for k, v in sorted(keys.items())))
        self.keys, self.labels, self.source = keys, labels, used
        self.browser = browser or [("R-PAD", "Pointer"), (labels["rt"], "Click"), ("L-PAD", "Scroll"),
                                   (labels["select"], "Close")]          # (Steam's defaults)

    def action(self, qt_key: int) -> str | None:
        name = QT_NAMES.get(qt_key)
        return self.keys.get(name) if name else None


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# Steam's key names → Qt keys (the keys a desktop layout can send)
QT_NAMES: dict[int, str] = {
    Qt.Key_Return: "RETURN", Qt.Key_Enter: "ENTER", Qt.Key_Escape: "ESCAPE", Qt.Key_Space: "SPACE",
    Qt.Key_Tab: "TAB", Qt.Key_Backspace: "BACKSPACE", Qt.Key_PageUp: "PAGE_UP", Qt.Key_PageDown: "PAGE_DOWN",
    Qt.Key_Home: "HOME", Qt.Key_End: "END", Qt.Key_Insert: "INSERT", Qt.Key_Delete: "DELETE",
    Qt.Key_Control: "LEFT_CONTROL", Qt.Key_Alt: "LEFT_ALT", Qt.Key_Shift: "LEFT_SHIFT", Qt.Key_Meta: "LEFT_WINDOWS",
    Qt.Key_Up: "UP_ARROW", Qt.Key_Down: "DOWN_ARROW", Qt.Key_Left: "LEFT_ARROW", Qt.Key_Right: "RIGHT_ARROW",
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    QT_NAMES[getattr(Qt, f"Key_{_c}")] = _c
for _n in range(1, 13):
    QT_NAMES[getattr(Qt, f"Key_F{_n}")] = f"F{_n}"


def normalise(keys: dict[str, str]) -> dict[str, str]:
    """Steam names the same key two ways in places (ENTER / RETURN, RIGHT_ and LEFT_ modifiers): both count."""
    out = dict(keys)
    pairs = (("ENTER", "RETURN"), ("LEFT_CONTROL", "RIGHT_CONTROL"), ("LEFT_ALT", "RIGHT_ALT"),
             ("LEFT_SHIFT", "RIGHT_SHIFT"), ("LEFT_WINDOWS", "RIGHT_WINDOWS"))
    for a, b in pairs:
        if a in out and b not in out:
            out[b] = out[a]
        elif b in out and a not in out:
            out[a] = out[b]
    for a, b in pairs:                                    # Qt reports one key for both sides
        if b in out and a not in out:
            out[a] = out[b]
    return out


# ------------------------------------------------------------------ the Steam button
class GuideWatch(QObject):
    """Reports the Steam button from Steam's log (``tapped`` / ``held``) while Steam runs. Only new lines count."""

    tapped = Signal()
    held = Signal()
    POLL_MS = 120

    def __init__(self, parent=None, root: Path | None = None):
        super().__init__(parent)
        self.root = root
        self._pos: int | None = None
        self._ino = None
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self.poll)

    @property
    def path(self) -> Path | None:
        root = self.root or steam_root()
        return root / "logs" / "controller_ui.txt" if root is not None else None

    def start(self) -> None:
        if self._timer.isActive():
            return
        self._pos = None
        self.poll()                                      # (only lines written from now on)
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def poll(self) -> None:
        p = self.path
        if p is None:
            return
        try:
            st = p.stat()
        except OSError:
            self._pos = 0
            return
        if self._pos is None or st.st_ino != self._ino:
            if self._pos is not None and st.st_ino != self._ino:
                self._pos = 0                            # (Steam started a new log: read it from the top)
            else:
                self._pos = st.st_size
            self._ino = st.st_ino
        if st.st_size < self._pos:
            self._pos = 0                                # (emptied)
        if st.st_size == self._pos:
            return
        try:
            with open(p, "rb") as f:
                f.seek(self._pos)
                data = f.read(st.st_size - self._pos)
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return                                       # (half a line: next time)
        self._pos += end + 1
        for line in data[:end].decode("utf-8", "replace").splitlines():
            if GUIDE_TAP in line:
                self.tapped.emit()
            elif GUIDE_HOLD in line:
                self.held.emit()

