"""System check: which external tools are installed, and one-click installation of the missing ones."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout,
                               QWidget)

from ezgd.ui import theme
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import Banner, button, card, clear_layout, label

IMPORTANCE = {"required": "Required", "recommended": "Recommended", "optional": "Optional"}


class SystemCheckDialog(QDialog):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.core = actions.bridge.core
        self.setWindowTitle("System check")
        self.resize(760, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(12)
        lay.addWidget(label("System check", "h2"))
        lay.addWidget(label("Tools the app uses for extracting archives, running Windows games and the launch-option "
                            "helpers. Missing ones can be installed with your system's package manager — you will "
                            "be asked for your password once.", "muted", wrap=True))
        c, self.tools_lay = card()
        lay.addWidget(c)
        self.plan_label = label("Checking…", "small", wrap=True, selectable=True)
        lay.addWidget(self.plan_label)
        self.result = Banner("info", "")
        self.result.hide()
        lay.addWidget(self.result)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: monospace; font-size: 8.5pt;")
        self.log.hide()
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.install_btn = button("Install missing tools", "download", "primary", on_click=self._install)
        self.install_btn.setEnabled(False)
        row.addWidget(self.install_btn)
        self.copy_btn = button("Copy command", "link", "ghost", on_click=self._copy)
        self.copy_btn.setEnabled(False)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self._command = ""
        self.bridge.event.connect(self._event)
        self.refresh()
        # live: something installed in a terminal (or removed) shows up within seconds
        from PySide6.QtCore import QTimer
        self._live = QTimer(self)
        self._live.setInterval(4000)
        self._live.timeout.connect(lambda: self.bridge.call(self.core.system_status(), self._status, owner=self))
        self._live.start()

    def refresh(self) -> None:
        self.bridge.call(self.core.system_status(), self._status, owner=self)
        self.bridge.call(self.core.system_install_plan(), self._plan, owner=self)

    def _status(self, st: dict) -> None:
        if st == getattr(self, "_last_status", None):
            return                                  # nothing changed since the last check: no redraw
        self._last_status = st
        clear_layout(self.tools_lay)
        for t in st["tools"]:
            row = QHBoxLayout()
            ic = QLabel()
            ok = t["installed"]
            color = theme.CURRENT.success if ok else (theme.CURRENT.danger if t["importance"] == "required"
                                                      else theme.CURRENT.warning)
            ic.setPixmap(icon("check" if ok else "warning", color, 18).pixmap(18, 18))
            row.addWidget(ic, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            name = label(f"{t['name']}  ·  {IMPORTANCE.get(t['importance'], '')}" + ("" if ok else "  —  missing"))
            name.setStyleSheet("font-weight: 650;")
            col.addWidget(name)
            col.addWidget(label(t["purpose"], "small", wrap=True))
            if t.get("detail"):
                d = label(t["detail"], "small", wrap=True, selectable=True)
                d.setStyleSheet(f"color: {color if not ok else theme.CURRENT.muted};")
                col.addWidget(d)
            row.addLayout(col, 1)
            w = QWidget()
            w.setLayout(row)
            self.tools_lay.addWidget(w)
        if not st["can_run_windows_games"]:
            self.tools_lay.addWidget(Banner("warning", "Nothing that can run Windows games was found. Installing "
                                                       "umu-launcher (or Steam) fixes that."))

    def _plan(self, p: dict) -> None:
        self._command = p["command"]
        lines = []
        if p["packages"]:
            lines.append(f"Will install: {', '.join(p['packages'])}")
            lines.append(f"Command: {p['command']}")
        else:
            lines.append("Nothing to install — everything available is already present." if not p["unavailable"]
                         else "Nothing more can be installed automatically.")
        if p["unavailable"]:
            lines.append(f"Not available in your repositories: {', '.join(p['unavailable'])}")
        lines += [f"Note: {n}" for n in p["notes"]]
        self.plan_label.setText("\n".join(lines))
        self.install_btn.setEnabled(bool(p["packages"]))
        self.copy_btn.setEnabled(bool(p["command"]))

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText("sudo " + self._command)
        self.actions.toasts.show("Command copied", "Paste it into a terminal.")

    def _install(self) -> None:
        self.install_btn.setEnabled(False)
        self.log.clear()
        self.log.show()
        self.result.hide()
        self.bridge.call(self.core.system_install(), None, lambda e: self._finished(-1, str(e)), owner=self)

    def _event(self, name: str, payload) -> None:
        if name != "system_install":
            return
        try:
            if payload.get("done"):
                self._finished(payload["code"])
            elif "line" in payload:
                self.log.appendPlainText(payload["line"])
        except RuntimeError:          # dialog closed
            pass

    def _finished(self, code: int, error: str | None = None) -> None:
        if code == 0:
            self.result.set_text("Installation finished.", "success")
        elif code in (126, 127):
            self.result.set_text("The password prompt was cancelled or no authentication agent is running. Use "
                                 "“Copy command” and run it in a terminal.", "warning")
        else:
            self.result.set_text(error or f"The package manager exited with code {code} — see the log above.",
                                 "error")
        self.result.show()
        self.refresh()
        self.actions.navigate.emit("refresh-environment")

    def done(self, r: int) -> None:
        try:
            self.bridge.event.disconnect(self._event)
        except (RuntimeError, TypeError):
            pass
        super().done(r)
