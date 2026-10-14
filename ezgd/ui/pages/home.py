"""Home: quick search, active downloads, recent additions, OnlineFix popular and source status."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QScrollArea,
                               QVBoxLayout, QWidget)

from ezgd.catalog.models import GameGroup
from ezgd.catalog.search import group_duplicates
from ezgd.core.fsutil import format_bytes, format_speed
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_url
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import Banner, button, card, chip, clear_layout, label
from ezgd.ui.widgets.game_grid import GameGridView

STATE_TEXT = {"ok": ("Online", "#22c55e"), "offline-cache": ("Offline — cached", "#f59e0b"),
              "unavailable": ("Unavailable", "#ef4444"), "layout-changed": ("Layout changed", "#ef4444"),
              "error": ("Error", "#ef4444"), "unknown": ("Not checked yet", "#8c95ab")}


class HomePage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.active: dict[int, dict] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 24, 28, 28)
        lay.setSpacing(22)

        hero = QFrame()
        hero.setObjectName("hero")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(28, 26, 28, 26)
        hl.setSpacing(10)
        hl.addWidget(label("Find your next game", "title"))
        hl.addWidget(label("Search every enabled source at once, download with the built-in torrent and HTTP "
                           "engines, and keep your games organised in one library.", "muted", wrap=True))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search games…")
        self.search.returnPressed.connect(self._search)
        search_icon = QLabel(self.search)
        search_icon.setPixmap(icon("search", theme.CURRENT.muted, 18).pixmap(18, 18))
        search_icon.move(12, 12)
        row.addWidget(self.search, 1)
        row.addWidget(button("Search", "search", "primary", on_click=self._search))
        hl.addLayout(row)
        quick = QHBoxLayout()
        quick.addWidget(button("Browse all games", "grid", on_click=lambda: self.actions.navigate.emit("games")))
        quick.addWidget(button("OnlineFix Picks", "star", on_click=lambda: self.actions.navigate.emit("picks")))
        quick.addWidget(button("Library", "library", on_click=lambda: self.actions.navigate.emit("library")))
        quick.addWidget(button("Add download", "plus", on_click=lambda: self.actions.add_download_dialog(self)))
        quick.addStretch(1)
        hl.addLayout(quick)
        lay.addWidget(hero)

        self.system_banner = Banner("warning", "")
        self.system_banner.add_action("Fix it", lambda: self.actions.navigate.emit("system-check"), "settings")
        self.system_banner.hide()
        lay.addWidget(self.system_banner)

        # active downloads
        self.active_card, al = card()
        head = QHBoxLayout()
        head.addWidget(label("Active downloads", "h2"), 1)
        head.addWidget(button("Open Downloads", "download", "ghost",
                              on_click=lambda: self.actions.navigate.emit("downloads")))
        al.addLayout(head)
        self.active_list = QVBoxLayout()
        self.active_list.setSpacing(8)
        al.addLayout(self.active_list)
        lay.addWidget(self.active_card)

        lay.addWidget(label("Recently added", "h2"))
        self.recent_note = label("", "small")
        lay.addWidget(self.recent_note)
        self.recent = GameGridView(actions.covers, horizontal=True)
        self.recent.activated_group.connect(self._open)
        lay.addWidget(self.recent)

        lay.addWidget(label("Popular on OnlineFix", "h2"))
        self.popular = GameGridView(actions.covers, horizontal=True)
        self.popular.activated_group.connect(self._open)
        lay.addWidget(self.popular)

        lay.addWidget(label("Recently downloaded", "h2"))
        self.downloaded = QVBoxLayout()
        lay.addLayout(self.downloaded)

        lay.addWidget(label("Sources", "h2"))
        self.sources = QGridLayout()
        self.sources.setHorizontalSpacing(14)
        self.sources.setVerticalSpacing(14)
        lay.addLayout(self.sources)
        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self._render_active()

    # ---------------------------------------------------------------- data
    def load(self) -> None:
        core = self.bridge.core
        self.bridge.call(core.cached_recent(30), self._recent_cached, owner=self)
        # each source on its own, so a slow site does not hold up the others
        self._live_gen = getattr(self, "_live_gen", 0) + 1
        self._live: dict[str, object] = {}
        self._live_sources: set[str] = set()
        self._live_errors: dict[str, str] = {}
        for sid in list(theme.SOURCE_NAMES):
            self.bridge.call(core.latest(1, [sid]), lambda res, g=self._live_gen, s=sid: self._recent_live(res, g, s),
                             owner=self)
        self.bridge.call(core.featured(), self._featured, owner=self)
        self.bridge.call(core.recently_downloaded(6), self._downloaded, owner=self)
        self.load_sources()
        self.check_system()

    def check_system(self) -> None:
        self.bridge.call(self.bridge.core.system_status(), self._system, owner=self)

    def _system(self, st: dict) -> None:
        problems = []
        if st["missing_required"]:
            problems.append(f"{', '.join(st['missing_required'])} is missing, so downloaded RAR archives cannot "
                            f"be extracted")
        if not st["can_run_windows_games"]:
            problems.append("nothing that can run Windows games (umu-launcher or Steam's Proton) is installed")
        self.system_banner.setVisible(bool(problems))
        if problems:
            self.system_banner.set_text("Setup is incomplete: " + "; ".join(problems) + ". One click installs "
                                        "everything.")

    def load_sources(self) -> None:
        self.bridge.call(self.bridge.core.get_sources(), self._sources, owner=self)

    def _recent_cached(self, games) -> None:
        self._cached_recent = list(games or [])
        if games and not self.recent.model_.groups:
            self.recent.set_groups(group_duplicates(games))
            self.recent_note.setText("Cached — refreshing…")

    def _recent_live(self, res, gen: int | None = None, sid: str | None = None) -> None:
        if gen is not None and gen != self._live_gen:
            return
        if sid is not None:
            self._live_sources.add(sid)
        for g in res.games:
            self._live.setdefault(g.id, g)
        self._live_errors.update(res.errors)
        # sources that have not answered yet keep their cached games until they do
        waiting = [g for g in getattr(self, "_cached_recent", []) if g.source not in self._live_sources]
        games = sorted({**{g.id: g for g in waiting}, **self._live}.values(), key=lambda g: g.sort_time,
                       reverse=True)
        if games:
            self.recent.set_groups(group_duplicates(games))
        errs = "; ".join(f"{theme.source_name(k)}: {v}" for k, v in self._live_errors.items())
        pending = len(set(theme.SOURCE_NAMES) - self._live_sources)
        self.recent_note.setText("Newest listings from the enabled sources." + (f" Problems: {errs}" if errs else "")
                                 + (" Still loading some sources…" if pending else ""))
        if not pending:
            self.load_sources()

    def _featured(self, result) -> None:
        sections, _at, error = result
        popular = next((s for s in sections if s.key == "popular"), sections[0] if sections else None)
        if popular:
            self.popular.set_groups([GameGroup(g.dedup_key, [g]) for g in popular.games])
        self.load_sources()

    def _downloaded(self, snaps: list[dict]) -> None:
        clear_layout(self.downloaded)
        if not snaps:
            self.downloaded.addWidget(label("Nothing downloaded yet.", "muted"))
            return
        for s in snaps:
            row = QHBoxLayout()
            row.addWidget(label(s["title"]), 1)
            row.addWidget(label(format_bytes(s["total_bytes"]), "small"))
            row.addWidget(chip("Complete", theme.CURRENT.success))
            w = QWidget()
            w.setLayout(row)
            self.downloaded.addWidget(w)

    def _sources(self, sources: list[dict]) -> None:
        clear_layout(self.sources)
        for i, s in enumerate(sources):
            c, cl = card()
            top = QHBoxLayout()
            top.addWidget(label(s["name"], "h2"), 1)
            text, color = STATE_TEXT.get(s["status"], (s["status"], "#8c95ab"))
            if not s["enabled"]:
                text, color = "Disabled", "#8c95ab"
            top.addWidget(chip(text, color))
            cl.addLayout(top)
            cl.addWidget(label(s["description"], "muted", wrap=True))
            if s.get("message") and s["status"] != "ok":
                cl.addWidget(label(s["message"], "small", wrap=True))
            for note in s["notes"]:
                cl.addWidget(label("• " + note, "small", wrap=True))
            btns = QHBoxLayout()
            btns.addWidget(button("Visit site", "external", "ghost", on_click=lambda _=False, u=s["homepage"]: open_url(u)))
            btns.addStretch(1)
            cl.addLayout(btns)
            self.sources.addWidget(c, i // 2, i % 2)

    # ------------------------------------------------------------ downloads
    def download_updated(self, snap: dict) -> None:
        if snap["status"] in ("downloading", "metadata", "checking", "verifying", "queued"):
            self.active[snap["id"]] = snap
        else:
            self.active.pop(snap["id"], None)
            if snap["status"] == "completed":
                self.bridge.call(self.bridge.core.recently_downloaded(6), self._downloaded, owner=self)
        self._render_active()

    def download_removed(self, tid: int) -> None:
        self.active.pop(tid, None)
        self._render_active()

    def _render_active(self) -> None:
        clear_layout(self.active_list)
        items = sorted(self.active.values(), key=lambda s: s["id"])[:5]
        if not items:
            self.active_list.addWidget(label("No active downloads.", "muted"))
            return
        for s in items:
            w = QWidget()
            row = QHBoxLayout(w)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(label(s["title"]), 2)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(int(s["progress"] * 1000))
            bar.setFixedHeight(8)
            row.addWidget(bar, 3)
            info = "queued" if s["status"] == "queued" else f"{s['progress'] * 100:.0f}% · {format_speed(s['speed'])}"
            row.addWidget(label(info, "small"), 1, Qt.AlignRight)
            self.active_list.addWidget(w)

    def _search(self) -> None:
        q = self.search.text().strip()
        if q:
            self.actions.navigate.emit("games:" + q)

    def apply_card_size(self, key: str) -> None:
        self.recent.set_card_size(key)
        self.popular.set_card_size(key)

    def hide_ids(self, ids: set[str]) -> None:
        self.recent.hide_ids(ids)
        self.popular.hide_ids(ids)

    def refresh_installed(self) -> None:
        self.recent.set_installed(self.actions.installed_keys)
        self.popular.set_installed(self.actions.installed_keys)

    def _open(self, group) -> None:
        from ezgd.ui.dialogs.game_details import GameDetailsDialog
        GameDetailsDialog(self.actions, group, self).exec()
