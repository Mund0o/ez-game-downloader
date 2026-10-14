"""Confirm a download: destination, extraction and (for torrents) file selection."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ezgd.core.fsutil import format_bytes, free_space, safe_dirname
from ezgd.ui import theme
from ezgd.ui.widgets.common import Banner, PasswordBanner, button, chip, label, source_chip


def folder_row(edit: QLineEdit, parent: QWidget, title: str) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)

    def browse():
        d = QFileDialog.getExistingDirectory(parent, title, edit.text() or str(Path.home()))
        if d:
            edit.setText(d)
    lay.addWidget(button("Browse…", "folder", on_click=browse))
    return w


class DownloadOptionsDialog(QDialog):
    def __init__(self, prep: dict, settings: dict, parent=None):
        super().__init__(parent)
        self.prep = prep
        self.setWindowTitle("Start download")
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 18, 20, 18)

        head = QHBoxLayout()
        title = label(prep["title"], "h2", wrap=True)
        head.addWidget(title, 1)
        head.addWidget(source_chip(prep.get("source")))
        kind = "Torrent" if prep["kind"] == "torrent" else "Direct download"
        head.addWidget(chip(kind, theme.CURRENT.accent))
        lay.addLayout(head)
        if prep.get("password"):
            lay.addWidget(PasswordBanner(prep.get("source"), prep["password"]))
        size = format_bytes(prep["size"]) if prep.get("size") else "size unknown until the transfer starts"
        lay.addWidget(label(f"{prep['name']} — {size} · via {prep['method']}", "muted", wrap=True))
        if prep.get("checksum"):
            lay.addWidget(Banner("success", "The host publishes a SHA-256 checksum; the file will be verified "
                                            "after downloading."))
        if prep["kind"] == "torrent":
            lay.addWidget(label("Piece hashes are verified by the torrent engine while downloading. Speed depends on "
                                "how many peers are sharing this torrent.", "small", wrap=True))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)
        default_x = str(Path(settings.get("install_dir", "")) / safe_dirname(prep["title"]))
        one = bool(settings.get("downloads_in_game_folder", True))
        # one folder: the download goes into the game's folder and is extracted right there
        self.dest = QLineEdit(default_x if one else settings.get("download_dir", ""))
        self.dest.textChanged.connect(self._update_space)
        form.addRow("Game folder" if one else "Download to", folder_row(self.dest, self, "Choose the folder"))
        self.space = label("", "small")
        form.addRow("", self.space)
        self.extract = QCheckBox("Extract archives automatically when the download finishes")
        self.extract.setChecked(bool(settings.get("auto_extract", True)))
        form.addRow("", self.extract)
        self.extract_dest = QLineEdit(default_x)
        self.extract_row = folder_row(self.extract_dest, self, "Choose extraction folder")
        form.addRow("" if one else "Extract to", self.extract_row)
        self.extract.toggled.connect(self.extract_row.setEnabled)
        if one:
            self.extract_row.hide()
            self.dest.textChanged.connect(self.extract_dest.setText)
            note = "The download is saved in this folder and extracted into it"
            if settings.get("delete_archive_after_extract"):
                note += "; the archive is deleted afterwards, so only the game is left"
            form.addRow("", label(note + ".", "small", wrap=True))
        lay.addLayout(form)

        self.tree: QTreeWidget | None = None
        if prep.get("files"):
            lay.addWidget(label("Files", "h2"))
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["File", "Size"])
            self.tree.setRootIsDecorated(False)
            self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
            self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            for f in prep["files"]:
                it = QTreeWidgetItem([f["path"], format_bytes(f["size"])])
                it.setData(0, Qt.UserRole, f["index"])
                it.setData(1, Qt.UserRole, f["size"])
                it.setCheckState(0, Qt.Checked)
                self.tree.addTopLevelItem(it)
            self.tree.setMinimumHeight(min(260, 40 + 26 * len(prep["files"])))
            self.tree.itemChanged.connect(self._update_space)
            lay.addWidget(self.tree)
            row = QHBoxLayout()
            row.addWidget(button("Select all", on_click=lambda: self._check_all(True), variant="ghost"))
            row.addWidget(button("Select none", on_click=lambda: self._check_all(False), variant="ghost"))
            row.addStretch(1)
            lay.addLayout(row)

        self.warning = Banner("warning", "")
        self.warning.hide()
        lay.addWidget(self.warning)
        lay.addWidget(label("Only download content you are allowed to download. The app never runs downloaded "
                            "files on its own.", "small", wrap=True))
        bb = QDialogButtonBox()
        self.ok = bb.addButton("Start download", QDialogButtonBox.AcceptRole)
        self.ok.setProperty("variant", "primary")
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._update_space()

    def _check_all(self, on: bool) -> None:
        if not self.tree:
            return
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def selected(self) -> list[int] | None:
        if not self.tree:
            return None
        idx = [self.tree.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.tree.topLevelItemCount())
               if self.tree.topLevelItem(i).checkState(0) == Qt.Checked]
        return None if len(idx) == self.tree.topLevelItemCount() else idx

    def _wanted(self) -> int | None:
        if self.tree:
            return sum(self.tree.topLevelItem(i).data(1, Qt.UserRole) for i in range(self.tree.topLevelItemCount())
                       if self.tree.topLevelItem(i).checkState(0) == Qt.Checked)
        return self.prep.get("size")

    def _update_space(self, *_args) -> None:
        dest = self.dest.text().strip()
        wanted = self._wanted()
        try:
            free = free_space(Path(dest)) if dest else None
        except OSError:
            free = None
        text = f"Free space: {format_bytes(free)}" if free is not None else "Free space: unknown"
        if wanted:
            text += f" · needed: {format_bytes(wanted)}"
        self.space.setText(text)
        problem = None
        if not dest or not Path(dest).is_absolute():
            problem = "Choose an absolute folder for the download."
        elif free is not None and wanted and wanted > free:
            problem = "There is not enough free space in this folder."
        elif self.tree is not None and self.selected() == []:
            problem = "Select at least one file."
        self.warning.setVisible(bool(problem))
        if problem:
            self.warning.set_text(problem)
        self.ok.setEnabled(problem is None)

    def _accept(self) -> None:
        if self.ok.isEnabled():
            self.accept()

    def values(self) -> dict:
        return {"dest_dir": self.dest.text().strip(), "selected_files": self.selected(),
                "auto_extract": self.extract.isChecked(),
                "extract_dest": self.extract_dest.text().strip() if self.extract.isChecked() else None}
