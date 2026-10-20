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
