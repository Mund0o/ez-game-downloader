"""Deeper debugging-assistant checks: import tables, crash dumps, system health, ProtonDB, prefix rebuild."""

import os

import pytest

from ezgd.library import diagnose as dg
from ezgd.library.minidump import read_minidump
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.platform import syscheck
from ezgd.platform.pe import read_pe
from pe_builder import build_minidump, build_pe

WINE_BASE = ["kernel32.dll", "user32.dll", "ntdll.dll", "gdi32.dll", "advapi32.dll", "ucrtbase.dll", "msvcp140.dll",
             "vcruntime140.dll", "d3d11.dll", "dxgi.dll"]


class FakeProton:
    def __init__(self, path, name="GE-Proton11-5"):
        self.path, self.name = path, name


def fake_proton(tmp_path, dlls=WINE_BASE):
    root = tmp_path / "Proton"
    for arch in ("x86_64-windows", "i386-windows"):
        d = root / "files" / "lib" / "wine" / arch
        d.mkdir(parents=True)
        for n in dlls:
            (d / n).write_bytes(b"MZ")
    dg._BUILTINS.clear()
    return FakeProton(root)


def game(tmp_path, exe_imports, files=None, bits=64):
    g = tmp_path / "Game"
    g.mkdir()
    (g / "Game.exe").write_bytes(build_pe(exe_imports, bits))
    for name, (imports, b) in (files or {}).items():
        (g / name).write_bytes(build_pe(imports, b))
    log = tmp_path / "game-1.log"
    log.write_text("")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"),
                     install_path=str(g))
    return e, log


def diag(tmp_path, e, log, proton, **kw):
    return dg.diagnose(e, tmp_path / "pfx", log, protons=[proton], proton_name=proton.name, **kw).to_dict()


def test_pe_reader():
    data = build_pe(["kernel32.dll", "MSVCP140.dll"], 32, dotnet=True)
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fh:
        fh.write(data)
    info = read_pe(Path(fh.name))
    os.unlink(fh.name)
    assert info.bits == 32 and info.imports == ["kernel32.dll", "msvcp140.dll"] and info.dotnet


def test_predicts_missing_runtime_and_missing_game_file(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll", "Engine.dll"],
                  {"Engine.dll": (["user32.dll", "d3dx9_43.dll", "Physics.dll"], 64)})
    keys = {f["key"]: f for f in diag(tmp_path, e, log, proton)["findings"]}
    assert keys["predicted-dll:d3dx9_43.dll"]["fix"]["value"] == "d3dx9"           # found via Engine.dll
    assert "Engine.dll imports d3dx9_43.dll" in keys["predicted-dll:d3dx9_43.dll"]["evidence"]
    assert "predicted-file:physics.dll" in keys                                    # game's own file missing
    assert not any(k.endswith("kernel32.dll") or k.endswith("user32.dll") for k in keys)   # Proton has them


def test_runtimes_installed_in_prefix_are_not_missing(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll", "xinput9_1_0.dll"])
    sysdir = tmp_path / "pfx" / "pfx" / "drive_c" / "windows" / "system32"
    sysdir.mkdir(parents=True)
    (sysdir / "xinput9_1_0.dll").write_bytes(b"MZ")
    assert not [f for f in diag(tmp_path, e, log, proton)["findings"] if f["key"].startswith("predicted")]


def test_bitness_mismatch(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll", "dinput8.dll"], {"dinput8.dll": (["kernel32.dll"], 32)})
    f = next(f for f in diag(tmp_path, e, log, proton)["findings"] if f["key"] == "bitness")
    assert "dinput8.dll is 32-bit but Game.exe is 64-bit" in f["explanation"]


def test_unimplemented_function(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll"])
    log.write_text("\n=== 2026-10-10 10:00:00 Proton\nwine: Call from 00006FFFFF to unimplemented function "
                   "msvcp140.dll.?_Xbad_function_call@std@@YAXXZ, aborting\n=== exited with code 1 after 2s\n")
    top = diag(tmp_path, e, log, proton)["findings"][0]
    assert top["key"] == "unimplemented:msvcp140.dll" and top["fix"]["value"] == "vcrun2022"


def test_crash_dump(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll"])
    dump = build_minidump(0xC0000005, 0x7FF600001234, [(0x7FF600000000, 0x100000, r"X:\Game\Game.exe"),
                                                       (0x7FFA00000000, 0x200000, r"C:\windows\system32\d3d11.dll")])
    (tmp_path / "Game" / "crash.dmp").write_bytes(dump)
    d = read_minidump(tmp_path / "Game" / "crash.dmp")
    assert d.code == 0xC0000005 and d.module.name == "Game.exe" and d.offset == 0x1234
    keys = {f["key"]: f for f in diag(tmp_path, e, log, proton)["findings"]}
    assert "Game.exe+0x1234" in keys["dump-game"]["evidence"][0]
    (tmp_path / "Game" / "crash.dmp").write_bytes(
        build_minidump(0xC0000005, 0x7FFA00000500, [(0x7FFA00000000, 0x200000, r"C:\windows\system32\d3d11.dll")]))
    f = next(f for f in diag(tmp_path, e, log, proton)["findings"] if f["key"] == "dump-graphics")
    assert f["fix"]["value"] == "opt:wined3d"


def test_system_health(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll"])
    log.write_text("\n=== 2026-10-10 10:00:00 Proton\nLowLevelFatalError [File:Unknown]\n=== exited with code 3 after 9s\n")
    facts = syscheck.SystemFacts(max_map_count=65530, nofile_hard=1048576, free_game=500 << 20, free_prefix=50 << 30,
                                 fs_game="btrfs", fs_prefix="ntfs3")
    keys = {f["key"]: f for f in diag(tmp_path, e, log, proton, system=facts)["findings"]}
    assert keys["map-count"]["fix"]["kind"] == "command" and "2147483642" in keys["map-count"]["fix"]["value"]
    assert "fs-prefix" in keys and "disk-game" in keys and "disk-prefix" not in keys
    assert syscheck.fs_type(tmp_path) is not None


def test_protondb_borked(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll"])
    d = diag(tmp_path, e, log, proton, protondb={"tier": "borked", "total": 41})
    f = next(f for f in d["findings"] if f["key"] == "protondb-borked")
    assert f["fixable"] is False and "ProtonDB: borked (41 reports)" in d["facts"]


async def test_core_protondb_summary(app_paths, routes):
    import httpx
    from ezgd.core.app_core import AppCore
    routes.add("https://www.protondb.com/api/v1/reports/summaries/202970.json",
               '{"tier": "gold", "total": 120, "confidence": "strong", "trendingTier": "gold"}',
               content_type="application/json")
    core = AppCore(app_paths, transport=httpx.MockTransport(routes), enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        assert (await core.protondb_summary(202970))["tier"] == "gold"
        assert await core.protondb_summary(1) is None and await core.protondb_summary(None) is None
    finally:
        await core.shutdown()


def test_rebuild_prefix_keeps_saves(tmp_path):
    from ezgd.library.manager import LibraryManager
    from ezgd.platform.paths import AppPaths

    class Settings:
        class settings:
            proton_version = ""
            steam_root = ""
            install_dir = str(tmp_path)
            download_dir = str(tmp_path)

    class Repo:
        def get(self, i):
            return LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1)
    paths = AppPaths(data=tmp_path / "data", config=tmp_path / "c", cache=tmp_path / "ca", logs=tmp_path / "l")
    paths.prefixes.mkdir(parents=True)
    lib = LibraryManager(Repo(), None, Settings(), paths, lambda *a: None)
    pfx = lib.prefix_dir(1)
    save = pfx / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "G" / "save.dat"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"progress")
    (pfx / "pfx" / "system.reg").write_text("x")
    old = lib.rebuild_prefix(1)
    assert not pfx.exists() and (tmp_path / "data" / "prefixes" / old.split("/")[-1]).exists()
    with pytest.raises(Exception, match="Start the game once"):
        lib.restore_saves(1, old)
    (pfx / "pfx").mkdir(parents=True)
    (pfx / "pfx" / "system.reg").write_text("new")
    assert lib.restore_saves(1, old) == 1
    assert (pfx / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "G" / "save.dat").read_bytes() \
        == b"progress"
    with pytest.raises(Exception, match="not an old prefix"):
        lib.restore_saves(1, str(tmp_path))


def test_crash_while_closing_is_not_the_cause(tmp_path):
    proton = fake_proton(tmp_path)
    e, log = game(tmp_path, ["kernel32.dll"], {"UnityPlayer.dll": (["kernel32.dll"], 64)})
    log.write_text("\n=== 2026-10-10 10:00:00 Proton\nwine: Call from 00006FFFFFF961E0 to unimplemented function "
                   "UIAutomationCore.DLL.UiaDisconnectAllProviders, aborting\n=== exited with code 1 after 36s\n")
    plog = tmp_path / "pfx" / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "LocalLow" / "Co" / "G"
    plog.mkdir(parents=True)
    (plog / "Player.log").write_text("Loading...\nGarbageCollector disposing of ComputeBuffer. Please use Release\n")
    f = diag(tmp_path, e, log, proton)["findings"][0]
    assert f["key"] == "exit-unimplemented:uiautomationcore.dll" and f["confidence"] < 0.5
    assert "harmless" in f["explanation"] and f["fix"] is None


def test_appid_from_release_files_and_titles(tmp_path):
    from ezgd.library import appid
    g = tmp_path / "G"
    (g / "bin").mkdir(parents=True)
    exe = g / "bin" / "G.exe"
    exe.write_bytes(build_pe(["kernel32.dll"]))
    (g / "bin" / "steam_api64.dll").write_bytes(b"MZ codex")
    (g / "bin" / "steam_emu.ini").write_text("[Settings]\nAppId=394970\n")
    assert appid.from_files(exe, g) == 394970
    (g / "bin" / "steam_appid.txt").write_text("111\n")                 # sources disagree: refuse to guess
    assert appid.from_files(exe, g) is None
    (g / "bin" / "steam_appid.txt").write_text("480\n")                 # Spacewar is never the game
    assert appid.from_files(exe, g) == 394970
    assert appid.key("Call of Duty: Black Ops II") == appid.key("Call of Duty Black Ops 2")
    assert "deadbolt" in appid.title_keys("DEADBOLT Online")
    assert "cyberpunk 2077" in appid.title_keys("Cyberpunk 2077: Ultimate Edition")


def test_try_everything_tests_more_setups(tmp_path):
    from ezgd.library.manager import LibraryManager
    gen = [{"gen_id": f"g{i}", "label": f"Fix {i}", "overrides": {"launch_args": f"-x{i}"}} for i in range(30)]

    class Lib:
        MAX_TRIALS, MAX_DEEP_TRIALS = LibraryManager.MAX_TRIALS, LibraryManager.MAX_DEEP_TRIALS
    # call the real method on a minimal object
    from types import SimpleNamespace
    from ezgd.library.models import CompatConfig
    e = SimpleNamespace(compat_config=CompatConfig(), launch_args="")
    lib = Lib()
    lib.get = lambda _i: e
    lib._proton_for = lambda _e: None
    lib.protons = lambda: []
    quick = LibraryManager.trial_candidates(lib, 1, [], gen)
    deep = LibraryManager.trial_candidates(lib, 1, [], gen, deep=True)
    assert len(quick) == LibraryManager.MAX_TRIALS + 2 and len(deep) == LibraryManager.MAX_DEEP_TRIALS
    assert any("wined3d" in str(c["overrides"]) for c in LibraryManager.trial_candidates(lib, 1, [], [], deep=True))


def test_imports_far_into_a_huge_exe_are_read(tmp_path):
    """Valor Mortis' 191 MB Unreal exe keeps its import table 178 MB in: it used to read as "imports nothing"."""
    import struct
    exe = tmp_path / "Big.exe"
    pe = bytearray(build_pe(["kernel32.dll", "UIAutomationCore.dll"], 64))
    far = 100 * 1024 * 1024
    struct.pack_into("<I", pe, 0x40 + 4 + 20 + 240 + 20, far)      # the section's data starts 100 MB in
    with open(exe, "wb") as fh:
        fh.write(pe[:0x400])
        fh.seek(far)                                   # (sparse: nothing written in between)
        fh.write(pe[0x400:])
    assert read_pe(exe).imports == ["kernel32.dll", "uiautomationcore.dll"]


def test_a_dll_this_proton_lacks_suggests_another_proton(tmp_path):
    """Proton-CachyOS 11.0-20260703 ships no uiautomationcore.dll (every Unreal Engine 5 game imports it): the
    assistant used to call it a missing game file and offered .NET; the answer is another Proton."""
    cachy = fake_proton(tmp_path)
    cachy.name = "Proton-CachyOS Latest"
    ge_root = tmp_path / "GE"
    d = ge_root / "files" / "lib" / "wine" / "x86_64-windows"
    d.mkdir(parents=True)
    for n in WINE_BASE + ["uiautomationcore.dll"]:
        (d / n).write_bytes(b"MZ")
    ge = FakeProton(ge_root, "Proton-GE Latest")
    e, log = game(tmp_path, ["kernel32.dll", "UIAutomationCore.dll"])
    r = dg.diagnose(e, tmp_path / "pfx", log, protons=[cachy, ge], proton_name=cachy.name).to_dict()
    f = next(f for f in r["findings"] if f["key"] == "proton-lacks:uiautomationcore.dll")
    assert f["fix"]["kind"] == "proton" and f["fix"]["value"] == "Proton-GE Latest"
    log.write_text('013c:err:module:import_dll Library UIAutomationCore.DLL (which is needed by L"X:\\\\Game\\\\'
                   'Game.exe") not found\n')
    r = dg.diagnose(e, tmp_path / "pfx", log, protons=[cachy, ge], proton_name=cachy.name).to_dict()
    keys = [f["key"] for f in r["findings"]]
    assert "proton-lacks:uiautomationcore.dll" in keys and "missing-file:uiautomationcore.dll" not in keys
