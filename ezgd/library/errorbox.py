"""Error messages a game shows in a pop-up (a Windows message box) — read from Wine's log.

With Wine's ``msgbox`` debug channel on, every message box a program opens is logged with its text
(``trace:msgbox:MSGBOX_OnInit L"VCRUNTIME140.dll was not found…"``) — whether or not anyone can see the window. Game
launches log that channel, so the debugging assistant can read exactly what the game said instead of guessing, and
test runs end as soon as a game stops on an error box (closing it) instead of waiting for the test's time to run out.
"""

from __future__ import annotations

import re

# a normal launch: message boxes, and which DLLs were loaded from where (a few hundred lines a start — what the
# investigator needs to explain a crash while playing: whose loader was replaced, which module a crash dump names)
PLAY_WINEDEBUG = "-all,+msgbox,+loaddll"

_LINE = re.compile(r"trace:msgbox:MSGBOX_OnInit L?\"(.*?)\"(\.\.\.)?\s*$", re.M)
_ESC = re.compile(r"\\(x[0-9a-fA-F]{2,4}|u[0-9a-fA-F]{4}|.)")


def _unescape(s: str) -> str:
    def one(m: re.Match) -> str:
        e = m.group(1)
        if e[0] in "xu" and len(e) > 1:
            try:
                return chr(int(e[1:], 16))
            except ValueError:
                return e
        return {"n": "\n", "r": "", "t": " ", "0": ""}.get(e, e)
    return _ESC.sub(one, s)


# boxes that are not errors: the game goes on once they are closed. OnlineFix greets once ("The fix is made by
# online-fix.me. Please notice that the only trusted sites are online-fix.me and cs.rin.ru. You will get this message
# only once. Enjoy :)") — Expedition 33's test runs were stopped on it and counted as crashes.
_NOTICE = re.compile(r"fix is made by online-fix\.me|only trusted sites are online-fix\.me", re.I)


def is_notice(msg: str) -> bool:
    return bool(_NOTICE.search(msg or ""))


def messages(text: str, limit: int = 5, notices: bool = False) -> list[str]:
    """The texts of the message boxes in a Wine log, oldest first (each once) — errors only, unless ``notices``."""
    out: list[str] = []
    for m in _LINE.finditer(text or ""):
        msg = " ".join(_unescape(m.group(1)).split())
        if m.group(2):
            msg += "…"
        if not notices and is_notice(msg):
            continue
        if msg and msg not in out:
            out.append(msg)
            if len(out) >= limit:
                break
    return out


def shown(text: str) -> bool:
    """Has an error box opened (for the live watch of a test run)? Notices (is_notice) do not count."""
    return "trace:msgbox:MSGBOX_OnInit" in (text or "") and bool(messages(text, 1))


def notice_shown(text: str) -> bool:
    """Has a harmless notice opened (it waits for OK: the run is not getting anywhere until it is closed)?"""
    return any(is_notice(m) for m in messages(text, 20, notices=True))


# what a message says → (key, what it means, a fix: (kind, value, label) or None)
_KNOWN: list[tuple[str, str, str, tuple[str, str, str] | None]] = [
    (r"(vcruntime|msvcp|msvcr|vcomp)1[0-4]\d?(_\d)?\.dll|Visual C\+\+", "vcrun",
     "a Microsoft Visual C++ runtime is missing", ("winetricks", "vcrun2022", "Install the Visual C++ runtimes")),
    (r"d3dx9_\d+\.dll|DirectX (End-User )?Runtime|d3dx10|d3dx11", "d3dx",
     "an old DirectX runtime (d3dx) is missing", ("winetricks", "d3dx9 d3dx10 d3dx11_43",
                                                  "Install the DirectX runtimes")),
    (r"xinput1_[34]\.dll", "xinput", "XInput (controller support) is missing", ("winetricks", "xinput",
                                                                               "Install XInput")),
    (r"d3dcompiler_4[37]\.dll", "d3dcompiler", "the DirectX shader compiler is missing",
     ("winetricks", "d3dcompiler_47", "Install the shader compiler")),
    (r"\.NET (Framework|Runtime|Desktop Runtime)|mscoree|hostfxr", "dotnet", "the game asks for .NET",
     ("winetricks", "dotnet48", "Install .NET Framework 4.8")),
    (r"(DirectX|D3D|DX) ?12.*(not supported|unsupported|failed|requires)|"
     r"(requires|needs).*(DirectX|D3D) ?12|D3D12 .*(error|fail)", "dx12",
     "DirectX 12 did not start", ("add_args", "-dx11", "Start with DirectX 11")),
    (r"feature level|DXGI_ERROR|graphics (card|device|driver).*(not supported|failed|unsupported)", "gpu",
     "the graphics device could not be set up", ("add_args", "-dx11", "Start with DirectX 11")),
    (r"Vulkan.*(not (found|supported|available)|failed|error)|vkCreate", "vulkan",
     "Vulkan could not start", None),
    (r"Steam (must be running|is not running|client)|SteamAPI|Failed to initiali[sz]e Steam|steam_api", "steam",
     "the game could not connect to Steam (or its Steam emulator)", None),
    (r"(EasyAntiCheat|BattlEye|anti-?cheat)", "anticheat", "the anti-cheat refused to run", None),
    (r"(out of (video )?memory|not enough memory|insufficient memory)", "memory", "the game ran out of memory",
     None),
    (r"(AVX2?|SSE4\.\d|instruction set).*(not supported|required|requires)", "cpu",
     "the CPU lacks an instruction set the game needs", None),
]


def explain(msg: str) -> tuple[str, str, tuple[str, str, str] | None]:
    """(key, what it means, fix or None) for one message's text."""
    for rx, key, what, fix in _KNOWN:
        if re.search(rx, msg, re.I):
            return key, what, fix
    return "other", "", None
