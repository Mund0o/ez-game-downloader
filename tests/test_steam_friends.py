"""Steam friends: who you are and your friends from Steam's files, live status from Steam's Web API with the user's
own key, and Steam's own links for chat / join / start their game / profile."""

import json

import httpx
import pytest

from test_features_v2 import core  # noqa: F401 — the AppCore fixture

from ezgd.platform import steamfriends as sf

ME = "76561198000000010"
A, B, C, D = (sf.to_steamid64(n) for n in (101, 102, 103, 104))
KEY = "0123456789ABCDEF0123456789ABCDEF"


def fake_steam(root):
    (root / "config").mkdir(parents=True)
    (root / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n\t"76561198000000099"\n\t{\n\t\t"AccountName"\t\t"old"\n\t\t"Timestamp"\t\t"100"\n\t}\n'
        f'\t"{ME}"\n\t{{\n\t\t"AccountName"\t\t"me"\n\t\t"MostRecent"\t\t"1"\n\t\t"Timestamp"\t\t"50"\n\t}}\n}}\n')
    cfg = root / "userdata" / str(sf.to_account_id(ME)) / "config"
    cfg.mkdir(parents=True)
    friends = "".join(f'\t\t"{n}"\n\t\t{{\n\t\t\t"name"\t\t"{name}"\n\t\t\t"avatar"\t\t"{av}"\n\t\t}}\n'
                      for n, name, av in ((101, "Alice", "ab" * 20), (102, "Bob", "0" * 40), (103, "Cleo", "cd" * 20),
                                          (sf.to_account_id(ME), "Me myself", "ee" * 20)))
    (cfg / "localconfig.vdf").write_text('"UserLocalConfigStore"\n{\n\t"friends"\n\t{\n'
                                         f'\t\t"PersonaName"\t\t"me"\n{friends}\t}}\n}}\n')
    return root


def test_who_you_are_and_your_friends_from_steams_files(tmp_path):
    root = fake_steam(tmp_path / "Steam")
    assert sf.my_steamid(root) == ME                                 # MostRecent wins over a newer timestamp
    local = sf.local_friends(root, ME)
    assert sorted(f.name for f in local.values()) == ["Alice", "Bob", "Cleo"]       # not yourself, not settings
    assert local[A].avatar == f"https://avatars.steamstatic.com/{'ab' * 20}_medium.jpg"
    assert local[B].avatar == ""                                      # the default (all-zero) picture
    assert sf.my_steamid(tmp_path / "nothing") is None and sf.local_friends(None, ME) == {}


def _summaries(*players):
    return json.dumps({"response": {"players": list(players)}})


@pytest.fixture
async def client(routes):
    from ezgd.net.client import SafeHttpClient
    c = SafeHttpClient(transport=httpx.MockTransport(routes))
    yield c
    await c.aclose()


async def test_live_status_with_the_users_key(tmp_path, routes, client):
    root = fake_steam(tmp_path / "Steam")
    routes.add(f"{sf.API}/GetFriendList/v1/?key={KEY}&steamid={ME}&relationship=friend",
               json.dumps({"friendslist": {"friends": [{"steamid": s} for s in (A, B, C, D)]}}),
               content_type="application/json")
    routes.add(f"{sf.API}/GetPlayerSummaries/v2/*", _summaries(
        {"steamid": A, "personaname": "Alice", "personastate": 1, "gameextrainfo": "Deep Rock Galactic",
         "gameid": "548430", "lobbysteamid": "109775241047227397", "avatarmedium": "https://x/a.jpg"},
        {"steamid": B, "personaname": "Bob", "personastate": 3},
        {"steamid": C, "personaname": "Cleo", "personastate": 0, "lastlogoff": 1700000000},
        {"steamid": D, "personaname": "Dan", "personastate": 1, "gameid": "730", "gameserverip": "1.2.3.4:27015",
         "gameextrainfo": "Counter-Strike 2"},
        {"steamid": ME, "personaname": "me", "personastate": 1, "gameid": "480", "gameextrainfo": "Spacewar"}),
        content_type="application/json")
    out = await sf.load(client, root, KEY)
    assert out["live"] and not out["private"] and out["error"] == "" and out["me"] == ME
    assert out["self"] == {"playing": True, "game": "Spacewar", "gameid": "480"}    # what Steam shows you doing
    assert ME in routes.calls[-1].url.params["steamids"]
    names = [f["name"] for f in out["friends"]]
    assert names == ["Alice", "Dan", "Bob", "Cleo"]                  # playing, then online, away, offline
    alice, dan, bob, cleo = out["friends"]
    assert alice["status"] == "Playing Deep Rock Galactic" and alice["joinable"]
    assert sf.join_url(alice) == f"steam://joinlobby/548430/109775241047227397/{A}"
    assert sf.join_url(dan) == "steam://connect/1.2.3.4:27015?appid=730"
    assert sf.launch_url(alice) == "steam://rungameid/548430"
    assert bob["status"] == "Away" and not bob["joinable"] and sf.join_url(bob) is None
    assert cleo["status"] == "Offline" and sf.launch_url(cleo) is None
    assert sf.chat_url(A) == f"steam://friends/message/{A}" and sf.profile_url(A).endswith(A)
    assert all(r.url.host == "api.steampowered.com" for r in routes.calls)    # the key goes to Steam only


async def test_private_friends_list_and_bad_key_and_no_key(tmp_path, routes, client):
    root = fake_steam(tmp_path / "Steam")
    routes.add(f"{sf.API}/GetFriendList/v1/*", b"", status=401)       # friends list private: Steam's files
    routes.add(f"{sf.API}/GetPlayerSummaries/v2/*", _summaries(
        {"steamid": A, "personaname": "Alice", "personastate": 1}), content_type="application/json")
    out = await sf.load(client, root, KEY)
    assert out["private"] and out["live"]
    assert {f["name"]: f["status"] for f in out["friends"]} == {"Alice": "Online", "Bob": "", "Cleo": ""}
    routes.add(f"{sf.API}/GetFriendList/v1/*", b"", status=403)       # a wrong key: said so, the list still shows
    out = await sf.load(client, root, "wrong")
    assert not out["live"] and "Web API key" in out["error"] and len(out["friends"]) == 3
    assert "key=" not in out["error"]                                 # (never the URL with the key)
    calls = len(routes.calls)
    out = await sf.load(client, root, "")                             # no key: Steam's files, nothing asked
    assert len(routes.calls) == calls and not out["live"] and "Settings" in out["error"]
    with pytest.raises(sf.FriendsError, match="No Steam account"):
        await sf.load(client, tmp_path / "empty", KEY)


async def test_steam_friend_links_go_to_steam(core, monkeypatch):
    from ezgd.platform import steam
    opened = []
    monkeypatch.setattr(steam, "open_steam_url", opened.append)
    f = {"steamid": A, "name": "Alice", "gameid": "548430", "lobby": "42", "server": ""}
    for action in ("chat", "join", "launch", "profile", "invite"):
        await core.steam_friend_open(action, f)
    assert opened == [f"steam://friends/message/{A}", f"steam://joinlobby/548430/42/{A}", "steam://rungameid/548430",
                      f"steam://url/SteamIDPage/{A}", f"steam://friends/message/{A}"]    # invite: Steam's chat
    with pytest.raises(RuntimeError, match="Nothing to join"):
        await core.steam_friend_open("join", {"steamid": B, "name": "Bob"})


CLIENT = {"self": {"accountid": 9, "name": "me", "state": 1, "appid": 480, "gameid": "480", "game": "Spacewar",
                   "lobby": "0", "ip": 0, "port": 0, "avatar": "", "last": 0},
          "friends": [
              {"accountid": 3, "name": "zed", "state": 0, "appid": 0, "gameid": "0", "game": "", "lobby": "0",
               "ip": 0, "port": 0, "avatar": "", "last": 100},
              {"accountid": 4, "name": "Amy", "state": 4, "appid": 0, "gameid": "0", "game": "", "lobby": "0",
               "ip": 0, "port": 0, "avatar": "https://avatars.steamstatic.com/x_medium.jpg", "last": 0},
              {"accountid": 5, "name": "Jinx", "state": 1, "appid": 2868840, "gameid": "2868840",
               "game": "Slay the Spire 2", "lobby": "109775241", "ip": 0, "port": 0, "avatar": "", "last": 0},
              {"accountid": 6, "name": "bo", "state": 1, "appid": 0, "gameid": "0", "game": "", "lobby": "0",
               "ip": 16909060, "port": 27015, "avatar": "", "last": 0},
              {"accountid": 7, "name": "Old", "state": 0, "appid": 0, "gameid": "0", "game": "", "lobby": "0",
               "ip": 0, "port": 0, "avatar": "", "last": 900}]}


def test_the_steam_clients_own_list_is_live_and_in_steams_order():
    """Our overlay on: the friends list comes from the Steam client itself — live, no Web API key — in a game first,
    then online, away / snooze, then offline (most recently online first)."""
    d = sf.from_client_list(CLIENT, "x")
    assert d["live"] and not d["error"]
    assert [f["name"] for f in d["friends"]] == ["Jinx", "bo", "Amy", "Old", "zed"]
    jinx = d["friends"][0]
    assert jinx["status"] == "Playing Slay the Spire 2" and jinx["joinable"]
    assert jinx["steamid"] == sf.to_steamid64(5) and sf.join_url(jinx) == \
        f"steam://joinlobby/2868840/109775241/{sf.to_steamid64(5)}"
    assert d["friends"][1]["server"] == "" and d["friends"][1]["status"] == "Online"   # (no game: no server)
    assert d["friends"][2]["state"] == "snooze" and d["friends"][2]["avatar"].startswith("https://")
    assert d["self"] == {"playing": True, "game": "Spacewar", "gameid": "480"}


def test_join_their_game_like_steams_own_join_game():
    """Steam's friends list joins a friend with their lobby only when it is valid (is_in_valid_lobby), else with
    their game's own connect string (rich presence) — read 2026-10-27 in Steam's sp.js. A friend with a stale lobby
    and nothing else is not joinable."""
    def friend(**kw):
        base = {"accountid": 9, "name": "P", "state": 1, "appid": 730, "gameid": "730", "game": "Counter-Strike 2",
                "lobby": "0", "ip": 0, "port": 0, "avatar": "", "last": 0}
        return sf.from_client({**base, **kw}).as_dict()
    sid = sf.to_steamid64(9)
    cs = friend(connect="+connect 10.0.0.5:27015", valid_lobby=False)
    assert cs["joinable"] and sf.join_url(cs) == f"steam://rungame/730/{sid}/%2Bconnect%2010.0.0.5%3A27015"
    stale = friend(lobby="109775241", valid_lobby=False)
    assert not stale["joinable"] and sf.join_url(stale) is None
    valid = friend(lobby="109775241", valid_lobby=True, connect="+connect 1.2.3.4:1")
    assert sf.join_url(valid) == f"steam://joinlobby/730/109775241/{sid}"
    sneaky = friend(connect="+exec evil.cfg")
    assert not sneaky["joinable"]                                   # (their text: only connect forms)


async def test_core_reads_the_steam_client_with_our_overlay_on(core, monkeypatch):
    from ezgd.platform.steambridge import BridgeError
    calls = []

    class FakeBridge:
        async def friends(self):
            calls.append("client")
            return CLIENT

    async def web(*_a, **_k):
        calls.append("web")
        return {"me": "x", "live": False, "private": False, "error": "no key", "at": 0, "friends": []}
    monkeypatch.setattr(sf, "load", web)
    monkeypatch.setattr(sf, "my_steamid", lambda root: "x")
    core._steam_bridge = FakeBridge()
    assert (await core.steam_friends())["error"] == "no key" and calls == ["web"]      # off: the Web API as before
    core.settings.set("own_overlay", True)
    d = await core.steam_friends(force=True)
    assert d["live"] and d["friends"][0]["name"] == "Jinx" and calls == ["web", "client"]

    class Down:
        async def friends(self):
            raise BridgeError("Steam is not running.")
    core._steam_bridge = Down()
    assert (await core.steam_friends(force=True))["error"] == "no key"                 # the client away: Web API
