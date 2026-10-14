"""The Fixes screen: one checkbox per fix that can apply to a game."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QScrollArea, QVBoxLayout,
                               QWidget)

from ezgd.library.fixes import CATEGORIES
from ezgd.ui import theme
from ezgd.ui.widgets.common import Banner, card, chip, clear_layout, label


class FixesDialog(QDialog):
    def __init__(self, actions, entry, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.core = actions.bridge.core
        self.entry = entry
        self.boxes: dict[str, QCheckBox] = {}
        self.groups: dict[str, str | None] = {}
        self.setWindowTitle(f"Fixes — {entry.title}")
        self.resize(820, 760)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(10)
        outer.addWidget(label(f"Fixes for {entry.title}", "h2", wrap=True))
        outer.addWidget(label("Tick the fixes you want to try, then Save — they are used the next time you press "
                              "Play, and unticking one undoes it completely. “Recommended” fixes were found by "
                              "checking this game's files and its own logs. Try one change at a time if a game "
                              "misbehaves.", "muted", wrap=True))
        self.problems = QVBoxLayout()
        outer.addLayout(self.problems)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setSpacing(12)
        self.body.addWidget(label("Checking this game…", "muted"))
        self.body.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.preview = label("", "small", wrap=True, selectable=True)
        self.preview.setStyleSheet("font-family: monospace; font-size: 8pt;")
        outer.addWidget(self.preview)
        self.error = Banner("error", "")
        self.error.hide()
        outer.addWidget(self.error)
        bb = QDialogButtonBox()
        save = bb.addButton("Save", QDialogButtonBox.AcceptRole)
        save.setProperty("variant", "primary")
        self.recommended_btn = bb.addButton("Turn on all recommended", QDialogButtonBox.ActionRole)
        self.recommended_btn.clicked.connect(self._enable_recommended)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self.bridge.call(self.core.library_call("fixes_for", entry.id), self._loaded, self._failed, owner=self)
        self.bridge.call(self.core.library_call("suggestions", entry.id), self._show_problems, owner=self)

    # ----------------------------------------------------------------- build
    def _loaded(self, items: list[dict]) -> None:
        clear_layout(self.body)
        self.items = {i["key"]: i for i in items}
        if not items:
            self.body.addWidget(label("No fixes apply to this game. Choose its executable first if you have not.",
                                      "muted", wrap=True))
            self.body.addStretch(1)
            return
        recommended = [i for i in items if i["recommended"]]
        sections = [("Recommended", recommended)] + [
            (cat, [i for i in items if i["category"] == cat and not i["recommended"]]) for cat in CATEGORIES[1:]]
        for title, rows in sections:
            if not rows:
                continue
            c, cl = card(margins=(16, 12, 16, 12), spacing=8)
            head = label(title, "h2")
            if title == "Recommended":
                head.setText("Recommended for this game")
            cl.addWidget(head)
            for i in rows:
                cl.addWidget(self._row(i))
            self.body.addWidget(c)
        self.body.addStretch(1)
        self.recommended_btn.setEnabled(any(not i["enabled"] for i in recommended))
        self._refresh_preview()

    def _row(self, i: dict) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 2, 0, 4)
        lay.setSpacing(2)
        top = QHBoxLayout()
        box = QCheckBox(i["title"])
        box.setChecked(i["enabled"])
        box.setEnabled(i["available"] or i["enabled"])
        box.setStyleSheet("font-weight: 600;")
        box.toggled.connect(lambda on, k=i["key"]: self._toggled(k, on))
        self.boxes[i["key"]] = box
        self.groups[i["key"]] = i.get("group")
        top.addWidget(box, 1)
        if i["recommended"]:
            top.addWidget(chip("Recommended", theme.CURRENT.success))
        if i["enabled"]:
            top.addWidget(chip("On", theme.CURRENT.accent))
        if not i["available"]:
            top.addWidget(chip("Not installed", theme.CURRENT.warning))
        lay.addLayout(top)
        desc = label(i["description"], "small", wrap=True)
        desc.setContentsMargins(26, 0, 0, 0)
        lay.addWidget(desc)
        for extra, color in ((i["recommended"], theme.CURRENT.success), (i["unavailable_reason"],
                                                                        theme.CURRENT.warning)):
            if extra:
                why = label(("Why: " if color == theme.CURRENT.success else "") + extra, "small", wrap=True)
                why.setContentsMargins(26, 0, 0, 0)
                why.setStyleSheet(f"color: {color};")
                lay.addWidget(why)
        return w

    def _show_problems(self, suggestions: list[dict]) -> None:
        # problems that are not simple switches (e.g. launch options written without %command%)
        for s in suggestions:
            if "enable_fix" in s["changes"]:
                continue
            b = Banner(s["severity"], f"{s['title']}\n{s['detail']}")
            if s["changes"]:
                b.add_action("Fix it", lambda k=s["key"], bb=b: self._apply_problem(k, bb), "check")
            self.problems.addWidget(b)

    def _apply_problem(self, key: str, banner: Banner) -> None:
        self.bridge.call(self.core.library_call("apply_suggestion", self.entry.id, key),
                         lambda _r: (banner.set_text("Fixed.", "success"), banner.clear_actions(),
                                     self._refresh_preview()),
                         lambda e: self.actions.fail("Could not apply", e), owner=self)

    # ----------------------------------------------------------------- state
    def _toggled(self, key: str, on: bool) -> None:
        group = self.groups.get(key)
        if on and group:
            for other, g in self.groups.items():
                if other != key and g == group and self.boxes[other].isChecked():
                    self.boxes[other].setChecked(False)          # only one renderer at a time
        self._preview_timer.start()

    def selected(self) -> list[str]:
        return [k for k, b in self.boxes.items() if b.isChecked()]

    def _enable_recommended(self) -> None:
        for k, i in self.items.items():
            if i["recommended"] and k in self.boxes:
                self.boxes[k].setChecked(True)

    def _refresh_preview(self) -> None:
        # preview with the ticked fixes, without saving: simulate through a temporary compat
        self.bridge.call(self._preview_coro(), self._show_preview, owner=self)

    async def _preview_coro(self):
        from ezgd.library import fixes as fx
        lib = self.core.library
        e = lib.get(self.entry.id)
        cfg = fx.apply_selection(lib._context(e), set(self.selected()))
        return lib.preview(self.entry.id, {"compat": cfg.to_dict()})

    def _show_preview(self, p: dict) -> None:
        self.preview.setText(("Will run: " + p["command"]) if p["ok"] else ("⚠ " + p["error"]))

    def _save(self) -> None:
        self.bridge.call(self.core.library_call("set_fixes", self.entry.id, self.selected()),
                         lambda _r: self.accept(), lambda e: (self.error.set_text(f"Not saved: {e}"),
                                                              self.error.show()), owner=self)

    def _failed(self, err: BaseException) -> None:
        clear_layout(self.body)
        self.body.addWidget(Banner("error", f"Could not check this game: {err}"))


def open_fixes(actions, entry, parent) -> bool:
    return FixesDialog(actions, entry, parent).exec() == QDialog.Accepted
