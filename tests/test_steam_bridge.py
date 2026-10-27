"""Our overlay's Steam client bridge: Steam's debugging interface (Chrome DevTools Protocol on 127.0.0.1) answered by a
fake Steam here — the real one was checked by hand (2026-10-20): chats, sending, Steam's own Invite to Game."""

import asyncio
import base64
import hashlib
import json
import struct

import pytest

from ezgd.platform import steambridge as sb

ME, BUDDY = 1001, 2002


class FakeSteam:
    """HTTP /json + one WebSocket that answers Runtime.evaluate like Steam's SharedJSContext would."""

    def __init__(self):
        self.calls: list[str] = []
        self.server = None
        self.port = 0

    def answer(self, expr: str):
        self.calls.append(expr)
        if "logged_on" in expr:
            return True
        if "m_rgFriendChats" in expr and "GetPlayerIfCached" in expr:
            return {"self": {"accountid": ME, "appid": 480, "lobby": "109775241047227397", "connect": "", "game": ""},
                    "chats": [{"accountid": BUDDY, "name": "Buddy", "unread": 2, "time": 1700000000,
                               "last": '[lobbyinvite lobbyid="109775241047227400" appid="480"][/lobbyinvite]',
                               "last_from": BUDDY}]}
        if "LoadChatLogs" in expr:
            return {"self": ME, "messages": [{"from": BUDDY, "time": 1, "text": "hey [b]you[/b]"},
                                             {"from": ME, "time": 2, "text": "hi!"},
                                             {"from": BUDDY, "time": 3,
                                              "text": "[gameinvite appid=\"480\" connect=\"+connect 1.2.3.4\"][/gameinvite]"}]}
        if "SendChatMessage" in expr:
            return "ok"
        if "InviteToLobby" in expr:
            return "lobby"
        if "ExecuteSteamURL" in expr:
            return "ok"
        return None

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        if head.startswith("GET /json"):
            body = json.dumps([{"title": "SharedJSContext", "url": "https://steamloopback.host/index.html",
                                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/X"}]).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                         % len(body) + body)      # (keeps the connection open, as Steam's does)
            await writer.drain()
            await asyncio.sleep(5)
            return
        key = next(l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key"))
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        try:
            while True:
                b1, b2 = await reader.readexactly(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", await reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await reader.readexactly(8))[0]
                mask = await reader.readexactly(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(n)))
                msg = json.loads(data)
                value = self.answer(msg["params"]["expression"])
                out = json.dumps({"id": msg["id"], "result": {"result": {"type": "object", "value": value}}}).encode()
                hdr = bytes([0x81]) + (bytes([len(out)]) if len(out) < 126 else bytes([126]) + struct.pack(">H", len(out))
                                       if len(out) < 65536 else bytes([127]) + struct.pack(">Q", len(out)))
                writer.write(hdr + out)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


@pytest.fixture
async def steam():
    s = FakeSteam()
    await s.start()
    yield s
    s.server.close()


async def test_chats_history_send_invite_and_join_through_the_client(steam):
    b = sb.Bridge(port=steam.port)
    assert await b.ready()
    st = await b.state()
    [c] = st["chats"]
    assert c["unread"] == 2 and c["name"] == "Buddy" and c["last"] == "Invited you to play"
    assert c["invite"] == {"kind": "lobbyinvite", "appid": "480", "lobby": "109775241047227400", "connect": "",
                           "remoteplay": ""}
    msgs = await b.history(BUDDY, 30)
    assert [m["mine"] for m in msgs] == [False, True, False] and msgs[0]["text"] == "hey you"
    assert msgs[2]["invite"]["connect"] == "+connect 1.2.3.4"
    await b.send(BUDDY, 'he said "hi"; </script>')
    sent = steam.calls[-1]
    assert json.dumps('he said "hi"; </script>') in sent and f"({BUDDY}," in sent    # (data, never code)
    assert await b.invite(BUDDY) == "lobby"
    await b.open_url("steam://joinlobby/480/1/2")
    with pytest.raises(sb.BridgeError):
        await b.open_url("https://example.com")
    await b.close()


async def test_nothing_listening_is_a_clear_error():
    b = sb.Bridge(port=1)                                   # (nothing there)
    assert not await b.ready()
    with pytest.raises(sb.BridgeError, match="not reachable"):
        await b.state()


def test_invite_links_are_steams_own_accept():
    inv = sb.parse_invite('[lobbyinvite lobbyid="5" appid="480"][/lobbyinvite]')
    assert sb.join_url(inv, "76561198000000001") == "steam://joinlobby/480/5/76561198000000001"
    inv = sb.parse_invite("[gameinvite appid=480 connect='+connect 1.2.3.4:27015'][/gameinvite]")
    assert sb.join_url(inv, "7656") == "steam://rungame/480/7656/%2Bconnect%201.2.3.4%3A27015"
    assert sb.parse_invite("just text") is None and sb.clean_text("[url=x]link[/url] :steamhappy:") == "link :steamhappy:"


def test_invites_as_steam_writes_them_and_what_can_be_joined():
    """The invites in a real Steam chat (2026-10): a lobby invite joins like Steam's own chat; a game invite without a
    connect string, lobby or Remote Play session has no Join in Steam's chat — the app's used to start Spacewar (a
    friend pressed it four times). Steam names a game invite's parts connectString / lobbyID / remoteplay."""
    lobby = sb.parse_invite('[lobbyinvite  appid="480" lobbyid="109775241575688370"][/lobbyinvite]')
    assert sb.joinable(lobby)
    assert sb.join_url(lobby, "7656") == "steam://joinlobby/480/109775241575688370/7656"
    empty = sb.parse_invite('[gameinvite  appid="480"][/gameinvite]')
    assert empty is not None and not sb.joinable(empty) and sb.join_url(empty, "7656") == ""
    cs = sb.parse_invite('[gameinvite appid="730" connectString="+connect 10.0.0.5:27015"][/gameinvite]')
    assert sb.joinable(cs) and sb.join_url(cs, "7656") == "steam://rungame/730/7656/%2Bconnect%2010.0.0.5%3A27015"
    lid = sb.parse_invite('[gameinvite appid="1903340" lobbyID="109775241692069606"][/gameinvite]')
    assert sb.join_url(lid, "7656") == "steam://joinlobby/1903340/109775241692069606/7656"
    rp = sb.parse_invite('[gameinvite appid="413150" remoteplay="token=abc123&tok2=x"][/gameinvite]')
    url = sb.join_url(rp, "7656")
    assert url == "steam://remoteplay/connect/7656?appid=413150&token=abc123&tok2=x" and sb.allowed_url(url)
    bad = sb.parse_invite('[gameinvite appid="413150" remoteplay="a;rm -rf /"][/gameinvite]')
    assert not sb.joinable(bad)                                     # (a friend's text: only plain tokens)


async def test_no_empty_invites_like_steams_own_friends_list(steam):
    """Steam's friends list offers Invite only with a lobby or a connect string (its private one first); with
    neither the app used to send an empty invite nobody could join — it now says why instead."""
    b = sb.Bridge(port=steam.port)
    steam.answer = lambda expr: "nothing" if "GetPrivateConnectString" in expr else None
    with pytest.raises(sb.BridgeError, match="no lobby"):
        await b.invite(BUDDY)
    await b.close()


def test_the_marker_switches_debugging_on_and_off(tmp_path):
    assert not sb.switched_on(tmp_path)
    sb.switch_on(tmp_path)
    assert (tmp_path / ".cef-enable-remote-debugging").is_file() and sb.switched_on(tmp_path)
    sb.switch_off(tmp_path)
    assert not sb.switched_on(tmp_path)
    assert len(sb.FIRST_TIME) == 2                           # (the first-time note: two lines, no more)


def test_invite_cool_down_grows_only_with_spam():
    """A held button / mashing: each invite before the last cool-down has calmed down doubles the wait. One invite
    and another a minute later (the first did not arrive) never wait; only a double press within 3 s is held."""
    lim = sb.InviteLimiter()
    assert lim.refusal(7, 0) == ""
    lim.sent(7, 0)
    assert lim.refusal(7, 1) == "Invite already sent."
    assert lim.refusal(8, 1) == ""                       # (another friend: no wait)
    lim.sent(7, 3)                                       # spam: 15 s
    assert "wait 15 s" in lim.refusal(7, 3)
    lim.sent(7, 18)
    assert "wait 30 s" in lim.refusal(7, 18)
    t = 18
    for want in (60, 120, 240, 300, 300):                # … up to five minutes
        t += lim.wait(7, t)
        lim.sent(7, t)
        assert lim.wait(7, t) == want
    calm = sb.InviteLimiter()                            # one a minute because the first failed: never held up
    for k in range(5):
        assert calm.refusal(9, k * 60) == ""
        calm.sent(9, k * 60)
    assert calm.wait(9, 4 * 60 + 4) == 0


def test_core_holds_back_spammed_invites(monkeypatch):
    import types
    from ezgd.core.app_core import AppCore
    sent = []

    class FakeBridge:
        async def invite(self, acct):
            sent.append(acct)
            return "lobby"
    core = types.SimpleNamespace(_bridge=lambda: FakeBridge())
    invite = AppCore.steam_invite.__get__(core)
    assert asyncio.run(invite(5)) == "lobby"
    with pytest.raises(RuntimeError, match="already sent"):
        asyncio.run(invite(5))
    assert sent == [5]
