"""System check: which external tools are installed, and installation of the missing ones you tick."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QVBoxLayout, QWidget)

from ezgd.ui import theme
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import Banner, button, card, clear_layout, label

IMPORTANCE = {"required": "Required", "highly_recommended": "★ Highly recommended", "recommended": "Recommended",
              "optional": "Optional"}


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
                            "helpers. Tick the missing ones you want and install them with your system's package "
                            "manager — you will be asked for your password once.", "muted", wrap=True))
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
        self.install_btn = button("Install selected tools", "download", "primary", on_click=self._install)
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
        self.selected: set[str] = set()      # missing tools ticked for installing (all of them at first)
        self._seen: set[str] = set()         # missing tools already shown (a newly missing one starts ticked)
        self.checks: dict[str, QCheckBox] = {}
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

    def _refresh_plan(self) -> None:
        if not self.selected:
            self._command = ""
            self.plan_label.setText("Nothing selected — tick the tools you want to install.")
            self.install_btn.setEnabled(False)
            self.copy_btn.setEnabled(False)
            return
        self.bridge.call(self.core.system_install_plan(sorted(self.selected)), self._plan, owner=self)

    def _toggle(self, key: str, on: bool) -> None:
        if on:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self._refresh_plan()

    def _status(self, st: dict) -> None:
        if st == getattr(self, "_last_status", None):
            return                                  # nothing changed since the last check: no redraw
        self._last_status = st
        clear_layout(self.tools_lay)
        self.checks = {}
        missing = {t["key"] for t in st["tools"] if not t["installed"] and t.get("packages")}
        optional = {t["key"] for t in st["tools"] if t["importance"] == "optional"}
        # a newly missing tool starts ticked — not an optional one (Steam: umu-launcher covers it)
        self.selected = (self.selected & missing) | (missing - self._seen - optional)
        self._seen |= missing
        for t in st["tools"]:
            row = QHBoxLayout()
            ok = t["installed"]
            color = theme.CURRENT.success if ok else (theme.CURRENT.danger if t["importance"] == "required"
                                                      else theme.CURRENT.warning)
            if ok:
                ic = QLabel()
                ic.setPixmap(icon("check", color, 18).pixmap(18, 18))
                row.addWidget(ic, 0, Qt.AlignTop)
            else:                                   # missing: tick it to install it
                box = QCheckBox()
                box.setChecked(t["key"] in self.selected)
                box.setEnabled(bool(t.get("packages")))
                box.setToolTip("Install this one" if t.get("packages") else
                               "Your distribution has no package for it — see the note below")
                box.toggled.connect(lambda on, k=t["key"]: self._toggle(k, on))
                self.checks[t["key"]] = box
                row.addWidget(box, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            head = QHBoxLayout()
            head.setSpacing(8)
            name = label(t["name"] + ("" if ok else "  —  missing"))
            name.setStyleSheet("font-weight: 650;")
            head.addWidget(name)
            imp = label(IMPORTANCE.get(t["importance"], ""), "small")
            if t["importance"] == "highly_recommended":            # what runs Windows games without Steam
                imp.setStyleSheet(f"color: {theme.CURRENT.accent}; font-weight: 750;")
            head.addWidget(imp)
            head.addStretch(1)
            col.addLayout(head)
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
        self._refresh_plan()

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
        self.install_btn.setEnabled(bool(p["packages"]) and bool(self.selected))
        self.copy_btn.setEnabled(bool(p["command"]))

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText("sudo " + self._command)
        self.actions.toasts.show("Command copied", "Paste it into a terminal.")

    def _install(self) -> None:
        self.install_btn.setEnabled(False)
        self.log.clear()
        self.log.show()
        self.result.hide()
        self.bridge.call(self.core.system_install(sorted(self.selected)), None,
                         lambda e: self._finished(-1, str(e)), owner=self)

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
