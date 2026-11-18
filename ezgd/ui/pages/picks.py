"""OnlineFix Picks: centered auto-rotating showcases for online-fix.me's Popular & Recently Updated carousels."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from ezgd.catalog.models import GameGroup
from ezgd.ui import theme
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import Banner, EmptyState, button, label, relative_time
from ezgd.ui.widgets.showcase import GameShowcase, clear_now

EXPLANATION_TIP = (
    "online-fix.me has no separate “picks” page — this section shows the two curated carousels on its home page: "
    "“Популярное” (Popular) and “Обновления” (Recently updated). Results are cached for an hour; click Refresh to "
    "fetch them again."
)


class PicksPage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.grids: list[GameShowcase] = []
        self._section_keys: list[str] = []
        self._active_key: str = "popular"
        self._tab_buttons: list[tuple[QPushButton, str]] = []
        self._loaded_once = False
        self.card_size = "comfortable"

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
        self.lay = QVBoxLayout(canvas)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(14)
        wrap_lay.addWidget(canvas, 100)
        wrap_lay.addStretch(1)

        # Compact header & section switcher card matching Home's top command bar
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setToolTip(EXPLANATION_TIP)
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(20, 14, 20, 14)
        hl.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        star_ic = label()
        star_ic.setPixmap(icon("star", theme.CURRENT.accent_hover, 22).pixmap(22, 22))
        top.addWidget(star_ic, 0, Qt.AlignVCenter)
        title_lbl = label("OnlineFix Picks")
        title_lbl.setStyleSheet("font-size: 16pt; font-weight: 750;")
        top.addWidget(title_lbl, 0, Qt.AlignVCenter)
        sub_lbl = label("Curated carousels from online-fix.me", "muted")
        top.addWidget(sub_lbl, 1, Qt.AlignVCenter)
        self.updated = label("", "small")
        top.addWidget(self.updated, 0, Qt.AlignVCenter)
        top.addWidget(button("Refresh", "refresh", on_click=lambda: self.load(force=True)), 0, Qt.AlignVCenter)
        hl.addLayout(top)

        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(8)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tabs_box = QHBoxLayout()
        self.tabs_box.setSpacing(8)
        tabs_row.addLayout(self.tabs_box)
        tabs_row.addStretch(1)
        tabs_row.addWidget(label("Click another card to spotlight  ·  Click again or View Game to open", "small"))
        hl.addLayout(tabs_row)
        self.lay.addWidget(hero)

        self.banner = Banner("warning")
        self.banner.hide()
        self.lay.addWidget(self.banner)

        # Centered showcase stage (mirrors Home's centered rhythm)
        self.lay.addStretch(1)
        self.section_stack = QStackedWidget(self)
        self.lay.addWidget(self.section_stack)

        self.empty_state = EmptyState(
            "star",
            "No picks available",
            "The OnlineFix home page could not be read. Try again later.",
            self,
        )
        self.empty_state.hide()
        self.lay.addWidget(self.empty_state)
        self.lay.addStretch(1)

        scroll.setWidget(wrap)
        outer.addWidget(scroll)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self._loaded_once:
            self._loaded_once = True
            self.load()

    def load(self, force: bool = False) -> None:
        self.updated.setText("Loading…")
        self.bridge.call(self.bridge.core.featured(force), self._show, self._fail, owner=self)

    def _clear_stack(self) -> None:
        for b, _ic in self._tab_buttons:
            self.tab_group.removeButton(b)
        self._tab_buttons.clear()
        clear_now(self.tabs_box)
        while self.section_stack.count():
            w = self.section_stack.widget(0)
            self.section_stack.removeWidget(w)
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self.grids = []
        self._section_keys = []

    def _select_section(self, index: int) -> None:
        if not (0 <= index < len(self.grids)):
            return
        pal = theme.CURRENT
        self._active_key = self._section_keys[index]
        self.section_stack.setCurrentIndex(index)
        for i, (b, ic_name) in enumerate(self._tab_buttons):
            active = i == index
            b.setChecked(active)
            b.setIcon(icon(ic_name, "#ffffff" if active else pal.muted))
            if active:
                b.setStyleSheet(
                    f"QPushButton {{ background: {pal.accent}; border: 1px solid {pal.accent}; color: #ffffff; "
                    f"padding: 7px 18px; border-radius: 11px; font-size: 9.8pt; font-weight: 700; }}"
                    f"QPushButton:hover {{ background: {pal.accent_hover}; }}"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: {pal.bg2}; border: 1px solid {pal.border}; color: {pal.text}; "
                    f"padding: 7px 18px; border-radius: 11px; font-size: 9.8pt; font-weight: 650; }}"
                    f"QPushButton:hover {{ border-color: {pal.accent}; background: {pal.bg3}; }}"
                )

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
        self._clear_stack()
        if not sections:
            self.section_stack.hide()
            self.empty_state.show()
            return

        self.empty_state.hide()
        self.section_stack.show()
        target_idx = 0
        for i, s in enumerate(sections):
            ic_name = "star" if s.key == "popular" else "update"
            tab = QPushButton(f"  {s.title}  ·  {len(s.games)}")
            tab.setCheckable(True)
            tab.setCursor(Qt.PointingHandCursor)
            tab.setIconSize(QSize(16, 16))
            tab.clicked.connect(lambda _=False, idx=i: self._select_section(idx))
            self.tab_group.addButton(tab)
            self.tabs_box.addWidget(tab)
            self._tab_buttons.append((tab, ic_name))

            showcase = GameShowcase(
                self.actions,
                title_text=s.title,
                trailing_button=("Browse all", "grid", lambda: self.actions.navigate.emit("games")),
                parent=self.section_stack,
            )
            showcase.note_label.setText(
                f"From the “{s.source_label}” carousel on online-fix.me  ·  Auto-rotating"
            )
            showcase.set_card_size(self.card_size)
            showcase.set_installed(self.actions.installed_keys)
            showcase.set_groups([GameGroup(g.dedup_key, [g]) for g in s.games])
            showcase.activated_group.connect(self._open)
            self.section_stack.addWidget(showcase)
            self.grids.append(showcase)
            self._section_keys.append(s.key)
            if s.key == self._active_key:
                target_idx = i

        self._select_section(target_idx)

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

    def refresh_installed(self, *_args) -> None:
        for g in self.grids:
            g.set_installed(self.actions.installed_keys)

    def _open(self, group) -> None:
        from ezgd.ui.dialogs.game_details import GameDetailsDialog
        GameDetailsDialog(self.actions, group, self).exec()
