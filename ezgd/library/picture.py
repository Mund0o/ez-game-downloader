"""What a test game showed: its pictures, judged — an error it drew itself, a menu, a moving picture, black, frozen.

"Kept running for 45 s" alone can mean a black screen, a loading screen that never ends or an error the game drew
itself. A hidden test's window is photographed a few times (ezgd.platform.hidden.capture); each picture is reduced
to a 64×36 brightness map (black? the same as the last one?) and its text read with Tesseract (dim text — Expedition
33's grey "PRESS ANY BUTTON" on black — is stretched to full contrast first). The pictures are not kept, except one
small copy of the last for the investigation's report.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MENU = re.compile(r"\b(press any (key|button)|press (start|enter|a)\b|new game|continue|load game|start game|"
                  r"main menu|single ?player|multi ?player|campaign|click to (start|continue|play)|"
                  r"tap to start|press .{1,12} to (start|continue))\b", re.I)
MENU_WEAK = re.compile(r"\b(play|options|settings|quit|exit|credits|extras|online|co-?op)\b", re.I)


@dataclass
class Look:
    at: int                                   # seconds into the test
    mean: float = 0.0
    peak: int = 0
    grid: bytes = b""                         # 64×36 brightness map
    text: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    menu: bool = False

    @property
    def black(self) -> bool:
        return self.peak < 16

    def differs(self, other: "Look") -> bool:
        if not self.grid or not other.grid or len(self.grid) != len(other.grid):
            return True
        changed = sum(1 for a, b in zip(self.grid, other.grid) if abs(a - b) > 12)
        return changed > len(self.grid) // 100           # more than 1 % of the picture changed


def _gray(img):
    from PySide6.QtGui import QImage
    return img.convertToFormat(QImage.Format.Format_Grayscale8)


def _bytes(g) -> bytes:
    """A Grayscale8 image's pixels, row padding removed."""
    w, h, bpl = g.width(), g.height(), g.bytesPerLine()
    raw = bytes(g.constBits())[:bpl * h]
    return raw if bpl == w else b"".join(raw[y * bpl:y * bpl + w] for y in range(h))


def _stretched(img):
    """The picture as grey levels stretched to full contrast (dim menu text becomes readable)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    small = _gray(img.scaled(1920, 1080, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
    px = _bytes(small)
    hist = [0] * 256
    for v in px[::7]:
        hist[v] += 1
    total = sum(hist) or 1
    acc, lo, hi = 0, 0, 255
    for v in range(256):
        acc += hist[v]
        if acc >= total * 0.02:
            lo = v
            break
    acc = 0
    for v in range(255, -1, -1):
        acc += hist[v]
        if acc >= total * 0.002:
            hi = v
            break
    if hi - lo < 8:
        return small
    lut = bytes(max(0, min(255, (v - lo) * 255 // (hi - lo))) for v in range(256))
    out = px.translate(lut)
    return QImage(out, small.width(), small.height(), small.width(), QImage.Format.Format_Grayscale8).copy()


def ocr(img, timeout: float = 20.0) -> str:
    if not shutil.which("tesseract"):
        return ""
    fd, name = tempfile.mkstemp(prefix="ezgd-look-", suffix=".png")
    os.close(fd)
    try:
        _stretched(img).save(name, "PNG")
        r = subprocess.run(["tesseract", name, "stdout", "--psm", "11"], capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def analyze(img, at: int, read_text: bool = True) -> Look:
    from PySide6.QtCore import Qt
    look = Look(at)
    if img is None or img.isNull():
        return look
    g = _gray(img.scaled(64, 36, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
    look.grid = _bytes(g)
    look.mean = sum(look.grid) / max(1, len(look.grid))
    look.peak = max(look.grid) if look.grid else 0
    if read_text and not look.black:
        from ezgd.library.screenread import STRONG, error_lines
        text = ocr(img)
        look.text = [" ".join(ln.split()) for ln in text.splitlines() if len(ln.strip()) >= 3][:40]
        look.errors = [ln for ln in error_lines(text) if STRONG.search(ln)]
        joined = " ".join(look.text)
        look.menu = bool(MENU.search(joined)) or len(set(m.group(0).lower() for m in MENU_WEAK.finditer(joined))) >= 3
    return look


def judge(looks: list[Look]) -> tuple[str, str]:
    """('error' | 'menu' | 'moving' | 'black' | 'frozen' | 'unknown', in words)."""
    seen = [lk for lk in looks if lk.grid]
    if not seen:
        return "unknown", ""
    err = next((lk for lk in seen if lk.errors), None)
    if err is not None:
        return "error", f"its screen showed: “{err.errors[0][:160]}”"
    menu = next((lk for lk in seen if lk.menu), None)
    if menu is not None:
        words = next((t for t in menu.text if MENU.search(t)), menu.text[0] if menu.text else "")
        return "menu", f"it showed its menu (“{words[:60]}”) after {menu.at}s"
    if all(lk.black for lk in seen):
        return "black", f"its picture stayed black ({len(seen)} looks)"
    last = [lk for lk in seen if not lk.black][-2:]
    if len(last) == 2 and not last[1].differs(last[0]):
        return "frozen", f"its picture did not change between {last[0].at}s and {last[1].at}s"
    if len(seen) >= 2 and any(seen[i].differs(seen[i - 1]) for i in range(1, len(seen))):
        return "moving", "its picture was changing"
    return "unknown", ""


def thumbnail(img, path: Path) -> str | None:
    """A small copy of the last picture, for the investigation's report."""
    from PySide6.QtCore import Qt
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        small = img.scaled(960, 540, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return str(path) if small.save(str(path), "JPG", 80) else None
    except Exception:
        return None
