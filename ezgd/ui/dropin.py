"""Getting things into the app without hunting for the right dialog.

* Drop on the window: a ``.torrent`` file, a magnet / download link (from the browser's address bar or a page), a game
  folder or a downloaded archive (all parts of a multi-part set count once). Links and torrents open *Add download*
  filled in; folders and archives are imported into the Library.
* A magnet link (or a link to a ``.torrent``) on the clipboard: coming back to the app offers to add it — once per
  link, nothing happens without a click.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ezgd.ui import theme

ARCHIVES = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".zst")


def _is_link(text: str) -> bool:
    t = text.strip()
    if t.startswith("magnet:?"):
        return True
    url = QUrl(t)
    return url.scheme() in ("http", "https") and bool(url.host()) and " " not in t


def _is_archive(p: Path) -> bool:
    from ezgd.installation.volumes import volume_info
    name = p.name.lower()
    return name.endswith(ARCHIVES) or volume_info(p.name) is not None or bool(
        name[-4:-3] == "." and name[-3:].isdigit())


def classify(mime) -> list[tuple[str, str]]:
    """What a drop holds: ("link", url) / ("torrent", path) / ("import", path), multi-part sets once (first part)."""
    from ezgd.installation.volumes import volume_info
    out: list[tuple[str, str]] = []
    sets: set[str] = set()
    if mime.hasUrls():
        for url in mime.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.suffix.lower() == ".torrent" and p.is_file():
                    out.append(("torrent", str(p)))
                elif p.is_dir():
                    out.append(("import", str(p)))
                elif p.is_file() and _is_archive(p):
                    v = volume_info(p.name)
                    if v is not None:
                        if v.set_key in sets:
                            continue
                        sets.add(v.set_key)
                        first = p.with_name(v.first_name)
                        p = first if first.is_file() else p
                    out.append(("import", str(p)))
            elif _is_link(url.toString()):
                out.append(("link", url.toString()))
    elif mime.hasText():
        for line in mime.text().splitlines():
            if _is_link(line):
                out.append(("link", line.strip()))
    return out


def clipboard_link() -> str | None:
    """A magnet link or a link to a .torrent file on the clipboard (not any web address: those are too common)."""
    text = (QGuiApplication.clipboard().text() or "").strip()
    if len(text) > 4000 or "\n" in text:
        return None
    if text.startswith("magnet:?xt="):
        return text
    url = QUrl(text)
    if url.scheme() in ("http", "https") and url.path().lower().endswith(".torrent"):
        return text
    return None


_EXT = re.compile(r"(\.(part\d+|r\d{2,3}|\d{3}))?(\.(zip|7z|rar|tar|gz|tgz|xz|bz2|zst))*(\.\d{3})?$", re.I)
_TAIL = re.compile(r"[\s._-]+(v\d[\w.]*|build[\s._]*\d+|update[\s._]*\d[\w.]*|repack|multi\d*|"
                   r"x64|x86|win(64|32)?|linux|portable|(steamrip(\.com)?)|ofme|online-?fix(\.me)?|"
                   r"fitgirl|dodi|rune|codex|tenoke|elamigos|gog|plaza|skidrow|empress|razor1911|flt|"
                   r"cracked-games(\.org)?|ankergames)\b.*$", re.I)


def guess_title(path: str) -> str:
    """A readable game title from a folder or archive name: ``How.to.Fish.v1.0.10-OFME.part1.rar`` → How to Fish."""
    name = Path(path).name
    if not Path(path).is_dir():
        name = _EXT.sub("", name)
    name = re.sub(r"(\s*[\[(][^\[\]()]*[\])])+\s*$", "", name)     # "[FitGirl Repack]", "(v1.2)"
    name = _TAIL.sub("", name)
    if " " not in name:
        name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -–_.[](){}")
    return name or Path(path).stem


def link_key(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


class DropHint(QWidget):
    """A frame over the window while something is dragged over it, saying what will happen."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.text = ""
        self.hide()

    def show_for(self, items: list[tuple[str, str]]) -> None:
        kinds = {k for k, _ in items}
        n = len(items)
        if kinds == {"import"}:
            self.text = "Drop to import into your Library" if n == 1 else f"Drop to import {n} games into your Library"
        elif kinds <= {"link", "torrent"}:
            self.text = "Drop to add the download" if n == 1 else f"Drop to add {n} downloads"
        else:
            self.text = f"Drop to add {n} items"
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self.update()

    def paintEvent(self, _e) -> None:                          # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(5, 6, 10, 170))
        r = QRectF(self.rect()).adjusted(24, 24, -24, -24)
        path = QPainterPath()
        path.addRoundedRect(r, 22, 22)
        accent = QColor(theme.CURRENT.accent)
        pen = QPen(accent, 3, Qt.DashLine)
        p.setPen(pen)
        p.drawPath(path)
        f = QFont(self.font())
        f.setPixelSize(26)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(r, Qt.AlignCenter, f"⤓\n{self.text}")
        p.end()
