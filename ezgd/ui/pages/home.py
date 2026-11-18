"""Home: sleek search bar, centered auto-rotating Recently Added showcase, active/recent downloads & source health."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from ezgd.catalog.search import group_duplicates
from ezgd.core.fsutil import format_bytes, format_speed
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_url
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import Banner, button, card, chip, label, set_chip, tool
from ezgd.ui.widgets.showcase import ElidedLabel, GameShowcase, clear_now

STATE_TEXT = {"ok": ("Online", "#22c55e"), "offline-cache": ("Cached", "#f59e0b"),
              "unavailable": ("Unavailable", "#ef4444"), "layout-changed": ("Layout changed", "#ef4444"),
              "error": ("Error", "#ef4444"), "unknown": ("Unchecked", "#8c95ab")}


class HomePage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.active: dict[int, dict] = {}
        self._active_ids: list[int] | None = None                       # the downloads the rows show now
        self._active_rows: dict[int, tuple[QLabel, QProgressBar, QLabel]] = {}
        self._statuses: dict[int, str] = {}                            # each download's last status

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        wrap = QWidget()
        wrap_lay = QHBoxLayout(wrap)
        wrap_lay.setContentsMargins(24, 6, 24, 20)
        wrap_lay.setSpacing(0)
        wrap_lay.addStretch(1)

        canvas = QWidget()
        canvas.setMaximumWidth(1320)
        canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay = QVBoxLayout(canvas)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        wrap_lay.addWidget(canvas, 100)
        wrap_lay.addStretch(1)

        # Compact search & quick-navigation command bar
        hero = QFrame()
        hero.setObjectName("hero")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(20, 15, 20, 13)
        hl.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search games across every enabled source…")
        self.search.returnPressed.connect(self._search)
        self._search_icon = QLabel(self.search)
        self._search_icon.setPixmap(icon("search", theme.CURRENT.muted, 18).pixmap(18, 18))
        self._search_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.search.installEventFilter(self)
        row.addWidget(self.search, 1)
        row.addWidget(button("Search", "search", "primary", on_click=self._search))
        hl.addLayout(row)

        quick = QHBoxLayout()
        quick.setSpacing(8)
        quick.addWidget(button("Browse all games", "grid", on_click=lambda: self.actions.navigate.emit("games")))
        quick.addWidget(button("OnlineFix Picks", "star", on_click=lambda: self.actions.navigate.emit("picks")))
        quick.addWidget(button("Library", "library", on_click=lambda: self.actions.navigate.emit("library")))
        quick.addWidget(button("Add download", "plus", on_click=lambda: self.actions.add_download_dialog(self)))
        quick.addStretch(1)
        quick.addWidget(label("Ctrl+F search  ·  F11 Couch mode", "small"))
        hl.addLayout(quick)
        lay.addWidget(hero)

        self.system_banner = Banner("warning", "")
        self.system_banner.add_action("Fix it", lambda: self.actions.navigate.emit("system-check"), "settings")
        self.system_banner.hide()
        lay.addWidget(self.system_banner)

        # Active downloads (shown automatically while transfers are running/queued)
        self.active_card, al = card(margins=(16, 12, 16, 12), spacing=8)
        head = QHBoxLayout()
        head.addWidget(label("Active downloads", "h2"), 1)
        head.addWidget(button("Open Downloads", "download", "ghost",
                              on_click=lambda: self.actions.navigate.emit("downloads")))
        al.addLayout(head)
        self.active_list = QVBoxLayout()
        self.active_list.setSpacing(8)
        al.addLayout(self.active_list)
        lay.addWidget(self.active_card)

        # Vertical centering springs keep Recently Added poised right at the center on tall displays
        lay.addStretch(1)
        self.recent = GameShowcase(
            actions,
            title_text="Recently added",
            trailing_button=("Browse all", "grid", lambda: self.actions.navigate.emit("games")),
            parent=self,
        )
        self.recent_note = self.recent.note_label
        self.recent.activated_group.connect(self._open)
        lay.addWidget(self.recent)
        lay.addStretch(1)

        # Compact bottom dashboard: Sources status + Recently downloaded side-by-side
        bottom = QHBoxLayout()
        bottom.setSpacing(16)

        src_card, sl = card(margins=(16, 12, 16, 12), spacing=8)
        src_head = QHBoxLayout()
        src_head.addWidget(label("Sources", "h2"), 1)
        self._sources_chip = chip("Checking…", "#8c95ab")
        src_head.addWidget(self._sources_chip)
        sl.addLayout(src_head)
        self.sources = QGridLayout()
        self.sources.setHorizontalSpacing(10)
        self.sources.setVerticalSpacing(8)
        sl.addLayout(self.sources)
        sl.addStretch(1)
        bottom.addWidget(src_card, 7)

        dl_card, dl = card(margins=(16, 12, 16, 12), spacing=8)
        dl_head = QHBoxLayout()
        dl_head.addWidget(label("Recently downloaded", "h2"), 1)
        dl_head.addWidget(button("Downloads", "download", "ghost",
                                 on_click=lambda: self.actions.navigate.emit("downloads")))
        dl.addLayout(dl_head)
        self.downloaded = QVBoxLayout()
        self.downloaded.setSpacing(6)
        dl.addLayout(self.downloaded)
        dl.addStretch(1)
        bottom.addWidget(dl_card, 5)

        lay.addLayout(bottom)

        scroll.setWidget(wrap)
        outer.addWidget(scroll)
        self._place_search_icon()
        self._render_active()

    def _place_search_icon(self) -> None:
        y = max(8, (self.search.height() - 18) // 2)
        self._search_icon.move(12, y)

    def eventFilter(self, obj, e) -> bool:
        if obj is self.search and e.type() == QEvent.Resize:
            self._place_search_icon()
        return super().eventFilter(obj, e)

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
        self.bridge.call(core.recently_downloaded(5), self._downloaded, owner=self)
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
        pending = len(set(theme.SOURCE_NAMES) - self._live_sources)
        if not self._live_errors:
            err_summary, err_tip = "", ""
        elif len(self._live_errors) == 1:
            k, v = next(iter(self._live_errors.items()))
            err_summary = f"  ·  {theme.source_name(k)}: {v}"
            err_tip = f"{theme.source_name(k)}: {v}"
        else:
            err_summary = f"  ·  {len(self._live_errors)} sources unreachable"
            err_tip = "\n".join(f"{theme.source_name(k)}: {v}" for k, v in self._live_errors.items())
        self.recent_note.setToolTip(err_tip)
        self.recent_note.setText("Auto-rotating newest releases"
                                 + err_summary
                                 + ("  ·  Refreshing…" if pending else ""))
        if not pending:
            self.load_sources()

    def _downloaded(self, snaps: list[dict]) -> None:
        clear_now(self.downloaded)
        if not snaps:
            self.downloaded.addWidget(label("Nothing downloaded yet.", "muted"))
            return
        for s in snaps[:4]:
            w = QWidget()
            row = QHBoxLayout(w)
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(8)
            t = ElidedLabel(s["title"], parent=w)
            t.setToolTip(s["title"])
            row.addWidget(t, 1)
            row.addWidget(label(format_bytes(s["total_bytes"]), "small"))
            row.addWidget(chip("Complete", theme.CURRENT.success))
            self.downloaded.addWidget(w)

    def _sources(self, sources: list[dict]) -> None:
        clear_now(self.sources)
        enabled = [s for s in sources if s["enabled"]]
        online = sum(1 for s in enabled if s["status"] == "ok")
        issues = sum(1 for s in enabled if s["status"] in ("unavailable", "layout-changed", "error"))
        if not enabled:
            summary_txt, summary_col = "All disabled", "#8c95ab"
        elif issues:
            summary_txt, summary_col = f"{online}/{len(enabled)} online · {issues} issue", "#f59e0b"
        elif online:
            summary_txt, summary_col = f"{online}/{len(enabled)} online", "#22c55e"
        else:
            summary_txt, summary_col = f"{len(enabled)} enabled", "#8c95ab"
        set_chip(self._sources_chip, summary_txt, summary_col)

        pal = theme.CURRENT
        cols = 3 if len(sources) >= 3 else 2
        for c in range(cols):
            self.sources.setColumnStretch(c, 1)
        for i, s in enumerate(sources):
            tile = QFrame()
            tile.setStyleSheet(f"QFrame {{ background: {pal.bg1}; border: 1px solid {pal.border}; border-radius: 9px; }}"
                               "QFrame QLabel, QFrame QToolButton { border: none; background: transparent; }")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(10, 6, 6, 6)
            tl.setSpacing(2)

            top = QHBoxLayout()
            top.setSpacing(6)
            dot = QFrame()
            dot.setFixedSize(8, 8)
            sc = theme.SOURCE_COLORS.get(s["id"], pal.accent)
            dot.setStyleSheet(f"background: {sc}; border-radius: 4px; border: none;")
            top.addWidget(dot, 0, Qt.AlignVCenter)

            short_name = theme.SOURCE_NAMES.get(s["id"], s["name"])
            name_lbl = ElidedLabel(short_name, parent=tile)
            name_lbl.setStyleSheet("font-weight: 650;")
            top.addWidget(name_lbl, 1)

            text, color = STATE_TEXT.get(s["status"], (s["status"], "#8c95ab"))
            if not s["enabled"]:
                text, color = "Disabled", "#8c95ab"
            top.addWidget(chip(text, color))
            visit = tool("external", f"Visit {s['name']}",
                         on_click=lambda u=s["homepage"]: open_url(u), color=pal.muted)
            visit.setFixedSize(24, 24)
            visit.setIconSize(QSize(14, 14))
            top.addWidget(visit)
            tl.addLayout(top)

            tip_lines = [s["name"], s["description"]]
            if s.get("message") and s["status"] != "ok":
                tip_lines.append(f"Status: {s['message']}")
            for note in s.get("notes") or ():
                tip_lines.append(f"• {note}")
            tile.setToolTip("\n".join(x for x in tip_lines if x))
            self.sources.addWidget(tile, i // cols, i % cols)

    # ------------------------------------------------------------ downloads
    def download_updated(self, snap: dict) -> None:
        tid, status = snap["id"], snap["status"]
        before = self._statuses.get(tid)
        self._statuses[tid] = status
        if status in ("downloading", "metadata", "checking", "verifying", "queued"):
            self.active[tid] = snap
        elif self.active.pop(tid, None) is None and status == before:
            return                     # (a paused or seeding torrent says so every second: nothing here changes)
        if status == "completed" and before != "completed":
            self.bridge.call(self.bridge.core.recently_downloaded(5), self._downloaded, owner=self)
        self._render_active()

    def download_removed(self, tid: int) -> None:
        self.active.pop(tid, None)
        self._statuses.pop(tid, None)
        self._render_active()

    def _render_active(self) -> None:
        items = sorted(self.active.values(), key=lambda s: s["id"])[:5]
        ids = [s["id"] for s in items]
        self.active_card.setVisible(bool(items))
        if ids != self._active_ids:
            # rows only when downloads come or go — every progress update built them all again (2.5× a second per
            # download, with the page out of sight too: in couch mode, in the tray, during a game)
            self._active_ids = ids
            clear_now(self.active_list)
            self._active_rows = {}
            for s in items:
                w = QWidget()
                row = QHBoxLayout(w)
                row.setContentsMargins(0, 0, 0, 0)
                title, info = label(), label("", "small")
                bar = QProgressBar()
                bar.setRange(0, 1000)
                bar.setFixedHeight(8)
                row.addWidget(title, 2)
                row.addWidget(bar, 3)
                row.addWidget(info, 1, Qt.AlignRight)
                self.active_list.addWidget(w)
                self._active_rows[s["id"]] = (title, bar, info)
        for s in items:
            title, bar, info = self._active_rows[s["id"]]
            title.setText(s["title"])
            bar.setValue(int(s["progress"] * 1000))
            info.setText("queued" if s["status"] == "queued" else
                         f"{s['progress'] * 100:.0f}% · {format_speed(s['speed'])}")

    def _search(self) -> None:
        q = self.search.text().strip()
        if q:
            self.actions.navigate.emit("games:" + q)

    def apply_card_size(self, key: str) -> None:
        self.recent.set_card_size(key)

    def hide_ids(self, ids: set[str]) -> None:
        self.recent.hide_ids(ids)

    def refresh_installed(self) -> None:
        self.recent.set_installed(self.actions.installed_keys)

    def _open(self, group) -> None:
        from ezgd.ui.dialogs.game_details import GameDetailsDialog
        GameDetailsDialog(self.actions, group, self).exec()
