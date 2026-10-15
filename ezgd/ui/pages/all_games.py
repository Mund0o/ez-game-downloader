"""All Games: unified search across enabled sources with filters, sorting and incremental loading."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from ezgd.catalog.models import Game
from ezgd.catalog.search import Filters, InstallFilter, SortKey, facet_values, filter_and_group
from ezgd.ui import theme
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import Banner, EmptyState, button, clear_layout, label
from ezgd.ui.widgets.game_grid import GameGridView

DEBOUNCE_MS = 400


class AllGamesPage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.games: dict[str, Game] = {}
        self.page = 0
        self.pages: dict[str, int] = {}
        self.inflight: set[str] = set()
        self.has_more: dict[str, bool] = {}
        self.generation = 0
        self.loading = False
        self.errors: dict[str, str] = {}
        self.stale: set[str] = set()
        self.known_genres: list[str] = []
        self.enabled_sources: list[str] = list(theme.SOURCE_NAMES)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 12)
        lay.setSpacing(12)
        head = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.addWidget(label("All Games", "title"))
        title_col.addWidget(label("One search across every enabled source. Results are fetched live and cached.",
                                  "muted"))
        head.addLayout(title_col, 1)
        head.addWidget(button("Add download", "plus", on_click=lambda: self.actions.add_download_dialog(self)))
        lay.addLayout(head)

        search_wrap = QWidget()
        sw = QHBoxLayout(search_wrap)
        sw.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search " + ", ".join(theme.SOURCE_NAMES.values()) + "…")
        self.search.setClearButtonEnabled(True)
        sw.addWidget(self.search)
        ic = QLabel(self.search)
        ic.setPixmap(icon("search", theme.CURRENT.muted, 18).pixmap(18, 18))
        ic.move(12, 12)
        lay.addWidget(search_wrap)

        # sources on their own row: chips keep their full names however many sites there are
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_row.addWidget(label("Sources", "muted"))
        self.source_buttons: dict[str, QPushButton] = {}
        self.source_box = QHBoxLayout()
        self.source_box.setSpacing(8)
        source_row.addLayout(self.source_box)
        source_row.addStretch(1)
        self._build_source_chips()
        lay.addLayout(source_row)
        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.genre = self._combo(["All genres"], self._genre_changed)
        self.platform = self._combo(["Any platform"], self._refilter)
        self.method = self._combo(["Any download method", "In-app torrent", "In-app direct", "Browser only"],
                                  self._refilter)
        self.method.setToolTip("Download methods are known after a game's page has been opened once.")
        self.install = self._combo(["Any status", "In library", "Not in library"], self._refilter)
        self.sort = self._combo(["Best match", "Recently added", "Title A–Z", "Most sources"], self._refilter)
        for w in (self.genre, self.platform, self.method, self.install):
            filters.addWidget(w)
        filters.addStretch(1)
        filters.addWidget(label("Sort", "muted"))
        filters.addWidget(self.sort)
        lay.addLayout(filters)

        self.banners = QVBoxLayout()
        self.banners.setSpacing(6)
        lay.addLayout(self.banners)
        self.status = label("", "muted")
        lay.addWidget(self.status)

        self.stack = QStackedWidget()
        self.grid = GameGridView(actions.covers)
        self.grid.activated_group.connect(self._open)
        self.grid.near_bottom.connect(self.load_more)
        self.empty = EmptyState("search", "No games found", "Try a different search or adjust the filters.")
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.empty)
        lay.addWidget(self.stack, 1)
        self.more_btn = button("Load more", "plus", "ghost", on_click=self.load_more)
        lay.addWidget(self.more_btn, 0, Qt.AlignCenter)

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(DEBOUNCE_MS)
        self.debounce.timeout.connect(self.reload)
        self.search.textChanged.connect(lambda _t: self.debounce.start())
        self.search.returnPressed.connect(self._search_now)
        self._started = False

    def _combo(self, items: list[str], slot) -> QComboBox:
        c = QComboBox()
        c.addItems(items)
        c.currentIndexChanged.connect(slot)
        c.setMinimumWidth(130)
        return c

    # ---------------------------------------------------------------- state
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._started:
            self._started = True
            self.reload()
            self.bridge.call(self.bridge.core.genres(), self._set_genres, owner=self)

    def view_state(self) -> dict:
        """Filters and sort to remember for the next start."""
        return {"sort": self.sort.currentIndex(), "method": self.method.currentIndex(),
                "install": self.install.currentIndex(), "genre": self.genre.currentText(),
                "platform": self.platform.currentText(),
                "off": [sid for sid, b in self.source_buttons.items() if not b.isChecked()]}

    def restore_view(self, st: dict) -> None:
        for combo, key in ((self.sort, "sort"), (self.method, "method"), (self.install, "install")):
            i = st.get(key)
            if isinstance(i, int) and 0 <= i < combo.count():
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
        for sid in st.get("off") or []:
            b = self.source_buttons.get(sid)
            if b is not None:
                b.blockSignals(True)
                b.setChecked(False)
                b.blockSignals(False)
        self._wanted_genre = st.get("genre") or ""          # genres and platforms arrive later
        self._wanted_platform = st.get("platform") or ""

    def set_query(self, text: str) -> None:
        self._started = True
        self.search.blockSignals(True)
        self.search.setText(text)
        self.search.blockSignals(False)
        self.reload()

    def _build_source_chips(self) -> None:
        for b in self.source_buttons.values():
            b.deleteLater()
        self.source_buttons.clear()
        for sid, name in theme.SOURCE_NAMES.items():
            pw = theme.source_password(sid)
            b = QPushButton(f"🔒 {name}" if pw else name)
            b.setToolTip(f"Show games from {name}." + (f" All {name} archives are password-protected "
                                                       f"(password: {pw})." if pw else ""))
            b.setProperty("variant", "chip")
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            b.setCheckable(True)
            b.setChecked(True)
            b.toggled.connect(self._source_toggled)
            self.source_buttons[sid] = b
            self.source_box.addWidget(b)

    def sources_changed(self) -> None:
        """A site was added or removed: new chips, fresh results."""
        self._build_source_chips()
        self.enabled_sources = list(theme.SOURCE_NAMES)
        self.search.setPlaceholderText("Search " + ", ".join(theme.SOURCE_NAMES.values()) + "…")
        self.reload()

    def set_enabled_sources(self, enabled: dict) -> None:
        self.enabled_sources = [s for s in theme.SOURCE_NAMES if enabled.get(s, True)]
        for sid, b in self.source_buttons.items():
            b.setVisible(enabled.get(sid, True))

    def _set_genres(self, genres: list[str]) -> None:
        self.known_genres = genres
        self._update_facets()

    def _search_now(self) -> None:
        self.debounce.stop()
        self.reload()

    def _selected_sources(self) -> list[str]:
        return [s for s, b in self.source_buttons.items() if b.isChecked() and s in self.enabled_sources]

    def _source_toggled(self, _on: bool) -> None:
        self.reload()

    def _genre_changed(self, _i: int) -> None:
        # with an empty query the genre is applied server-side (category listing)
        if not self.search.text().strip():
            self.reload()
        else:
            self._refilter()

    # -------------------------------------------------------------- loading
    def reload(self) -> None:
        self.generation += 1
        self.games.clear()
        self.page = 0
        self.pages: dict[str, int] = {}             # each source pages on its own: a slow site holds up no one
        self.inflight: set[str] = set()
        self.has_more = {}
        self.errors = {}
        self.stale = set()
        self.loading = False
        self._refilter()
        self.load_more()

    def load_more(self) -> None:
        sources = [s for s in self._selected_sources() if s not in self.inflight
                   and (not self.pages.get(s) or self.has_more.get(s))]
        if not sources:
            self._update_status()
            return
        gen = self.generation
        query = self.search.text().strip()
        genre = self.genre.currentText() if self.genre.currentIndex() > 0 else None
        core = self.bridge.core
        for sid in sources:
            self.pages[sid] = self.pages.get(sid, 0) + 1
            n = self.pages[sid]
            self.inflight.add(sid)
            coro = core.search(query, n, [sid]) if query else core.latest(n, [sid], genre)
            self.bridge.call(coro, lambda res, s=sid: self._loaded(gen, res, s),
                             lambda err, s=sid: self._failed(gen, err, s), owner=self)
        self.page = max(self.pages.values())
        self.loading = True
        self._update_status(loading=True)

    def _loaded(self, gen: int, res, sid: str | None = None) -> None:
        if gen != self.generation:
            return                                   # a newer search replaced this one
        self.inflight.discard(sid)
        self.loading = bool(self.inflight)
        for g in res.games:
            self.games.setdefault(g.id, g)
        self.has_more.update(res.has_more)
        for s in res.has_more:
            self.errors.pop(s, None)
        self.errors.update(res.errors)
        self.stale |= res.stale
        self._render_banners()
        self._update_facets()
        self._refilter(keep_order=True)
        if self.stack.currentWidget() is self.grid and self.grid.verticalScrollBar().maximum() == 0 and \
                any(self.has_more.values()) and len(self.games) < 200:
            QTimer.singleShot(50, self.load_more)    # fill the viewport

    def _failed(self, gen: int, err: BaseException, sid: str | None = None) -> None:
        if gen != self.generation:
            return
        self.inflight.discard(sid)
        self.loading = bool(self.inflight)
        self.errors[sid or "all"] = str(err)
        self._render_banners()
        self._update_status()

    # ------------------------------------------------------------ filtering
    def filters(self) -> Filters:
        method_map = {1: {"torrent"}, 2: {"direct"}, 3: {"external"}}
        install_map = {0: InstallFilter.ANY, 1: InstallFilter.INSTALLED, 2: InstallFilter.NOT_INSTALLED}
        genre = {self.genre.currentText()} if self.genre.currentIndex() > 0 else set()
        platform = {self.platform.currentText()} if self.platform.currentIndex() > 0 else set()
        return Filters(query=self.search.text().strip(), sources=set(self._selected_sources()), genres=genre,
                       platforms=platform, methods=method_map.get(self.method.currentIndex(), set()),
                       install=install_map[self.install.currentIndex()])

    def sort_key(self) -> SortKey:
        return [SortKey.RELEVANCE, SortKey.RECENT, SortKey.TITLE, SortKey.SOURCES][self.sort.currentIndex()]

    def _refilter(self, *_args, keep_order: bool = False) -> None:
        """``keep_order``: more results arrived while scrolling — the games already shown keep their places and
        the new ones go at the end (re-sorting everything moved the games under the mouse, 2026-10-13). Changing
        the search, a filter or the sort order sorts everything afresh."""
        f = self.filters()
        if not self.search.text().strip():
            f.query = ""
        groups = filter_and_group(self.games.values(), f, self.actions.installed_keys, self.sort_key())
        order = getattr(self, "_shown_order", [])
        if keep_order and order:
            by_key = {g.key: g for g in groups}
            kept = [by_key.pop(k) for k in order if k in by_key]
            groups = kept + [g for g in groups if g.key in by_key]
        self._shown_order = [g.key for g in groups]
        self.grid.set_installed(self.actions.installed_keys)
        self.grid.set_groups(groups)
        self.stack.setCurrentWidget(self.grid if groups or self.loading else self.empty)
        if not groups and not self.loading:
            if self.errors and not self.games:
                self.empty.set("Sources unavailable", "No source could be reached. Cached results are shown when "
                                                      "available; check your connection and retry.")
            elif self.filters().methods and self.games:
                self.empty.set("No games match", "Download methods are only known for games whose page you have "
                                                 "opened. Clear the download-method filter to see everything.")
            else:
                self.empty.set("No games found", "Try a different search or adjust the filters.")
        self._update_status()

    def _update_facets(self) -> None:
        facets = facet_values(self.games.values())
        genres = sorted(set(facets["genres"]) | set(self.known_genres))
        self._fill(self.genre, "All genres", genres)
        self._fill(self.platform, "Any platform", facets["platforms"])
        for combo, attr in ((self.genre, "_wanted_genre"), (self.platform, "_wanted_platform")):
            want = getattr(self, attr, "")
            i = combo.findText(want) if want else -1
            if i > 0:                                        # last session's choice, once it is offered again
                setattr(self, attr, "")
                combo.setCurrentIndex(i)

    @staticmethod
    def _fill(combo: QComboBox, first: str, values: list[str]) -> None:
        current = combo.currentText()
        if [combo.itemText(i) for i in range(1, combo.count())] == values:
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(first)
        combo.addItems(values)
        i = combo.findText(current)
        combo.setCurrentIndex(max(0, i))
        combo.blockSignals(False)

    def _update_status(self, loading: bool = False) -> None:
        n = self.grid.model_.rowCount()
        dupes = sum(1 for g in self.grid.model_.groups if g.is_duplicate)
        parts = [f"{n} game{'s' if n != 1 else ''}"]
        if dupes:
            parts.append(f"{dupes} offered by several sources")
        if loading or self.loading:
            parts.append("loading…")
        self.status.setText(" · ".join(parts))
        self.more_btn.setVisible(any(self.has_more.get(s) and s not in self.inflight for s in self._selected_sources()))

    def _render_banners(self) -> None:
        clear_layout(self.banners)
        for sid, msg in self.errors.items():
            name = theme.SOURCE_NAMES.get(sid, "Search")
            b = Banner("warning", f"{name}: {msg}")
            b.add_action("Retry", self.reload, "retry")
            if sid in theme.SOURCE_NAMES:
                b.add_action("Report", lambda s=sid, m=msg: self.actions.report(self, s, None, m), "bug")
            self.banners.addWidget(b)
        for sid in self.stale:
            if sid not in self.errors:
                self.banners.addWidget(Banner("info", f"{theme.SOURCE_NAMES.get(sid, sid)} is unreachable — showing "
                                                      f"cached results."))

    def hide_ids(self, ids: set[str]) -> None:
        for i in ids:
            self.games.pop(i, None)
        self._refilter(keep_order=True)

    def refresh_installed(self) -> None:
        self.grid.set_installed(self.actions.installed_keys)
        if self.install.currentIndex() > 0:
            self._refilter(keep_order=True)

    def apply_card_size(self, key: str) -> None:
        self.grid.set_card_size(key)

    def _open(self, group) -> None:
        from ezgd.ui.dialogs.game_details import GameDetailsDialog
        GameDetailsDialog(self.actions, group, self).exec()
