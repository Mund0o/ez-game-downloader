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
