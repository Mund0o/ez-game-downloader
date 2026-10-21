"""The Steam client bridge: "our overlay" in the middle between the player and Steam's chat and invites.

Steam offers no API for this, so the app talks to the Steam client itself, the way Decky Loader does: with Steam's
debugging interface switched on (the marker file ``<Steam>/.cef-enable-remote-debugging``, read when Steam starts),
Steam's own friends code runs in a page ("SharedJSContext") the app can reach on ``127.0.0.1:8080`` (Chrome DevTools
Protocol). Everything below calls what Steam's own friends list and chat window call — checked in the Steam client
(2026-10): nothing is faked, and no Steam window opens.

* chats: ``g_FriendsUIApp.m_ChatStore.GetFriendChat(accountid)`` — ``m_rgChatMessages`` (unAccountID, rtTimestamp,
  strMessage), ``SendChatMessage(text)`` (the chat box's own send), unread count / acknowledging like opening it
* invite to your game: ``m_FriendStore.InviteToLobby(friend, appid, lobby)`` when you are in a lobby, else
  ``InviteToGame(friend, appid, connect)`` — the friends list's "Invite to Game"
* invites you get: chat messages with ``[lobbyinvite appid lobbyid]`` / ``[gameinvite appid connect]``; joining opens
  the same link Steam's "Accept" opens (``steam://joinlobby/…`` / ``steam://rungame/…``) inside Steam
  (``SteamClient.URL.ExecuteSteamURL``)

Only 127.0.0.1 is ever contacted. The debugging interface is only switched on after the player agreed (Settings →
Our overlay), and the marker is removed when the setting is switched off.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import struct
from pathlib import Path

log = logging.getLogger(__name__)

HOST, PORT = "127.0.0.1", int(os.environ.get("EZGD_STEAM_BRIDGE_PORT") or 8080)   # (tests: never the real Steam)
MARKER = ".cef-enable-remote-debugging"
TARGET = "SharedJSContext"
_INVITE = re.compile(r"\[(lobbyinvite|gameinvite)\b([^\]]*)\]", re.I)
_ATTR = re.compile(r"(\w+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\]]+))")
_TAG = re.compile(r"\[/?[a-z][a-z0-9_]*(?:\s[^\]]*|=[^\]]*)?\]", re.I)


# what the player is told the first time "Our overlay" is switched on (two lines, no more)
FIRST_TIME = ("Our overlay talks to the Steam client directly to show your chats and send invites — nothing pops up.",
              "Steam restarts once to switch this on.")


class BridgeError(Exception):
    """The Steam client cannot be reached (debugging off, Steam not running or not signed in) — the message says."""


# ------------------------------------------------------------------ the marker file
def marker(steam_root: Path | None) -> Path | None:
    return Path(steam_root) / MARKER if steam_root else None


def switched_on(steam_root: Path | None) -> bool:
    m = marker(steam_root)
    return bool(m and m.is_file())


def switch_on(steam_root: Path | None) -> None:
    m = marker(steam_root)
    if m is None:
        raise BridgeError("Steam was not found.")
    m.touch()
    log.info("Steam client bridge: debugging interface switched on for the next Steam start (%s)", m)


def switch_off(steam_root: Path | None) -> None:
    m = marker(steam_root)
    if m is not None and m.is_file():
        m.unlink()
        log.info("Steam client bridge: debugging interface switched off (from the next Steam start)")


# ------------------------------------------------------------------ invite cool-down
class InviteLimiter:
    """A cool-down on invites to the same friend that grows while they keep coming. One invite, and another a
    minute later because the first did not arrive, is never held up (only a press within ``BASE`` seconds — a
    double press or a held button — is). An invite sent before the last cool-down has been over for ``CALM``
    seconds counts as spam: each one doubles the wait (``STEP``, 2×, 4× … up to ``MAX``). Seen on a friend's PC: a
    held button sent 230 invites in 40 seconds."""

    BASE, STEP, CALM, MAX = 3.0, 15.0, 30.0, 300.0

    def __init__(self) -> None:
        self._last: dict[int, tuple[float, int, float]] = {}       # friend → (sent at, strikes, cool-down)

    def wait(self, friend: int, now: float) -> float:
        """Seconds until ``friend`` may be invited again (0: now)."""
        at, _strikes, cool = self._last.get(friend, (0.0, 0, 0.0))
        return max(0.0, at + cool - now) if friend in self._last else 0.0

    def refusal(self, friend: int, now: float) -> str:
        """Why an invite now is held back ("" when it is not)."""
        left = self.wait(friend, now)
        if left <= 0:
            return ""
        if self._last[friend][1] == 0:
            return "Invite already sent."
        return f"Too many invites in a row — wait {max(1, round(left))} s before inviting them again."

    def sent(self, friend: int, now: float) -> None:
        at, strikes, cool = self._last.get(friend, (0.0, 0, 0.0))
        strikes = strikes + 1 if friend in self._last and now - at < cool + self.CALM else 0
        cool = self.BASE if strikes == 0 else min(self.MAX, self.STEP * 2 ** (strikes - 1))
        self._last[friend] = (now, strikes, cool)


# ------------------------------------------------------------------ messages
def clean_text(text: str) -> str:
    """A chat message as plain text (Steam's BBCode tags out; an invite reads as one)."""
    m = _INVITE.search(text or "")
    if m:
        return "Invited you to play" if m.group(1).lower() in ("lobbyinvite", "gameinvite") else ""
    return _TAG.sub("", text or "").strip()


def parse_invite(text: str) -> dict | None:
    """{kind, appid, lobby, connect} from an invite message, else None."""
    m = _INVITE.search(text or "")
    if not m:
        return None
    attrs = {k.lower(): (a if a is not None and a != "" else b if b else c) for k, a, b, c in _ATTR.findall(m.group(2))}
    appid = str(attrs.get("appid") or "")
    lobby = str(attrs.get("lobbyid") or attrs.get("lobby") or "")
    connect = str(attrs.get("connect") or "")
    if not appid.isdigit() or len(appid) > 10 or not safe_connect(connect):
        return None
    return {"kind": m.group(1).lower(), "appid": appid, "lobby": lobby if lobby.isdigit() else "", "connect": connect}


# Steam hands an invite's connect string to the game as its command line ("+connect 1.2.3.4:27015",
# "+connect_lobby 1097…", "-join 5678"). The text comes from whoever wrote the chat message, so only those forms join:
# no other switches (Source's "+exec cfg", "-insecure", a "-game" folder …) and no shell or path characters.
_CONNECT_SWITCH = re.compile(r"^[+-](connect\w*|join\w*|lobby\w*|password|server|ip|port)$", re.I)
_CONNECT_TOKEN = re.compile(r"^[A-Za-z0-9_.:\[\]-]{1,64}$")


# the Steam links the app ever hands to Steam (joining, chats, profiles, starting a game) — nothing else, whoever
# built the text
_ALLOWED_URL = re.compile(r"^steam://(joinlobby/\d+/\d+/\d+|rungame/\d+/\d+(/[A-Za-z0-9%._\-]*)?|"
                          r"connect/[A-Za-z0-9.\-\[\]:]+\?appid=\d+|rungameid/\d+|friends/message/\d+|"
                          r"url/SteamIDPage/\d+)$")


def allowed_url(url: str) -> bool:
    return bool(_ALLOWED_URL.match(url or ""))


def safe_connect(connect: str) -> bool:
    if not connect:
        return True
    tokens = connect.split()
    if len(connect) > 160 or len(tokens) > 6:
        return False
    for t in tokens:
        if t[0] in "+-":
            if not _CONNECT_SWITCH.match(t):
                return False
        elif not _CONNECT_TOKEN.match(t):
            return False
    return True


def join_url(invite: dict, inviter_steamid64: str) -> str:
    """What Steam's own "Accept" opens for an invite."""
    from urllib.parse import quote
    if invite.get("lobby"):
        return f"steam://joinlobby/{invite['appid']}/{invite['lobby']}/{inviter_steamid64}"
    url = f"steam://rungame/{invite['appid']}/{inviter_steamid64}"
    if invite.get("connect"):
        url += "/" + quote(invite["connect"], safe="")
    return url


# ------------------------------------------------------------------ JavaScript (run in Steam's SharedJSContext)
_JS_READY = ("(()=>{const a=window.g_FriendsUIApp;return !!(a&&a.m_FriendStore&&a.m_ChatStore&&a.m_FriendStore.self"
             "&&a.m_CMInterface&&a.m_CMInterface.logged_on)})()")

_JS_STATE = """(()=>{const a=g_FriendsUIApp,fs=a.m_FriendStore,me=fs.self.persona;
const nm=id=>{try{const p=fs.GetPlayerIfCached&&fs.GetPlayerIfCached(id);return p?(p.display_name||''):''}catch(e){return ''}};
const chats=(a.m_ChatStore.m_FriendChatStore.m_rgFriendChats||[]).map(c=>({accountid:c.m_unAccountIDFriend,name:nm(c.m_unAccountIDFriend),
 unread:c.unread_message_count||0,last:c.m_strLastMessage||'',last_from:c.m_accountIDLastMessage||0,
 time:c.m_rtLastMessageReceived||0}));
let connect='';try{connect=me.connect_string||(me.m_mapRichPresence&&me.m_mapRichPresence.get&&me.m_mapRichPresence.get('connect'))||''}catch(e){}
return {self:{accountid:fs.self.accountid,appid:me.m_unGamePlayedAppID||0,lobby:String(me.m_game_lobby_id||'0'),
 connect:String(connect||''),game:me.m_strGameExtraInfo||''},chats:chats}})()"""

# every friend as Steam's own friends list has them: live status, game, lobby, picture — no Web API key needed
_JS_FRIENDS = """(()=>{const fs=g_FriendsUIApp.m_FriendStore;
const one=p=>{const r=p.persona;const g=k=>{try{return p[k]}catch(e){return undefined}};let av='';try{av=r.avatar_url_medium||''}catch(e){}
return {accountid:p.accountid,name:g('display_name')||r.m_strPlayerName||'',state:r.m_ePersonaState||0,
 appid:r.m_unGamePlayedAppID||0,gameid:String(r.m_gameid||'0'),game:g('current_game_name')||r.m_strGameExtraInfo||'',
 lobby:String(r.m_game_lobby_id||'0'),ip:r.m_unGameServerIP||0,port:r.m_unGameServerPort||0,avatar:av,
 last:r.m_rtLastSeenOnline||0}};
const out=[];for(const id of fs.m_setFriendAccountIDs){const p=fs.m_mapPlayerCache.get(id);if(p&&p.is_friend!==false)out.push(one(p))}
return {self:one(fs.self),friends:out}})()"""

_JS_HISTORY = """(async(acct,limit,read)=>{const a=g_FriendsUIApp,c=a.m_ChatStore.GetFriendChat(acct,true);
if(!c)return null;try{await c.LoadChatLogs()}catch(e){}
const m=(c.m_rgChatMessages||[]).slice(-limit).map(x=>({from:x.unAccountID,time:x.rtTimestamp,text:x.strMessage||''}));
if(read&&(c.m_cUnreadChatMessages||c.m_bHasUnreadPriorityChatMessages)){c.m_cUnreadChatMessages=0;
 c.m_bHasUnreadPriorityChatMessages=false;if(c.m_rtLastMessageReceived){c.m_rtLastAckedChatMsg=c.m_rtLastMessageReceived;
 c.AckChatMsg(c.m_rtLastMessageReceived)}}
return {self:a.m_FriendStore.self.accountid,messages:m}})(%s,%d,%s)"""

_JS_SEND = """(async(acct,text)=>{const c=g_FriendsUIApp.m_ChatStore.GetFriendChat(acct,true);if(!c)return 'no-chat';
const r=await c.SendChatMessage(text);return r===undefined||r===0?'ok':String(r)})(%s,%s)"""

_JS_INVITE = """((acct)=>{const fs=g_FriendsUIApp.m_FriendStore,f=fs.GetFriend(acct);if(!f)return 'not-a-friend';
const me=fs.self.persona,app=me.m_unGamePlayedAppID;if(!app)return 'not-in-game';
if(me.m_game_lobby_id&&String(me.m_game_lobby_id)!=='0'){fs.InviteToLobby(f,app,me.m_game_lobby_id);return 'lobby'}
let cs='';try{cs=me.connect_string||(me.m_mapRichPresence&&me.m_mapRichPresence.get&&me.m_mapRichPresence.get('connect'))||''}catch(e){}
if(cs){fs.InviteToGame(f,app,cs);return 'connect'}fs.InviteToGame(f,app,'');return 'game'})(%s)"""

_JS_URL = "(()=>{SteamClient.URL.ExecuteSteamURL(%s);return 'ok'})()"


# ------------------------------------------------------------------ the connection
class Bridge:
    """One connection to Steam's SharedJSContext, opened on first use and reopened when Steam restarts. Used from
    the core's asyncio loop."""

    def __init__(self, host: str = HOST, port: int = PORT):
        self.host, self.port = host, port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._n = 0
        self._lock = asyncio.Lock()

    # ----------------------------------------------------- public
    async def ready(self) -> bool:
        try:
            return bool(await self.eval(_JS_READY))
        except BridgeError:
            return False

    async def state(self) -> dict:
        """Your game (app, lobby, connect string) and every friend chat Steam has: unread count, last message."""
        out = await self.eval(_JS_STATE)
        if not isinstance(out, dict):
            raise BridgeError("Steam did not answer.")
        for c in out.get("chats") or []:
            inv = parse_invite(c.get("last") or "")
            c["invite"] = inv if inv and c.get("last_from") == c.get("accountid") else None
            c["last"] = clean_text(c.get("last") or "")
        return out

    async def friends(self) -> dict:
        """{"self": {…}, "friends": [{accountid, name, state (Steam's persona state number), appid, gameid, game,
        lobby, ip, port, avatar, last}]} — the Steam client's own friends list, live."""
        out = await self.eval(_JS_FRIENDS, timeout=15)
        if not isinstance(out, dict) or not isinstance(out.get("friends"), list):
            raise BridgeError("Steam did not answer.")
        return out

    async def history(self, accountid: int, limit: int = 40, mark_read: bool = True) -> list[dict]:
        out = await self.eval(_JS_HISTORY % (int(accountid), int(limit), "true" if mark_read else "false"),
                              timeout=15)
        if not isinstance(out, dict):
            raise BridgeError("That chat could not be opened.")
        me = out.get("self")
        msgs = []
        for m in out.get("messages") or []:
            text = m.get("text") or ""
            msgs.append({"mine": m.get("from") == me, "time": int(m.get("time") or 0), "text": clean_text(text),
                         "invite": parse_invite(text)})
        return msgs

    async def send(self, accountid: int, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        r = await self.eval(_JS_SEND % (int(accountid), json.dumps(text[:4000])), timeout=15)
        if r != "ok":
            raise BridgeError(f"Steam did not send the message ({r}).")

    async def invite(self, accountid: int) -> str:
        """lobby | connect | game: what was sent; raises when there is nothing to invite to."""
        r = await self.eval(_JS_INVITE % int(accountid))
        if r == "not-in-game":
            raise BridgeError("Steam does not show you in a game right now — start the game first.")
        if r == "not-a-friend":
            raise BridgeError("Only friends can be invited.")
        if r not in ("lobby", "connect", "game"):
            raise BridgeError(f"Steam did not send the invite ({r}).")
        return r

    async def open_url(self, url: str) -> None:
        if not allowed_url(url):
            raise BridgeError("Only Steam's join, chat and profile links.")
        await self.eval(_JS_URL % json.dumps(url))

    async def close(self) -> None:
        w, self._writer, self._reader = self._writer, None, None
        if w is not None:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass

    # ----------------------------------------------------- CDP
    async def eval(self, expression: str, timeout: float = 6.0):
        async with self._lock:
            for attempt in (1, 2):                    # (Steam restarted: the old connection is gone — once more)
                try:
                    if self._writer is None:
                        await self._connect()
                    return await asyncio.wait_for(self._evaluate(expression), timeout)
                except (OSError, asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError) as exc:
                    await self.close()
                    if attempt == 2:
                        raise BridgeError("The Steam client is not reachable (is Steam running, with our overlay "
                                          "set up?).") from exc
        return None

    async def _http_json(self, path: str):
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 2)
        try:
            writer.write(f"GET {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)).lower()
            m = re.search(rb"content-length:\s*(\d+)", head)
            if m:                                        # (the server keeps the connection open: read what it sent)
                body = await asyncio.wait_for(reader.readexactly(int(m.group(1))), 3)
            else:
                body = await asyncio.wait_for(reader.read(), 3)
                if b"chunked" in head:
                    body = _unchunk(body)
        finally:
            writer.close()
        return json.loads(body.decode("utf-8", "replace"))

    async def _connect(self) -> None:
        targets = await self._http_json("/json")
        t = next((t for t in targets if t.get("title") == TARGET and "webSocketDebuggerUrl" in t
                  and str(t.get("url", "")).startswith("https://steamloopback.host/")), None)
        if t is None:
            raise ConnectionError("Steam's friends page is not there yet")
        url = t["webSocketDebuggerUrl"]
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 2)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((f"GET {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            writer.close()
            raise ConnectionError("Steam refused the connection")
        self._reader, self._writer = reader, writer

    async def _evaluate(self, expression: str):
        self._n += 1
        n = self._n
        await self._send({"id": n, "method": "Runtime.evaluate",
                          "params": {"expression": expression, "returnByValue": True, "awaitPromise": True}})
        while True:
            msg = await self._recv()
            if msg.get("id") != n:
                continue                              # (events, or an answer to a call that timed out)
            res = msg.get("result") or {}
            if "exceptionDetails" in res:
                d = res["exceptionDetails"]
                text = (d.get("exception") or {}).get("description") or d.get("text") or "error"
                log.info("Steam client bridge: script failed: %s", str(text)[:300])
                raise BridgeError("Steam's friends code changed — this needs an app update.")
            return (res.get("result") or {}).get("value")

    async def _send(self, obj: dict) -> None:
        assert self._writer is not None
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self._writer.write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        await self._writer.drain()

    async def _recv(self) -> dict:
        assert self._reader is not None
        payload = b""
        while True:
            b1, b2 = await self._reader.readexactly(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", await self._reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", await self._reader.readexactly(8))[0]
            mask = await self._reader.readexactly(4) if b2 & 0x80 else b""
            data = await self._reader.readexactly(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b1 & 0x0F
            if op == 0x8:
                raise ConnectionError("Steam closed the connection")
            if op == 0x9:                               # ping → pong
                await self._pong(data)
                continue
            if op == 0xA:
                continue
            payload += data
            if b1 & 0x80:
                return json.loads(payload.decode("utf-8", "replace"))

    async def _pong(self, data: bytes) -> None:
        assert self._writer is not None
        mask = os.urandom(4)
        self._writer.write(bytes([0x8A, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        await self._writer.drain()


def _unchunk(body: bytes) -> bytes:
    out = b""
    while body:
        size, _, rest = body.partition(b"\r\n")
        n = int(size.split(b";")[0] or b"0", 16)
        if n == 0:
            break
        out += rest[:n]
        body = rest[n + 2:]
    return out
