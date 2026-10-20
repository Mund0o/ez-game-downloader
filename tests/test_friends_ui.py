"""Steam friends in the quick menu and couch mode's Friends tab: the list, a friend's actions, and Steam's links
handed over (chat makes room for Steam's window)."""

import sys

import pytest

from test_couch_ui import _open_couch
from test_quick_menu import _running
from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.platform import steamfriends as sf

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="game processes from /proc")

A, B, C = (sf.to_steamid64(n) for n in (101, 102, 103))
FRIENDS = {"me": "1", "live": True, "private": False, "error": "", "at": 0, "friends": [
    sf.Friend(A, "Alice", "online", game="Deep Rock Galactic", gameid="548430", lobby="42").as_dict(),
    sf.Friend(B, "Bob", "online").as_dict(),
    sf.Friend(C, "Cleo", "offline").as_dict()]}


def _fake_steam(core, monkeypatch):
    opened = []

    async def friends(force=False):
        return FRIENDS

    async def friend_open(action, f):
        opened.append((action, f["name"]))
        return "steam://…"
    monkeypatch.setattr(core, "steam_friends", friends)
    monkeypatch.setattr(core, "steam_friend_open", friend_open)
    from ezgd.ui import window_control
    monkeypatch.setattr(window_control, "show_steam_window", lambda *a, **k: True)
    return opened


@linux_only
def test_friends_in_the_quick_menu(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    opened = _fake_steam(core, monkeypatch)
    a = _running(win, core, tmp_path, qtbot, "Friendly Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    qtbot.waitUntil(lambda: q.friends is not None, timeout=3000)
    row = next(r for r in q.rows if r.key == "friends")
    assert row.note == "2 online · 1 playing"
    q.focus = q.rows.index(row)
    q.grab()
    q.command("a")
    assert q.view == "friends" and [r.label for r in q.rows if r.kind == "friend"] == ["Alice", "Bob", "Cleo"]
    assert q.rows[q.focus].label == "Alice" and q.rows[q.focus].note == "Playing Deep Rock Galactic"
    q.grab()
    q.command("down")
    q.command("a")                                                    # Bob: online, no game
    assert q.view == "friend" and [r.key for r in q.rows] == ["fa-chat", "fa-profile"]
    q.command("b")
    assert q.view == "friends" and q.rows[q.focus].label == "Bob"     # back on him
    q.command("up")
    q.command("a")                                                    # Alice: in a joinable game
    assert [r.key for r in q.rows] == ["fa-chat", "fa-join", "fa-launch", "fa-profile"]
    assert q.rows[1].label == "Join their game — Deep Rock Galactic"
    q.grab()
    q.command("a")                                                    # Chat: Steam's chat, the game keeps running
    qtbot.waitUntil(lambda: opened == [("chat", "Alice")], timeout=3000)
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    assert not core.library.is_suspended(a.id)
    couch._home_held()                                               # again: Back goes friends → menu
    qtbot.waitUntil(lambda: q.isVisible() and q._shown >= 1.0, timeout=5000)
    assert q.view == "main"
    q.focus = next(k for k, r in enumerate(q.rows) if r.key == "friends")
    q.command("a")
    q.command("b")
    assert q.view == "main" and q.rows[q.focus].key == "friends"
    q.command("b")
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_couch_friends_tab(ui, qtbot, monkeypatch):
    from ezgd.ui import couch as C
    win, core, _ = ui
    opened = _fake_steam(core, monkeypatch)
    couch = _open_couch(win, qtbot)
    couch._switch_tab(C.T_FRIENDS, force=True)
    page = couch.current_page()
    qtbot.waitUntil(lambda: page.loaded, timeout=3000)
    assert [f["name"] for f in page.friends] == ["Alice", "Bob", "Cleo"]
    couch.zone = "main"
    couch.grab()
    page.command("a")                                                 # Alice's options
    assert couch.overlay is not None
    assert couch.overlay.title == "Alice" and couch.overlay.text == "Playing Deep Rock Galactic"
    assert [b[0] for b in couch.overlay.buttons] == ["Chat", "Join their game — Deep Rock Galactic",
                                                     "Play Deep Rock Galactic", "Steam profile", "Close"]
    page._do("chat", page.friends[0])
    qtbot.waitUntil(lambda: opened == [("chat", "Alice")], timeout=3000)
    qtbot.waitUntil(lambda: couch._away == "steam", timeout=3000)    # makes room for Steam's chat window
    couch.come_back_from("steam")
    couch.force_close()


@linux_only
def test_finding_a_friend(ui, qtbot, tmp_path, monkeypatch):
    """X (or typing a name on a real keyboard) searches the friends list — in couch mode's Friends tab and in the
    quick menu; B shows everyone again before it goes back. The quick menu's list opens on the first friend."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from ezgd.ui import couch as C
    win, core, _ = ui
    _fake_steam(core, monkeypatch)
    a = _running(win, core, tmp_path, qtbot, "Search Game")
    couch = _open_couch(win, qtbot)
    couch._switch_tab(C.T_FRIENDS, force=True)
    page = couch.current_page()
    qtbot.waitUntil(lambda: page.loaded, timeout=3000)
    couch.zone = "main"
    couch.command("x")                                                # Search
    assert couch.keyboard is not None and couch.keyboard.title == "Find a friend"
    for ch in "BO":
        couch.keyboard.type(ch)
    couch.command("start")
    assert page.query == "BO" and [f["name"] for f in page.friends] == ["Bob"]
    couch.grab()
    assert ("b", "Clear search") in page.hints()
    couch.command("b")                                                # B: everyone again, still on Friends
    assert page.query == "" and len(page.friends) == 3 and couch.current_page() is page
    couch.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Z, Qt.NoModifier, "z"))   # a real keyboard types
    assert couch.keyboard is not None and couch.keyboard.text == "z"
    couch.keyboard.done()
    from ezgd.ui.couch_friends import search_note
    assert page.friends == [] and search_note("z", 0) == "No friend called “z”"
    couch.grab()
    page.search("")
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()                                                # the quick menu's Friends
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    q.open_friends()
    qtbot.waitUntil(lambda: q.friends is not None, timeout=3000)
    assert q.rows[0].key == "f-search" and q.rows[q.focus].label == "Alice"     # opens on the first friend
    q.command("x")
    assert q.keyboard is not None
    q.keyboard.type("cl")
    q.command("start")
    assert [r.label for r in q.rows if r.kind == "friend"] == ["Cleo"] and q.rows[q.focus].label == "Cleo"
    assert "“cl” — 1 friend" == q.rows[0].label
    q.grab()
    q.command("b")                                                    # everyone again
    assert q.view == "friends" and len([r for r in q.rows if r.kind == "friend"]) == 3
    q.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "a"))
    assert q.keyboard is not None and q.keyboard.text == "a"
    q.keyboard.cancel()
    q.command("b")
    assert q.view == "main"
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_friends_summary_and_actions():
    from ezgd.ui.couch_friends import actions, summary
    assert summary(None) == "See who is online"
    assert summary(FRIENDS) == "2 online · 1 playing"
    assert summary({"live": False, "friends": FRIENDS["friends"]}).startswith("3 friends — add a Steam Web API key")
    assert summary({"live": True, "friends": [FRIENDS["friends"][2]]}) == "Nobody is online right now"
    cleo = FRIENDS["friends"][2]
    assert [a for a, _l, _n in actions(cleo)] == ["chat", "profile"]


def test_invite_to_your_game():
    """Steam has no link that sends an invite; its friends list offers "Invite to Game" while Steam shows you in a
    game. The app offers "Invite to <game>" then (named from the Library: an OnlineFix game is "Spacewar" to Steam)."""
    from ezgd.ui.couch_friends import INVITE_HOW, actions, invite_game
    live = {**FRIENDS, "self": {"playing": True, "game": "Spacewar", "gameid": "480"}}
    assert invite_game(FRIENDS, "Friendly Game") == ""                # live data, Steam does not show you playing
    assert invite_game(live, "Friendly Game") == "Friendly Game"      # the app's name, not Steam's "Spacewar"
    assert invite_game(live) == "Spacewar"                            # a game started in Steam itself
    assert invite_game({"live": False}, "Friendly Game") == "Friendly Game"   # no key: the app's game being played
    assert invite_game({"live": False}) == "" and invite_game(None) == ""
    bob, cleo = FRIENDS["friends"][1], FRIENDS["friends"][2]
    assert ("invite", "Invite to Friendly Game", INVITE_HOW) in actions(bob, "Friendly Game")
    assert "invite" not in [a for a, _l, _n in actions(cleo, "Friendly Game")]     # offline: no invite
    assert "Invite to Game" in INVITE_HOW


@linux_only
def test_invite_from_the_quick_menu(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    opened = _fake_steam(core, monkeypatch)
    live = {**FRIENDS, "self": {"playing": True, "game": "Spacewar", "gameid": "480"}}

    async def friends(force=False):
        return live
    monkeypatch.setattr(core, "steam_friends", friends)
    a = _running(win, core, tmp_path, qtbot, "Invite Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    qtbot.waitUntil(lambda: q.friends is not None, timeout=3000)
    q.open_friends()
    q.command("down")
    q.command("a")                                                    # Bob
    assert [r.key for r in q.rows] == ["fa-chat", "fa-invite", "fa-profile"]
    assert q.rows[1].label == "Invite to Invite Game" and "Invite to Game" in q.rows[1].note
    q.grab()
    q.focus = 1
    q.command("a")
    qtbot.waitUntil(lambda: opened == [("invite", "Bob")], timeout=3000)
    qtbot.waitUntil(lambda: not q.isVisible(), timeout=3000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_our_overlay_chats_invites_and_joins_without_steam_windows(ui, qtbot, tmp_path, monkeypatch):
    """Our overlay in the middle: a friend's invite is a Join row, unread messages show, Invite is one press (Steam's
    own Invite to Game), Message opens the chat in the quick menu and the on-screen keyboard sends — Steam's chat
    window is never opened."""
    win, core, _ = ui
    opened = _fake_steam(core, monkeypatch)
    bob = sf.to_account_id(B)
    calls = []

    async def chats():
        return {"self": {"accountid": 1, "appid": 480, "lobby": "77", "connect": "", "game": ""},
                "chats": [{"accountid": bob, "name": "Bob", "unread": 2, "time": 1, "last": "Invited you to play",
                           "last_from": bob, "invite": {"kind": "lobbyinvite", "appid": "480", "lobby": "99",
                                                        "connect": ""}}]}

    async def history(acct, limit=40):
        calls.append(("history", acct))
        return [{"mine": False, "time": 1, "text": "you up?", "invite": None},
                {"mine": True, "time": 2, "text": "yep", "invite": None}]

    async def send(acct, text):
        calls.append(("send", acct, text))

    async def invite(acct):
        calls.append(("invite", acct))
        return "lobby"

    async def open_url(url):
        calls.append(("open", url))
    for name, fn in (("steam_chats", chats), ("steam_chat_history", history), ("steam_chat_send", send),
                     ("steam_invite", invite), ("steam_open", open_url)):
        monkeypatch.setattr(core, name, fn)
    a = _running(win, core, tmp_path, qtbot, "Overlay Game")
    couch = _open_couch(win, qtbot)
    monkeypatch.setitem(couch.actions.settings, "own_overlay", True)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    qtbot.waitUntil(lambda: q.chats is not None and q.friends is not None, timeout=3000)
    q._build()
    assert q.rows[0].key == f"inv-{bob}" and q.rows[0].label == "Join Bob"
    assert next(r for r in q.rows if r.key == "friends").note.startswith("2 new messages")
    q.grab()
    q.open_friends()
    assert next(r for r in q.rows if r.label == "Bob").note.startswith("● 2 new")
    q.open_friend(next(f for f in FRIENDS["friends"] if f["name"] == "Bob"))
    assert [r.label for r in q.rows][:2] == ["Message", "Invite to Overlay Game"]
    q.focus = 1
    q.command("a")                                                    # one press: Steam sends the invite
    qtbot.waitUntil(lambda: ("invite", bob) in calls and "Invite sent to Bob" in q.flash, timeout=3000)
    q.grab()
    q.focus = 0
    q.command("a")                                                    # Message: the chat, in the quick menu
    assert q.view == "chat"
    qtbot.waitUntil(lambda: len(q.chat_msgs) == 2, timeout=3000)
    assert [r.kind for r in q.rows] == ["msg", "msg", "primary"] and q.rows[q.focus].key == "write"
    q.grab()
    q.command("a")                                                    # Write → the on-screen keyboard
    assert q.keyboard is not None
    for ch in "gg":
        q.keyboard.type(ch)
    q.grab()
    q.command("start")                                                # Start sends
    qtbot.waitUntil(lambda: ("send", bob, "gg") in calls, timeout=3000)
    assert q.keyboard is None and ("history", bob) in calls          # (reloaded from Steam after sending)
    q.command("b")
    assert q.view == "friend"
    q.command("b")
    q.command("b")
    assert q.view == "main"
    q.focus = 0
    q.command("a")                                                    # Join Bob: the game waits, Steam joins
    qtbot.waitUntil(lambda: any(c[0] == "open" for c in calls), timeout=8000)
    assert ("open", f"steam://joinlobby/480/99/{B}") in calls
    assert a.id not in couch.suspended                                # an invite to this game: it keeps running
    assert opened == []                                               # Steam's chat window: never
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


@linux_only
def test_steam_controller_types_in_the_chat_and_a_held_button_invites_once(ui, qtbot, tmp_path, monkeypatch):
    """A Steam controller that Steam sends as keys (A = Enter, B = Esc, X = Space, Start = Tab): in the quick menu's
    chat A types the key in focus (Enter used to send the empty message at once and close the keyboard), X is Space,
    B Backspace, Start sends. A held A (auto-repeated Enter) on Invite sends one invite, not hundreds; an invite to
    another game than the one being played suspends it, like switching games."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    win, core, _ = ui
    _fake_steam(core, monkeypatch)
    bob = sf.to_account_id(B)
    calls = []

    async def chats():
        return {"self": {"accountid": 1, "appid": 480, "lobby": "77", "connect": "", "game": ""}, "chats": []}

    async def history(acct, limit=40):
        return []

    async def send(acct, text):
        calls.append(("send", text))

    async def invite(acct):
        calls.append(("invite", acct))
        return "lobby"

    async def open_url(url):
        calls.append(("open", url))
    for name, fn in (("steam_chats", chats), ("steam_chat_history", history), ("steam_chat_send", send),
                     ("steam_invite", invite), ("steam_open", open_url)):
        monkeypatch.setattr(core, name, fn)
    a = _running(win, core, tmp_path, qtbot, "Keys Game")
    couch = _open_couch(win, qtbot)
    monkeypatch.setitem(couch.actions.settings, "own_overlay", True)
    keys = {Qt.Key_Return: "a", Qt.Key_Escape: "b", Qt.Key_Space: "x", Qt.Key_Tab: "start"}
    monkeypatch.setattr(couch, "key_action", lambda k: keys.get(int(k)))
    monkeypatch.setattr(couch.pads, "steam_hw", True)
    qtbot.waitUntil(lambda: a.id in couch.running, timeout=5000)
    couch._home_held()
    qtbot.waitUntil(lambda: couch.quick is not None and couch.quick._shown >= 1.0, timeout=5000)
    q = couch.quick
    qtbot.waitUntil(lambda: q.chats is not None and q.friends is not None, timeout=3000)

    def key(k, text="", repeat=False):
        q.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier, text, repeat))

    bob_f = next(f for f in FRIENDS["friends"] if f["name"] == "Bob")
    q.open_friend(bob_f)
    q.focus = 1                                                       # Invite
    key(Qt.Key_Return, "\r")
    for _ in range(20):                                               # A held: the key repeats
        key(Qt.Key_Return, "\r", repeat=True)
    qtbot.waitUntil(lambda: ("invite", bob) in calls, timeout=3000)
    qtbot.wait(200)
    assert calls.count(("invite", bob)) == 1
    q.open_chat(bob_f)
    q.focus = len(q.rows) - 1
    key(Qt.Key_Return, "\r")                                         # A on "Write a message": the keyboard
    assert q.keyboard is not None
    kb = q.keyboard
    kb.row, kb.col = 1, 0                                             # q
    key(Qt.Key_Return, "\r")                                         # A types it — the keyboard stays up
    assert q.keyboard is kb and kb.text == "q"
    key(Qt.Key_Space, " ")                                            # X: Space
    kb.row, kb.col = 1, 1
    key(Qt.Key_Return, "\r")
    assert kb.text == "q w"
    key(Qt.Key_Escape, "\x1b")                                       # B: Backspace
    assert kb.text == "q " and q.keyboard is kb
    kb.row, kb.col = 1, 4
    key(Qt.Key_Return, "\r")
    q.grab()                                                          # (the shortcuts drawn along the keyboard)
    key(Qt.Key_Tab, "\t")                                            # Start sends
    qtbot.waitUntil(lambda: ("send", "q t") in calls, timeout=3000)
    assert q.keyboard is None
    q._join("steam://joinlobby/730/5/1", "Bob", "730")                # another game: this one waits, suspended
    qtbot.waitUntil(lambda: ("open", "steam://joinlobby/730/5/1") in calls, timeout=8000)
    assert a.id in couch.suspended
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))
    couch.force_close()


def test_switching_our_overlay_on_asks_once_in_two_lines(ui, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win, core, _ = ui
    page = win.pages["settings"]
    asked, setups = [], []

    async def status():
        return {"set_up": False, "steam_running": True, "ready": False}

    async def setup():
        setups.append(1)
        return {"set_up": True, "steam_running": True, "ready": True}
    monkeypatch.setattr(core, "steam_bridge_status", status)
    monkeypatch.setattr(core, "steam_bridge_setup", setup)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Cancel)
    box = page.widgets["own_overlay"]
    box.setChecked(True)
    qtbot.waitUntil(lambda: bool(asked) and not box.isChecked(), timeout=3000)   # cancelled: unticked again
    assert asked[0].count("\n") == 1 and not setups                                # two lines
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Ok)
    box.setChecked(True)
    qtbot.waitUntil(lambda: bool(setups) and core.settings.settings.own_overlay, timeout=3000)
    assert box.isChecked()
    box.setChecked(False)
    qtbot.waitUntil(lambda: not core.settings.settings.own_overlay, timeout=3000)
