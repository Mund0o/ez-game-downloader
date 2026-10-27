"""EMPRESS's Denuvo emulation (Ghost Recon Wildlands, 2026-10-23): EMP.dll raises and fixes thousands of exceptions
on purpose — none of them is the crash — and on a CPU with performance and efficiency cores it gives up at a random
moment and ends the game itself (exit code 255). Kept on the performance cores the game ran."""

from pathlib import Path

from ezgd.library import diagnose as dg
from ezgd.library import fixes, tracer
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.platform import machine


def wine_path(p: Path) -> str:
    """A Linux path as Wine's debug output prints it: backslashes doubled, ’ as \\2019."""
    s = "Z:" + str(p).replace("/", "\\\\")
    return "".join(c if ord(c) < 128 else f"\\{ord(c):04x}" for c in s)


def release(tmp_path) -> Path:
    folder = tmp_path / "Tom Clancy’s Game" / "Game"
    folder.mkdir(parents=True)
    for name in ("GRW.exe", "EMP.dll", "uplay_r1_loader64.dll"):
        (folder / name).write_bytes(b"MZ")
    return folder


def handled(tid: str, code: str, addr: str, resumed: bool = True) -> list[str]:
    """One exception the way +seh logs it, handled by EMP.dll's vectored handler (or still inside it)."""
    out = [f"{tid}:warn:seh:dispatch_exception backtrace: --- Exception 0x{code}.",
           f"{tid}:trace:seh:dispatch_exception code={code} (X) flags=0 addr={addr}",
           f"{tid}:trace:seh:dispatch_exception  info[0]=0000000000000001",
           f"{tid}:trace:seh:dispatch_exception  info[1]=0000000068CDA6B7",
           f"{tid}:trace:seh:call_vectored_handlers calling handler at 000000001301FB00 code={code} flags=0"]
    if resumed:
        out.append(f"{tid}:trace:seh:call_vectored_handlers handler at 000000001301FAC0 returned ffffffff")
    return out


def run_log(folder: Path, *, emp_exit: bool) -> str:
    lines = [f'0144:trace:loaddll:build_module Loaded L"{wine_path(folder / "GRW.exe")}" at 0000000140000000: native',
             f'0144:trace:loaddll:build_module Loaded L"{wine_path(folder / "EMP.dll")}" at 0000000013000000: native']
    for i in range(50):
        lines += handled("0144", ("c0000005", "c000001d", "80000004")[i % 3], f"00000001489D8B{i:02X}")
    lines += handled("0144", "c0000005", "00000001489D8BC9", resumed=False)    # the one it could not fix
    if emp_exit:
        lines += ["02e8:Starting thread proc 0000000013021E24 (arg=000000000000003C)",
                  "02e8:Call KERNEL32.ExitProcess(fffffffffffff) ret=1302207f"]
    return "\n".join(lines) + "\n"


def test_exceptions_the_crack_handles_are_not_the_crash(tmp_path):
    folder = release(tmp_path)
    t = tracer.trace(run_log(folder, emp_exit=False), folder / "GRW.exe", exit_code=255, seconds=12)
    assert t.crash is None                                   # every one was resumed, or still being handled
    assert t.resumed == 34 and t.resumers == ["EMP.dll"]     # (the access violations and illegal instructions)
    # a real crash next to them is still found
    crash = run_log(folder, emp_exit=False) + "\n".join(
        ["0144:trace:seh:dispatch_exception code=c0000005 (X) flags=0 addr=0000000140001234",
         "0144:trace:seh:dispatch_exception  info[0]=0000000000000000",
         "0144:trace:seh:dispatch_exception  info[1]=0000000000000000",
         "0144:trace:seh:_XcptFilter (c0000005,0000000000C9D9B0)"]) + "\n"
    t = tracer.trace(crash, folder / "GRW.exe", exit_code=5, seconds=12)
    assert t.crash is not None and t.crash.certain and t.crash.module == "GRW.exe"


def test_wine_escapes_in_paths_are_read(tmp_path):
    folder = release(tmp_path)
    t = tracer.trace(run_log(folder, emp_exit=True), folder / "GRW.exe", exit_code=255, seconds=12)
    assert "’" in t.loads[0].path                            # “\2019” was not matched against the real folder
    assert [(x["module"], x["code"]) for x in t.exits] == [("EMP.dll", 0xFFFFFFFF)]


def test_emp_quitting_points_at_the_performance_cores(tmp_path, monkeypatch):
    folder = release(tmp_path)
    monkeypatch.setattr(machine, "performance_cpus", lambda *a: "0-15")
    entry = LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1, executable=str(folder / "GRW.exe"),
                         install_path=str(folder.parent))
    for emp_exit in (True, False):                           # a closer look sees the call; a normal run its -1
        t = tracer.trace(run_log(folder, emp_exit=emp_exit), folder / "GRW.exe", exit_code=255, seconds=12)
        assert tracer.emp_quit(t)
        chain, suspects = tracer.follow(t, entry)
        assert any("EMPRESS" in c for c in chain)
        top = suspects[0]
        assert top.key == "empress:pcores" and top.confidence < 0.85     # a test, never set by itself (unproven)
        assert top.changes(entry)["compat"]["fixes"] == ["pcores"]
    # the test with the fix: EMP.dll did not end it → kept
    ok = tracer.trace(run_log(folder, emp_exit=False), folder / "GRW.exe", ran=True, seconds=300)
    assert tracer.verdict(t, ok, top.expect)[0] == "fixed"
    # on a CPU with one kind of core there is nothing to pin
    monkeypatch.setattr(machine, "performance_cpus", lambda *a: None)
    assert not any(s.key == "empress:pcores" for s in tracer.follow(t, entry)[1])


def test_performance_cores_are_read_from_sysfs(tmp_path):
    (tmp_path / "devices" / "cpu_core").mkdir(parents=True)
    (tmp_path / "devices" / "cpu_core" / "cpus").write_text("0-15\n")
    assert machine.performance_cpus(tmp_path) is None        # no efficiency cores: not a hybrid CPU
    (tmp_path / "devices" / "cpu_atom").mkdir()
    (tmp_path / "devices" / "cpu_atom" / "cpus").write_text("16-31\n")
    assert machine.performance_cpus(tmp_path) == "0-15"


def test_the_fix_pins_the_game_and_is_recommended_for_empress(tmp_path, monkeypatch):
    folder = release(tmp_path)
    monkeypatch.setattr(machine, "performance_cpus", lambda *a: "0-15")
    monkeypatch.setattr(fixes, "is_windows_binary", lambda p: True)
    monkeypatch.setattr(fixes.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    e = LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1, executable=str(folder / "GRW.exe"),
                     install_path=str(folder.parent))
    ctx = fixes.GameContext(e, folder / "GRW.exe", "", linux=True)
    st = {s.key: s for s in fixes.catalog(ctx)}["pcores"]
    assert st.recommended and "EMPRESS" in st.recommended and st.available and not st.enabled
    e.compat = fixes.apply_selection(ctx, {"pcores"}).to_dict()
    fx = fixes.effects(fixes.GameContext(e, folder / "GRW.exe", "", linux=True))
    assert fx.wrappers == ["/usr/bin/taskset", "-c", "0-15"] and "performance cores" in fx.notes

    # the regular checks offer it too — not strongly enough for the new-game check to switch it on
    run = dg.LastRun(started="2026-10-23 14:33:08", text="=== exited with code 255 after 12s", exit_code=255,
                     seconds=12)
    c = dg.Context(LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1, executable=str(folder / "GRW.exe"),
                                install_path=str(folder.parent)),
                   folder / "GRW.exe", tmp_path / "pfx", run, None, "", "", False, [], "GE", True, True, {})
    f = dg.r_empress(c)
    assert f and f[0].fix.kind == "enable_fix" and f[0].fix.value == "pcores" and f[0].confidence < 0.85
    assert dg.r_empress(dg.Context(e, folder / "GRW.exe", tmp_path / "pfx", run, None, "", "", False, [], "GE", True,
                                   True, {})) == []                 # already on
