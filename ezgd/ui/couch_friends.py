"""Steam friends in couch mode and the quick menu: who is online and what they play, and what can be done with a
friend — chat, join their game, start the game they play, their profile (Steam's own links; see
platform.steamfriends). Shared by the quick menu's Friends view and couch mode's Friends tab."""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen

from ezgd.ui import theme
from ezgd.ui.couch_pages import Page

log = logging.getLogger(__name__)

REFRESH_MS = 30_000                   # while a friends view is open, the list is asked for again this often
STEAM_GREEN, STEAM_BLUE, GREY = "#90ba3c", "#57cbde", "#8a8f98"     # Steam's own in-game / online colours


def state_color(f: dict) -> QColor:
    if f.get("playing") and f.get("state") != "offline":
        return QColor(STEAM_GREEN)
    if f.get("state") in ("online", "busy"):
        return QColor(STEAM_BLUE)
    if f.get("state") in ("away", "snooze"):
        c = QColor(STEAM_BLUE)
        c.setAlpha(140)
        return c
    return QColor(GREY)


def summary(data: dict | None) -> str:
    """"5 online · 2 playing" (the quick menu's Friends row)."""
    if not data:
        return "See who is online"
    fr = data.get("friends") or []
    if not data.get("live"):
        return f"{len(fr)} friends — add a Steam Web API key to see who is online"
    online = [f for f in fr if f.get("state") not in ("offline", "unknown")]
    playing = [f for f in online if f.get("playing")]
    if not online:
        return "Nobody is online right now"
    return f"{len(online)} online" + (f" · {len(playing)} playing" if playing else "")


def matching(friends: list[dict], query: str) -> list[dict]:
    """The friends whose name has ``query`` in it (any case), in the list's order (in a game, online, offline)."""
    q = (query or "").strip().casefold()
    return [f for f in friends if q in (f.get("name") or "").casefold()] if q else list(friends)


def search_note(query: str, found: int) -> str:
    q = query.strip()
    if not found:
        return f"No friend called “{q}”"
    return f"“{q}” — {found} friend{'s' if found != 1 else ''}"


INVITE_HOW = "Opens Steam's chat — choose “Invite to Game” in the menu by their name"


def invite_game(data: dict | None, playing: str = "") -> str:
    """The game a friend can be invited to ("": none). Steam's friends list offers *Invite to Game* while Steam shows
    you in a game: with live data that is your own Steam status; without it, one of the app's games being played
    (``playing``: its title). The name is the app's Library title — Steam's own name for an OnlineFix game is the
    app it plays as ("Spacewar")."""
    d = data or {}
    if d.get("live"):
        me = d.get("self") or {}
        if not me.get("playing"):
            return ""
        return playing or me.get("game") or "your game"
    return playing


def playing_title(w) -> str:
    """The title of the app's game being played now (not a suspended one), "" when none. ``w`` is couch mode."""
    ids = [i for i in getattr(w, "running", ()) if i not in getattr(w, "suspended", ())]
    last = getattr(w, "_last_game", None)
    pick = last if last in ids else (ids[0] if ids else None)
    e = next((x for x in getattr(w, "entries", []) if x.id == pick), None) if pick is not None else None
    return e.title if e is not None else ""


def actions(f: dict, invite: str = "") -> list[tuple[str, str, str]]:
    """(action, label, note) for a friend — only what can be done right now. ``invite``: the game you are in (see
    invite_game), offered to friends who are not offline."""
    out = [("chat", "Chat", "Opens Steam's chat with them")]
    if invite and f.get("state") != "offline":
        out.append(("invite", f"Invite to {invite}", INVITE_HOW))
    if f.get("joinable"):
        out.append(("join", f"Join their game — {f.get('game') or 'in progress'}", "Steam starts it and joins them"))
    if f.get("gameid") and f.get("state") != "offline":
        out.append(("launch", f"Play {f.get('game') or 'the same game'}", "Starts it through Steam (if you have it)"))
    out.append(("profile", "Steam profile", "Opens in Steam"))
    return out


def open_action(w, action: str, f: dict, done: Callable[[], None] | None = None) -> None:
    """Do ``action`` for friend ``f`` through Steam; ``w`` is couch mode (bridge, toasts). Steam's chat / profile
    window is brought up by KWin (the guide button's Steam guard is called off first, or it would put it away)."""
    from ezgd.ui.window_control import release_steam_guard, show_steam_window
    b = w.bridge
    release_steam_guard()

    def ok(_url) -> None:
        if done:
            done()
        if action in ("chat", "invite", "profile"):
            hint = f.get("name", "") if action != "profile" else ""
            for ms in (700, 1600, 3200):                  # (Steam takes a moment to open the window)
                QTimer.singleShot(ms, lambda: show_steam_window(hint, main=action == "profile"))
            if action == "invite":
                w.toasts.show(f"Invite {f.get('name', '')}", "In Steam's chat, open the menu by their name and choose "
                              "“Invite to Game”.", "info")
        elif action in ("join", "launch"):
            w.toasts.show("Steam is starting it", f.get("game") or "", "info")

    def failed(err) -> None:
        w.toasts.show("Steam could not do that", str(err), "warning")
    b.call(b.core.steam_friend_open(action, f), ok, failed, owner=w)


def paint_avatar(p: QPainter, w, f: dict, r: QRectF, s: float) -> bool:
    """A friend's round picture with Steam's status ring. False while the picture is still on its way."""
    ring = state_color(f)
    p.save()
    p.setPen(QPen(ring, 3 * s))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(r.adjusted(-3 * s, -3 * s, 3 * s, 3 * s))
    img = w.images.fill(f.get("avatar") or None, int(r.width()), int(r.height()), radius=r.width() / 2) \
        if f.get("avatar") else None
    ready = True
    if img is not None:
        p.drawImage(r, img, QRectF(img.rect()))
    else:
        ready = not f.get("avatar")
        path = QPainterPath()
        path.addEllipse(r)
        p.fillPath(path, QColor(52, 56, 68))              # (solid: readable on a focused, white row too)
        p.setFont(w._font(r.height() * 0.42, 900))
        p.setPen(QColor(255, 255, 255, 200))
        p.drawText(r, Qt.AlignCenter, (f.get("name") or "?")[:1].upper())
    p.restore()
    return ready


# ================================================================== couch mode: the Friends tab
class FriendsPage(Page):
    def __init__(self, w):
        super().__init__(w)
        self.data: dict | None = None
        self.error = ""
        self.focus = 0
        self.loaded = False
        self.query = ""                         # the search (X): only the friends whose name has it
        self._timer = QTimer(w)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def friends(self) -> list[dict]:
        return matching((self.data or {}).get("friends") or [], self.query)

    def open_search(self, first: str = "") -> None:
        from ezgd.ui.couch_pages import Keyboard
        self.w.keyboard = Keyboard(self.w, "Find a friend", self.search, text=(self.query + first) if first else
                                   self.query, done_label="Find", symbols=True, limit=40)

    def search(self, text: str) -> None:
        self.query = text.strip()
        self.focus = 0
        self._fr_t = 0.0
        self.w.update()

    def type_text(self, text: str) -> bool:
        if text.isprintable() and text.strip():
            self.open_search(text)                 # typing on a real keyboard starts a search
            return True
        return False

    def activate(self) -> None:
        self.reload()
        self._timer.start()

    def _tick(self) -> None:
        if self.w.current_page() is self and self.w.isVisible() and not self.w.isMinimized():
            self.reload()
        else:
            self._timer.stop()

    def reload(self, force: bool = False) -> None:
        b = self.w.bridge

        def got(data: dict) -> None:
            self.data, self.error, self.loaded = data, "", True
            self.focus = max(0, min(self.focus, len(self.friends) - 1))
            self.w.update()

        def failed(err) -> None:
            self.error, self.loaded = str(err), True
            self.w.update()
        b.call(b.core.steam_friends(force), got, failed, owner=self.w)

    def _friend(self) -> dict | None:
        return self.friends[self.focus] if 0 <= self.focus < len(self.friends) else None

    def command(self, name: str) -> str | None:
        n = len(self.friends)
        if name == "up":
            if self.focus == 0:
                return "top"
            self.focus -= 1
        elif name == "down":
            self.focus = min(max(0, n - 1), self.focus + 1)
        elif name in ("lt", "rt"):
            self.focus = max(0, min(n - 1, self.focus + (6 if name == "rt" else -6)))
        elif name == "a":
            self.activate_row()
        elif name == "x":
            self.open_search()
        elif name == "b":
            if self.query:                          # B: the search goes first, then the page
                self.search("")
                return None
            return "back"
        return None

    def hit(self, idx: int, click: bool) -> None:
        self.focus = idx
        if click:
            self.activate_row()

    def hints(self) -> list[tuple[str, str]]:
        back = ("b", "Clear search" if self.query else "Back")
        return [("a", "Options"), ("x", "Search"), back] if self.friends else [("x", "Search"), back]

    def activate_row(self) -> None:
        f = self._friend()
        if f is None:
            return
        rows = []
        bridged = bool(self.w.actions.settings.get("own_overlay", False))
        for action, label, note in actions(f, invite_game(self.data, playing_title(self.w))):
            if bridged and action == "chat":
                label, note = "Message", "Your chat with them, right here"
            elif bridged and action == "invite":
                note = "One press — Steam sends it"
            rows.append((label, lambda a=action: self._do(a, f), "primary" if action == "chat" else "ghost", note))
        rows.append(("Close", self.w.close_overlay, "ghost", ""))
        self.w.menu(f.get("name", ""), rows, text=f.get("status") or "")

    def _do(self, action: str, f: dict) -> None:
        self.w.close_overlay()
        if self.w.actions.settings.get("own_overlay", False) and action in ("chat", "invite", "join", "launch"):
            self._bridged(action, f)                     # our overlay: through the Steam client, no Steam window
            return

        def done() -> None:
            if action in ("chat", "invite", "profile"):
                self.w.step_away("steam")                # Steam's window needs the screen (the guide button
        open_action(self.w, action, f, done)             # brings couch mode back)

    def _bridged(self, action: str, f: dict) -> None:
        from ezgd.platform import steamfriends
        w, b = self.w, self.w.bridge
        acct = steamfriends.to_account_id(f["steamid"])
        if action == "chat":
            self._chat(f)
        elif action == "invite":
            b.call(b.core.steam_invite(acct), lambda _h: w.toasts.show("Invite sent", f.get("name", ""), "success"),
                   lambda e: w.toasts.show("No invite sent", str(e), "warning"), owner=w)
        else:
            url = steamfriends.join_url(f) if action == "join" else steamfriends.launch_url(f)
            if url:
                b.call(b.core.steam_open(url), lambda _r: w.toasts.show("Steam is starting it", f.get("game") or "",
                                                                       "info"),
                       lambda e: w.toasts.show("Could not join", str(e), "warning"), owner=w)
            else:
                w.toasts.show("Nothing to join right now", f"{f.get('name', 'They')} are not in a joinable game.")

    def _chat(self, f: dict) -> None:
        """The last messages with a friend, and Write (the on-screen keyboard) — all through the Steam client."""
        from ezgd.platform import steamfriends
        from ezgd.ui.couch_pages import Keyboard
        w, b = self.w, self.w.bridge
        acct = steamfriends.to_account_id(f["steamid"])
        name = f.get("name", "")

        def send(text: str) -> None:
            if text:
                b.call(b.core.steam_chat_send(acct, text), lambda _r: self._chat(f),
                       lambda e: w.toasts.show("Not sent", str(e), "warning"), owner=w)

        def write() -> None:
            w.close_overlay()
            w.keyboard = Keyboard(w, f"Message {name}", send, done_label="Send", symbols=True, limit=400)
            w.update()

        def show(msgs: list) -> None:
            lines = [("You" if m["mine"] else name,
                      ("Invite to play" if m.get("invite") else m["text"])[:160]) for m in msgs[-7:]]
            from ezgd.platform.steambridge import join_url, joinable
            invite = next((m for m in reversed(msgs) if m.get("invite") and not m["mine"]), None)
            buttons = [("Write", write, "primary")]
            if invite is not None and joinable(invite["invite"]):      # (an empty invite: nothing Steam can join)
                url = join_url(invite["invite"], f["steamid"])
                buttons.append(("Join their game", lambda: (w.close_overlay(), b.call(
                    b.core.steam_open(url), lambda _r: None, lambda e: w.toasts.show("Could not join", str(e),
                                                                                    "warning"), owner=w)), "play"))
            buttons.append(("Close", w.close_overlay, "ghost"))
            w.show_overlay(name, "" if lines else "No messages yet — say hi.", buttons, lines=lines)
        b.call(b.core.steam_chat_history(acct, 20), show, lambda e: w.message("Steam chat", str(e)), owner=w)

    def paint(self, p: QPainter) -> None:
        w = self.w
        s, W, H = self.s, w.width(), w.height()
        d = self.data or {}
        if not self.loaded:
            sub = "Asking Steam…"
        elif self.error:
            sub = self.error
        elif self.query:
            sub = search_note(self.query, len(self.friends))
        else:
            sub = summary(d)
        y = self._heading(p, "Friends", sub)
        left = 72 * s
        if d.get("error") or (d.get("private") and d.get("live")):
            note = d.get("error") or "Your friends list is private on Steam: showing the friends Steam keeps here."
            p.setFont(w._font(17 * s, 600))
            p.setPen(QColor(theme.CURRENT.warning))
            p.drawText(QRectF(left, y - 6 * s, W - 2 * left, 30 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(note, Qt.ElideRight, int(W - 2 * left)))
            y += 32 * s
        rect = QRectF(left, y + 10 * s, W - 2 * left, H - 76 * s - 20 * s - y - 10 * s)
        cols = 2
        gap = 18 * s
        cw = (rect.width() - gap) / cols
        row_h = 96 * s
        n = len(self.friends)
        frow = self.focus // cols
        target = getattr(self, "_fr_t", 0.0)
        ft = frow * (row_h + gap)
        if ft < target:
            target = ft
        elif ft + row_h - target > rect.height():
            target = ft + row_h - rect.height()
        self._fr_t = max(0.0, target)
        off = self._smooth("_fr", self._fr_t)
        p.save()
        p.setClipRect(rect.adjusted(-20 * s, -10 * s, 20 * s, 10 * s), Qt.IntersectClip)
        active = w.zone == "main" and w.overlay is None and w.keyboard is None
        pending = False
        for k, f in enumerate(self.friends):
            row, col = divmod(k, cols)
            r = QRectF(rect.left() + col * (cw + gap), rect.top() + row * (row_h + gap) - off, cw, row_h)
            if r.bottom() < rect.top() - 20 * s or r.top() > rect.bottom() + 20 * s:
                continue
            w._hit.append((r, "page", k))
            pending |= not self._row(p, f, r, active and k == self.focus)
        p.restore()
        if pending:
            QTimer.singleShot(150, w, w.update)
        if self.loaded and not n and not self.error and not self.query:
            p.setFont(w._font(20 * s))
            p.setPen(QColor(255, 255, 255, 160))
            p.drawText(QRectF(left, rect.top() + 20 * s, W - 2 * left, 40 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       "No Steam friends found.")

    def _row(self, p: QPainter, f: dict, r: QRectF, focused: bool) -> bool:
        w, s = self.w, self.s
        path = QPainterPath()
        path.addRoundedRect(r, 18 * s, 18 * s)
        p.fillPath(path, QColor(255, 255, 255, 44 if focused else 18))
        if focused:
            p.setPen(QPen(QColor("white"), 3 * s))
            p.drawPath(path)
        a = r.height() - 32 * s
        ready = paint_avatar(p, w, f, QRectF(r.left() + 18 * s, r.top() + 16 * s, a, a), s)
        x = r.left() + 18 * s + a + 22 * s
        tw = r.right() - x - 20 * s
        p.setFont(w._font(22 * s, 800))
        p.setPen(QColor("white") if f.get("state") != "offline" else QColor(255, 255, 255, 150))
        p.drawText(QRectF(x, r.top() + 14 * s, tw, 34 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(f.get("name", ""), Qt.ElideRight, int(tw)))
        p.setFont(w._font(16 * s, 700))
        p.setPen(state_color(f))
        status = f.get("status") or ""
        if f.get("joinable"):
            status += "  ·  can be joined"
        p.drawText(QRectF(x, r.top() + 50 * s, tw, 28 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(status, Qt.ElideRight, int(tw)))
        return ready
