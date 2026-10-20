"""Steam friends: who is online and what they play — for the quick menu and couch mode's Friends tab.

Where it comes from (nothing is guessed or scraped):

* **Who you are**: Steam's ``config/loginusers.vdf`` (the account Steam signed in last; no password involved).
* **Your friends, offline**: the names and avatars Steam keeps in ``userdata/<account>/config/localconfig.vdf``.
* **Live status** (online / away / in a game, which game, a joinable lobby or server): Steam's Web API —
  ``ISteamUser/GetFriendList`` and ``GetPlayerSummaries`` — with the user's own Web API key (Settings; made once at
  steamcommunity.com/dev/apikey). The key is only ever sent to api.steampowered.com, and never logged. When the
  friends list is private, GetFriendList answers 401: the friends Steam keeps on disk are asked about instead.

What can be done with a friend uses Steam's own links, the same ones Steam's friends list uses (checked in the Steam
client's code): chat (``steam://friends/message/<id>`` opens Steam's chat with them), join their game
(``steam://joinlobby/<app>/<lobby>/<id>`` or ``steam://connect/<server>``), start the game they play
(``steam://rungameid/<game>``) and their profile (``steam://url/SteamIDPage/<id>``). Inviting someone into a game is
not among them: Steam only lets the game itself, Steam's overlay or Steam's own friends list send an invite. Its
friends list offers *Invite to Game* while you are in a joinable game, so "Invite to your game" opens Steam's chat
with the friend and says where that choice is (your own status comes with the live data: ``self``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ezgd.platform import vdf

log = logging.getLogger(__name__)

API = "https://api.steampowered.com/ISteamUser"
KEY_PAGE = "https://steamcommunity.com/dev/apikey"
ID_BASE = 76561197960265728            # SteamID64 of account id 0 (individual, public universe)
BATCH = 100                            # GetPlayerSummaries takes at most 100 ids per call
STATES = {0: "offline", 1: "online", 2: "busy", 3: "away", 4: "snooze", 5: "online", 6: "online"}
STATE_TEXT = {"offline": "Offline", "online": "Online", "busy": "Busy", "away": "Away", "snooze": "Snooze"}


class FriendsError(Exception):
    """Something the user can act on (a wrong key, Steam not set up …) — the message says what."""


@dataclass
class Friend:
    steamid: str
    name: str
    state: str = "offline"             # online | busy | away | snooze | offline | unknown (no live data)
    game: str = ""                     # the game they play (its name), "" when none
    gameid: str = ""                   # its Steam game id (a number; non-Steam games have big ones)
    lobby: str = ""                    # a lobby they can be joined in
    server: str = ""                   # the game server they are on (ip:port)
    avatar: str = ""                   # picture URL
    last_online: int = 0               # (offline) when they were last online, unix time

    @property
    def playing(self) -> bool:
        return bool(self.game or self.gameid)

    @property
    def status(self) -> str:
        if self.playing:
            return f"Playing {self.game}" if self.game else "In a game"
        if self.state == "unknown":
            return ""
        return STATE_TEXT.get(self.state, "Online")

    @property
    def joinable(self) -> bool:
        return bool(self.gameid and (self.lobby or self.server))

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(playing=self.playing, status=self.status, joinable=self.joinable)
        return d


def to_steamid64(account_id: int | str) -> str:
    return str(ID_BASE + int(account_id))


def to_account_id(steamid64: str) -> int:
    return int(steamid64) - ID_BASE


# ------------------------------------------------------------------ on this computer
def my_steamid(steam_root: Path | None) -> str | None:
    """The SteamID64 of the account Steam signed in last (``MostRecent``, else the newest ``timestamp``)."""
    if steam_root is None:
        return None
    try:
        users = vdf.text_loads((steam_root / "config" / "loginusers.vdf").read_text("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    users = next((v for k, v in users.items() if k.lower() == "users" and isinstance(v, dict)), {})
    best, best_key = None, None
    for sid, info in users.items():
        if not (isinstance(info, dict) and str(sid).isdigit()):
            continue
        key = (str(info.get("MostRecent") or info.get("mostrecent") or "0") == "1",
               int(str(info.get("Timestamp") or info.get("timestamp") or "0") or 0))
        if best_key is None or key > best_key:
            best, best_key = str(sid), key
    return best


def avatar_url(avatar_hash: str, size: str = "medium") -> str:
    """Steam's picture for an avatar hash ("" for none / the default one)."""
    h = (avatar_hash or "").strip().lower()
    if not h or set(h) == {"0"}:
        return ""
    return f"https://avatars.steamstatic.com/{h}_{size}.jpg"


def local_friends(steam_root: Path | None, steamid64: str | None) -> dict[str, Friend]:
    """The friends Steam keeps on disk for this account (names and pictures; no live status)."""
    if steam_root is None or not steamid64:
        return {}
    path = steam_root / "userdata" / str(to_account_id(steamid64)) / "config" / "localconfig.vdf"
    try:
        data = vdf.text_loads(path.read_text("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
    root = next((v for k, v in data.items() if k.lower() == "userlocalconfigstore" and isinstance(v, dict)), data)
    block = next((v for k, v in root.items() if k.lower() == "friends" and isinstance(v, dict)), {})
    out: dict[str, Friend] = {}
    me = to_account_id(steamid64)
    for acct, info in block.items():
        if not (str(acct).isdigit() and isinstance(info, dict)) or int(acct) == me:
            continue
        name = str(info.get("name") or "").strip()
        if not name:
            continue
        sid = to_steamid64(acct)
        out[sid] = Friend(sid, name, "unknown", avatar=avatar_url(str(info.get("avatar") or "")))
    return out


# ------------------------------------------------------------------ Steam's Web API
async def _get(client, url: str) -> tuple[int, dict]:
    """(status, JSON) — errors never carry the URL (it holds the key)."""
    try:
        res = await client.fetch(url, max_bytes=4 * 1024 * 1024, headers={"Accept": "application/json"})
    except Exception as exc:
        raise FriendsError(f"Steam could not be reached ({type(exc).__name__}).") from None
    if res.status != 200:
        return res.status, {}
    try:
        return 200, json.loads(res.content)
    except ValueError:
        raise FriendsError("Steam sent an answer the app could not read.") from None


async def friend_ids(client, key: str, steamid: str) -> list[str] | None:
    """The account's friends from Steam (``None``: the friends list is private — Steam answers 401)."""
    status, data = await _get(client, f"{API}/GetFriendList/v1/?key={key}&steamid={steamid}&relationship=friend")
    if status == 401:
        return None
    if status == 403:
        raise FriendsError("Steam did not accept the Web API key. Check it in Settings → Steam friends.")
    if status != 200:
        raise FriendsError(f"Steam's friends list answered HTTP {status}.")
    friends = (data.get("friendslist") or {}).get("friends") or []
    return [str(f["steamid"]) for f in friends if isinstance(f, dict) and str(f.get("steamid", "")).isdigit()]


async def summaries(client, key: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), BATCH):
        part = ",".join(ids[i:i + BATCH])
        status, data = await _get(client, f"{API}/GetPlayerSummaries/v2/?key={key}&steamids={part}")
        if status == 403:
            raise FriendsError("Steam did not accept the Web API key. Check it in Settings → Steam friends.")
        if status != 200:
            raise FriendsError(f"Steam's player data answered HTTP {status}.")
        for p in (data.get("response") or {}).get("players") or []:
            if isinstance(p, dict) and p.get("steamid"):
                out[str(p["steamid"])] = p
    return out


def from_summary(p: dict, known: Friend | None = None) -> Friend:
    sid = str(p.get("steamid"))
    state = STATES.get(int(p.get("personastate") or 0), "online")
    return Friend(
        sid, str(p.get("personaname") or (known.name if known else sid)), state,
        game=str(p.get("gameextrainfo") or ""), gameid=str(p.get("gameid") or ""),
        lobby=str(p.get("lobbysteamid") or ""), server=str(p.get("gameserverip") or "").strip(),
        avatar=str(p.get("avatarmedium") or p.get("avatar") or (known.avatar if known else "")),
        last_online=int(p.get("lastlogoff") or 0))


def order(friends: list[Friend]) -> list[Friend]:
    """In a game first, then online, busy / away / snooze, then offline (most recently online first); by name."""
    rank = {"online": 1, "busy": 2, "away": 3, "snooze": 4, "unknown": 5, "offline": 6}
    return sorted(friends, key=lambda f: (0 if f.playing and f.state != "offline" else rank.get(f.state, 5),
                                          -f.last_online if f.state == "offline" else 0, f.name.casefold()))


async def load(client, steam_root: Path | None, key: str) -> dict:
    """Everything the friends views show: {"friends": [dict…], "live": bool, "private": bool, "me": id,
    "self": {"playing", "game", "gameid"} (live only: what Steam shows you doing), "error": "" | why the live part
    is missing, "at": time}."""
    me = my_steamid(steam_root)
    if me is None:
        raise FriendsError("No Steam account found on this computer — sign in to Steam once.")
    known = local_friends(steam_root, me)
    out = {"me": me, "live": False, "private": False, "error": "", "at": time.time()}
    key = (key or "").strip()
    if not key:
        out.update(friends=[f.as_dict() for f in order(list(known.values()))],
                   error="Add your Steam Web API key (Settings → Steam friends) to see who is online.")
        return out
    try:
        ids = await friend_ids(client, key, me)
        if ids is None:                               # private friends list: ask about the friends Steam knows
            out["private"] = True
            ids = list(known)
        data = await summaries(client, key, ids + [me])
    except FriendsError as exc:                       # (no live data: the list Steam keeps, and why)
        out.update(friends=[f.as_dict() for f in order(list(known.values()))], error=str(exc))
        return out
    friends = [from_summary(data[i], known.get(i)) if i in data else known.get(i) or Friend(i, i, "unknown")
               for i in ids if i != me]
    mine = from_summary(data[me]) if me in data else None
    out.update(friends=[f.as_dict() for f in order(friends)], live=True,
               self={"playing": bool(mine and mine.playing), "game": mine.game if mine else "",
                     "gameid": mine.gameid if mine else ""})
    return out


# ------------------------------------------------------------------ Steam's own links
def chat_url(steamid: str) -> str:
    return f"steam://friends/message/{steamid}"


def profile_url(steamid: str) -> str:
    return f"steam://url/SteamIDPage/{steamid}"


def join_url(f: dict) -> str | None:
    """Join the friend's game the way Steam's own "Join game" does (None: nothing joinable)."""
    gid, lobby, server = str(f.get("gameid") or ""), str(f.get("lobby") or ""), str(f.get("server") or "")
    if not gid.isdigit():
        return None
    if lobby.isdigit():
        return f"steam://joinlobby/{gid}/{lobby}/{f['steamid']}"
    if server and server != "0.0.0.0:0":
        return f"steam://connect/{server}?appid={gid}"
    return None


def launch_url(f: dict) -> str | None:
    """Start the game the friend plays (through Steam: only games Steam has)."""
    gid = str(f.get("gameid") or "")
    return f"steam://rungameid/{gid}" if gid.isdigit() else None
