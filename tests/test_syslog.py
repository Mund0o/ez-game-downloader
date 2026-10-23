"""What the system logged about a game's run (ezgd.library.syslog): a game the kernel killed for memory, a GPU fault
under it, a crash Wine could not catch — read from the journal, kept as markers in the run's log, followed by the
investigator and shown by Diagnose."""

import subprocess
import sys
from pathlib import Path

import pytest

from ezgd.library import crashes, syslog, tracer
from ezgd.library import diagnose as dg
from ezgd.library.models import LibraryEntry, LibraryStatus
from pe_builder import build_pe

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux journal")

AT = "1792758576000000"


def k(msg: str, **kw) -> dict:
    return {"MESSAGE": msg, "__REALTIME_TIMESTAMP": AT, "_TRANSPORT": "kernel", **kw}


OOM = k("Out of memory: Killed process 4242 (Slay the Spire ) total-vm:30000000kB, anon-rss:15728640kB, "
        "file-rss:0kB, shmem-rss:0kB, UID:1000 pgtables:40000kB oom_score_adj:0")
XID13 = k("NVRM: Xid (PCI:0000:01:00): 13, pid=4242, name=Slay the Spire , Graphics Exception: ESR 0x404600")


def test_the_journal_lines_that_concern_a_run():
    entries = [
        OOM, XID13,
        k("NVRM: Xid (PCI:0000:01:00): 79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus."),
        k("amdgpu 0000:03:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=1234, emitted seq=1236"),
        k("amdgpu 0000:03:00.0: amdgpu:  Process information: process Game.exe pid 777 thread Game.exe pid 777"),
        k("i915 0000:00:02.0: [drm] GPU HANG: ecode 12:1:85dffffb, in Game.exe [777]"),
        k("Game.exe[777]: segfault at 0 ip 00007f1234 sp 00007ffd error 4 in libnvidia-glcore.so.615[7f00+2000]"),
        k("traps: Game.exe[777] trap invalid opcode ip:7f12 sp:7ffd error:0 in libfoo.so[7f00+100]"),
        k("wineserver[999]: segfault at 0 ip 0 sp 0 error 4 in wineserver[5555]"),      # another Wine program's
        k("Out of memory: Killed process 31337 (firefox) total-vm:1kB, anon-rss:1kB"),   # not the game
        {"MESSAGE": "Process 777 (Game.exe) of user 1000 dumped core.", "SYSLOG_IDENTIFIER": "systemd-coredump",
         "COREDUMP_PID": "777", "COREDUMP_COMM": "Game.exe", "COREDUMP_EXE": "/home/someone/x/Game.exe",
         "COREDUMP_SIGNAL_NAME": "SIGSEGV", "__REALTIME_TIMESTAMP": AT},
    ]
    ev = syslog.parse(entries, {"Slay the Spire 2.exe", "Game.exe"}, set())
    got = [(e.kind, e.code, e.ours) for e in ev]
    assert ("oom", "oom-kill", True) in got and ("gpu", "nvidia:13", True) in got   # (names cut at 15 characters)
    assert ("gpu", "nvidia:79", False) in got                                       # no process named: by time only
    assert ("gpu", "amdgpu:ring-timeout", True) in got and ("gpu", "intel:hang", True) in got
    assert ("segfault", "SIGSEGV", True) in got and ("segfault", "SIGILL", True) in got
    assert ("core", "SIGSEGV", True) in got
    assert not any(e.proc in ("wineserver", "firefox") for e in ev)
    oom = next(e for e in ev if e.kind == "oom")
    assert oom.mem == 15 << 30 and "using 15.0 GB" in oom.describe()
    seg = next(e for e in ev if e.code == "SIGSEGV" and e.kind == "segfault")
    assert seg.lib == "libnvidia-glcore.so.615"
    # a helper of Wine's counts when it is the run's own process
    assert syslog.parse([entries[8]], {"Game.exe"}, {999})[0].proc == "wineserver"
    # kept as markers (no paths: the core dump's file is not in them) and read back
    text = syslog.markers(ev, True)
    assert "/home/someone" not in text
    back, readable = syslog.from_text("noise\n" + text)
    assert readable and [(e.kind, e.code) for e in back] == [(e.kind, e.code) for e in ev]


def test_an_unreadable_journal_is_said_not_taken_for_nothing(monkeypatch):
    monkeypatch.setenv("EZGD_SYSLOG", "1")
    monkeypatch.setattr(syslog.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(cmd, **kw):
        if cmd[0].endswith("journalctl"):          # a user outside the journal's groups
            return subprocess.CompletedProcess(cmd, 0, "", "Hint: You are currently not seeing messages from other "
                                                           "users and the system.\n")
        return subprocess.CompletedProcess(cmd, 1, "", "dmesg: read kernel buffer failed: Operation not permitted")
    monkeypatch.setattr(syslog.subprocess, "run", run)
    events, readable = syslog.read(0, 10, {"Game.exe"})
    assert events == [] and not readable
    text = syslog.markers(events, readable)
    assert text.startswith(syslog.UNREADABLE)
    assert syslog.from_text(text) == ([], False)


def _game(tmp_path: Path, shipping: bool = False) -> Path:
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    exe = game / "Game.exe"
    exe.write_bytes(build_pe(["kernel32.dll"]))
    return exe


def test_the_investigator_follows_a_kill_for_memory(tmp_path, monkeypatch):
    exe = _game(tmp_path)
    monkeypatch.setattr(syslog, "memory_facts", lambda: {"MemTotal": 16 << 30, "SwapTotal": 0})
    ev = syslog.parse([k("Out of memory: Killed process 4242 (Game.exe) total-vm:1kB, anon-rss:15728640kB")],
                      {"Game.exe"}, set())
    t = tracer.trace('0134:trace:loaddll:build_module Loaded L"Z:\\\\x\\\\Game.exe" at 0000000140000000: native\n'
                     + syslog.markers(ev, True), exe, exit_code=-9, seconds=300)
    assert "ran out of memory" in t.first_problem()
    chain, suspects = tracer.follow(t)
    assert any("killed it because the computer ran out of memory — the game was using 15.0 GB of 16 GB" in c
               and "no swap space" in c for c in chain)
    mem = next(s for s in suspects if s.key == "system:memory")
    assert mem.fix is None and "add swap space or zram" in mem.why          # said plainly: no setting fixes it
    assert tracer.fingerprint(t)["system"] == ["oom"]


def test_a_gpu_fault_is_tested_with_a_prediction(tmp_path):
    exe = _game(tmp_path)
    loads = "".join(f'0134:trace:loaddll:build_module Loaded L"Z:\\\\x\\\\{n}" at 00000001{i:02d}000000: native\n'
                    for i, n in enumerate(["Game.exe", "Game-Win64-Shipping.exe", "d3d12.dll", "nvapi64.dll"]))
    gpu = syslog.markers(syslog.parse([k("NVRM: Xid (PCI:0000:01:00): 13, pid=4242, name=Game.exe, x")],
                                      {"Game.exe"}, set()), True)
    before = tracer.trace(loads + gpu, exe, exit_code=3, seconds=40)
    assert before.first_problem().startswith("the graphics driver reported a GPU fault (nvidia:13")
    chain, suspects = tracer.follow(before)
    assert any("the GPU failed on the game's own commands" in c for c in chain)
    keys = {s.key: s for s in suspects}
    assert keys["system:dx11"].fix.args == "-dx11" and keys["system:dx11"].expect == {"no_system": "gpu"}
    assert keys["system:no-nvapi"].fix.options == ["no_nvapi"]
    after = tracer.trace(loads, exe, exit_code=3, seconds=40)                  # the fault is gone
    v, why = tracer.verdict(before, after, {"no_system": "gpu"})
    assert v == "confirmed" and "as predicted: no GPU fault any more" in why
    still = tracer.trace(loads + gpu, exe, exit_code=3, seconds=40)
    assert tracer.verdict(before, still, {"no_system": "gpu"})[0] == "no-change"
    # a hardware fault: no game setting is tested for it
    hw = syslog.markers(syslog.parse([k("NVRM: Xid (PCI:0000:01:00): 79, pid=4242, name=Game.exe, off the bus")],
                                     {"Game.exe"}, set()), True)
    _c, sus = tracer.follow(tracer.trace(loads + hw, exe, exit_code=3, seconds=40))
    assert [s.key for s in sus if s.key.startswith("system:")] == ["system:gpu-hardware"] and sus[0].fix is None


def test_a_session_killed_for_memory_is_kept_although_its_exit_code_looks_normal(tmp_path):
    exe = _game(tmp_path)
    log = tmp_path / "game-1.log"
    oom = syslog.markers(syslog.parse([k("Out of memory: Killed process 42 (Game.exe) total-vm:1kB, anon-rss:8388608kB")],
                                      {"Game.exe"}, set()), True)
    # the exit line is written first; the system log's markers follow once the session has ended
    log.write_text("\n=== 2026-10-23 10:00:00 GE-Proton\n"
                   '0134:trace:loaddll:build_module Loaded L"Z:\\\\x\\\\Game.exe" at 0000000140000000: native\n'
                   "=== exited with code -9 after 1500s\n" + oom)
    run = dg.last_run(log)
    assert run.exit_code == -9 and syslog.MARK in run.text
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(exe),
                     install_path=str(exe.parent))
    rec = crashes.capture(tmp_path / "data", e, tmp_path / "pfx", log, -9, 1500)
    assert rec is not None and rec["system"][0].startswith("the kernel killed Game.exe")
    assert crashes.summary(rec).startswith("stopped after 25 min: the kernel killed Game.exe")
    # without the system's word, -9 is the app (or the user) stopping it: nothing kept
    log.write_text("\n=== 2026-10-23 11:00:00 GE-Proton\n=== exited with code -9 after 1500s\n")
    assert crashes.capture(tmp_path / "data", e, tmp_path / "pfx", log, -9, 1500) is None


def _diagnose(tmp_path: Path, body: str, code: int):
    exe = _game(tmp_path)
    log = tmp_path / "game-1.log"
    log.write_text(f"\n=== 2026-10-23 10:00:00 GE-Proton\n{body}=== exited with code {code} after 20s\n")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(exe),
                     install_path=str(exe.parent))
    return dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="").to_dict()


def test_diagnose_shows_what_the_system_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(syslog, "memory_facts", lambda: {"MemTotal": 16 << 30, "SwapTotal": 8 << 30})
    oom = syslog.markers(syslog.parse([k("Out of memory: Killed process 42 (Game.exe) total-vm:1kB, anon-rss:1kB")],
                                      {"Game.exe"}, set()), True)
    keys = {f["key"]: f for f in _diagnose(tmp_path, oom, -9)["findings"]}
    assert keys["sys-oom"]["confidence"] >= 0.9 and not keys["sys-oom"]["fixable"]
    # the game quit without a word and the system log could not be read: said, with the way to fix that
    keys = {f["key"]: f for f in _diagnose(tmp_path, syslog.markers([], False), 1)["findings"]}
    assert keys["sys-unreadable"]["fix"]["value"] == "sudo usermod -aG systemd-journal $USER"
    # … but not when it ended normally
    assert "sys-unreadable" not in {f["key"] for f in _diagnose(tmp_path, syslog.markers([], False), 0)["findings"]}
