"""Debugging assistant: causes are named from evidence, fixes only when the evidence points to one."""

import struct

import pytest

from ezgd.library.diagnose import diagnose, last_run
from ezgd.library.models import LibraryEntry, LibraryStatus


def pe(path, machine=0x8664):
    data = bytearray(b"MZ" + b"\0" * 0x3E + b"\0" * 200)
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, machine)
    path.write_bytes(bytes(data))
    return path


def make(tmp_path, log: str, name="Game.exe", machine=0x8664, files=(), seconds=3, code=1, args=""):
    game = tmp_path / "Game"
    game.mkdir(exist_ok=True)
    exe = pe(game / name, machine)
    for f in files:
        p = game / f
        p.parent.mkdir(parents=True, exist_ok=True)
        if f.endswith(".exe"):
            pe(p, machine)
        else:
            p.write_bytes(b"x" * 2000)
    logp = tmp_path / "game-1.log"
    logp.write_text("\n=== 2026-10-09 22:00:00 old run\nnothing\n=== exited with code 0 after 500s\n"
                    f"\n=== 2026-10-10 10:00:00 Proton-GE Latest · Steam Linux Runtime 4.0\ncommand line\n{log}\n"
                    f"=== exited with code {code} after {seconds}s\n")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(exe),
                     install_path=str(game), launch_args=args)
    return e, logp


def run(tmp_path, e, logp, protons=()):
    return diagnose(e, tmp_path / "pfx", logp, protons=list(protons), proton_name="GE-Proton11-5").to_dict()


def test_last_run_is_parsed(tmp_path):
    e, logp = make(tmp_path, "0024:err:module:import_dll Library X.dll not found", seconds=4, code=1)
    r = last_run(logp)
    assert r.started == "2026-10-10 10:00:00" and r.exit_code == 1 and r.seconds == 4 and "import_dll" in r.text


def test_missing_vc_runtime_recommends_exactly_that(tmp_path):
    e, logp = make(tmp_path, '0124:err:module:import_dll Library VCRUNTIME140_1.dll (which is needed by '
                             'L"X:\\\\Games\\\\Game\\\\Game.exe") not found\n'
                             '0124:err:module:loader_init Importing dlls for L"X:\\\\Games\\\\Game.exe" failed')
    d = run(tmp_path, e, logp)
    top = d["findings"][0]
    assert top["title"] == "Missing Microsoft Visual C++ 2015–2022 runtime" and top["confidence"] > 0.9
    assert top["fix"] == {"kind": "winetricks", "value": "vcrun2022", "label":
                          "Install Microsoft Visual C++ 2015–2022 runtime"}
    assert "VCRUNTIME140_1.dll" in top["evidence"][0] and "Most likely cause: Missing Microsoft" in d["summary"]


def test_missing_game_file_means_incomplete_extraction(tmp_path):
    e, logp = make(tmp_path, '0124:err:module:import_dll Library GameCore.dll (which is needed by '
                             'L"X:\\\\Game\\\\Game.exe") not found')
    top = run(tmp_path, e, logp)["findings"][0]
    assert top["key"] == "missing-file:gamecore.dll" and "incomplete" in top["explanation"]


def test_patched_windows_code_like_black_ops_2(tmp_path):
    log = ('017c:trace:loaddll:build_module Loaded L"X:\\\\Game\\\\Game.exe" at 00400000: native\n'
           '017c:trace:loaddll:build_module Loaded L"C:\\\\windows\\\\system32\\\\kernel32.dll" at 7BE90000: builtin\n'
           '017c:trace:loaddll:build_module Loaded L"X:\\\\Game\\\\steam_api.dll" at 7BAB0000: native\n'
           '017c:trace:seh:dispatch_exception code=c0000005 (EXCEPTION_ACCESS_VIOLATION) flags=0 addr=7BE9F7B4\n'
           '017c:trace:seh:dispatch_exception  info[0]=00000008\n'
           '017c:err:seh:NtRaiseException Unhandled exception code c0000005 flags 0 addr 0x7be9f980\n')
    e, logp = make(tmp_path, log, machine=0x14C)
    top = run(tmp_path, e, logp)["findings"][0]
    assert top["key"] == "patched-windows-code" and top["fixable"] is False and top["fix"] is None
    assert "kernel32.dll" in " ".join(top["evidence"])


def test_crash_in_dxvk_offers_wined3d_and_game_crash(tmp_path):
    log = ('0100:trace:loaddll:build_module Loaded L"X:\\\\Game\\\\Game.exe" at 0000000140000000: native\n'
           '0100:trace:loaddll:build_module Loaded L"C:\\\\windows\\\\system32\\\\d3d11.dll" at 00006FFFF0000000: native\n'
           '0100:err:seh:NtRaiseException Unhandled exception code c0000005 flags 0 addr 0x6ffff0012345\n')
    e, logp = make(tmp_path, log)
    top = run(tmp_path, e, logp)["findings"][0]
    assert top["key"] == "graphics-crash" and top["fix"]["value"] == "opt:wined3d"
    log2 = log.replace("0x6ffff0012345", "0x140001234")
    e, logp = make(tmp_path, log2)
    assert run(tmp_path, e, logp)["findings"][0]["key"] == "game-crash"


def test_unreal_dx12_offers_dx11(tmp_path):
    e, logp = make(tmp_path, "LowLevelFatalError: D3D12CreateDevice failed (vkd3d)", name="Game-Win64-Shipping.exe")
    top = run(tmp_path, e, logp)["findings"][0]
    assert top["key"] == "dx12-unreal" and top["fix"] == {"kind": "add_args", "value": "-dx11",
                                                          "label": "Start with DirectX 11"}


def test_xna_and_cpu(tmp_path):
    e, logp = make(tmp_path, "Unhandled Exception: System.IO.FileNotFoundException: Could not load file or assembly "
                             "'Microsoft.Xna.Framework, Version=4.0.0.0'")
    assert run(tmp_path, e, logp)["findings"][0]["fix"]["value"] == "xna40"
    e, logp = make(tmp_path, "0100:err:seh:NtRaiseException Unhandled exception code c000001d flags 0 addr 0x1400")
    top = run(tmp_path, e, logp)["findings"][0]
    assert top["key"] == "cpu-instruction" and top["fixable"] is False


def test_launcher_that_exits_points_to_the_game(tmp_path):
    e, logp = make(tmp_path, "nothing special", name="Launcher.exe", files=["Bin/Game.exe"], seconds=2, code=0)
    e.title = "Game"
    top = next(f for f in run(tmp_path, e, logp)["findings"] if f["key"] == "wrong-exe")
    assert top["fix"]["kind"] == "set_executable" and top["fix"]["value"].endswith("Game.exe")


def test_nothing_found_is_said_plainly(tmp_path):
    e, logp = make(tmp_path, "everything fine", seconds=300, code=0)
    d = run(tmp_path, e, logp)
    assert d["findings"] == [] and "no problem is visible" in d["summary"]
    assert d["suggest_diagnostic_run"] is True


async def test_core_apply_fix(app_paths, tmp_path):
    import httpx
    from ezgd.core.app_core import AppCore
    core = AppCore(app_paths, transport=httpx.MockTransport(lambda r: httpx.Response(404)), enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        folder = tmp_path / "games" / "G"
        folder.mkdir(parents=True)
        pe(folder / "G-Win64-Shipping.exe")
        e = core.library.import_folder("G", folder)
        core.library.set_executable(e.id, folder / "G-Win64-Shipping.exe")
        (core.paths.logs / f"game-{e.id}.log").write_text(
            "\n=== 2026-10-10 10:00:00 Proton\nD3D12CreateDevice failed\n=== exited with code 1 after 3s\n")
        d = await core.library_call("diagnose", e.id)
        fix = d["findings"][0]["fix"]
        msg = await core.library_call("apply_diagnosis_fix", e.id, fix["kind"], fix["value"])
        assert "-dx11" in msg and core.library.get(e.id).launch_args == "-dx11"
        assert await core.library_call("apply_diagnosis_fix", e.id, "enable_fix", "opt:wined3d")
        assert "wined3d" in core.library.get(e.id).compat_config.options
        with pytest.raises(Exception):
            await core.library_call("apply_diagnosis_fix", e.id, "winetricks", "rm -rf /")   # verbs are validated
    finally:
        await core.shutdown()


def test_stack_frames_naming_steam_are_not_a_failure(tmp_path):
    e, logp = make(tmp_path, "ERROR: System.NullReferenceException\n"
                             "       [3] Steamworks.ESteamAPIInitResult Steamworks.NativeMethods.SteamAPI_Init(nint)\n"
                             "   at Steamworks.SteamAPI.InitEx()", seconds=600, code=0)
    assert not [f for f in run(tmp_path, e, logp)["findings"] if f["key"] == "steam-init"]
    e, logp = make(tmp_path, "[S_API FAIL] SteamAPI_Init() failed; no appID found.")
    assert run(tmp_path, e, logp)["findings"][0]["key"] == "steam-init"
