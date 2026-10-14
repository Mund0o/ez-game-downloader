"""Uninstall a game: everything the app put on disk for it, chosen item by item."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QMessageBox, QVBoxLayout

from ezgd.core.fsutil import format_bytes
from ezgd.ui import theme
from ezgd.ui.widgets.common import Banner, card, clear_layout, label


class UninstallDialog(QDialog):
    def __init__(self, actions, entry, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.entry = entry
        self.boxes: dict[str, QCheckBox] = {}
        self.items: dict[str, dict] = {}
        self.result_info: dict | None = None
        self.setWindowTitle(f"Uninstall — {entry.title}")
        self.resize(720, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.addWidget(label(f"Uninstall {entry.title}", "h2", wrap=True))
        lay.addWidget(label("Choose what to delete. The game is removed from your Library in any case. Only files "
                            "inside the app's own download and installation folders are ever deleted.", "muted",
                            wrap=True))
        self.body = QVBoxLayout()
        self.body.addWidget(label("Checking what belongs to this game…", "muted"))
        lay.addLayout(self.body, 1)
        self.total = label("", "small")
        lay.addWidget(self.total)
        self.problem = label("", wrap=True, selectable=True)
        self.problem.setStyleSheet(f"color: {theme.CURRENT.danger}; font-weight: 600;")
        self.problem.hide()
        lay.addWidget(self.problem)
        bb = QDialogButtonBox()
        self.go = bb.addButton("Uninstall", QDialogButtonBox.AcceptRole)
        self.go.setProperty("variant", "danger")
        self.go.setEnabled(False)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self._uninstall)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.bridge.call(self.bridge.core.uninstall_plan(entry.id), self._plan,
                         lambda e: self._show_error(f"Could not check this game: {e}"), owner=self)

    def _plan(self, plan: dict) -> None:
        clear_layout(self.body)
        if plan["running"]:
            self.body.addWidget(Banner("error", "The game is running. Stop it first, then uninstall."))
        for it in plan["items"]:
            self.items[it["key"]] = it
            c, cl = card(margins=(14, 10, 14, 10), spacing=4)
            deletable = any(r["ok"] for r in it["paths"]) or not it["paths"]
            box = QCheckBox(f"{it['title']}" + (f" — {format_bytes(it['size'])}" if it["size"] else ""))
            box.setStyleSheet("font-weight: 600;")
            box.setChecked(it["default"] and deletable)
            box.setEnabled(deletable)
            box.toggled.connect(self._update_total)
            self.boxes[it["key"]] = box
            cl.addWidget(box)
            for r in it["paths"]:
                line = label(r["path"] + ("" if r["ok"] else f"  ({r['why']})"), "small", selectable=True, wrap=True)
                if not r["ok"]:
                    line.setStyleSheet(f"color: {theme.CURRENT.muted};")
                cl.addWidget(line)
            if it["note"]:
                note = label(it["note"], "small", wrap=True)
                if it["key"] == "prefix":
                    note.setStyleSheet(f"color: {theme.CURRENT.warning};")
                cl.addWidget(note)
            if it["key"] == "prefix":
                self.backup_box = QCheckBox("Back up my saves first (kept after uninstalling — a reinstall finds them "
                                            "under Library → Saves)")
                self.backup_box.setChecked(True)
                cl.addWidget(self.backup_box)
            self.body.addWidget(c)
        self.body.addStretch(1)
        self.go.setEnabled(not plan["running"])
        self._update_total()

    def _update_total(self) -> None:
        size = sum(self.items[k]["size"] for k, b in self.boxes.items() if b.isChecked())
        self.total.setText(f"Frees about {format_bytes(size)}." if size else "")

    def selected(self) -> list[str]:
        return [k for k, b in self.boxes.items() if b.isChecked()]

    def _uninstall(self) -> None:
        keys = self.selected()
        lines = [self.items[k]["title"] for k in keys]
        warn = "\n• ".join(["Remove it from the Library"] + lines)
        backup = getattr(self, "backup_box", None) is not None and self.backup_box.isChecked()
        extra = ("\n\nYour saves in the Proton prefix are backed up first." if backup else
                 "\n\nThis deletes your saved games for it if they are in the Proton prefix.") if "prefix" in keys \
            else ""
        if QMessageBox.warning(self, "Uninstall", f"This cannot be undone:\n• {warn}{extra}\n\nContinue?",
                               QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.go.setEnabled(False)
        self.go.setText("Uninstalling…")

        def done(r: dict) -> None:
            self.result_info = r
            if not r["kept"]:
                self.accept()
                return
            # finished, but some ticked items were not deleted on purpose: say so in red before closing
            self._red("Uninstalled, but these were left on your PC:\n" +
                      "\n".join(f"• {k['path']} — {k['why']}" for k in r["kept"]))
            self.go.setText("Close")
            self.go.setEnabled(True)
            self.go.clicked.disconnect()
            self.go.clicked.connect(self.accept)

        def failed(err: BaseException) -> None:
            from ezgd.core.app_core import UninstallIncomplete
            if isinstance(err, UninstallIncomplete):
                self._red(f"{err.title} could not be fully uninstalled, so it stays in your Library — fix the "
                          "problem and press Retry:\n" + "\n".join(f"• {f['path']} — {f['why']}" for f in err.failed))
            else:
                self._red(f"Uninstall failed: {err}")
            self.go.setEnabled(True)
            self.go.setText("Retry")
        self.bridge.call(self.bridge.core.uninstall_game(self.entry.id, keys, backup), done, failed, owner=self)

    def _red(self, text: str) -> None:
        self.problem.setText(text)
        self.problem.show()

    def _show_error(self, text: str) -> None:
        self.body.insertWidget(0, Banner("error", text))


class LeftoversDialog(QDialog):
    """Game folders and archives in the app's folders that no Library entry or download uses any more."""

    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.boxes: dict[str, QCheckBox] = {}
        self.sizes: dict[str, int] = {}
        self.setWindowTitle("Clean up leftover game folders")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.addWidget(label("Leftover game folders", "h2"))
        lay.addWidget(label("Game folders and archives in your Games and Downloads folders that no game in your "
                            "Library and no download uses — for example games removed with “Remove from Library "
                            "(keep files)” or by older versions of the app. Other folders you keep there (an engine, "
                            "tools…) are never listed. Tick what to delete.", "muted", wrap=True))
        self.body = QVBoxLayout()
        self.body.addWidget(label("Looking for leftovers…", "muted"))
        lay.addLayout(self.body, 1)
        self.total = label("", "small")
        lay.addWidget(self.total)
        self.problem = label("", wrap=True, selectable=True)
        self.problem.setStyleSheet(f"color: {theme.CURRENT.danger}; font-weight: 600;")
        self.problem.hide()
        lay.addWidget(self.problem)
        bb = QDialogButtonBox()
        self.go = bb.addButton("Delete selected", QDialogButtonBox.AcceptRole)
        self.go.setProperty("variant", "danger")
        self.go.setEnabled(False)
        bb.addButton(QDialogButtonBox.Close)
        bb.accepted.connect(self._delete)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.result_info: dict | None = None
        self.bridge.call(self.bridge.core.find_leftovers(), self._show, owner=self)

    def _show(self, rows: list[dict]) -> None:
        clear_layout(self.body)
        self.boxes.clear()
        if not rows:
            self.body.addWidget(label("Nothing left over — every folder belongs to a game in your Library.", "muted"))
            self.body.addStretch(1)
            self.go.setEnabled(False)
            return
        for r in rows:
            box = QCheckBox(f"{r['path']}  — {format_bytes(r['size'])} ({r['where']})")
            box.setChecked(r["default"])
            box.toggled.connect(self._update_total)
            self.boxes[r["path"]] = box
            self.sizes[r["path"]] = r["size"]
            self.body.addWidget(box)
            why = label(r["why"] + ("" if r["default"] else " — not ticked: the app has no record of creating it, "
                                    "so check it is not something of yours"), "small", wrap=True)
            why.setContentsMargins(26, 0, 0, 4)
            self.body.addWidget(why)
        self.body.addStretch(1)
        self.go.setEnabled(True)
        self._update_total()

    def _update_total(self) -> None:
        size = sum(self.sizes[p] for p, b in self.boxes.items() if b.isChecked())
        self.total.setText(f"Frees about {format_bytes(size)}." if size else "")

    def _delete(self) -> None:
        paths = [p for p, b in self.boxes.items() if b.isChecked()]
        if not paths or QMessageBox.warning(self, "Delete leftovers", f"Permanently delete {len(paths)} item(s)? "
                                            "This cannot be undone.", QMessageBox.Yes | QMessageBox.Cancel,
                                            QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.go.setEnabled(False)

        def done(r: dict) -> None:
            self.result_info = r
            if r["failed"]:
                self.problem.setText("Could not delete:\n" + "\n".join(f"• {f['path']} — {f['why']}"
                                                                        for f in r["failed"]))
                self.problem.show()
            self.actions.toasts.show("Leftovers deleted", f"Freed {format_bytes(r['freed'])}.", "success")
            self.bridge.call(self.bridge.core.find_leftovers(), self._show, owner=self)
        self.bridge.call(self.bridge.core.delete_leftovers(paths), done,
                         lambda e: (self.problem.setText(f"Could not delete: {e}"), self.problem.show()), owner=self)


def open_leftovers(actions, parent) -> None:
    LeftoversDialog(actions, parent).exec()


def open_uninstall(actions, entry, parent) -> dict | None:
    dlg = UninstallDialog(actions, entry, parent)
    return dlg.result_info if dlg.exec() == QDialog.Accepted else None
