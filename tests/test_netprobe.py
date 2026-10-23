"""Online play (ezgd.library.netprobe): the game's ports, the firewall's blocks of exactly those ports, Wine's TLS
errors, Steam for OnlineFix — and the smaller checks: screen mode / HDR, many CPU cores."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ezgd.library import diagnose as dg
from ezgd.library import fixgen, netprobe, probes
from ezgd.library.models import LibraryEntry, LibraryStatus
from pe_builder import build_pe

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc, the journal")


def ufw(src: str, dst: str, dpt: int, proto: str = "UDP") -> dict:
    """A real UFW log line, as this computer's kernel writes it."""
    return {"MESSAGE": f"[UFW BLOCK] IN=enp3s0 OUT= MAC=d8:43:ae:20:08:d8:a0:91:ca:19:25:21:08:00 SRC={src} DST={dst} "
                       f"LEN=527 TOS=0x00 PREC=0x00 TTL=64 ID=37860 DF PROTO={proto} SPT=42750 DPT={dpt} LEN=507",
            "__REALTIME_TIMESTAMP": "1", "_TRANSPORT": "kernel"}


def test_the_games_sockets_from_proc(tmp_path):
    proc = tmp_path / "proc"
    fd = proc / "4242" / "fd"
    fd.mkdir(parents=True)
    (fd / "7").symlink_to("socket:[555]")
    (fd / "8").symlink_to("socket:[556]")
    (fd / "9").symlink_to("/dev/null")
    net = proc / "4242" / "net"
    net.mkdir()
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (net / "udp").write_text(head + "   1: 00000000:6987 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000 "
                                    "       0 555 2 0000000000000000 0\n")
    (net / "tcp").write_text(head + "   1: 0100007F:1F90 0100007F:D2F0 01 00000000:00000000 00:00000000 00000000  1000"
                                    "        0 556 1 0000000000000000 20 4 30 10 -1\n")
    socks = netprobe.sockets({4242}, proc)
    assert {(s["proto"], s["port"]) for s in socks} == {("udp", 27015), ("tcp", 8080)}
    assert netprobe.listening(socks) == {("udp", 27015)}            # (the TCP socket is a connection, not a listener)


def test_only_blocks_of_the_games_ports_count():
    entries = [ufw("37.30.54.48", "192.168.1.88", 27015), ufw("37.30.54.49", "192.168.1.88", 27015),
               ufw("192.168.1.1", "224.0.0.1", 27015),               # multicast chatter
               ufw("192.168.1.144", "239.255.255.250", 15600),
               ufw("37.30.54.48", "192.168.1.88", 6880)]             # another program's port
    got = netprobe.blocks(entries, {("udp", 27015)})
    assert got == [{"proto": "udp", "port": 27015, "packets": 2, "sources": 2}]
    assert netprobe.allow_command("UFW", got[0]) == "sudo ufw allow 27015/udp"
    assert netprobe.allow_command("firewalld", got[0]) == "sudo firewall-cmd --add-port=27015/udp"


def online(monkeypatch, text="", steam=True, found=(), ports=(("udp", 27015),), connections=2):
    monkeypatch.setattr(netprobe, "firewall", lambda: "UFW")
    monkeypatch.setattr(netprobe, "steam_running", lambda proc_root=None: steam)
    monkeypatch.setattr(netprobe, "read_blocks", lambda since, until, p: (list(found), True))
    p = probes.Probe("online")
    p.samples, p.ports, p.connections = 1, set(ports), connections
    return p.result(text)


def test_what_the_online_check_tells_apart(monkeypatch):
    of = '0134:trace:loaddll:build_module Loaded L"Z:\\\\g\\\\OnlineFix64.dll" at 0000000180000000: native\n'
    assert online(monkeypatch, of, steam=False)["cause"] == "steam-off"
    r = online(monkeypatch, found=[{"proto": "udp", "port": 27015, "packets": 9, "sources": 3}])
    assert r["cause"] == "firewall" and r["command"] == "sudo ufw allow 27015/udp" and "UDP port 27015" in r["detail"]
    tls = "0150:err:secur32:schan_InitializeSecurityContextW Handshake failed: -110\n"
    r = online(monkeypatch, tls)
    assert r["cause"] == "tls" and "Handshake failed" in r["detail"]
    assert online(monkeypatch, ports=(), connections=0)["ok"] is None
    assert online(monkeypatch)["ok"] is True


def _diagnose(tmp_path: Path, body: str, after_exit: str = "", options=()):
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    exe = game / "Game.exe"
    exe.write_bytes(build_pe(["kernel32.dll"]))
    log = tmp_path / "game-1.log"
    log.write_text(f"\n=== 2026-10-23 10:00:00 GE-Proton\n{body}=== exited with code 0 after 1800s\n{after_exit}")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(exe), install_path=str(game),
                     compat={"options": list(options)})
    return {f["key"]: f for f in dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="").to_dict()["findings"]}


def test_diagnose_shows_a_firewall_that_turned_friends_away(tmp_path):
    # the session's blocks are added after it ended (after the exit line)
    mark = netprobe.marker({"kind": "session", "firewall": "UFW", "ports": ["27015/udp"],
                            "blocks": [{"proto": "udp", "port": 27015, "packets": 12, "sources": 2}]})
    keys = _diagnose(tmp_path, "", mark)
    f = keys["net-firewall"]
    assert f["title"] == "UFW blocked connections to the game" and f["fix"]["value"] == "sudo ufw allow 27015/udp"
    tls = probes.marker({"symptom": "online", "ok": False, "cause": "tls", "detail": "its secure (TLS) connections "
                         "failed under Wine", "facts": {"net_errors": ["err:secur32:schan Handshake failed"]}})
    assert "wine-tls" in _diagnose(tmp_path, tls)      # ("wine…": “Fix my game” then tries other Protons)


def test_screen_mode_and_hdr(tmp_path):
    keys = _diagnose(tmp_path, "0024:err:winewayland:ChangeDisplaySettingsExW failed: DISP_CHANGE_BADMODE\n")
    assert "display-mode" in keys
    keys = _diagnose(tmp_path, "info:  DXVK: HDR is not supported by the swapchain's output\n")
    assert "Native Wayland" in keys["display-hdr"]["explanation"]
    assert "on for this game" in _diagnose(tmp_path, "Failed to enable HDR\n", options=["wayland"])["display-hdr"][
        "explanation"]


def test_the_cpu_core_fix_follows_the_log(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 12)
    ctx = SimpleNamespace(everything="Fatal: Failed to create worker thread (too many threads)", entry=None)
    got = fixgen._cpu_cores(ctx, "", [], False)
    assert got and "Failed to create worker thread" in got[0].why          # 12 threads: only on evidence
    assert fixgen._cpu_cores(SimpleNamespace(everything="all good"), "", [], False) == []
