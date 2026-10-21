"""Crashes and freezes while playing: kept when they happen, followed by the investigator, judged by the next
play session (a 45-second test start cannot show a crash 20 minutes in)."""

import json
import subprocess
import time
from pathlib import Path

from ezgd.library import crashes, tracer
from ezgd.library.models import LibraryEntry, LibraryStatus
from pe_builder import build_minidump, build_pe

from test_release_fixes import core, install  # noqa: F401 — the shared AppCore fixture


def sentry_dump(prefix: Path, exe_name: str = "Game-Win64-Shipping.exe") -> Path:
    """A crash dump where Sentry keeps it (Expedition 33's were missed before)."""
    d = prefix / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Game" / ".sentry-native" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "041df4ed.dmp"
    p.write_bytes(build_minidump(0xC0000005, 0x140001234, [(0x140000000, 0x2000000, exe_name),
                                                            (0x180000000, 0x100000, "d3d12core.dll")], access=0))
    return p


def test_dumps_are_found_where_crash_handlers_keep_them(tmp_path):
    from ezgd.library.minidump import find_dumps, read_minidump
    game = tmp_path / "game"
    game.mkdir()
    p = sentry_dump(tmp_path / "pfx")
    assert find_dumps(game, tmp_path / "pfx", since=time.time() - 60) == [p]
    d = read_minidump(p)
    assert d.code == 0xC0000005 and d.module.name == "Game-Win64-Shipping.exe" and d.offset == 0x1234
    assert find_dumps(game, tmp_path / "pfx", since=time.time() + 60) == []          # older than the session


def test_a_crash_while_playing_is_kept_with_its_cause(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(build_pe(["kernel32.dll"]))
    prefix = tmp_path / "pfx"
    sentry_dump(prefix)
    log = tmp_path / "game-1.log"
    log.write_text("\n=== 2026-10-21 10:00:00 GE-Proton\n"
                   '0134:trace:loaddll:build_module Loaded L"Z:\\\\x\\\\Game.exe" at 0000000140000000: native\n'
                   "=== exited with code 5 after 1500s\n")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(game / "Game.exe"),
                     install_path=str(game))
    rec = crashes.capture(tmp_path / "data", e, prefix, log, 5, 1500, machine={"id": "m1"})
    assert rec is not None and rec["crash"]["module"] == "Game-Win64-Shipping.exe" and rec["crash"]["null"]
    assert rec["dumps"] == ["041df4ed.dmp"] and rec["machine"] == {"id": "m1"}
    assert "crashed after 25 min" in crashes.summary(rec)
    assert crashes.latest(tmp_path / "data", 1)["at"] == rec["at"]
    ev = crashes.evidence(rec)
    t = tracer.trace(ev["text"], game / "Game.exe", exit_code=5, seconds=1500)
    assert t.crash is not None and t.crash.module == "Game-Win64-Shipping.exe" and t.crash.certain
    # a normal quit after 25 min is not a crash
    log.write_text("\n=== 2026-10-21 11:00:00 GE-Proton\n=== exited with code 0 after 1500s\n")
    for p in (prefix / "pfx").rglob("*.dmp"):
        p.unlink()
    assert crashes.capture(tmp_path / "data", e, prefix, log, 0, 1500) is None


def test_a_freeze_is_read_from_the_threads():
    """A freeze leaves no dump: what every thread was doing, read from Linux itself."""
    proc = subprocess.Popen(["bash", "-c", "exec -a Game.exe sleep 30"])
    try:
        time.sleep(0.3)
        snap = crashes.snapshot([proc.pid], interval=0.3)
        assert snap["threads"] and snap["threads"][0]["kind"] in ("sleeping", "other")
        assert snap["verdict"]
    finally:
        proc.kill()
    assert "deadlock" in crashes.verdict([{"tid": i, "kind": "waiting", "name": f"T{i}"} for i in range(6)])
    assert "endless loop" in crashes.verdict([{"tid": 1, "kind": "busy", "name": "GameThread"},
                                              {"tid": 2, "kind": "waiting"}])
    assert "graphics driver" in crashes.verdict([{"tid": 1, "kind": "gpu", "name": "RHIThread"},
                                                 {"tid": 2, "kind": "waiting"}])
    assert "disk" in crashes.verdict([{"tid": 1, "kind": "kernel", "name": "IOThread"}])


async def test_a_crash_minutes_in_is_fixed_over_play_sessions(core, tmp_path, monkeypatch):  # noqa: F811
    """It crashed 25 min in: Fix my game starts from that crash, checks the first idea with a hidden start, sets
    it on trial. The next session crashes the same way → undone, the next idea set. The one after runs well past
    25 min → kept, and the crash memory learns it."""
    e, root = install(core, tmp_path, {"Game/dwmapi.dll": b"MZ", "Game/ue4ss/UE4SS.dll": b"MZ"},
                      exe="Game/Game.exe", log="\n=== 2026-10-21 10:00:00 Proton\n")
    folder = root / "Game"
    log_path = core.paths.logs / f"game-{e.id}.log"
    prefix = core.library.prefix_dir(e.id)
    loads = (f'0134:trace:loaddll:build_module Loaded L"Z:{str(folder / "Game.exe").replace("/", chr(92) * 2)}" '
             "at 0000000140000000: native\n"
             f'0134:trace:loaddll:build_module Loaded L"Z:{str(folder / "dwmapi.dll").replace("/", chr(92) * 2)}" '
             "at 0000000180000000: builtin\n")

    def session(code, played, crash=True):
        with open(log_path, "a") as fh:
            fh.write(f"\n=== 2026-10-21 10:00:00 Proton\n{loads}=== exited with code {code} after {played}s\n")
        for p in (prefix / "pfx").rglob("*.dmp") if (prefix / "pfx").exists() else []:
            p.unlink()
        if crash:
            sentry_dump(prefix, "Game.exe")

    session(5, 1500)
    monkeypatch.setattr(core, "_memory_path", lambda: tmp_path / "mem.json")
    notes = []
    monkeypatch.setattr(core, "emit", lambda name, payload: notes.append((name, payload)))
    await core._after_play(e.id, 5, 1500)
    assert crashes.latest(core.paths.data, e.id)["played"] == 1500
    assert any(n == "notify" and "crashed" in p[0] for n, p in notes)

    tests = []

    def trial(entry_id, cand, n, cancel=None, **_kw):
        tests.append(cand["label"])
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "ran", "stuck": False,
                "progress": ["Proton started", "game loaded"], "detail": "kept running"}
    monkeypatch.setattr(core.library, "run_trial", trial)
    v = await core.fix_my_game(e.id)
    assert v["status"] == "trying", v
    first = v["kept"]
    assert "dwmapi" in first                                  # the replaced loader is the first idea
    fol = json.loads(core._followup_path().read_text())[str(e.id)]
    assert fol["crash_after"] == 1500 and fol["queue"]
    assert "dwmapi=n,b" in core.library.get(e.id).compat["dll_overrides"]

    # next session: the same crash after 24 min → undone, the next idea set
    session(5, 1440)
    await core._after_play(e.id, 5, 1440, probation=fol)
    assert "dwmapi=n,b" not in (core.library.get(e.id).compat.get("dll_overrides") or "")
    fol2 = json.loads(core._followup_path().read_text())[str(e.id)]
    assert fol2["current"]["title"] != first
    assert any(n == "notify" and p[0] == "Trying the next idea" for n, p in notes)

    # quit after 5 min without a crash: not judged yet
    session(0, 300, crash=False)
    await core._after_play(e.id, 0, 300, probation=fol2)
    assert str(e.id) in json.loads(core._followup_path().read_text())

    # then a long session without a crash: kept, and remembered
    session(0, 3600, crash=False)
    await core._after_play(e.id, 0, 3600, probation=fol2)
    assert str(e.id) not in json.loads(core._followup_path().read_text())
    assert any(n == "notify" and p[0] == "The fix held" for n, p in notes)
    mem = json.loads((tmp_path / "mem.json").read_text())
    assert mem["solved"] and mem["solved"][-1]["labels"] == [fol2["current"]["title"]]


def test_what_was_solved_counts_by_how_alike_the_computers_are(tmp_path):
    """A crash in NVIDIA's own driver DLL means nothing on an AMD card; a fix from another NVIDIA PC still helps,
    a bit less sure. Other computers' fixes arrive through the synced folder."""
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    text = ('0134:trace:loaddll:build_module Loaded L"Z:\\\\x\\\\Game.exe" at 0000000140000000: native\n'
            "ezgd:dump code=c0000005 module=nvapi64.dll offset=1234 access=read address=0\n")
    t = tracer.trace(text, exe, exit_code=5, seconds=900)
    nvidia_a = {"id": "a", "gpus": [{"vendor": "nvidia"}], "cpu": {"flags": ["avx2"]}, "session": "wayland"}
    nvidia_b = {"id": "b", "gpus": [{"vendor": "nvidia"}], "cpu": {"flags": ["avx2"]}, "session": "x11"}
    amd = {"id": "c", "gpus": [{"vendor": "amd"}], "cpu": {"flags": ["avx2"]}, "session": "wayland"}
    from ezgd.library.fixgen import GeneratedFix
    fix = GeneratedFix("trace:nvapi-off", "Turn off NVAPI", "x", options=["no_nvapi"]).to_dict()
    mem_a = tmp_path / "a.json"
    tracer.remember(mem_a, t, "Unreal Engine", "Game One", [fix], {}, ["Turn off NVAPI"], machine=nvidia_a)
    shared = tracer._load_memory(mem_a)["solved"]                  # (as read from computer A's decrypted file)
    on_amd = tracer.recall(tmp_path / "c.json", t, "Unreal Engine", "Game Two", shared=shared, here=amd)
    assert on_amd == []
    on_nvidia = tracer.recall(tmp_path / "b.json", t, "Unreal Engine", "Game Two", shared=shared, here=nvidia_b)
    assert on_nvidia and on_nvidia[0].fix.options == ["no_nvapi"]
    assert "on another computer" in on_nvidia[0].why and on_nvidia[0].confidence < 0.95
    same_pc = tracer.recall(mem_a, t, "Unreal Engine", "Game Two", here=nvidia_a)
    assert same_pc[0].confidence > on_nvidia[0].confidence
