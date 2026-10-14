"""Smaller dialogs: add download, launch confirmation, problem report, recent events."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ezgd.ui.dialogs.download_options import folder_row
from ezgd.ui.widgets.common import Banner, button, label


class AddDownloadDialog(QDialog):
    """Paste a magnet link / direct file URL / .torrent URL, or pick a local .torrent file."""

    def __init__(self, settings: dict, parent=None, initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Add download")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)
        lay.addWidget(label("Add download", "h2"))
        lay.addWidget(label("Paste a magnet link, a link to a .torrent file, or a direct file link (for example the "
                            "final download link you obtained in your browser). File-host pages that need a browser "
                            "cannot be downloaded in-app.", "muted", wrap=True))
        form = QFormLayout()
        form.setSpacing(10)
        self.link = QLineEdit(initial)
        self.link.setPlaceholderText("magnet:?xt=…  or  https://…")
        row = QHBoxLayout()
        row.addWidget(self.link, 1)
        row.addWidget(button("Paste", on_click=self._paste))
        form.addRow("Link", row)
        self.torrent_path = QLineEdit()
        self.torrent_path.setPlaceholderText("…or choose a .torrent file")
        trow = QHBoxLayout()
        trow.addWidget(self.torrent_path, 1)
        trow.addWidget(button("Choose…", "folder", on_click=self._choose))
        form.addRow(".torrent file", trow)
        self.title = QLineEdit()
        self.title.setPlaceholderText("optional")
        form.addRow("Title", self.title)
        one = bool(settings.get("downloads_in_game_folder", True))
        self.dest = QLineEdit(settings.get("install_dir", "") if one else settings.get("download_dir", ""))
        form.addRow("Download to", folder_row(self.dest, self, "Choose download folder"))
        if one:                                    # with a title: that game's own folder
            from ezgd.core.fsutil import safe_dirname
            base = settings.get("install_dir", "")
            self.title.textChanged.connect(
                lambda t: self.dest.setText(str(Path(base) / safe_dirname(t)) if t.strip() else base))
        self.extract = QCheckBox("Extract archives automatically")
        self.extract.setChecked(bool(settings.get("auto_extract", True)))
        form.addRow("", self.extract)
        lay.addLayout(form)
        self.error = Banner("error", "")
        self.error.hide()
        lay.addWidget(self.error)
        bb = QDialogButtonBox()
        ok = bb.addButton("Add", QDialogButtonBox.AcceptRole)
        ok.setProperty("variant", "primary")
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _paste(self) -> None:
        self.link.setText(QGuiApplication.clipboard().text().strip())

    def _choose(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Choose torrent file", str(Path.home()), "Torrent files (*.torrent)")
        if f:
            self.torrent_path.setText(f)

    def _accept(self) -> None:
        if not self.link.text().strip() and not self.torrent_path.text().strip():
            self.show_error("Enter a link or choose a .torrent file.")
            return
        if not Path(self.dest.text().strip()).is_absolute():
            self.show_error("Choose an absolute download folder.")
            return
        self.accept()

    def show_error(self, text: str) -> None:
        self.error.set_text(text)
        self.error.show()

    def values(self) -> dict:
        return {"link": self.link.text().strip(), "torrent": self.torrent_path.text().strip(),
                "title": self.title.text().strip() or None, "dest_dir": self.dest.text().strip(),
                "auto_extract": self.extract.isChecked()}


class ConfirmLaunchDialog(QDialog):
    def __init__(self, title: str, exe: str, how: str, trusted: bool, parent=None,
                 before: list[str] | None = None, after: list[str] | None = None, script: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Launch game")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)
        lay.addWidget(label(f"Launch {title}?", "h2", wrap=True))

        def mono(text: str):
            w = label(text, selectable=True, wrap=True)
            w.setStyleSheet("font-family: monospace;")
            return w
        if before:
            lay.addWidget(label("First, from your launch options:", "muted"))
            for c in before:
                lay.addWidget(mono(c))
        lay.addWidget(label("This will run:", "muted"))
        lay.addWidget(mono(exe))
        if script:
            lay.addWidget(label("using your launch options, run by /bin/sh exactly like Steam:", "muted"))
            lay.addWidget(mono(script))
        if after:
            lay.addWidget(label("After the game exits:", "muted"))
            for c in after:
                lay.addWidget(mono(c))
        lay.addWidget(label(f"Method: {how}", "muted"))
        if not trusted:
            lay.addWidget(Banner("warning", "You have not run this executable from EZ Game Downloader before. Files "
                                            "downloaded from the internet can contain malware — only continue if you "
                                            "trust the source. The app never runs downloads automatically."))
        bb = QDialogButtonBox()
        ok = bb.addButton("Launch", QDialogButtonBox.AcceptRole)
        ok.setProperty("variant", "primary")
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ReportDialog(QDialog):
    """Write a local problem report for an unsupported source / malformed listing."""

    KINDS = ["Listing looks wrong or incomplete", "Game page could not be read", "Download link does not work",
             "Unsupported file host", "Other"]

    def __init__(self, source: str, url: str | None, details: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Report a problem")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        lay.addWidget(label("Report a problem", "h2"))
        lay.addWidget(label("A plain-text report is saved on this computer (nothing is uploaded). It contains the "
                            "source, the page address without query parameters, the error and your comment. Attach it "
                            "when you file an issue.", "muted", wrap=True))
        form = QFormLayout()
        self.source = QLineEdit(source)
        self.source.setReadOnly(True)
        form.addRow("Source", self.source)
        self.kind = QComboBox()
        self.kind.addItems(self.KINDS)
        form.addRow("Problem", self.kind)
        self.url = QLineEdit(url or "")
        form.addRow("Page", self.url)
        self.details = QPlainTextEdit(details)
        self.details.setMaximumHeight(110)
        form.addRow("Details", self.details)
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("What did you expect to see?")
        self.comment.setMaximumHeight(90)
        form.addRow("Comment", self.comment)
        lay.addLayout(form)
        bb = QDialogButtonBox()
        ok = bb.addButton("Save report", QDialogButtonBox.AcceptRole)
        ok.setProperty("variant", "primary")
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self) -> dict:
        return {"source": self.source.text(), "kind": self.kind.currentText(), "url": self.url.text().strip() or None,
                "message": self.details.toPlainText(), "comment": self.comment.toPlainText()}


class EventsDialog(QDialog):
    def __init__(self, events: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recent events and errors")
        self.resize(820, 480)
        lay = QVBoxLayout(self)
        table = QTableWidget(len(events), 4)
        table.setHorizontalHeaderLabels(["Time", "Level", "Category", "Message"])
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, e in enumerate(events):
            for c, val in enumerate((time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])), e["level"],
                                     e["category"], e["message"])):
                table.setItem(r, c, QTableWidgetItem(str(val)))
        lay.addWidget(table)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def open_url(url: str) -> None:
    if url and url.startswith(("http://", "https://")):
        QDesktopServices.openUrl(QUrl(url))


def open_folder(path: str | Path) -> None:
    p = Path(path)
    if not p.is_dir():
        p = p.parent
    if p.exists():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


class NotificationsDialog(QDialog):
    """Every notification of this session, in full and copyable (pop-ups and desktop notifications can be short)."""

    def __init__(self, history: list[dict], parent=None):
        import time as _time
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QPlainTextEdit
        super().__init__(parent)
        self.setWindowTitle("Notifications")
        self.resize(680, 520)
        lay = QVBoxLayout(self)
        if not history:
            lay.addWidget(label("No notifications yet in this session.", "muted"))
        text = "\n\n".join(f"[{_time.strftime('%H:%M:%S', _time.localtime(h['time']))}] {h['title']}"
                            + (f"\n{h['text']}" if h.get("text") else "") for h in reversed(history))
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        copy = button("Copy all", "link", on_click=lambda: QGuiApplication.clipboard().setText(text))
        row.addWidget(copy)
        row.addWidget(button("Close", on_click=self.accept))
        lay.addLayout(row)
