"""OnlineFix Picks: the curated carousels that online-fix.me actually shows on its home page."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ezgd.catalog.models import GameGroup
from ezgd.ui.widgets.common import Banner, EmptyState, button, clear_layout, label, relative_time
from ezgd.ui.widgets.game_grid import GameGridView

EXPLANATION = ("online-fix.me has no separate “picks” page. This section shows the two curated carousels on its "
               "home page: “Популярное” (Popular) and “Обновления” (Recently updated). Results are cached for an "
               "hour; use Refresh to fetch them again.")


class PicksPage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.grids: list[GameGridView] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(28, 24, 28, 24)
        self.lay.setSpacing(14)
        head = QHBoxLayout()
        col = QVBoxLayout()
        col.addWidget(label("OnlineFix Picks", "title"))
        col.addWidget(label(EXPLANATION, "muted", wrap=True))
        head.addLayout(col, 1)
        self.updated = label("", "small")
        head.addWidget(self.updated)
        head.addWidget(button("Refresh", "refresh", on_click=lambda: self.load(force=True)))
        self.lay.addLayout(head)
        self.banner = Banner("warning")
        self.banner.hide()
        self.lay.addWidget(self.banner)
        self.sections = QVBoxLayout()
        self.sections.setSpacing(18)
        self.lay.addLayout(self.sections)
        self.lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self._loaded_once = False
        self.card_size = "comfortable"

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self._loaded_once:
            self._loaded_once = True
            self.load()

    def load(self, force: bool = False) -> None:
        self.updated.setText("Loading…")
        self.bridge.call(self.bridge.core.featured(force), self._show, self._fail, owner=self)

    def _show(self, result) -> None:
        sections, fetched_at, error = result
        self.updated.setText(f"Updated {relative_time(fetched_at)}" if fetched_at else "")
        self.banner.setVisible(bool(error))
        if error:
            self.banner.clear_actions()
            self.banner.set_text(("Could not refresh OnlineFix picks: " + error +
                                  (" Showing the last cached copy." if sections else "")))
            self.banner.add_action("Report", lambda: self.actions.report(self, "onlinefix", "https://online-fix.me/",
                                                                         error), "bug")
        clear_layout(self.sections)
        self.grids = []
        if not sections:
            self.sections.addWidget(EmptyState("star", "No picks available",
                                               "The OnlineFix home page could not be read. Try again later."))
            return
        for s in sections:
            box = QVBoxLayout()
            box.setSpacing(6)
            box.addWidget(label(f"{s.title}", "h2"))
            box.addWidget(label(f"From the “{s.source_label}” carousel on online-fix.me · {len(s.games)} games",
                                "small"))
            grid = GameGridView(self.actions.covers, horizontal=True)
            grid.set_card_size(self.card_size)
            grid.set_installed(self.actions.installed_keys)
            grid.set_groups([GameGroup(g.dedup_key, [g]) for g in s.games])
            grid.activated_group.connect(self._open)
            self.grids.append(grid)
            box.addWidget(grid)
            self.sections.addLayout(box)

    def _fail(self, err: BaseException) -> None:
        self.updated.setText("")
        self.banner.set_text(f"Could not load OnlineFix picks: {err}")
        self.banner.show()

    def apply_card_size(self, key: str) -> None:
        self.card_size = key
        for g in self.grids:
            g.set_card_size(key)

    def hide_ids(self, ids: set[str]) -> None:
        for g in self.grids:
            g.hide_ids(ids)

    def refresh_installed(self) -> None:
        for g in self.grids:
            g.set_installed(self.actions.installed_keys)

    def _open(self, group) -> None:
        from ezgd.ui.dialogs.game_details import GameDetailsDialog
        GameDetailsDialog(self.actions, group, self).exec()
