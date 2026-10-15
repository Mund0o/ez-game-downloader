"""Game details: metadata, source link, and every download method with what the app can do with it."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ezgd.catalog.models import Game, GameGroup, MethodKind
from ezgd.core.fsutil import format_bytes
from ezgd.ui import theme
from ezgd.sources.hosts import browser_url
from ezgd.ui.dialogs import browser
from ezgd.ui.dialogs.misc import open_url
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import Banner, PasswordBanner, button, chip, clear_layout, label, relative_time

KIND_ICON = {MethodKind.TORRENT: "magnet", MethodKind.DIRECT: "download", MethodKind.EXTERNAL: "external",
             MethodKind.MIRRORS: "link"}


_CLOSED_PHRASES = [  # phrases seen in OnlineFix closed-guide notices (2026-10-09)
    ("Руководство закрыто", "The guide is closed"),
    ("используйте версию от", "use the version from"),
    ("бесплатная игра по сети более невозможна", "free online play is no longer possible"),
    ("покупайте игру", "buy the game"),
    (" и ", " and "),
]


def translate_closed_notice(text: str) -> str:
    """English rendering of OnlineFix's closed-guide notice; generic if a phrase is unknown."""
    out = text
    for ru, en in _CLOSED_PHRASES:
        out = out.replace(ru, en)
    if any("Ѐ" <= ch <= "ӿ" for ch in out):       # something we could not translate
        return "The guide is closed."
    return out


class GameDetailsDialog(QDialog):
    def __init__(self, actions, group: GameGroup, parent=None, update: dict | None = None):
        super().__init__(parent)
        self.update_info = update            # opened from the Library's "Update": the download goes over the game
        self.actions = actions
        self.bridge = actions.bridge
        self.entries: list[Game] = list(group.entries)
        self.current: Game | None = None
        self.setWindowTitle(group.title)
        self.resize(980, 760)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setContentsMargins(20, 14, 20, 0)
        self.tabs_box = QHBoxLayout()
        self.tabs_box.setSpacing(6)
        top.addLayout(self.tabs_box)
        top.addStretch(1)
        self.refresh_btn = button("Refresh", "refresh", "ghost", on_click=lambda: self.load(self.current, True))
        top.addWidget(self.refresh_btn)
        outer.addLayout(top)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(20, 12, 20, 20)
        self.body_lay.setSpacing(14)
        scroll.setWidget(self.body)
        outer.addWidget(scroll, 1)
        self.actions.covers.ready.connect(self._cover_ready)
        self._cover_label: QLabel | None = None
        self._cover_url: str | None = None

        self._build_tabs()
        self.load(group.primary)
        # duplicates offered by other sources that are already in the local catalog
        self.bridge.call(self.bridge.core.related(group.primary.id), self._add_related, owner=self)

    # ---------------------------------------------------------------- tabs
    def _build_tabs(self) -> None:
        clear_layout(self.tabs_box)
        for b in self.tab_group.buttons():
            self.tab_group.removeButton(b)
        if len(self.entries) < 2:
            return
        self.tabs_box.addWidget(label("Available from:", "muted"))
        for g in self.entries:
            b = QPushButton(theme.source_name(g.source))
            b.setProperty("variant", "chip")
            b.setCheckable(True)
            b.setChecked(self.current is not None and g.id == self.current.id)
            b.clicked.connect(lambda _=False, g=g: self.load(g))
            self.tab_group.addButton(b)
            self.tabs_box.addWidget(b)

    def _add_related(self, related: list[Game]) -> None:
        known = {g.id for g in self.entries}
        new = [g for g in related if g.id not in known]
        if new:
            self.entries += new
            self._build_tabs()

    # ---------------------------------------------------------------- load
    def load(self, game: Game | None, force: bool = False) -> None:
        if game is None:
            return
        self.current = game
        for b, g in zip(self.tab_group.buttons(), self.entries):
            b.setChecked(g.id == game.id)
        clear_layout(self.body_lay)
        self._render_header(game, loading=True)
        self.loading = label("Loading the game page…", "muted")
        self.body_lay.addWidget(self.loading)
        self.body_lay.addStretch(1)
        self.bridge.call(self.bridge.core.details(game.id, force), self._loaded, self._failed, owner=self)

    def _loaded(self, game: Game) -> None:
        if self.current is None or game.id != self.current.id:
            return
        self.current = game
        idx = next((i for i, g in enumerate(self.entries) if g.id == game.id), None)
        if idx is not None:
            self.entries[idx] = game
        clear_layout(self.body_lay)
        self._render_header(game, loading=False)
        if game.withdrawn:
            notice = game.extra.get("closed_notice") or "Руководство закрыто."
            english = translate_closed_notice(notice)
            self.body_lay.addWidget(Banner(
                "error", f"{theme.source_name(game.source)} has closed this guide, so the app does not offer it for "
                         f"download and has removed it from your lists. The site says: “{notice}”"
                         + (f" — “{english}”" if english != notice else "")))
        else:
            if self.update_info:
                u = self.update_info
                ver = f" from {u.get('installed')} to {u.get('latest')}" if u.get("installed") and u.get("latest") \
                    else ""
                self.body_lay.addWidget(Banner(
                    "success", f"Updating {u.get('title') or game.title}{ver}. Download it below (or in your browser "
                               "and import the file): it is extracted over your installed game"
                               + (f" in {u['dest']}" if u.get("dest") else "") + ". Your saves are backed up first; "
                               "settings, fixes and the Proton prefix stay as they are."))
            if game.extra.get("warning"):
                self.body_lay.addWidget(Banner("warning", game.extra["warning"]))
            if game.extra.get("emulated"):
                self.body_lay.addWidget(Banner("info", f"This repack is the {game.extra['emulated']} version of the "
                                                       "game and comes with a console emulator to play it."))
            if game.extra.get("installer"):
                from ezgd.sources.fitgirl import INSTALL_NOTE
                self.body_lay.addWidget(Banner("info", INSTALL_NOTE))
            if game.extra.get("steam_base_game"):
                self._steam_base_holder = QVBoxLayout()
                self.body_lay.addLayout(self._steam_base_holder)
                self.bridge.call(self.bridge.core.steam_base_info(game.id),
                                 lambda info, gid=game.id: self._steam_base(gid, info), owner=self)
            self._render_methods(game)
        self.body_lay.addStretch(1)

    def _password(self, g: Game) -> str | None:
        return g.extra.get("archive_password") or theme.source_password(g.source)

    def _download_in_app(self, g: Game, m, target: str) -> None:
        browser.open_browser_download(self.actions, g, target, m.label, self)
        if target != m.url:
            self.actions.toasts.show(f"Click “{m.label}” on the page",
                                     f"{m.label} only accepts visits that come from {theme.source_name(g.source)}'s "
                                     "game page.", "info")

    def _open_via(self, g: Game, m, target: str) -> None:
        self._open_page(g, target)
        if target != m.url:
            self.actions.toasts.show(f"Click “{m.label}” on the page",
                                     f"{m.label} only accepts visits that come from {theme.source_name(g.source)}'s "
                                     "game page.", "info")

    def _open_page(self, g: Game, url: str) -> None:
        open_url(url)
        pw = self._password(g)
        if pw:
            self.actions.toasts.show(f"🔒 Archive password: {pw}",
                                     f"Every {theme.source_name(g.source)} archive needs it when you unpack it.",
                                     "warning")

    def _failed(self, err: BaseException) -> None:
        try:
            self.loading.hide()
        except RuntimeError:          # replaced by a newer load
            return
        b = Banner("error", f"Could not load the game page: {err}")
        b.add_action("Retry", lambda: self.load(self.current, True), "retry")
        b.add_action("Report", lambda: self.actions.report(self, self.current.source, self.current.url, str(err)),
                     "bug")
        self.body_lay.insertWidget(self.body_lay.count() - 1, b)

    # -------------------------------------------------------------- render
    def _render_header(self, g: Game, loading: bool) -> None:
        row = QHBoxLayout()
        row.setSpacing(18)
        cover = QLabel()
        cover.setFixedSize(360, 203)
        cover.setStyleSheet(f"background: {theme.CURRENT.bg3}; border-radius: 12px;")
        cover.setAlignment(Qt.AlignCenter)
        self._cover_label, self._cover_url = cover, g.cover_url
        self._set_cover()
        row.addWidget(cover, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(8)
        t = label(g.title, "title", wrap=True)
        col.addWidget(t)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(chip(theme.source_name(g.source), theme.SOURCE_COLORS.get(g.source, theme.CURRENT.accent)))
        for gen in g.genres[:4]:
            chips.addWidget(chip(gen, theme.CURRENT.muted))
        if g.dedup_key in self.actions.installed_keys:
            chips.addWidget(chip("In library", theme.CURRENT.success))
        chips.addStretch(1)
        col.addLayout(chips)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(5)

        def add(name, value):
            if value:
                v = label(str(value), wrap=True, selectable=True)
                form.addRow(label(name, "muted"), v)
        add("Version", g.version)
        add("Release date", g.release_date)
        if g.size_text:
            add("Size", f"{g.size_text} (as stated by the source)")
        from ezgd.core.fsutil import install_estimate
        est = install_estimate(g, g.size_bytes)
        if g.size_bytes and est["install_size"]:
            approx = "about " if est["install_size_guessed"] else ""
            add("Space needed", f"{approx}{format_bytes(g.size_bytes + est['install_size'])} "
                                "(download + installed game)")
        add("Platform", ", ".join(g.platforms))
        add("Developer", g.extra.get("developer"))
        add("Released by", g.extra.get("released_by") or g.extra.get("release_group"))
        add("Uploaded", g.extra.get("uploaded"))
        add("Repack", g.extra.get("repack_by"))
        add("Languages", g.extra.get("languages"))
        add("After install", g.extra.get("installed_size") or g.extra.get("original_size"))
        add("Install time", g.extra.get("install_time"))
        add("Multiplayer", g.extra.get("multiplayer"))
        add("Play via", g.extra.get("network"))
        modes = []
        if g.extra.get("coop_players"):
            modes.append(f"co-op up to {g.extra['coop_players']}")
        if g.extra.get("multiplayer_players"):
            modes.append(f"multiplayer up to {g.extra['multiplayer_players']}")
        if not modes and g.extra.get("modes"):
            modes = g.extra["modes"]
        add("Modes", ", ".join(modes))
        if g.extra.get("official_servers"):
            add("Servers", "Plays on official servers")
        add("Steam App ID", g.steam_app_id)
        add("Page checked", relative_time(g.details_checked_at) if g.details_checked_at else None)
        self._linux = label("", wrap=True, selectable=True)
        self._linux_row = label("Runs on Linux", "muted")
        form.addRow(self._linux_row, self._linux)
        self._linux_row.hide()
        self._linux.hide()
        self._linux_info: dict = {}
        self.bridge.call(self.bridge.core.linux_ratings([(g.steam_app_id, g.title)]),
                         lambda r: self._show_linux(rating=r[0] if r else None), lambda _e: None, owner=self)
        if g.steam_app_id:
            self.bridge.call(self.bridge.core.protondb_summary(g.steam_app_id),
                             lambda t: self._show_linux(tier=t), lambda _e: None, owner=self)
        col.addLayout(form)
        btns = QHBoxLayout()
        btns.addWidget(button(f"Open on {theme.source_name(g.source)}", "external",
                              on_click=lambda: self._open_page(g, g.url)))
        btns.addWidget(button("Report a problem", "bug", "ghost",
                              on_click=lambda: self.actions.report(self, g.source, g.url, "")))
        self._wish_btn = button("  Wishlist", "heart", "ghost",
                                tooltip="Keep track of this game: the app tells you when a source has it (or a newer "
                                        "version)", on_click=lambda: self._toggle_wishlist(g))
        btns.addWidget(self._wish_btn)
        self.bridge.call(self.bridge.core.wishlist_item_for(g.id), lambda it: self._wish_state(it is not None),
                         lambda _e: None, owner=self)
        btns.addStretch(1)
        col.addLayout(btns)
        col.addStretch(1)
        row.addLayout(col, 1)
        self.body_lay.addLayout(row)
        if not loading and not g.withdrawn and self._password(g):
            browser = not any(m.supported for m in (g.download_methods or []))
            self.body_lay.addWidget(PasswordBanner(g.source, self._password(g), browser=browser))
        if g.description and not loading:
            d = label(g.description, wrap=True, selectable=True)
            d.setStyleSheet(f"color: {theme.CURRENT.text}; line-height: 140%;")
            self.body_lay.addWidget(d)

    def _show_linux(self, rating: dict | None = None, tier: dict | None = None) -> None:
        """How well it runs under Proton: the players' reports (offline index) and ProtonDB's own tier."""
        from ezgd.ui.ratings import LEVEL_COLORS
        if rating is not None:
            self._linux_info["rating"] = rating
        if tier and tier.get("tier"):
            self._linux_info["tier"] = tier
        parts = []
        r, t = self._linux_info.get("rating"), self._linux_info.get("tier")
        if r:
            parts.append(f"<span style='color:{LEVEL_COLORS[r['level']]}'>●</span> {r['text']}")
        if t:
            parts.append(f"ProtonDB: <b>{str(t['tier']).capitalize()}</b>"
                         + (f" ({t['total']} reports)" if t.get("total") else ""))
        try:
            if parts:
                self._linux.setText(" · ".join(parts))
                self._linux.show()
                self._linux_row.show()
        except RuntimeError:
            pass

    def _wish_state(self, on: bool) -> None:
        from ezgd.ui.icons import icon
        try:
            self._wish_btn.setText("  On your wishlist" if on else "  Wishlist")
            self._wish_btn.setIcon(icon("heart-filled" if on else "heart",
                                        theme.CURRENT.danger if on else theme.CURRENT.text))
        except RuntimeError:
            pass

    def _toggle_wishlist(self, g: Game) -> None:
        def done(on: bool) -> None:
            self._wish_state(on)
            self.actions.toasts.show("Added to your wishlist" if on else "Removed from your wishlist", g.title,
                                     "success" if on else "info")
        self.bridge.call(self.bridge.core.wishlist_toggle(g.id), done, lambda e: self.actions.fail("Wishlist", e),
                         owner=self)

    def _set_cover(self) -> None:
        if self._cover_label is None:
            return
        pm = self.actions.covers.get(self._cover_url)
        if pm is not None and not pm.isNull():
            self._cover_label.setPixmap(pm.scaled(QSize(360, 203), Qt.KeepAspectRatioByExpanding,
                                                  Qt.SmoothTransformation).copy(0, 0, 360, 203))
        else:
            self._cover_label.setText("No cover" if not self._cover_url else "")

    def _cover_ready(self, url: str) -> None:
        if url == self._cover_url and self._cover_label is not None:
            try:
                self._set_cover()
            except RuntimeError:
                pass

    def _render_methods(self, g: Game) -> None:
        self.body_lay.addWidget(label(f"Download options — from {theme.source_name(g.source)}", "h2"))
        methods = g.download_methods or []
        if not methods:
            self.body_lay.addWidget(Banner("warning", "No download links were found on this game page. The page may "
                                                      "require signing in on the website, or its layout changed."))
            return
        supported = [m for m in methods if m.supported]
        if not supported:
            self.body_lay.addWidget(Banner("info", "None of this game's links can be downloaded inside the app — "
                                                   "they need a browser (captcha, login, signed form or Cloudflare "
                                                   "check). Download it in your browser, then bring the file back "
                                                   "with “Import downloaded file” below."))
        for card in self._part_sets(g, methods):
            self.body_lay.addWidget(card)
        grouped = self._browser_part_groups(methods)        # dozens of part links → one card per host
        done: set[int] = set()
        for i, m in enumerate(methods):
            if i in done:
                continue
            group = next((idx for idx in grouped if i in idx), None)
            if group:
                done.update(group)
                self.body_lay.addWidget(self._browser_parts_card(g, group, methods))
            else:
                self.body_lay.addWidget(self._method_row(g, i, m))
        if not supported:
            self.body_lay.addWidget(self._import_row(g))

    def _part_sets(self, g: Game, methods) -> list[QFrame]:
        """Multi-part archives: one 'download all parts' card per host (in-app) and a note for browser hosts."""
        from ezgd.installation.volumes import volume_info
        groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for i, m in enumerate(methods):
            name = m.file_name or m.label.split("—")[-1].strip()
            v = volume_info(name)
            if v is not None:
                groups.setdefault((m.label.split("—")[0].strip(), v.set_key), []).append((v.index, i))
        cards, browser_note = [], False
        for (host, _key), parts in groups.items():
            if len(parts) < 2:
                continue
            parts.sort()
            idx = [i for _, i in parts]
            if all(methods[i].supported for i in idx):
                f = QFrame()
                f.setProperty("card", True)
                lay = QHBoxLayout(f)
                lay.setContentsMargins(14, 10, 12, 10)
                col = QVBoxLayout()
                head = label(f"{host}: this game comes in {len(idx)} parts", wrap=True)
                head.setStyleSheet("font-weight: 650;")
                col.addWidget(head)
                total = sum(methods[i].size_bytes or 0 for i in idx)
                col.addWidget(label("Downloads every part in the app" + (f" ({format_bytes(total)} in total)"
                                                                          if total else "")
                                    + "; the game is extracted once the last part is in.", "small", wrap=True))
                lay.addLayout(col, 1)
                lay.addWidget(button(f"Download all {len(idx)} parts", "download", "primary",
                                     on_click=lambda _=False, idx=idx, host=host: self.actions.start_download_set(
                                         self, g.id, idx, f"{g.title} ({host})")))
                cards.append(f)
            else:
                browser_note = True
        if browser_note:
            cards.append(Banner("info", "Some links below are single parts of a multi-part archive. Download every "
                                        "part (part 1, part 2, …) — in any order — and the app collects them into "
                                        "one game and extracts it when the last part is in."))
        return cards

    def _steam_base(self, game_id: str, info: dict | None) -> None:
        """Fix-only pages: the game itself is free on Steam; the fix goes into its folder."""
        if not info or self.current is None or self.current.id != game_id:
            return
        try:
            holder = self._steam_base_holder
        except (AttributeError, RuntimeError):
            return
        if info["installed"]:
            b = Banner("success", f"Only the fix is offered here — the game is “{info['name']}” on Steam, and it is "
                                  f"installed ({info['folder']}). Download the fix: the app puts it into that folder "
                                  "(the files it replaces are backed up).")
        else:
            b = Banner("info", f"Only the fix is offered here: the game itself is “{info['name']}”, free on Steam. "
                               "1. Install it in Steam. 2. Download the fix below — when it finishes, the app puts it "
                               "into the Steam game's folder (the files it replaces are backed up). Then press Play "
                               "here, with Steam running.")

            def install() -> None:
                def done(r: dict) -> None:
                    if r.get("opened") == "store" and r.get("url"):
                        open_url(r["url"])
                    self.actions.toasts.show("Install it in Steam", f"Press “Install Game” for {r['name']} in Steam, "
                                             "then download the fix here.", "info")
                self.bridge.call(self.bridge.core.install_steam_base(game_id), done,
                                 lambda e: self.actions.fail("Steam", e))
            b.add_action("Install in Steam", install, "steam")
        try:
            holder.addWidget(b)
        except RuntimeError:
            pass

    @staticmethod
    def _browser_part_groups(methods) -> list[list[int]]:
        from ezgd.installation.volumes import volume_info
        groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for i, m in enumerate(methods):
            if m.supported:
                continue
            v = volume_info(m.file_name or m.label.split("—")[-1].strip())
            if v is not None:
                groups.setdefault((m.label.split("—")[0].strip(), v.set_key), []).append((v.index, i))
        return [[i for _, i in sorted(parts)] for parts in groups.values() if len(parts) >= 2]

    def _browser_parts_card(self, g: Game, idx: list[int], methods) -> QFrame:
        first = methods[idx[0]]
        host = first.label.split("—")[0].strip()
        f = QFrame()
        f.setProperty("card", True)
        lay = QHBoxLayout(f)
        lay.setContentsMargins(14, 10, 12, 10)
        ic = QLabel()
        ic.setPixmap(icon("external", theme.CURRENT.muted, 22).pixmap(22, 22))
        lay.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        head = QHBoxLayout()
        name = label(f"{host} — {len(idx)} parts", wrap=True)
        name.setStyleSheet("font-weight: 650;")
        head.addWidget(name, 1)
        head.addWidget(chip("Browser only", theme.CURRENT.muted))
        col.addLayout(head)
        if first.note:
            col.addWidget(label(first.note, "small", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for n, i in enumerate(idx):
            m = methods[i]
            target = browser_url(m, g.url)
            b = button(f"Part {n + 1}", "download" if browser.AVAILABLE else "external", "ghost",
                       tooltip=(m.file_name or m.url) + ("\nOpens in the in-app browser; the file joins this game in "
                                                         "your Library." if browser.AVAILABLE else ""),
                       on_click=lambda _=False, m=m, t=target: (self._download_in_app(g, m, t) if browser.AVAILABLE
                                                                else self._open_via(g, m, t)))
            grid.addWidget(b, n // 8, n % 8)
        col.addLayout(grid)
        lay.addLayout(col, 1)
        return f

    def _import_row(self, g: Game) -> QFrame:
        f = QFrame()
        f.setProperty("card", True)
        lay = QHBoxLayout(f)
        lay.setContentsMargins(14, 10, 12, 10)
        ic = QLabel()
        ic.setPixmap(icon("download", theme.CURRENT.accent, 22).pixmap(22, 22))
        lay.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        name = label("Already downloaded it in your browser?", wrap=True)
        name.setStyleSheet("font-weight: 650;")
        col.addWidget(name)
        sha = g.extra.get("sha256")
        text = ("Pick the file: the app adds it to your Library linked to this page"
                + (", checks it against the SHA-256 the site publishes" if sha else "")
                + (f" and extracts it with the password “{self._password(g)}”" if self._password(g)
                   else " and extracts it") + ". Nothing is run.")
        col.addWidget(label(text, "small", wrap=True))
        if sha:
            s = label(f"Published SHA-256: {sha}" + (f"   CRC32: {g.extra['crc32']}" if g.extra.get("crc32") else ""),
                      "small", wrap=True, selectable=True)
            s.setStyleSheet("font-family: monospace;")
            col.addWidget(s)
        lay.addLayout(col, 1)
        lay.addWidget(button("Import downloaded file…", "folder", "primary",
                             on_click=lambda: self.actions.import_downloaded(self, g)))
        return f

    def _method_row(self, g: Game, index: int, m) -> QFrame:
        f = QFrame()
        f.setProperty("card", True)
        lay = QHBoxLayout(f)
        lay.setContentsMargins(14, 10, 12, 10)
        ic = QLabel()
        ic.setPixmap(icon(KIND_ICON.get(m.kind, "link"),
                          theme.CURRENT.accent if m.supported else theme.CURRENT.muted, 22).pixmap(22, 22))
        lay.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(6)
        name = label(m.label, wrap=True)
        name.setStyleSheet("font-weight: 650;")
        name.setMinimumWidth(120)
        head.addWidget(name, 1)
        kind_text = {MethodKind.TORRENT: "Torrent · in-app", MethodKind.DIRECT: "Direct · in-app",
                     MethodKind.EXTERNAL: "Browser only", MethodKind.MIRRORS: "Mirror list"}[m.kind]
        head.addWidget(chip(kind_text, theme.CURRENT.success if m.supported else theme.CURRENT.muted))
        if m.flagged_dangerous:
            head.addWidget(chip("Source warns about deceptive ads", theme.CURRENT.warning))
        if m.size_bytes:
            head.addWidget(label(format_bytes(m.size_bytes), "small"))
        col.addLayout(head)
        if m.note:
            col.addWidget(label(m.note, "small", wrap=True))
        lay.addLayout(col, 1)
        if m.supported and m.kind in (MethodKind.TORRENT, MethodKind.DIRECT):
            lay.addWidget(button("Download", "download", "primary",
                                 on_click=lambda: self.actions.start_download(
                                     self, g.id, index, on_manual=lambda: self.load(self.current))))
        else:
            target = browser_url(m, g.url)
            via_page = target != m.url
            if via_page and not (m.note or "").startswith(m.label + " only opens"):
                col.addWidget(label(f"{m.label} only opens when you click it on the {theme.source_name(g.source)} "
                                    f"page, so this opens that page — click “{m.label}” there.", "small", wrap=True))
            if browser.AVAILABLE:
                lay.addWidget(button("Download in app", "download", "primary",
                                     tooltip="Opens the page in a browser window inside the app: do any check "
                                             "yourself and click the site's download button — the file is then "
                                             "added to your Library.",
                                     on_click=lambda: self._download_in_app(g, m, target)))
            lay.addWidget(button(f"Open on {theme.source_name(g.source)}" if via_page else "Open in browser",
                                 "external", "ghost" if browser.AVAILABLE else None, tooltip=f"Opens {target}",
                                 on_click=lambda: self._open_via(g, m, target)))
        lay.addWidget(button("", "link", "ghost", tooltip="Copy link",
                             on_click=lambda: QGuiApplication.clipboard().setText(m.url)))
        return f
