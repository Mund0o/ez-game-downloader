"""Wishlist: games you want, found on Steam's store and tracked on every source until one offers them."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from ezgd.catalog.models import GameGroup
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_url
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import (EmptyState, FlowLayout, button, chip, clear_layout, label, relative_time,
                                    source_chip)

STATUS = {
    "available": ("Available", "success"),
    "library": ("In your library", "info"),
    "closed": ("Guide closed", "warning"),
    "waiting": ("Not available yet", "muted"),
}
FILTERS = [("all", "All"), ("available", "Available"), ("waiting", "Waiting"), ("library", "In library")]
COVER_W, COVER_H = 104, 156


def rounded(pm: QPixmap, w: int, h: int, radius: int = 10) -> QPixmap:
    """``pm`` scaled to fill w×h, with rounded corners."""
    out = QPixmap(w * 2, h * 2)
    out.setDevicePixelRatio(2)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    p.setClipPath(path)
    scaled = pm.scaled(QSize(w * 2, h * 2), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    scaled.setDevicePixelRatio(2)
    p.drawPixmap(int((w - scaled.width() / 2) / 2), int((h - scaled.height() / 2) / 2), scaled)
    p.end()
    return out


def placeholder(title: str, w: int, h: int) -> QPixmap:
    out = QPixmap(w * 2, h * 2)
    out.setDevicePixelRatio(2)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, 10, 10)
    p.fillPath(path, QColor(theme.CURRENT.bg3))
    p.setPen(QColor(255, 255, 255, 80))
    f = p.font()
    f.setPointSize(18)
    f.setBold(True)
    p.setFont(f)
    p.drawText(0, 0, w, h, Qt.AlignCenter, "".join(x[0] for x in title.split()[:2] if x).upper() or "?")
    p.end()
    return out


class WishlistPage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.items: list[dict] = []
        self.kind = "all"
        self._covers: dict[str, QLabel] = {}
        actions.covers.ready.connect(self._cover_ready)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(28, 24, 28, 28)
        self.lay.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label("Wishlist", "title"))
        self.summary = label("Games you want. The app keeps looking for them on every source and tells you when "
                             "one has them.", "muted", wrap=True)
        col.addWidget(self.summary)
        head.addLayout(col, 1)
        self.check_btn = button("Check now", "refresh", tooltip="Search every source for your wishlist now",
                                on_click=self.check_now)
        head.addWidget(self.check_btn, 0, Qt.AlignTop)
        self.lay.addLayout(head)

        # add a game: Steam's store knows every game, even ones no site has yet
        add, al = self._card()
        al.addWidget(label("Add a game", "h2"))
        al.addWidget(label("Search Steam's store — every game has one Steam ID, whatever the download sites call "
                           "it, so the app recognises it on every source.", "small", wrap=True))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Game name, e.g. Hollow Knight Silksong")
        self.search.addAction(icon("search", theme.CURRENT.muted), QLineEdit.LeadingPosition)
        self.search.returnPressed.connect(self._search_now)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        row.addWidget(self.search, 1)
        al.addLayout(row)
        self.results_box = QVBoxLayout()
        self.results_box.setSpacing(6)
        al.addLayout(self.results_box)
        self.lay.addWidget(add)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(500)
        self._debounce.timeout.connect(self._search_now)
        self._search_gen = 0

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_buttons: dict[str, QPushButton] = {}
        for key, text in FILTERS:
            b = QPushButton(text)
            b.setProperty("variant", "chip")
            b.setCheckable(True)
            b.setChecked(key == "all")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self.filter_group.addButton(b)
            self.filter_buttons[key] = b
            chips.addWidget(b)
        chips.addStretch(1)
        self.lay.addLayout(chips)

        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(10)
        self.lay.addLayout(self.list_box)
        self.lay.addStretch(1)

    @staticmethod
    def _card() -> tuple[QFrame, QVBoxLayout]:
        f = QFrame()
        f.setObjectName("card")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        return f, lay

    # ----------------------------------------------------------------- data
    def load(self) -> None:
        self.bridge.call(self.bridge.core.wishlist(), self._loaded, owner=self)

    def _loaded(self, items: list[dict]) -> None:
        self.items = items
        n_av = sum(1 for i in items if i["status"] == "available")
        if items:
            self.summary.setText(f"{len(items)} game{'s' if len(items) != 1 else ''} · {n_av} available now · "
                                 "checked every few hours on every source that allows it")
        for key, b in self.filter_buttons.items():
            n = len(items) if key == "all" else sum(1 for i in items if i["status"] == key or
                                                     (key == "waiting" and i["status"] == "closed"))
            b.setText(f"{dict(FILTERS)[key]} ({n})" if items else dict(FILTERS)[key])
        self._render()

    def _set_filter(self, key: str) -> None:
        self.kind = key
        self._render()

    def _render(self) -> None:
        clear_layout(self.list_box)
        self._covers = {}
        shown = [i for i in self.items if self.kind == "all" or i["status"] == self.kind
                 or (self.kind == "waiting" and i["status"] == "closed")]
        order = {"available": 0, "waiting": 1, "closed": 2, "library": 3}
        shown.sort(key=lambda i: (order.get(i["status"], 9), -(i.get("added_at") or 0)))
        if not self.items:
            e = EmptyState("heart", "Your wishlist is empty",
                           "Search for a game above — even one that is not out yet, or one no site has. The app looks "
                           "for it on every source and lets you know when it can be downloaded. You can also press "
                           "♡ Wishlist on any game's page.")
            e.text.setMinimumHeight(e.text.heightForWidth(520) + 8)      # wrapped text: room for every line
            e.setMinimumHeight(300)
            self.list_box.addWidget(e)
            return
        if not shown:
            self.list_box.addWidget(label("Nothing here with this filter.", "muted"))
            return
        for it in shown:
            self.list_box.addWidget(self._row(it))

    def _row(self, it: dict) -> QFrame:
        f, outer = self._card()
        lay = QHBoxLayout()
        lay.setSpacing(16)
        outer.addLayout(lay)
        cover = QLabel()
        cover.setFixedSize(COVER_W, COVER_H)
        self._set_cover(cover, it)
        lay.addWidget(cover, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        t = label(it["title"], "h2", wrap=True)
        col.addWidget(t)
        chips = FlowLayout(spacing=6)
        text, kind = STATUS.get(it["status"], ("Unknown", "muted"))
        color = {"success": theme.CURRENT.success, "info": theme.CURRENT.info, "warning": theme.CURRENT.warning,
                 "muted": theme.CURRENT.muted}[kind]
        chips.addWidget(chip(("● " if it["status"] == "available" else "") + text, color))
        if it.get("coming_soon"):
            chips.addWidget(chip("Coming soon", theme.CURRENT.accent))
        if it.get("release_text") and not it.get("coming_soon"):
            chips.addWidget(chip(it["release_text"], theme.CURRENT.muted))
        elif it.get("release_text") and it["release_text"] != "Coming soon":
            chips.addWidget(chip(it["release_text"], theme.CURRENT.muted))
        col.addLayout(chips)
        live = [m for m in it["matches"] if not m["withdrawn"]]
        closed = [m for m in it["matches"] if m["withdrawn"]]
        if live:
            src = FlowLayout(spacing=6)
            src.addWidget(label("On:", "small"))
            for m in live:
                c = source_chip(m["source"])
                if m.get("version"):
                    c.setText(f"{theme.source_name(m['source'])} · {m['version']}")
                c.setToolTip(m["title"])
                src.addWidget(c)
            col.addLayout(src)
        if it["status"] == "closed" and closed:
            col.addWidget(label(f"{', '.join(sorted({theme.source_name(m['source']) for m in closed}))} closed its "
                                "guide for this game, so it is not offered. The app keeps watching other sources.",
                                "small", wrap=True))
        elif it["status"] == "waiting":
            col.addWidget(label("No source has it yet." + (" It is not released yet." if it.get("coming_soon")
                                                          else "") + " You get a notification when one does.",
                                "small", wrap=True))
        elif it["status"] == "library":
            col.addWidget(label("You already have it — remove it from the wishlist, or keep it to hear about other "
                                "sources.", "small", wrap=True))
        checked = it.get("checked_at")
        col.addWidget(label(f"Last checked {relative_time(checked)}" if checked else "Not checked on the sites yet",
                            "small"))
        col.addStretch(1)
        lay.addLayout(col, 1)
        acts = QVBoxLayout()
        acts.setSpacing(6)
        if live:
            b = button("  Get it", "download", "primary", tooltip="Open the game page with its download options",
                       on_click=lambda it=it: self._open(it))
            b.setMinimumHeight(38)
            acts.addWidget(b)
        if it.get("steam_app_id"):
            from ezgd.catalog.steamstore import store_url
            acts.addWidget(button("Steam page", "external", "ghost",
                                  on_click=lambda u=store_url(it["steam_app_id"]): open_url(u)))
        acts.addWidget(button("Check", "refresh", "ghost", tooltip="Search every source for this game now",
                              on_click=lambda it=it: self._check_one(it)))
        acts.addWidget(button("Remove", "heart-filled", "ghost", tooltip="Remove from the wishlist",
                              on_click=lambda it=it: self._remove(it)))
        acts.addStretch(1)
        lay.addLayout(acts)
        if it["status"] == "available":
            f.setStyleSheet(f"QFrame#card {{ border-color: {theme.CURRENT.success}; }}")
        return f

    def _set_cover(self, lab: QLabel, it: dict) -> None:
        url = it.get("cover_url") or next((m.get("cover_url") for m in it["matches"] if m.get("cover_url")), None)
        pm = self.actions.covers.get(url) if url else None
        if pm is not None and not pm.isNull():
            lab.setPixmap(rounded(pm, COVER_W, COVER_H))
        else:
            lab.setPixmap(placeholder(it["title"], COVER_W, COVER_H))
            if url:
                self._covers[url] = lab
                lab.setProperty("item", it["id"])

    def _cover_ready(self, url: str) -> None:
        lab = self._covers.get(url)
        if lab is None:
            return
        try:
            iid = lab.property("item")
            if iid is None:                                     # a search result's small image
                pm = self.actions.covers.get(url)
                if pm is not None and not pm.isNull():
                    lab.setPixmap(rounded(pm, lab.width(), lab.height(), 6))
                return
            it = next((i for i in self.items if i["id"] == iid), None)
            if it is not None:
                self._set_cover(lab, it)
        except RuntimeError:                                    # the row was rebuilt meanwhile
            self._covers.pop(url, None)

    # -------------------------------------------------------------- actions
    def _open(self, it: dict) -> None:
        ids = [m["game_id"] for m in it["matches"] if not m["withdrawn"]]

        def got(games) -> None:
            if not games:
                self.actions.toasts.show("Not in the catalog any more", "Press Check to look again.", "warning")
                return
            from ezgd.ui.dialogs.game_details import GameDetailsDialog
            GameDetailsDialog(self.actions, GameGroup(games[0].dedup_key, games), self).exec()
        self.bridge.call(self.bridge.core.games_by_ids(ids), got, owner=self)

    def _remove(self, it: dict) -> None:
        self.bridge.call(self.bridge.core.wishlist_remove(it["id"]),
                         lambda _r: (self.actions.toasts.show("Removed from your wishlist", it["title"]), self.load()),
                         lambda e: self.actions.fail("Wishlist", e), owner=self)

    def _check_one(self, it: dict) -> None:
        self.actions.toasts.show("Checking…", f"Looking for {it['title']} on every source.")
        self.bridge.call(self.bridge.core.wishlist_check(it["id"], True), lambda _r: self.load(),
                         lambda e: self.actions.fail("Wishlist", e), owner=self)

    def check_now(self) -> None:
        if not self.items:
            return
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Checking…")

        def done(res) -> None:
            self.check_btn.setEnabled(True)
            self.check_btn.setText("Check now")
            n = sum(1 for r in res if r["status"] == "available")
            self.actions.toasts.show("Wishlist checked", f"{n} of {len(res)} game(s) can be downloaded now.",
                                     "success" if n else "info")
            self.load()

        def failed(e) -> None:
            self.check_btn.setEnabled(True)
            self.check_btn.setText("Check now")
            self.actions.fail("Wishlist", e)
        self.bridge.call(self.bridge.core.wishlist_check(None, True), done, failed, owner=self)

    # --------------------------------------------------------------- search
    def _search_now(self) -> None:
        term = self.search.text().strip()
        self._search_gen += 1
        gen = self._search_gen
        if len(term) < 2:
            clear_layout(self.results_box)
            return
        clear_layout(self.results_box)
        self.results_box.addWidget(label("Searching Steam…", "small"))
        self.bridge.call(self.bridge.core.steam_search(term),
                         lambda res: gen == self._search_gen and self._results(res),
                         lambda e: gen == self._search_gen and self._search_failed(e), owner=self)

    def _search_failed(self, err) -> None:
        clear_layout(self.results_box)
        self.results_box.addWidget(label(f"Steam's search did not answer ({err}). You can still add the game by "
                                         "name:", "small", wrap=True))
        self.results_box.addWidget(button(f"Add “{self.search.text().strip()}”", "heart", on_click=lambda: self._add(
            None, self.search.text().strip())), 0, Qt.AlignLeft)

    def _results(self, res: list[dict]) -> None:
        clear_layout(self.results_box)
        if not res:
            self.results_box.addWidget(label("Steam knows no game by that name. Add it by name anyway:", "small"))
            self.results_box.addWidget(button(f"Add “{self.search.text().strip()}”", "heart",
                                              on_click=lambda: self._add(None, self.search.text().strip())),
                                       0, Qt.AlignLeft)
            return
        for r in res[:8]:
            row = QHBoxLayout()
            row.setSpacing(10)
            img = QLabel()
            img.setFixedSize(92, 34)
            img.setStyleSheet(f"background: {theme.CURRENT.bg3}; border-radius: 6px;")
            if r.get("image"):
                pm = self.actions.covers.get(r["image"])
                if pm is not None and not pm.isNull():
                    img.setPixmap(rounded(pm, 92, 34, 6))
                else:
                    self._covers[r["image"]] = img
                    img.setProperty("item", None)
            row.addWidget(img)
            name = label(r["name"])
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            name.setStyleSheet("font-weight: 600;")
            row.addWidget(name, 1)
            if r.get("price") is not None:
                row.addWidget(label(f"{r['price']:.2f} {r.get('currency') or ''}".strip(), "small"))
            if r.get("wishlisted"):
                row.addWidget(chip("On your wishlist", theme.CURRENT.success))
            else:
                row.addWidget(button("Add", "heart", "primary",
                                     on_click=lambda r=r: self._add(r["appid"], r["name"])))
            self.results_box.addLayout(row)

    def _add(self, appid: int | None, name: str) -> None:
        if not name:
            return

        def done(it: dict) -> None:
            self.actions.toasts.show("Added to your wishlist", f"{it['title']} — the app now looks for it on every "
                                                               "source.", "success")
            self.search.clear()
            clear_layout(self.results_box)
            self.load()
        self.bridge.call(self.bridge.core.wishlist_add(appid, name), done, lambda e: self.actions.fail("Wishlist", e),
                         owner=self)
