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
    assert c["invite"] == {"kind": "lobbyinvite", "appid": "480", "lobby": "109775241047227400", "connect": ""}
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


def test_the_marker_switches_debugging_on_and_off(tmp_path):
    assert not sb.switched_on(tmp_path)
    sb.switch_on(tmp_path)
    assert (tmp_path / ".cef-enable-remote-debugging").is_file() and sb.switched_on(tmp_path)
    sb.switch_off(tmp_path)
    assert not sb.switched_on(tmp_path)
    assert len(sb.FIRST_TIME) == 2                           # (the first-time note: two lines, no more)
