"""Steam-style game Properties: General (launch options) and Compatibility (Proton) tabs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QMessageBox, QTabWidget, QVBoxLayout, QWidget)

from ezgd.library.models import CompatConfig
from ezgd.platform.proton import COMPAT_OPTIONS
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_folder
from ezgd.ui.widgets.common import Banner, button, card, label

LAUNCH_HELP = ("Exactly like Steam's launch options: <b>%command%</b> is replaced by the game's full command "
               "and the line is run by the shell (/bin/sh), so anything that works in Steam works here — "
               "<code>MANGOHUD=1 gamemoderun %command%</code>, "
               "<code>systemctl --user restart pipewire wireplumber; %command%</code>, "
               "<code>gamescope -W 1920 -H 1080 -- %command% -dx11</code>. Text without %command% is added to "
               "the end of the game's command line, e.g. <code>-windowed</code>. Only use commands you "
               "understand: launch options can run anything, just like in Steam.")
METHODS = [("auto", "Automatic"), ("proton", "Proton"), ("wine", "Wine"), ("native", "Native (no Proton)"),
           ("steam", "Steam shortcut")]


class PropertiesDialog(QDialog):
    def __init__(self, actions, entry, env: dict, parent=None, tab: str = "general"):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.core = actions.bridge.core
        self.entry = entry
        self.env = env
        self.linux = bool(env.get("linux"))
        self.setWindowTitle(f"{entry.title} — Properties")
        self.resize(820, 720)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.addWidget(label(entry.title, "h2", wrap=True))
        self.tabs = QTabWidget()
        self.tabs.addTab(self._general(), "General")
        if self.linux:
            self.tabs.addTab(self._compat(), "Compatibility")
        lay.addWidget(self.tabs, 1)
        self.error = Banner("error", "")
        self.error.hide()
        lay.addWidget(self.error)
        bb = QDialogButtonBox()
        save = bb.addButton("Save", QDialogButtonBox.AcceptRole)
        save.setProperty("variant", "primary")
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if tab == "compat" and self.linux:
            self.tabs.setCurrentIndex(1)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._wire_preview()
        self._refresh_preview()
        self.bridge.call(self.core.library_call("candidates", entry.id), self._candidates, owner=self)
        if self.linux:
            self._refresh_fix_status()
            self.bridge.call(self.core.library_call("prefix_info", entry.id), self._prefix_info, owner=self)
            if not entry.steam_app_id:
                self._lookup(silent=True)

    # ---------------------------------------------------------------- general
    def _general(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.title_edit = QLineEdit(self.entry.title)
        form.addRow("Name", self.title_edit)
        self.exe = QComboBox()
        self.exe.setMinimumWidth(420)
        if self.entry.executable:
            self.exe.addItem(Path(self.entry.executable).name + "  (current)", self.entry.executable)
        erow = QHBoxLayout()
        erow.addWidget(self.exe, 1)
        erow.addWidget(button("Browse…", "folder", on_click=self._browse))
        form.addRow("Executable", erow)
        self.exe_hint = label("Scanning the install folder…", "small", wrap=True)
        form.addRow("", self.exe_hint)
        self.options = QLineEdit(self.entry.launch_args)
        self.options.setPlaceholderText("e.g.  -windowed   or   MANGOHUD=1 gamemoderun %command%")
        self.options.setStyleSheet("font-family: monospace;")
        form.addRow("Launch options", self.options)
        help_label = label("", "small", wrap=True)
        help_label.setTextFormat(Qt.RichText)
        help_label.setText(LAUNCH_HELP)
        form.addRow("", help_label)
        lay.addLayout(form)
        c, cl = card()
        cl.addWidget(label("Command that will run", "muted"))
        self.preview_desc = label("", "small", wrap=True)
        cl.addWidget(self.preview_desc)
        self.preview = label("", wrap=True, selectable=True)
        self.preview.setStyleSheet("font-family: monospace; font-size: 8.5pt;")
        cl.addWidget(self.preview)
        lay.addWidget(c)
        lay.addStretch(1)
        return w

    # ---------------------------------------------------------- compatibility
    def _compat(self) -> QWidget:
        e = self.entry
        cfg = e.compat_config
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.method = QComboBox()
        for v, t in METHODS:
            self.method.addItem(t, v)
        self.method.setCurrentIndex(max(0, self.method.findData(str(e.launch_method))))
        form.addRow("Run with", self.method)
        self.proton = QComboBox()
        self.proton.addItem("Default (newest detected)", "")
        for p in self.env.get("proton_details", []):
            tags = []
            if p["protonfixes"]:
                tags.append("protonfixes")
            if p["needs_runtime"]:
                tags.append(p["runtime"] or "runtime missing")
            self.proton.addItem(p["name"] + (f"   · {', '.join(tags)}" if tags else ""), p["name"])
        self.proton.setCurrentIndex(max(0, self.proton.findData(e.proton_version or "")))
        form.addRow("Proton version", self.proton)
        if not self.env.get("protons"):
            form.addRow("", label("No Proton found. Install it in Steam (Library → Tools) or add GE-Proton to "
                                  "compatibilitytools.d.", "small", wrap=True))
        self.runtime = QCheckBox("Run inside the Steam Linux Runtime container (what Steam does)")
        self.runtime.setChecked(cfg.use_runtime)
        self.runtime.setToolTip("Proton builds declare which runtime they need; the app uses the same one Steam "
                                "would. Turn off only to troubleshoot.")
        form.addRow("", self.runtime)
        lay.addLayout(form)

        fc, fl = card()
        fl.addWidget(label("Per-game fixes", "h2"))
        row = QHBoxLayout()
        row.addWidget(label("Steam App ID", "muted"))
        self.app_id = QLineEdit(str(e.steam_app_id or ""))
        self.app_id.setPlaceholderText("unknown")
        self.app_id.setMaximumWidth(140)
        row.addWidget(self.app_id)
        row.addWidget(button("Look up on Steam", "search", on_click=lambda: self._lookup(False)))
        row.addStretch(1)
        fl.addLayout(row)
        self.fix_status = label("", "small", wrap=True)
        fl.addWidget(self.fix_status)
        self.protonfixes = QCheckBox("Apply protonfixes (GE-Proton / Proton-CachyOS per-game fixes)")
        self.protonfixes.setChecked(cfg.protonfixes)
        fl.addWidget(self.protonfixes)
        self.report = QCheckBox("Also report the Steam App ID to Proton as SteamAppId (Valve Proton per-game tweaks)")
        self.report.setChecked(cfg.report_app_id)
        self.report.setToolTip("Off by default: games whose Steam emulation reads SteamAppId (for example "
                               "OnlineFix's multiplayer fix) can stop working when it is set.")
        fl.addWidget(self.report)
        lay.addWidget(fc)

        oc, ol = card()
        fhead = QHBoxLayout()
        fhead.addWidget(label("Fixes", "h2"), 1)
        fhead.addWidget(button("Open Fixes…", "wrench", "primary", on_click=self._open_fixes))
        ol.addLayout(fhead)
        on = [o.label for o in COMPAT_OPTIONS if o.key in cfg.options] + cfg.fixes
        ol.addWidget(label("Proton options (WineD3D, esync/fsync, NVAPI, Wayland…), rendering, audio and "
                           "performance fixes are all switched on and off on the Fixes screen."
                           + (f" Currently on: {len(on)}." if on else ""), "small", wrap=True))
        drow = QHBoxLayout()
        drow.addWidget(label("Manual DLL overrides (advanced)", "muted"))
        self.dlls = QLineEdit(cfg.dll_overrides)
        self.dlls.setPlaceholderText("e.g. winmm=n,b;version=n,b")
        self.dlls.setStyleSheet("font-family: monospace;")
        drow.addWidget(self.dlls, 1)
        drow.addWidget(button("Detect", "search", on_click=self._detect))
        ol.addLayout(drow)
        self.dll_note = label("Use Detect to find DLLs the game ships that Wine would otherwise replace with its "
                              "own (common with mods and multiplayer fixes).", "small", wrap=True)
        ol.addWidget(self.dll_note)
        lay.addWidget(oc)

        pc, pl = card()
        pl.addWidget(label("Proton prefix", "h2"))
        self.prefix_label = label("", "small", wrap=True, selectable=True)
        pl.addWidget(self.prefix_label)
        prow = QHBoxLayout()
        self.btn_prefix = button("Open prefix", "folder", on_click=lambda: open_folder(self._prefix_path))
        self.btn_winecfg = button("winecfg", "settings", on_click=lambda: self._tool("winecfg"))
        self.btn_winetricks = button("Winetricks", "settings", on_click=lambda: self._tool("winetricks"))
        self.btn_winetricks.setEnabled(bool(self.env.get("winetricks")))
        self.btn_log = button("Game log", "info", on_click=lambda: open_folder(self._log_path))
        self.btn_reset = button("Reset prefix…", "trash", "danger", on_click=self._reset)
        for b in (self.btn_prefix, self.btn_winecfg, self.btn_winetricks, self.btn_log, self.btn_reset):
            prow.addWidget(b)
        prow.addStretch(1)
        pl.addLayout(prow)
        lay.addWidget(pc)
        lay.addStretch(1)
        self._prefix_path = ""
        self._log_path = ""
        return w

    # ----------------------------------------------------------------- state
    def overrides(self) -> dict:
        o = {"launch_args": self.options.text().strip()}
        if self.exe.currentData():
            o["executable"] = self.exe.currentData()
        if self.linux:
            app_id = self.app_id.text().strip()
            o.update(launch_method=self.method.currentData(), proton_version=self.proton.currentData() or None,
                     steam_app_id=int(app_id) if app_id.isdigit() else None,
                     compat=CompatConfig(options=list(self.entry.compat_config.options),
                                         fixes=list(self.entry.compat_config.fixes),
                                         dll_overrides=self.dlls.text().strip(), use_runtime=self.runtime.isChecked(),
                                         protonfixes=self.protonfixes.isChecked(),
                                         report_app_id=self.report.isChecked(),
                                         fps_limit=self.entry.compat_config.fps_limit).to_dict())
        return o

    def _open_fixes(self) -> None:
        from ezgd.ui.dialogs.fixes import open_fixes
        if open_fixes(self.actions, self.entry, self):
            self.entry = self.core.library.get(self.entry.id)      # pick up the saved fixes
            self._refresh_preview()

    def _wire_preview(self) -> None:
        widgets = [self.options, self.exe]
        if self.linux:
            widgets += [self.method, self.proton, self.app_id, self.dlls, self.runtime, self.protonfixes,
                        self.report]
        for wd in widgets:
            sig = getattr(wd, "textChanged", None) or getattr(wd, "currentIndexChanged", None) or wd.toggled
            sig.connect(lambda *_: self._preview_timer.start())
        if self.linux:
            self.proton.currentIndexChanged.connect(lambda *_: self._refresh_fix_status())
            self.app_id.textChanged.connect(lambda *_: self._refresh_fix_status())

    def _refresh_preview(self) -> None:
        self.bridge.call(self.core.library_call("preview", self.entry.id, self.overrides()), self._show_preview,
                         owner=self)

    def _show_preview(self, p: dict) -> None:
        if p["ok"]:
            desc = p["description"]
            if p.get("warnings"):
                desc += "\n⚠ " + "\n⚠ ".join(p["warnings"])
            self.preview_desc.setText(desc)
            self.preview.setText(p["command"])
            self.preview.setStyleSheet("font-family: monospace; font-size: 8.5pt;")
        else:
            self.preview_desc.setText("")
            self.preview.setText(p["error"])
            self.preview.setStyleSheet(f"color: {theme.CURRENT.danger};")

    def _refresh_fix_status(self) -> None:
        app_id = self.app_id.text().strip()
        self.bridge.call(self.core.library_call("protonfix_status", self.entry.id, self.proton.currentData() or None,
                                                int(app_id) if app_id.isdigit() else 0),
                         self.fix_status.setText, owner=self)

    def _candidates(self, cands: list) -> None:
        current = self.entry.executable
        for c in cands[:25]:
            if str(c.path) != current:
                self.exe.addItem(c.label, str(c.path))
                self.exe.setItemData(self.exe.count() - 1, c.details, Qt.ToolTipRole)
        self.exe_hint.setText(f"{len(cands)} executable(s) found; best guesses first. Nothing runs until you "
                              f"press Play and confirm." if cands else "No executables found. Use Browse…")

    def _browse(self) -> None:
        start = self.entry.install_path or self.entry.download_path or str(Path.home())
        f, _ = QFileDialog.getOpenFileName(self, "Choose the game's executable", start)
        if f:
            self.exe.insertItem(0, Path(f).name, f)
            self.exe.setCurrentIndex(0)

    def _lookup(self, silent: bool) -> None:
        def done(app_id):
            if app_id:
                self.app_id.setText(str(app_id))
            elif not silent:
                self.actions.toasts.show("No exact match on Steam", "Enter the App ID manually (it is the number "
                                                                    "in the game's store.steampowered.com/app/ URL).",
                                         "warning")
        self.bridge.call(self.core.lookup_steam_app_id(self.entry.id), done,
                         None if silent else (lambda e: self.actions.fail("Steam lookup failed", e)), owner=self)

    def _detect(self) -> None:
        def done(s: dict):
            if s["overrides"]:
                self.dlls.setText(s["overrides"])
            self.dll_note.setText(s["explanation"])
        self.bridge.call(self.core.library_call("suggest_overrides", self.entry.id, self.exe.currentData()), done,
                         lambda e: self.actions.fail("Detect", e), owner=self)

    def _prefix_info(self, info: dict) -> None:
        self._prefix_path = info["path"]
        self._log_path = info["log"]
        self.prefix_label.setText(info["path"] + ("" if info["exists"] else
                                                  "\n(created the first time the game runs with Proton)"))
        for b in (self.btn_winecfg, self.btn_winetricks, self.btn_reset):
            b.setEnabled(info["exists"] and (b is not self.btn_winetricks or bool(self.env.get("winetricks"))))
        self.btn_prefix.setEnabled(info["exists"])

    def _tool(self, tool: str) -> None:
        self.bridge.call(self.core.library_call("prefix_tool", self.entry.id, tool),
                         lambda _r: self.actions.toasts.show(f"{tool} started"),
                         lambda e: self.actions.fail(tool, e))

    def _reset(self) -> None:
        r = QMessageBox.warning(self, "Reset Proton prefix",
                                "Delete this game's Proton prefix? Saves stored inside the prefix (Documents, "
                                "AppData) are deleted too. A fresh prefix is created on the next launch.",
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r == QMessageBox.Yes:
            self.bridge.call(self.core.library_call("reset_prefix", self.entry.id),
                             lambda _r: (self.actions.toasts.show("Prefix reset", kind="success"),
                                         self.bridge.call(self.core.library_call("prefix_info", self.entry.id),
                                                          self._prefix_info, owner=self)),
                             lambda e: self.actions.fail("Reset prefix", e))

    # ------------------------------------------------------------------ save
    def _save(self) -> None:
        o = self.overrides()
        changes = {"title": self.title_edit.text().strip() or self.entry.title, "launch_args": o["launch_args"]}
        for k in ("launch_method", "proton_version", "steam_app_id", "compat"):
            if k in o:
                changes[k] = o[k]
        exe = o.get("executable")

        def saved(_r):
            if exe and exe != self.entry.executable:
                self.bridge.call(self.core.library_call("set_executable", self.entry.id, exe),
                                 lambda _e: self.accept(), self._show_error, owner=self)
            else:
                self.accept()
        self.bridge.call(self.core.library_call("update", self.entry.id, **changes), saved, self._show_error,
                         owner=self)

    def _show_error(self, err: BaseException) -> None:
        self.error.set_text(f"Not saved: {err}")
        self.error.show()


def open_properties(actions, entry, env: dict, parent, tab: str = "general") -> bool:
    return PropertiesDialog(actions, entry, env, parent, tab).exec() == QDialog.Accepted
