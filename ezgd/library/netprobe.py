"""Online play: what a game's connections did, and whether this computer's firewall turned anyone away.

OnlineFix releases are mostly about playing together, yet nothing looked at the network: a friend who cannot join, a
game whose secure connection fails under Wine, a firewall dropping the packets meant for the game — none of it shows
in the game's log. Here:

* the ports a game listens on, read from its processes' sockets (``/proc/<pid>/fd`` → ``/proc/<pid>/net/*``) — while
  it is played (sampled with the freeze watch) and during a test run;
* the firewall's own log lines (UFW's ``[UFW BLOCK]``, any netfilter ``LOG`` line with ``SRC=… DPT=…``) for exactly
  those ports during the session — broadcast and multicast noise left out;
* Wine's network and TLS errors (winsock, winhttp, schannel / gnutls) in the run's log.

What concerns the game becomes ``ezgd:net {...}`` lines in its log; Diagnose reads them like the system log's
(ezgd.library.syslog). The firewall is never changed by the app: the rule that would let the game through is offered
as a command to copy (it needs administrator rights, and it is the user's security decision).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MARK = "ezgd:net "
_STATES_LISTEN = {"0A"}                      # TCP_LISTEN (/proc/net/tcp "st")
_NET_ERRORS = re.compile(r"^[0-9a-f]+:(?:err|warn):(?:winsock|ws2_32|wininet|winhttp|secur32|schannel|bcrypt|"
                         r"crypt32|dnsapi|iphlpapi|nsi)[^\n]*|[^\n]*(?:gnutls|SSL handshake|certificate verify|"
                         r"TLS (?:handshake|alert))[^\n]*", re.I | re.M)
_TLS = re.compile(r"schan|secur32|gnutls|ssl|tls|certificate", re.I)
_BLOCK = re.compile(r"(?:\[UFW BLOCK\]|REJECT|DROP|DENIED|filter_IN)[^\n]*?\bSRC=(\S+) DST=(\S+)[^\n]*?\bPROTO=(\w+)"
                    r"(?:[^\n]*?\bSPT=(\d+) DPT=(\d+))?", re.I)


def _port(hexaddr: str) -> int:
    try:
        return int(hexaddr.rsplit(":", 1)[1], 16)
    except (IndexError, ValueError):
        return 0


def sockets(pids: set[int], proc_root: Path = Path("/proc")) -> list[dict]:
    """The sockets the processes hold: {proto (tcp|udp), port, state, remote_port}."""
    inodes: set[str] = set()
    for pid in pids:
        try:
            for fd in (proc_root / str(pid) / "fd").iterdir():
                try:
                    t = os.readlink(fd)
                except OSError:
                    continue
                if t.startswith("socket:["):
                    inodes.add(t[8:-1])
        except OSError:
            continue
    if not inodes:
        return []
    out = []
    any_pid = next((p for p in pids if (proc_root / str(p) / "net").is_dir()), None)
    if any_pid is None:
        return []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        try:
            lines = (proc_root / str(any_pid) / "net" / proto).read_text().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if len(f) < 10 or f[9] not in inodes:
                continue
            out.append({"proto": proto[:3], "port": _port(f[1]), "state": f[3], "remote_port": _port(f[2])})
    return out


def listening(socks: list[dict]) -> set[tuple[str, int]]:
    """The ports others can reach the game on: TCP listeners and every bound UDP port."""
    return {(s["proto"], s["port"]) for s in socks
            if s["port"] and (s["proto"] == "udp" or s["state"] in _STATES_LISTEN)}


def firewall() -> str:
    """The active firewall's name ("" when none known is running)."""
    exe = shutil.which("systemctl")
    if not exe:
        return ""
    for unit, name in (("ufw", "UFW"), ("firewalld", "firewalld")):
        try:
            r = subprocess.run([exe, "is-active", unit], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.stdout.strip() == "active":
            return name
    return ""


def _noise(dst: str) -> bool:
    """Broadcast / multicast traffic (discovery chatter on the network) — not someone trying to reach the game."""
    try:
        ip = ipaddress.ip_address(dst)
    except ValueError:
        return True
    return ip.is_multicast or dst.endswith(".255") or dst == "255.255.255.255"


def blocks(entries: list[dict], ports: set[tuple[str, int]]) -> list[dict]:
    """Firewall log lines (journal entries) aimed at the game's ports → [{proto, port, packets, sources}]."""
    found: dict[tuple[str, int], dict] = {}
    for e in entries:
        msg = e.get("MESSAGE", "")
        if isinstance(msg, list):
            msg = bytes(x & 0xFF for x in msg if isinstance(x, int)).decode("utf-8", "replace")
        m = _BLOCK.search(str(msg))
        if not m or not m.group(5) or _noise(m.group(2)):
            continue
        key = (m.group(3).lower(), int(m.group(5)))
        if key not in ports:
            continue
        b = found.setdefault(key, {"proto": key[0], "port": key[1], "packets": 0, "sources": set()})
        b["packets"] += 1
        b["sources"].add(m.group(1))
    return [{**b, "sources": len(b["sources"])} for b in sorted(found.values(), key=lambda x: -x["packets"])]


def read_blocks(since: float, until: float, ports: set[tuple[str, int]]) -> tuple[list[dict], bool]:
    """The firewall's blocks of the game's ports between ``since`` and ``until`` → (blocks, readable)."""
    if os.environ.get("EZGD_SYSLOG", "1") == "0" or not ports:
        return [], True
    from ezgd.library import syslog
    entries, readable = syslog._journal(since - 2, until + 2)
    if not readable:
        entries, readable = syslog._dmesg(since - 2, until + 2)
    return blocks(entries, ports), readable


def net_errors(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).strip()[:200] for m in _NET_ERRORS.finditer(text)))[:4]


def steam_running(proc_root: Path = Path("/proc")) -> bool:
    """The Steam client runs (OnlineFix connects through it)."""
    for d in proc_root.iterdir():
        if d.name.isdigit():
            try:
                if (d / "comm").read_text().strip() in ("steam", "steamwebhelper"):
                    return True
            except OSError:
                continue
    return False


def allow_command(fw: str, b: dict) -> str:
    """The firewall rule that lets the game's port through (offered, never run by the app)."""
    if fw == "firewalld":
        return f"sudo firewall-cmd --add-port={b['port']}/{b['proto']}"
    return f"sudo ufw allow {b['port']}/{b['proto']}"


def marker(d: dict) -> str:
    return MARK + json.dumps(d, sort_keys=True) + "\n"


def from_text(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"^" + re.escape(MARK) + r"(\{.*\})\s*$", text, re.M):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return out


def is_tls(errors: list[str]) -> bool:
    return any(_TLS.search(e) for e in errors)
