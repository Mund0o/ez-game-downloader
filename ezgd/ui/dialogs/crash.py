"""Crash report window: the error code and the full report, both one click to copy."""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from ezgd.core.crashreport import CrashReport
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_folder
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import button, label


class CrashDialog(QDialog):
    def __init__(self, report: CrashReport, parent: QWidget | None = None, repeats: int = 1):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("EZ Game Downloader — crash report")
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icon("warning", theme.CURRENT.danger, 40).pixmap(40, 40))
        head.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        title = ("EZ Game Downloader crashed last time" if report.fatal
                 else "Something went wrong")
        col.addWidget(label(title, "h2", wrap=True))
        sub = ("The app closed because of this crash. It is running normally again now."
               if report.fatal else "The app caught this error and is still running. If something on screen stopped "
                                    "working, restarting the app usually fixes it.")
        if repeats > 1:
            sub += f" (This happened {repeats} times.)"
        col.addWidget(label(sub, "muted", wrap=True))
        head.addLayout(col, 1)
        lay.addLayout(head)

        box = QWidget()
        box.setObjectName("crashCode")
        box.setStyleSheet(f"#crashCode {{ background: {theme.CURRENT.bg3}; border: 1px solid {theme.CURRENT.danger}; "
                          "border-radius: 10px; }")
        bl = QHBoxLayout(box)
        bl.setContentsMargins(14, 10, 10, 10)
        cl = QVBoxLayout()
        cl.setSpacing(2)
        cl.addWidget(label("Error code", "small"))
        self.code = QLabel(report.code)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(15)
        f.setBold(True)
        self.code.setFont(f)
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.code.setStyleSheet(f"color: {theme.CURRENT.danger};")
        cl.addWidget(self.code)
        bl.addLayout(cl, 1)
        self.copy_code = button("Copy code", "link", on_click=lambda: self._copy(report.code, self.copy_code,
                                                                                    "Copy code"))
        bl.addWidget(self.copy_code, 0, Qt.AlignVCenter)
        lay.addWidget(box)

        what = label(report.summary, None, wrap=True, selectable=True)
        what.setStyleSheet("font-weight: 600;")
        lay.addWidget(what)
        lay.addWidget(label("To get it fixed, send the full report (it has what went wrong and the app's recent "
                            "log). Your home folder name and download links are removed from it.", "small",
                            wrap=True))

        self.details = QPlainTextEdit(report.text)
        self.details.setReadOnly(True)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(9)
        self.details.setFont(mono)
        self.details.setMinimumHeight(220)
        self.details.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.details.hide()
        lay.addWidget(self.details, 1)

        row = QHBoxLayout()
        self.toggle = button("Show details", "info", "ghost", on_click=self._toggle)
        row.addWidget(self.toggle)
        if report.path is not None:
            row.addWidget(button("Open crash folder", "folder", "ghost",
                                 on_click=lambda: open_folder(str(report.path.parent))))
        row.addStretch(1)
        self.copy_all = button("Copy full report", "link", "primary",
                               on_click=lambda: self._copy(report.text, self.copy_all, "Copy full report"))
        row.addWidget(self.copy_all)
        row.addWidget(button("Close", on_click=self.accept))
        lay.addLayout(row)

    def _toggle(self) -> None:
        show = not self.details.isVisible()
        self.details.setVisible(show)
        self.toggle.setText("Hide details" if show else "Show details")
        self.adjustSize()

    def _copy(self, text: str, btn, original: str) -> None:
        QGuiApplication.clipboard().setText(text)
        btn.setText("Copied ✓")
        QTimer.singleShot(1800, lambda: btn.setText(original) if shiboken6.isValid(btn) else None)  # window may be gone


class CrashNotifier(QObject):
    """Brings error reports from any thread to the GUI; one window at a time, repeats are counted."""

    reported = Signal(object)

    def __init__(self, parent_window_getter, parent: QObject | None = None):
        super().__init__(parent)
        self._window = parent_window_getter
        self._open: CrashDialog | None = None
        self._counts: dict[str, int] = {}
        self.reported.connect(self._show, Qt.QueuedConnection)

    def submit(self, report: CrashReport) -> None:      # any thread
        self.reported.emit(report)

    def _show(self, report: CrashReport) -> None:
        self._counts[report.code] = self._counts.get(report.code, 0) + 1
        if self._open is not None and self._open.isVisible():
            return                          # already showing one; the saved reports keep the rest
        if getattr(report, "repeat", False):
            return                          # the same error again within a minute: shown once, not every time
        dlg = CrashDialog(report, self._window(), self._counts[report.code])
        self._open = dlg
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.finished.connect(lambda _r: setattr(self, "_open", None))
        dlg.show()
        dlg.raise_()


def show_report(report: CrashReport, parent: QWidget | None = None) -> None:
    CrashDialog(report, parent).exec()
