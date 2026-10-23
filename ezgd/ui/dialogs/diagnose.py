"""Debugging assistant window: what happened, the most likely cause (with the log lines that prove it) and the
fix — or an honest "no fix under Proton" / "nothing found"."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QScrollArea,
                               QVBoxLayout, QWidget)

from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_folder
from ezgd.ui.widgets.common import button, card, chip, clear_layout, label

log = logging.getLogger(__name__)


def _describe_change(g: dict) -> str:
    """The exact change a generated fix makes, for the user to see."""
    parts = []
    if g.get("env"):
        parts.append("launch options: " + " ".join(f"{k}={v}" for k, v in g["env"].items()) + " %command%")
    if g.get("args"):
        parts.append(f"game arguments: {g['args']}")
    if g.get("dll_overrides"):
        parts.append("DLL overrides: " + ";".join(f"{k}={v}" for k, v in g["dll_overrides"].items()))
    if g.get("options"):
        parts.append("Proton options: " + ", ".join(g["options"]))
    files: dict[str, list[str]] = {}
    for e in g.get("edits") or []:
        if e.get("kind") == "move_aside":
            parts.append(f"move {e['path']} aside (kept as a backup)")
        else:
            sec = f"[{e['section']}] " if e.get("section") else ""
            files.setdefault(e["path"], []).append(f"{sec}{e.get('key')}={e.get('value')}")
    for path, changes in files.items():                # one entry per file (Unreal fixes set several keys)
        parts.append(f"edit {path}: {', '.join(changes)} (backup kept)")
    return " · ".join(parts)


def _strength(conf: float) -> tuple[str, str]:
    if conf >= 0.8:
        return "Strong evidence", theme.CURRENT.danger
    if conf >= 0.6:
        return "Likely", theme.CURRENT.warning
    return "Possible", theme.CURRENT.muted


class DiagnoseDialog(QDialog):
    def __init__(self, actions, entry, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.core = actions.core
        self._applied: set = set()                 # fixes applied in this window (shown as done)
        self.entry = entry
        self._waiting_run = False
        self._findings: list[dict] = []
        self._generated: list[dict] = []
        self._generated_deep: list[dict] = []
        self._symptom = ""
        self._trial_rows: dict[int, object] = {}
        self.verdict_text = ""
        self.setWindowTitle(f"Debugging assistant — {entry.title}")
        self.resize(860, 760)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(label(f"Why isn't {entry.title} working?", "h2", wrap=True))
        srow = QHBoxLayout()
        srow.addWidget(label("What happens?", "muted"))
        self.symptom = QComboBox()
        self.symptom.addItem("Not sure / let the assistant check", "")
        from ezgd.library.fixgen import SYMPTOMS
        for key, text in SYMPTOMS.items():
            self.symptom.addItem(text, key)
        self.symptom.setToolTip("What you see shapes the fixes the assistant builds (a black screen needs different "
                                "fixes than a crash)")
        self.symptom.currentIndexChanged.connect(self._symptom_changed)
        srow.addWidget(self.symptom)
        srow.addStretch(1)
        self.fix_btn = button("Fix my game", "wrench", "primary", on_click=self._fix_my_game,
                              tooltip="Checks everything (and starts the game once to look, when the logs are not "
                                      "clear), repairs what the evidence points to, tests the game and keeps the "
                                      "setup that works — then tells you plainly what happened")
        self.fix_btn.setMinimumHeight(40)
        srow.addWidget(self.fix_btn)
        lay.addLayout(srow)
        self.stats = label("", "small")
        lay.addWidget(self.stats)
        self.summary = label("Reading the game's last run…", "muted", wrap=True, selectable=True)
        lay.addWidget(self.summary)
        from PySide6.QtWidgets import QProgressBar
        self.busy = QProgressBar()                 # "working…" while the logs, files and Steam are checked
        self.busy.setRange(0, 0)
        self.busy.setFixedHeight(4)
        self.busy.setTextVisible(False)
        lay.addWidget(self.busy)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setSpacing(10)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        self.facts = label("", "small", wrap=True)          # (kept for callers; the facts are tags now)
        self.facts.hide()
        from ezgd.ui.widgets.common import FlowLayout
        self.fact_tags = FlowLayout(spacing=6)
        lay.addLayout(self.fact_tags)
        row = QHBoxLayout()
        # "Fix my game" investigates by itself when the evidence is weak; looking without changing anything is in
        # "More" (the two buttons looked like the same thing — 2026-10-13)
        self.inv_btn = button("Investigate only", "bug", on_click=self._investigate)
        self.inv_btn.hide()
        self.diag_btn = button("Run a diagnostic launch", "bug", on_click=self._diagnostic_run)   # (in "More")
        self.try_btn = button("Try fixes for me", "wrench", on_click=lambda: self._try_fixes(),
                              tooltip="Launches the game a few times, each with a different candidate fix, in a "
                                      "throw-away copy of its prefix — and shows which ones kept it running")
        row.addWidget(self.try_btn)
        self.deep_btn = button("Try everything (slower)", "wrench", on_click=self._try_everything)   # (in "More")
        self.stop_btn = button("Stop", "close", "ghost", on_click=self._stop_trials)
        self.stop_btn.hide()
        row.addWidget(self.stop_btn)
        row.addWidget(button("Check again", "refresh", "ghost", on_click=self.refresh))
        log_path = self.core.paths.logs / f"game-{entry.id}.log"
        # the less common tools in one menu, so the main choices stand out
        from PySide6.QtWidgets import QMenu
        self.more_btn = button("More", "more", "ghost")
        menu = QMenu(self.more_btn)
        menu.addAction("Investigate only — start it once and look (changes nothing)", self._investigate)
        menu.addAction("Try everything (slower, up to 18 setups)", self._try_everything)
        menu.addAction("Run a diagnostic launch (you play, it records)", self._diagnostic_run)
        menu.addSeparator()
        menu.addAction("Open log folder", lambda: open_folder(log_path))
        self.more_btn.setMenu(menu)
        row.addWidget(self.more_btn)
        row.addStretch(1)
        row.addWidget(button("Close", variant="ghost", on_click=self.accept))
        lay.addLayout(row)
        self.bridge.event.connect(self._on_event)
        self._fm_rows: dict[str, object] = {}
        self._verdict: dict | None = None
        self._show_stats(self.core.fix_stats())
        self.refresh()

    # ----------------------------------------------------------------- data
    def refresh(self) -> None:
        self.summary.setText("Reading the game's last run, its files and how Steam runs it…")
        self.busy.show()
        self.bridge.call(self.core.diagnose_game(self.entry.id, self._symptom), self._show,
                         lambda e: (self.busy.hide(), self.summary.setText(f"Could not analyse the game: {e}")),
                         owner=self)

    # ---------------------------------------------------------- fix my game
    def _show_stats(self, st: dict) -> None:
        self.stats.setText(f"Games the assistant has fixed for you: {st['fixed']} of {st['games']} ({st['rate']}%)"
                           if st.get("games") else "")

    def _fix_my_game(self, confirm: bool = True) -> None:
        if confirm and QMessageBox.question(
                self, "Fix my game",
                f"The assistant will check {self.entry.title}, repair what the evidence clearly points to (damaged "
                "files, Steam setup, fix archives, missing runtimes — every change keeps a backup) and then start the "
                "game a few times in a throw-away copy of its prefix to find a setup that works. Game windows will "
                "open and close. Start?") != QMessageBox.Yes:
            return
        clear_layout(self.body)
        self._fm_rows.clear()
        c, self._fm_box = card(margins=(16, 12, 16, 12), spacing=6)
        self._fm_head = label("Fixing…", "h2")
        self._fm_box.addWidget(self._fm_head)
        self._fm_bar = QProgressBar()                 # how far it is (the steps report it)
        self._fm_bar.setRange(0, 100)
        self._fm_bar.setValue(0)
        self._fm_box.addWidget(self._fm_bar)
        self.body.addWidget(c)
        self._trial_rows.clear()
        c2, self._trial_box = card(margins=(16, 12, 16, 12), spacing=6)
        self._trial_head = label("Tests", "h2")
        self._trial_box.addWidget(self._trial_head)
        self._trial_card = c2
        self.body.addWidget(c2)
        self.body.addStretch(1)
        for b in (self.fix_btn, self.try_btn, self.deep_btn, self.diag_btn, self.more_btn, self.inv_btn):
            b.setEnabled(False)
        self.stop_btn.show()
        self.summary.setText("Working on it — this takes a few minutes.")
        self.bridge.call(self.core.fix_my_game(self.entry.id, self._symptom), self._fix_done,
                         lambda e: (self._trials_finished(), self.fix_btn.setEnabled(True),
                                    self.summary.setText(f"Could not finish: {e}")), owner=self)

    def _fm_event(self, p: dict) -> None:
        bar = getattr(self, "_fm_bar", None)
        if bar is not None and p.get("progress") is not None:
            bar.setValue(max(bar.value(), int(round(float(p["progress"]) * 100))))
        row = self._fm_rows.get(p["step"])
        if row is None:
            row = label("", wrap=True)
            self._fm_rows[p["step"]] = row
            self._fm_box.addWidget(row)
        mark = "…" if p["status"] == "running" else "✓"
        row.setText(f"{mark} {p['step']}" + (f" — {p['detail']}" if p["detail"] else ""))

    def _fix_done(self, v: dict) -> None:
        try:
            if getattr(self, "_fm_bar", None) is not None:
                self._fm_bar.setValue(100)
        except RuntimeError:                          # (the view was rebuilt meanwhile)
            pass
        self._trials_finished()
        self.fix_btn.setEnabled(True)
        self._fm_head.setText("What the assistant did")
        if not self._trial_rows:
            self._trial_card.hide()
        self._verdict = v
        c, cl = card(margins=(16, 14, 16, 14), spacing=8)
        if v["status"] == "fixed":
            head = label(f"✓ Fixed — {self.entry.title} kept running with: {v['kept']}", "h2", wrap=True)
            head.setStyleSheet(f"color: {theme.CURRENT.success};")
        elif v["status"] == "trying":
            head = label(f"On trial: {v['kept']}", "h2", wrap=True)
            cl.addWidget(head)
            head = label(v.get("reason", ""), wrap=True, selectable=True)
        elif v["status"] in ("changed",):
            head = label("Repairs applied — start the game to see if it works now", "h2", wrap=True)
        elif v["status"] == "seen":                   # the test run showed the problem's cause: no game fix for it
            head = label(f"What the test showed: {v.get('reason', '')}", "h2", wrap=True)
        else:
            head = label(f"✗ Not fixed: {v.get('reason', '')}", "h2", wrap=True)
            head.setStyleSheet(f"color: {theme.CURRENT.danger};")
        cl.addWidget(head)
        if v["applied"]:
            cl.addWidget(label("What was changed:\n" + "\n".join(f"• {a}" for a in v["applied"]), wrap=True,
                               selectable=True))
        if v.get("investigation"):
            cl.addWidget(label("What the investigation found: " + v["investigation"][-1][:600], wrap=True,
                               selectable=True))
        if v.get("report"):
            from pathlib import Path
            rep = Path(v["report"])
            row = QHBoxLayout()
            row.addWidget(label(f"Every step is written down in {rep.name}", "small", wrap=True), 1)
            row.addWidget(button("Open the report", "folder", on_click=lambda: _open_file(rep)))
            cl.addLayout(row)
        if v.get("offer_ge"):
            from ezgd.core.fsutil import format_bytes
            ge = v["offer_ge"]
            cl.addWidget(label(f"A newer Proton build, {ge['tag']}, has not been tried yet — a different Proton is the "
                               "most common fix.", wrap=True))
            self.ge_btn = button(f"Install {ge['tag']} ({format_bytes(ge['size'])}) and try again", "download",
                                 "primary", on_click=lambda: self._install_ge(ge["tag"]))
            cl.addWidget(self.ge_btn)
        if v["status"] == "fixed":
            cl.addWidget(label(f"The test checked the problem itself: {v['checked']}. Play it — did it actually work?"
                               if v.get("checked") else
                               "“Kept running” means it did not crash. Play it — did it actually work?", "small",
                               wrap=True))
            row = QHBoxLayout()
            row.addWidget(button("Yes, it works", "check", "primary", on_click=lambda: self._answer(True)))
            row.addWidget(button("No — undo and show other options", "close",
                                 on_click=lambda: self._answer(False)))
            row.addStretch(1)
            cl.addLayout(row)
        self.body.insertWidget(0, c)
        self.summary.setText({"fixed": "Done.", "changed": "Repairs applied.",
                              "nothing": "Nothing to repair automatically."}.get(v["status"], "Done — not fixed."))

    def _install_ge(self, tag: str) -> None:
        self.ge_btn.setEnabled(False)
        self.ge_btn.setText(f"Downloading {tag}…")
        self.bridge.call(self.core.install_ge_proton(),
                         lambda name: (self.actions.toasts.show(f"{name} installed", "Trying again with it…", "success"),
                                       self._fix_my_game(confirm=False)),
                         lambda e: (self.ge_btn.setEnabled(True), self.ge_btn.setText("Try the install again"),
                                    self.actions.fail("Could not install GE-Proton", e)), owner=self)

    def _answer(self, worked: bool) -> None:
        v = self._verdict or {}

        def done(st: dict) -> None:
            self._show_stats(st)
            if worked:
                self.actions.toasts.show("Great — noted", "The assistant remembers what fixed it.", "success")
            else:
                self.bridge.call(self.core.undo_fix_my_game(self.entry.id, v.get("before", {})),
                                 lambda msg: (self.actions.toasts.show("Undone", msg), self.refresh()), owner=self)
        self.bridge.call(self.core.record_outcome(self.entry.id, worked, v.get("applied", [])), done, owner=self)

    def _symptom_changed(self, _i: int) -> None:
        self._symptom = self.symptom.currentData() or ""
        self.refresh()

    def _show(self, d: dict) -> None:
        self.busy.hide()
        self._findings = d["findings"]
        self._generated = d.get("generated") or []
        self._generated_deep = d.get("generated_deep") or []
        clear_layout(self.body)
        steps = {st["kind"]: st["text"] for st in d.get("reasoning") or []}
        self.verdict_text = d["summary"]                        # the whole verdict (tooltip; tests)
        self.summary.setText(steps.get("what") or d["summary"].split(". ")[0] + ".")
        self.summary.setToolTip(d["summary"])
        self._set_facts(d["facts"])
        causes = [f for f in d["findings"] if not f["key"].startswith(("exit:", "rule-error"))]
        top = causes[0] if causes and causes[0]["confidence"] >= 0.5 else None
        self.body.addWidget(self._answer_card(d, top))
        if d.get("reasoning"):
            self.body.addWidget(self._section("Why the assistant thinks so", self._reasoning(d["reasoning"]),
                                              expanded=False))
        others = [f for f in causes if f is not top]
        if others:
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(6)
            for f in others:
                bl.addWidget(self._finding(f, top=False))
            self.body.addWidget(self._section(f"Other possible causes ({len(others)})", box,
                                              expanded=top is None and len(others) <= 3))
        if self._generated:
            self.body.addWidget(self._section(f"Fixes the assistant built for this game ({len(self._generated)})",
                                              self._generated_list(self._generated), expanded=False,
                                              action=("Test them", self._try_fixes)))
        self.body.addStretch(1)

    def _set_facts(self, facts: list[str]) -> None:
        while self.fact_tags.count():
            w = self.fact_tags.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for fact in facts[:8]:
            t = chip(fact if len(fact) <= 60 else fact[:57] + "…", theme.CURRENT.muted)
            t.setToolTip(fact)
            self.fact_tags.addWidget(t)

    def _answer_card(self, d: dict, top: dict | None) -> QWidget:
        """The answer, big: the cause, how sure, and the one button that fixes it."""
        from PySide6.QtWidgets import QProgressBar
        from ezgd.ui.icons import icon
        c, cl = card(margins=(22, 18, 22, 18), spacing=10)
        c.setStyleSheet(f"#card {{ border: 1px solid {theme.CURRENT.accent}; }}")
        head = QHBoxLayout()
        head.setSpacing(14)
        ic = QLabel()
        if top is None:
            name, color = ("info", theme.CURRENT.muted)
        elif not top["fixable"]:
            name, color = ("warning", theme.CURRENT.danger)
        else:
            name, color = ("bug", theme.CURRENT.warning)
        ic.setPixmap(icon(name, color, 34).pixmap(34, 34))
        head.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        if top is not None:
            title = top["title"]
            first = top["explanation"].split(". ")[0].rstrip(".") + "."
        elif not d["run"].get("started"):
            title = "The game has not been started from the app yet"
            first = "Press Play once — or “Fix my game” (top right), which starts it in a test copy and looks."
        elif not d["findings"]:
            title = "No known problem in the logs"
            first = ("The last run looks normal." if not d["suggest_diagnostic_run"] else
                     "If something is wrong anyway, “Fix my game” (top right) starts it once with detailed "
                     "logging to find out more.")
        else:
            title = "No clear cause yet"
            first = "The clues are weak. “Fix my game” (top right) looks closer with one test start and tries the likely fixes."
        h = label(title, None, wrap=True)
        h.setStyleSheet("font-size: 15pt; font-weight: 700;")
        col.addWidget(h)
        col.addWidget(label(first, None, wrap=True))
        if top is not None:
            sure = QHBoxLayout()
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(round(top["confidence"] * 100)))
            bar.setFixedSize(160, 8)
            bar.setTextVisible(False)
            sure.addWidget(bar)
            sure.addWidget(label(f"{int(round(top['confidence'] * 100))}% sure", "small"))
            sure.addStretch(1)
            col.addLayout(sure)
        head.addLayout(col, 1)
        cl.addLayout(head)
        row = QHBoxLayout()
        row.addStretch(1)
        if top is not None and top.get("fix"):
            key = (top["fix"]["kind"], top["fix"]["value"])
            b = button(top["fix"]["label"], "wrench", "primary")
            b.setMinimumHeight(40)
            if key in self._applied:
                b.setText("✓ Done")
                b.setEnabled(False)
            b.clicked.connect(lambda _=False, fx=top["fix"], b=b: self._apply(fx, b))
            row.addWidget(b)
        elif top is not None and not top["fixable"]:
            no = label("No setting fixes this under Proton.", "small")
            no.setStyleSheet(f"color: {theme.CURRENT.danger}; font-weight: 600;")
            row.addWidget(no)
        if top is not None:
            det = button("Details", "info", "ghost")
            more = self._details(top)
            more.hide()
            det.clicked.connect(lambda: (more.setVisible(not more.isVisible()),
                                         det.setText("Hide details" if more.isVisible() else "Details")))
            row.insertWidget(0, det)
            cl.addLayout(row)
            cl.addWidget(more)
        else:
            cl.addLayout(row)
        return c

    def _details(self, f: dict) -> QWidget:
        """The full explanation and the log lines behind a finding (shown on request)."""
        w = QWidget()
        wl = QVBoxLayout(w)
        wl.setContentsMargins(0, 4, 0, 0)
        wl.setSpacing(6)
        wl.addWidget(label(f["explanation"], None, wrap=True, selectable=True))
        if f["evidence"]:
            ev = label("\n".join(f["evidence"][:5]), "small", wrap=True, selectable=True)
            ev.setStyleSheet(f"font-family: monospace; color: {theme.CURRENT.muted}; background: {theme.CURRENT.bg3};"
                             " padding: 8px; border-radius: 6px;")
            wl.addWidget(ev)
        return w

    def _section(self, title: str, content: QWidget, expanded: bool = False, action: tuple | None = None) -> QWidget:
        """A folded section: a header to click, the content below it."""
        from PySide6.QtWidgets import QPushButton, QSizePolicy
        c, cl = card(margins=(16, 8, 16, 8), spacing=8)
        head = QHBoxLayout()
        t = QPushButton(("▾  " if expanded else "▸  ") + title)
        t.setCheckable(True)
        t.setChecked(expanded)
        t.setFlat(True)
        t.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        t.setStyleSheet("QPushButton { border: none; background: transparent; text-align: left; padding: 6px 2px; "
                        "font-weight: 650; font-size: 11pt; }")
        t.setCursor(Qt.PointingHandCursor)
        head.addWidget(t, 1)
        if action:
            head.addWidget(button(action[0], "wrench", "ghost", on_click=action[1]))
        cl.addLayout(head)
        content.setVisible(expanded)
        cl.addWidget(content)

        def toggle(on: bool) -> None:
            content.setVisible(on)
            t.setText(("▾  " if on else "▸  ") + title)
        t.toggled.connect(toggle)
        return c

    STEP_TITLES = {"what": "What happened", "last": "Last words", "phase": "How far it got", "history": "History",
                   "agree": "Agrees", "cause": "Conclusion", "ruled_out": "Ruled out", "next": "Next"}
    STEP_ICONS = {"what": "info", "last": "info", "phase": "play", "history": "refresh", "agree": "check",
                  "cause": "bug", "ruled_out": "check", "next": "wrench"}

    def _reasoning(self, steps: list[dict]) -> QWidget:
        """The assistant's reasoning, one step per line — so its conclusion can be checked, not just trusted."""
        from ezgd.ui.icons import icon
        w = QWidget()
        wl = QVBoxLayout(w)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(8)
        for st in steps:
            row = QHBoxLayout()
            row.setSpacing(10)
            ic = QLabel()
            ic.setPixmap(icon(self.STEP_ICONS.get(st["kind"], "info"), theme.CURRENT.accent, 16).pixmap(16, 16))
            row.addWidget(ic, 0, Qt.AlignTop)
            text = st["text"]
            for prefix in ("How far it got: ", "Checked and fine: ", "Next step: "):
                text = text.removeprefix(prefix)
            k = label(self.STEP_TITLES.get(st["kind"], ""), "small")
            k.setFixedWidth(96)
            k.setStyleSheet("font-weight: 650;")
            row.addWidget(k, 0, Qt.AlignTop)
            row.addWidget(label(text, None, wrap=True, selectable=True), 1)
            wl.addLayout(row)
        return w

    def _generated_list(self, gen: list[dict]) -> QWidget:
        w = QWidget()
        cl = QVBoxLayout(w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        cl.addWidget(label("Made from this game's files, logs and your PC. Nothing is applied until it has been "
                           "tested in a throw-away copy of the prefix.", "small", wrap=True))
        for g in gen:
            t = label(("★ " if g.get("learned_from") else "• ") + g["label"]
                      + (f" — worked for {g['learned_from']} before" if g.get("learned_from") else ""), wrap=True)
            t.setStyleSheet("font-weight: 650;")
            t.setToolTip(g["why"] + ("\n\n" + _describe_change(g) if _describe_change(g) else ""))
            cl.addWidget(t)
            why = label(g["why"], "small", wrap=True)
            why.setContentsMargins(14, 0, 0, 0)
            cl.addWidget(why)
            change = _describe_change(g)
            if change:                                         # the exact change, for those who want to see it
                ch = label(change, "small", wrap=True, selectable=True)
                ch.setStyleSheet(f"font-family: monospace; color: {theme.CURRENT.muted};")
                ch.setContentsMargins(14, 0, 0, 4)
                cl.addWidget(ch)
        return w

    def _generated_card(self, gen: list[dict]) -> QWidget:          # (kept for callers)
        return self._section("Fixes the assistant built for this game", self._generated_list(gen), True,
                             ("Test them", self._try_fixes))

    def _reasoning_card(self, steps: list[dict]) -> QWidget:          # (kept for callers)
        return self._section("Why the assistant thinks so", self._reasoning(steps), True)

    def _finding(self, f: dict, top: bool) -> QWidget:
        """One line per cause: how strong, what, its fix button — the explanation opens on click."""
        c, cl = card(margins=(14, 8, 14, 8), spacing=4)
        head = QHBoxLayout()
        head.setSpacing(10)
        text, color = _strength(f["confidence"])
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 12pt;")
        dot.setToolTip(text)
        head.addWidget(dot, 0, Qt.AlignVCenter)
        t = label(f["title"], None, wrap=True)
        t.setStyleSheet("font-weight: 650;")
        head.addWidget(t, 1)
        if f["fix"]:
            key = (f["fix"]["kind"], f["fix"]["value"])
            b = button(f["fix"]["label"], "wrench", None)
            if key in self._applied:
                b.setText("✓ Done")
                b.setEnabled(False)
            b.clicked.connect(lambda _=False, fx=f["fix"], b=b: self._apply(fx, b))
            head.addWidget(b, 0, Qt.AlignVCenter)
        det = button("", "info", "ghost", tooltip="Why, and the log lines behind it")
        head.addWidget(det, 0, Qt.AlignVCenter)
        cl.addLayout(head)
        if f.get("learned"):                    # what the app saw on this computer (ezgd.core.app_core learning)
            ln = label(("✗ " if "did not help" in f["learned"] else "✓ ") + f["learned"], "small", wrap=True)
            ln.setStyleSheet(f"color: {theme.CURRENT.danger if 'did not help' in f['learned'] else theme.CURRENT.success};")
            cl.addWidget(ln)
        more = self._details(f)
        if not f["fix"] and not f["fixable"]:
            no = label("No setting fixes this under Proton.", "small")
            no.setStyleSheet(f"color: {theme.CURRENT.danger}; font-weight: 600;")
            more.layout().addWidget(no)
        more.hide()
        det.clicked.connect(lambda: more.setVisible(not more.isVisible()))
        cl.addWidget(more)
        return c

    # -------------------------------------------------------------- actions
    def _apply(self, fix: dict, btn=None) -> None:
        if fix["kind"] == "command":
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(fix["value"])
            QMessageBox.information(self, "Command copied", "This needs administrator rights, so the app cannot do "
                                    f"it for you. Paste it into a terminal:\n\n{fix['value']}")
            return
        if fix["kind"] == "system":
            self.actions.navigate.emit("system-check")
            return
        if fix["kind"] == "reset_prefix" and QMessageBox.question(
                self, "Rebuild prefix", "The current prefix is moved aside (kept, not deleted) and a fresh one is "
                "created the next time you press Play. Afterwards the assistant offers to copy your saves into it. "
                "Continue?") != QMessageBox.Yes:
            return
        import shiboken6
        original = btn.text() if btn is not None else ""

        def state(text: str, enabled: bool) -> None:
            if btn is not None and shiboken6.isValid(btn):
                btn.setText(text)
                btn.setEnabled(enabled)
        state("Installing… (this can take a few minutes)" if fix["kind"] == "winetricks" else "Working…", False)

        def ok(msg: str) -> None:
            self._applied.add((fix["kind"], fix["value"]))
            state("✓ Done", False)
            self.actions.toasts.show("Fix applied", msg, "success")
            self.refresh()

        def failed(e: BaseException) -> None:
            state(original or "Try again", True)
            self.actions.fail("Could not apply the fix", e)
        self.bridge.call(self.core.apply_fix(self.entry.id, fix["kind"], fix["value"]), ok, failed, owner=self)

    # --------------------------------------------------------------- trials
    def _try_everything(self) -> None:
        self._try_fixes(deep=True)

    def _try_fixes(self, deep: bool = False) -> None:
        n = "up to 18" if deep else "several"
        extra = (" It also tries general fixes the findings do not point at (synchronisation, CPU cores, locale, "
                 "renderers, the overlay, the runtime …) and stops at the first setup that keeps the game running. "
                 "This can take 10–15 minutes." if deep else "")
        if QMessageBox.question(
                self, "Try everything" if deep else "Try fixes for me",
                f"The assistant will start {self.entry.title} {n} times (up to a minute each), every time with "
                "a different candidate fix, in a throw-away copy of its prefix — your real prefix and saves are not "
                "touched and nothing is changed until you choose a setup. Game windows will open and close; you can "
                f"press Stop at any time.{extra} Start?") != QMessageBox.Yes:
            return
        clear_layout(self.body)
        self._trial_rows.clear()
        c, self._trial_box = card(margins=(16, 12, 16, 12), spacing=6)
        self._trial_head = label("Trying fixes…", "h2")
        self._trial_box.addWidget(self._trial_head)
        self._trial_box.addWidget(label("“Kept running” means the game did not crash or quit within the test time — "
                                        "check that it actually looked right before keeping a setup.", "small",
                                        wrap=True))
        self.body.addWidget(c)
        self.body.addStretch(1)
        self.try_btn.setEnabled(False)
        self.deep_btn.setEnabled(False)
        self.more_btn.setEnabled(False)
        self.diag_btn.setEnabled(False)
        self.stop_btn.show()
        self.summary.setText("Testing candidate fixes one after another…")
        gen = self._generated + (self._generated_deep if deep else [])
        trials = (self.core.run_trials(self.entry.id, self._findings, gen, deep=True, symptom=self._symptom) if deep
                  else self.core.run_trials(self.entry.id, self._findings, gen, symptom=self._symptom))
        self.bridge.call(trials, self._trials_done,
                         lambda e: (self._trials_finished(), self.actions.fail("Could not run the trials", e)),
                         owner=self)

    def _investigate(self) -> None:
        if QMessageBox.question(
                self, "Investigate automatically",
                f"The assistant will start {self.entry.title} once (up to a minute) in a throw-away copy of its "
                "prefix, with Wine's detailed logging, and analyse what happens — which DLLs load, every error and "
                "where it stops. A game window may open and close; your real prefix and saves are not touched. "
                "Start?") != QMessageBox.Yes:
            return
        clear_layout(self.body)
        self._trial_rows.clear()
        c, self._trial_box = card(margins=(16, 12, 16, 12), spacing=6)
        self._trial_head = label("Investigating…", "h2")
        self._trial_box.addWidget(self._trial_head)
        self.body.addWidget(c)
        self.body.addStretch(1)
        for b in (self.fix_btn, self.try_btn, self.deep_btn, self.diag_btn, self.inv_btn, self.more_btn):
            b.setEnabled(False)
        self.stop_btn.show()
        self.summary.setText("Running the game once with detailed logging…")

        def done(d: dict) -> None:
            self._trials_finished()
            self._show(d)
            r = d.get("investigation") or {}
            self.summary.setText("Investigation: " + (r.get("detail") or "") + ". " + d["summary"])
        self.bridge.call(self.core.investigate(self.entry.id, self._symptom), done,
                         lambda e: (self._trials_finished(), self.actions.fail("Could not investigate", e)),
                         owner=self)

    def _trial_repairs(self, findings: list[dict]) -> None:
        """Causes the test runs revealed whose fix changes the real prefix (not testable in a copy): offered."""
        try:
            c, cl = card(margins=(16, 12, 16, 12), spacing=6)
            cl.addWidget(label("The test runs also found", "h2"))
            cl.addWidget(label("These were found in the detailed logs of the tests. They change the game's real "
                               "prefix, so they were not tried in the copies — apply them, then press Play or "
                               "“Try fixes for me” again.", "small", wrap=True))
            self.body.insertWidget(max(0, self.body.count() - 1), c)
            for f in findings:
                self.body.insertWidget(max(0, self.body.count() - 1), self._finding(f, top=False))
        except RuntimeError:
            pass

    def _stop_trials(self) -> None:
        self.bridge.call(self.core.cancel_trials())
        self.stop_btn.setEnabled(False)

    def _trial_event(self, p: dict) -> None:
        from shiboken6 import isValid
        row = self._trial_rows.get(p["index"])
        if row is not None and not isValid(row):           # the results were cleared for a new round of tests
            row = None
        if row is None:
            box = getattr(self, "_trial_box", None)
            if box is None or not isValid(box):
                return
            row = label("", wrap=True)
            self._trial_rows[p["index"]] = row
            self._trial_box.addWidget(row)
        if p["status"] == "running":
            row.setText(f"{p['index']}/{p['total']} · {p['label']} — running…")
        else:
            r = p["result"]
            mark = {"ran": "✓", "crashed": "✗", "exited": "✗", "error": "!", "cancelled": "–",
                    "symptom": "✗"}.get(r["outcome"], "?")
            row.setText(f"{mark} {p['label']} — {r['detail']}")
            color = theme.CURRENT.success if r["outcome"] == "ran" else theme.CURRENT.danger if r["outcome"] in (
                "crashed", "exited", "error") else theme.CURRENT.muted
            row.setStyleSheet(f"color: {color};")

    def _trials_done(self, results: list[dict]) -> None:
        from shiboken6 import isValid
        self._trials_finished()
        head = getattr(self, "_trial_head", None)
        if head is not None and isValid(head):            # (the results may have been cleared meanwhile)
            head.setText(f"Results ({len(results)} setup{'s' if len(results) != 1 else ''} tested)")
        good = sorted([r for r in results if r["outcome"] == "ran"],          # really in the game first
                      key=lambda r: (bool(r.get("stuck")), -len(r.get("progress") or [])))
        if not results:
            self.summary.setText("No trials were run.")
            return
        if not good:
            self.summary.setText(f"None of the {len(results)} setups kept the game running. The cause is probably "
                                 "something no setting changes (see the findings: “Check again”).")
            return
        baseline_ok = results[0]["outcome"] == "ran"
        self.summary.setText(
            ("The current setup kept running too, so the problem may be later in the game or in what you see "
             "(black screen, no sound) — tell the assistant what happens with a diagnostic launch."
             if baseline_ok else f"{len(good)} setup(s) kept the game running where the current one did not."))
        for r in good:
            if not r["overrides"]:
                continue
            row = QHBoxLayout()
            row.addWidget(label(f"{r['label']}" + ("  (built by the assistant)" if r.get("generated") else "")
                                + ("  — ⚠ may be stuck at start-up" if r.get("stuck") else ""), None), 1)
            row.addWidget(button("Use this setup", "check", "primary",
                                 on_click=lambda r=r: self._use_setup(r)))
            w = QWidget()
            w.setLayout(row)
            self._trial_box.addWidget(w)

    def _trials_finished(self) -> None:
        self.try_btn.setEnabled(True)
        self.deep_btn.setEnabled(True)
        self.more_btn.setEnabled(True)
        self.inv_btn.setEnabled(True)
        self.fix_btn.setEnabled(True)
        self.diag_btn.setEnabled(True)
        self.stop_btn.hide()
        self.stop_btn.setEnabled(True)

    def _use_setup(self, r: dict) -> None:
        problems = [f["key"] for f in self._findings[:3]] + ([f"symptom:{self._symptom}"] if self._symptom else [])
        self.bridge.call(self.core.apply_trial(self.entry.id, r["overrides"], r.get("gen_id"), problems),
                         lambda msg: self.actions.toasts.show("Setup saved", msg, "success"),
                         lambda e: self.actions.fail("Could not save the setup", e), owner=self)

    def _diagnostic_run(self) -> None:
        if QMessageBox.question(self, "Diagnostic launch", f"Start {self.entry.title} with detailed logging? Use it "
                                "as usual until the problem happens (or close it); the assistant then explains what "
                                "it recorded.") != QMessageBox.Yes:
            return
        self._waiting_run = True
        self.summary.setText("The game is running with detailed logging… close it (or wait until it fails) and the "
                             "analysis appears here.")
        self.bridge.call(self.core.library_call("launch", self.entry.id, True, True), lambda _p: None,
                         lambda e: (setattr(self, "_waiting_run", False),
                                    self.actions.fail("Could not start the diagnostic launch", e)), owner=self)

    def _on_event(self, name: str, payload) -> None:
        try:
            self._handle_event(name, payload)
        except RuntimeError as exc:                        # a part of this window was already rebuilt or closed
            log.debug("diagnose window: event %s after its widget went: %s", name, exc)

    def _handle_event(self, name: str, payload) -> None:
        if name == "trial" and isinstance(payload, dict) and payload.get("id") == self.entry.id:
            self._trial_event(payload)
            return
        if name == "trial_repairs" and isinstance(payload, dict) and payload.get("id") == self.entry.id:
            self._trial_repairs(payload.get("findings") or [])
            return
        if name == "fixmygame" and isinstance(payload, dict) and payload.get("id") == self.entry.id:
            self._fm_event(payload)
            return
        if name == "proton_install" and isinstance(payload, dict) and getattr(self, "ge_btn", None) is not None:
            from ezgd.core.fsutil import format_bytes
            try:
                if payload["status"] == "unpacking":
                    self.ge_btn.setText(f"Unpacking {payload['tag']}…")
                elif payload["status"] != "done" and payload.get("total"):
                    self.ge_btn.setText(f"Downloading {payload['tag']}: {format_bytes(payload['received'])} of "
                                        f"{format_bytes(payload['total'])}")
            except RuntimeError:
                pass
        if name == "game_state" and isinstance(payload, dict) and payload.get("id") == self.entry.id \
                and not payload.get("running") and self._waiting_run:
            self._waiting_run = False
            self.refresh()

    def done(self, r: int) -> None:
        try:
            self.bridge.event.disconnect(self._on_event)
        except (RuntimeError, TypeError):
            pass
        super().done(r)


def _open_file(path) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def open_diagnose(actions, entry, parent) -> None:
    DiagnoseDialog(actions, entry, parent).exec()
