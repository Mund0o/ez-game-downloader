"""Storage: what each game, its Proton prefix and its saves use on disk — and what can be freed."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QMessageBox,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ezgd.core.fsutil import format_bytes
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_folder
from ezgd.ui.widgets.common import button, card, chip, clear_layout, label

PARTS = [("files", "Game files", "#8b5cf6"), ("prefix", "Proton prefixes", "#38bdf8"),
         ("downloads", "Downloads & archives", "#f59e0b"), ("saves", "Save backups", "#22c55e"),
         ("other", "Cache, logs & backups", "#64748b")]


class Bar(QWidget):
    """A rounded bar split into coloured parts."""

    def __init__(self, parts: list[tuple[float, str]], total: float, height: int = 14, parent=None):
        super().__init__(parent)
        self.parts, self.total = parts, max(total, 1.0)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(300, self.height())

    def paintEvent(self, _e) -> None:                       # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setClipPath(clip)
        p.fillRect(r, QColor(theme.CURRENT.bg3))
        x = 0.0
        for value, color in self.parts:
            w = r.width() * value / self.total
            if w > 0:
                p.fillRect(QRectF(x, 0, w + 0.5, r.height()), QColor(color))
            x += w
        p.end()


class StorageDialog(QDialog):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.setWindowTitle("Storage")
        self.resize(900, 760)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(24, 20, 24, 20)
        self.lay.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.setContentsMargins(24, 0, 24, 16)
        outer.addWidget(bb)
        self.lay.addWidget(label("Storage", "title"))
        self.lay.addWidget(label("Measuring your games, prefixes and saves…", "muted"))
        self.lay.addStretch(1)
        self.load()

    def load(self) -> None:
        self.bridge.call(self.bridge.core.storage_report(), self._render,
                         lambda e: self.actions.fail("Could not measure the storage", e), owner=self)

    def _render(self, r: dict) -> None:
        clear_layout(self.lay)
        self.lay.addWidget(label("Storage", "title"))
        games = r["games"]
        totals = {"files": sum(g["files"] for g in games), "prefix": sum(g["prefix"] for g in games)
                  + sum(x["size"] for x in r["old_prefixes"]),
                  "downloads": sum(g["downloads"] for g in games) + r["archives"]["size"] + r["leftovers"]["size"],
                  "saves": r["save_backups"], "other": r["cache"] + r["logs"] + r["db_backups"]}
        total = sum(totals.values())
        self.lay.addWidget(label(f"The app's games and data use {format_bytes(total)}.", "muted"))

        # disks
        for d in r["disks"]:
            c, cl = card(margins=(16, 12, 16, 12), spacing=6)
            row = QHBoxLayout()
            row.addWidget(label(f"{d['label']} drive", "h2"))
            row.addStretch(1)
            pct = 100 * d["used"] / d["total"] if d["total"] else 0
            free_color = theme.CURRENT.danger if d["free"] < 20 * 1024 ** 3 else theme.CURRENT.success
            row.addWidget(chip(f"{format_bytes(d['free'])} free", free_color))
            cl.addLayout(row)
            cl.addWidget(Bar([(d["used"], theme.CURRENT.accent)], d["total"], 12))
            cl.addWidget(label(f"{format_bytes(d['used'])} of {format_bytes(d['total'])} used ({pct:.0f}%) · "
                               f"{d['path']}", "small"))
            self.lay.addWidget(c)

        # what the app uses
        c, cl = card()
        cl.addWidget(label("What the app keeps", "h2"))
        cl.addWidget(Bar([(totals[k], col) for k, _t, col in PARTS], total or 1, 18))
        legend = QGridLayout()
        legend.setHorizontalSpacing(18)
        for i, (k, text, col) in enumerate(PARTS):
            dot = label("●")
            dot.setStyleSheet(f"color: {col}; font-size: 14pt;")
            legend.addWidget(dot, i // 3, (i % 3) * 3)
            legend.addWidget(label(text), i // 3, (i % 3) * 3 + 1)
            v = label(format_bytes(totals[k]))
            v.setStyleSheet("font-weight: 700;")
            legend.addWidget(v, i // 3, (i % 3) * 3 + 2)
        cl.addLayout(legend)
        self.lay.addWidget(c)

        # what can go
        c, cl = card()
        cl.addWidget(label("Can be freed", "h2"))
        free_rows = 0
        if r["archives"]["count"]:
            free_rows += 1
            self._free_row(cl, f"Archives of installed games ({r['archives']['count']})", r["archives"]["size"],
                           "The games are extracted; their archives are not needed any more.", "Delete archives…",
                           self._delete_archives)
        if r["leftovers"]["count"]:
            free_rows += 1
            self._free_row(cl, f"Leftover folders and files ({r['leftovers']['count']})", r["leftovers"]["size"],
                           "Downloads and game folders no game in your Library uses.", "Review…", self._leftovers)
        if r["cache"] > 50 * 1024 * 1024:
            free_rows += 1
            self._free_row(cl, "Cache (pages and covers)", r["cache"], "Downloaded again when needed.",
                           "Clear cache", self._clear_cache)
        if r["old_prefixes"]:
            free_rows += 1
            size = sum(x["size"] for x in r["old_prefixes"])
            cl.addWidget(label(f"Unused Proton prefixes ({len(r['old_prefixes'])}) — {format_bytes(size)}", None))
            cl.addWidget(label("Windows settings of games that are gone, or older copies kept when a prefix was "
                               "rebuilt. Saves in them are lost when you delete them — back up or copy what you want "
                               "first.", "small", wrap=True))
            self._prefix_boxes: list[tuple[QCheckBox, dict]] = []
            for x in r["old_prefixes"]:
                row = QHBoxLayout()
                cb = QCheckBox(f"{Path(x['path']).name} — {format_bytes(x['size'])} · {x['why']}")
                row.addWidget(cb, 1)
                row.addWidget(button("", "folder", "ghost", tooltip="Open", on_click=lambda p=x["path"]:
                                     open_folder(p)))
                cl.addLayout(row)
                self._prefix_boxes.append((cb, x))
            cl.addWidget(button("Delete selected prefixes…", "trash", "danger", on_click=self._delete_prefixes),
                         0, Qt.AlignLeft)
        if not free_rows:
            cl.addWidget(label("Nothing to clean up — everything on disk is in use.", "muted"))
        self.lay.addWidget(c)

        # per game
        c, cl = card()
        cl.addWidget(label("Games", "h2"))
        if not games:
            cl.addWidget(label("No games in your Library.", "muted"))
        biggest = max((g["total"] + g["saves"] for g in games), default=1) or 1
        for g in games:
            row = QHBoxLayout()
            col = QVBoxLayout()
            col.setSpacing(3)
            t = label(g["title"])
            t.setStyleSheet("font-weight: 700;")
            col.addWidget(t)
            col.addWidget(Bar([(g["files"], PARTS[0][2]), (g["prefix"], PARTS[1][2]), (g["downloads"], PARTS[2][2]),
                               (g["saves"], PARTS[3][2])], biggest, 10))
            bits = [f"game {format_bytes(g['files'])}"]
            if g["prefix"]:
                bits.append(f"prefix {format_bytes(g['prefix'])}")
            if g["downloads"]:
                bits.append(f"download {format_bytes(g['downloads'])}")
            if g["saves"]:
                bits.append(f"save backups {format_bytes(g['saves'])}")
            if g.get("shaders"):
                bits.append(f"built shaders {format_bytes(g['shaders'])}")
            col.addWidget(label(" · ".join(bits), "small"))
            row.addLayout(col, 1)
            total_l = label(format_bytes(g["total"] + g["saves"]))
            total_l.setStyleSheet("font-size: 12pt; font-weight: 800;")
            row.addWidget(total_l)
            if g.get("install_path"):
                row.addWidget(button("", "folder", "ghost", tooltip="Open the game folder",
                                     on_click=lambda p=g["install_path"]: open_folder(p)))
            un = button("Uninstall…", "trash", "ghost", on_click=lambda g=g: self._uninstall(g))
            un.setEnabled(not g["running"])
            row.addWidget(un)
            cl.addLayout(row)
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"background: {theme.CURRENT.border};")
            cl.addWidget(sep)
        self.lay.addWidget(c)
        self.lay.addStretch(1)

    def _free_row(self, cl, title: str, size: int, note: str, action: str, fn) -> None:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        t = label(f"{title} — {format_bytes(size)}")
        t.setStyleSheet("font-weight: 600;")
        col.addWidget(t)
        col.addWidget(label(note, "small", wrap=True))
        row.addLayout(col, 1)
        row.addWidget(button(action, "trash", "ghost", on_click=fn))
        cl.addLayout(row)

    # -------------------------------------------------------------- actions
    def _delete_archives(self) -> None:
        core = self.bridge.core

        def got(r: dict) -> None:
            if not r["items"]:
                self.load()
                return
            names = "\n".join(f"• {i['game']}: {Path(i['archive']).name} — {format_bytes(i['size'])}"
                              for i in r["items"][:15])
            more = f"\n… and {len(r['items']) - 15} more" if len(r["items"]) > 15 else ""
            if QMessageBox.question(self, "Delete archives", f"These archives were extracted successfully:\n\n{names}"
                                    f"{more}\n\nDelete them and free {format_bytes(r['size'])}?") != QMessageBox.Yes:
                return
            self.bridge.call(core.delete_leftover_archives(),
                             lambda d: (self.actions.toasts.show("Archives deleted", f"{format_bytes(d['freed'])} "
                                                                 "freed.", "success"), self.load()),
                             lambda e: self.actions.fail("Could not delete the archives", e), owner=self)
        self.bridge.call(core.leftover_archives(), got, lambda e: self.actions.fail("Archives", e), owner=self)

    def _leftovers(self) -> None:
        from ezgd.ui.dialogs.uninstall import open_leftovers
        open_leftovers(self.actions, self)
        self.load()

    def _clear_cache(self) -> None:
        self.bridge.call(self.bridge.core.clear_cache(False),
                         lambda _r: (self.actions.toasts.show("Cache cleared", kind="success"), self.load()),
                         lambda e: self.actions.fail("Clear cache", e), owner=self)

    def _delete_prefixes(self) -> None:
        chosen = [x for cb, x in getattr(self, "_prefix_boxes", []) if cb.isChecked()]
        if not chosen:
            self.actions.toasts.show("Nothing selected", "Tick the prefixes to delete.")
            return
        size = sum(x["size"] for x in chosen)
        if QMessageBox.warning(self, "Delete prefixes", f"Delete {len(chosen)} prefix(es) ({format_bytes(size)})?\n\n"
                               "Saves stored in them are deleted too. This cannot be undone.",
                               QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.bridge.call(self.bridge.core.delete_old_prefixes([x["path"] for x in chosen]),
                         lambda d: (self.actions.toasts.show("Prefixes deleted", f"{format_bytes(d['freed'])} freed"
                                                             + (f"; {len(d['failed'])} could not be deleted"
                                                                if d["failed"] else "."), "success"), self.load()),
                         lambda e: self.actions.fail("Delete prefixes", e), owner=self)

    def _uninstall(self, g: dict) -> None:
        from ezgd.ui.dialogs.uninstall import open_uninstall

        def got(e) -> None:
            r = open_uninstall(self.actions, e, self)
            if r is not None:
                self.actions.toasts.show(f"{r['title']} uninstalled", f"Freed {format_bytes(r['freed'])}.", "success")
            self.load()
        self.bridge.call(self.bridge.core.library_call("get", g["entry_id"]), got,
                         lambda e: self.actions.fail("Uninstall", e), owner=self)
