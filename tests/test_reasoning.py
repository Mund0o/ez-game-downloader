"""The assistant's reasoning over a game's history: regressions, exit codes, phases, follow-up after a fix."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

from ezgd.library import diagnose as dg
from ezgd.library import history
from ezgd.library.models import LibraryEntry, LibraryStatus
from pe_builder import build_pe


def run_block(when, desc, setup, body, code, secs):
    s = f"\n=== {when} {desc}\n"
    if setup is not None:
        s += history.SETUP_MARK + json.dumps(setup) + "\n"
    return s + body + f"\n=== exited with code {code} after {secs}s\n"


def game(tmp_path, compat=None, source=None):
    g = tmp_path / "Game"
    g.mkdir(parents=True, exist_ok=True)
    (g / "Game.exe").write_bytes(build_pe(["kernel32.dll"], 64))
    return LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"),
                        install_path=str(g), compat=compat or {}, source=source)


def test_history_parses_runs_and_exit_codes(tmp_path):
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-01 10:00:00", "GE-Proton10-1 · fixes: game DLLs", None, "x", 0, 900)
                   + "\n=== 2026-10-01 11:00:00 winetricks vcrun2022\n=== exited with code 0 after 3s\n"
                   + run_block("2026-10-02 10:00:00", "Proton-CachyOS · Steam overlay (Shift+Tab)", {"proton": "P"},
                               "Proton: x", 53, 2))
    rs = history.runs(log)
    assert [r.started[:10] for r in rs] == ["2026-10-01", "2026-10-02"]       # winetricks run left out
    assert rs[0].worked and rs[1].failed and rs[1].setup == {"proton": "P"}
    assert rs[0].facet("fixes") == ["game DLLs"] and rs[1].facet("overlay") == "full"
    assert history.explain_exit(53)[0] == "dll" and history.explain_exit(53)[2] is False
    assert history.explain_exit(3221225781)[0] == "dll" and history.explain_exit(3221225781)[2] is True
    assert history.explain_exit(-9)[0] == "killed" and history.explain_exit(0) is None
    assert history.explain_exit(1) is None                                    # too common to mean anything


def test_regression_offers_the_setup_that_worked(tmp_path):
    e = game(tmp_path)
    log = tmp_path / "game-1.log"
    good = {"proton": "GE-Proton10-1", "fixes": [], "launch_args": "", "executable": e.executable}
    bad = {**good, "proton": "GE-Proton11-7"}
    log.write_text(run_block("2026-10-01 10:00:00", "GE-Proton10-1", good, "", 0, 1800)
                   + run_block("2026-10-03 10:00:00", "GE-Proton11-7", bad, "Proton: x", 5, 3))
    protons = [SimpleNamespace(name="GE-Proton10-1", path=tmp_path), SimpleNamespace(name="GE-Proton11-7", path=tmp_path)]
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=protons, proton_name="GE-Proton11-7").to_dict()
    f = next(f for f in d["findings"] if f["key"] == "regression")
    assert f["confidence"] >= 0.8 and "GE-Proton10-1 → GE-Proton11-7" in " ".join(f["evidence"])
    assert json.loads(f["fix"]["value"]) == {"proton_version": "GE-Proton10-1"}
    kinds = [s["kind"] for s in d["reasoning"]]
    assert kinds[0] == "what" and "history" in kinds and "next" in kinds
    assert "access violation" in d["reasoning"][0]["text"]                   # exit 5 decoded


def test_a_run_that_reached_the_game_is_not_a_failed_start(tmp_path):
    e = game(tmp_path)
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-01 10:00:00", "GE", None, "", 0, 900)
                   + run_block("2026-10-03 10:00:00", "GE", None, "[Startup] Time to main menu: 7,173ms", 1, 18))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    assert not [f for f in d["findings"] if f["key"].startswith("regression")]
    assert any("reached the game" in s["text"] for s in d["reasoning"])


def test_missing_native_override_and_missing_onlinefix(tmp_path):
    e = game(tmp_path, compat={"dll_overrides": "winmm=n;dinput8=n,b"}, source="onlinefix")
    log = tmp_path / "game-1.log"
    log.write_text(run_block(time.strftime("%Y-%m-%d %H:%M:%S"), "GE", None,
                             "WINEDLLOVERRIDES='winmm=n;dinput8=n,b' proton run Game.exe", 53, 2))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    by = {f["key"]: f for f in d["findings"]}
    assert by["override-missing"]["fix"]["value"] == "winmm=n,b;dinput8=n,b"
    assert by["onlinefix-missing"]["confidence"] >= 0.9                      # quick exit + exit 53 agree
    assert any(s["kind"] == "agree" for s in d["reasoning"])
    (tmp_path / "Game" / "OnlineFix64.dll").write_bytes(b"x")
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    assert "onlinefix-missing" not in {f["key"] for f in d["findings"]}


def test_harmless_overlay_preload_line_is_not_a_crash(tmp_path):
    e = game(tmp_path)
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-03 10:00:00", "GE", None,
                             "ERROR: ld.so: object '/s/ubuntu12_32/gameoverlayrenderer.so' from LD_PRELOAD cannot be "
                             "preloaded (wrong ELF class: ELFCLASS32): ignored.", 1, 18))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    assert "overlay-crash" not in {f["key"] for f in d["findings"]}


def test_fix_follow_up_counts_the_next_run(tmp_path):
    from ezgd.core.app_core import AppCore
    events = []
    core = SimpleNamespace(paths=SimpleNamespace(data=tmp_path), emit=lambda n, p: events.append((n, p)),
                           library=SimpleNamespace(get=lambda i: SimpleNamespace(title="Game")))
    for name in ("_followup_path", "_followups", "_expect_followup", "_record_outcome", "_outcomes_path",
                 "_outcome_context"):
        setattr(core, name, getattr(AppCore, name).__get__(core))
    explained = []
    core._explain_soon = lambda *a: explained.append(a)
    core._expect_followup(1, "installing vcrun2022")
    AppCore._game_exited(core, 1, 0, 600)
    assert events[-1][1][0] == "The fix worked"
    assert json.loads((tmp_path / "fix_outcomes.json").read_text())[-1]["worked"] is True
    AppCore._game_exited(core, 1, 5, 3)                       # no fix pending: no verdict, but it is explained
    assert len(events) == 1 and explained == [(1, 3, 5)]
    core._expect_followup(1, "switching Proton")
    AppCore._game_exited(core, 1, 53, 2)
    assert events[-1][1][0] == "Still not working"


def test_steam_reference_compares_prefixes_and_proton(tmp_path):
    from ezgd.library import steamref
    steam_root = tmp_path / "Steam"
    exp = steam_root / "steamapps" / "common" / "Proton - Experimental"
    exp.mkdir(parents=True)
    cd = steam_root / "steamapps" / "compatdata" / "1392860"
    (cd / "pfx" / "drive_c" / "windows" / "system32").mkdir(parents=True)
    for n in ("kernel32.dll", "mfc140u.dll", "vcruntime140_threads.dll", "xactengine2_10.dll"):
        (cd / "pfx" / "drive_c" / "windows" / "system32" / n).write_bytes(b"MZ")
    (cd / "config_info").write_text(f"11.0-100\n{exp}/files/share/fonts/\n")
    (cd / "version").write_text("11.0-100")
    protons = [SimpleNamespace(name="Proton - Experimental", path=exp), SimpleNamespace(name="GE", path=tmp_path / "ge")]
    ref = steamref.reference(steam_root, 1392860, protons)
    assert ref["proton"] == "Proton - Experimental" and "mfc140u.dll" in ref["dlls64"]
    assert steamref.reference(steam_root, 999, protons) is None
    e = game(tmp_path)
    ours = tmp_path / "pfx" / "pfx" / "drive_c" / "windows" / "system32"
    ours.mkdir(parents=True)
    (ours / "kernel32.dll").write_bytes(b"MZ")
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-03 10:00:00", "GE", None, "Proton: x", 53, 2))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=protons, proton_name="GE", steam_ref=ref).to_dict()
    by = {f["key"]: f for f in d["findings"]}
    assert by["steam-ref-runtime"]["fix"]["value"] == "vcrun2022 xact" and by["steam-ref-runtime"]["confidence"] >= 0.8
    assert by["steam-ref-proton"]["fix"] == {"kind": "proton", "value": "Proton - Experimental",
                                             "label": "Use Proton - Experimental, like Steam"}
    assert d["findings"][0]["key"] == "steam-ref-runtime"


def test_shipped_redistributables(tmp_path):
    e = game(tmp_path)
    (tmp_path / "Game" / "Game.exe").write_bytes(build_pe(["kernel32.dll", "d3dx9_43.dll", "xaudio2_7.dll"], 64))
    redist = tmp_path / "Game" / "_CommonRedist" / "vcredist" / "2022"
    redist.mkdir(parents=True)
    (redist / "VC_redist.x64.exe").write_bytes(b"MZ")
    (tmp_path / "Game" / "_CommonRedist" / "DirectX").mkdir()
    (tmp_path / "Game" / "_CommonRedist" / "DirectX" / "DXSETUP.exe").write_bytes(b"MZ")
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-03 10:00:00", "GE", None, "x", 53, 2))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    f = next(f for f in d["findings"] if f["key"] == "redist")
    assert set(f["fix"]["value"].split()) == {"vcrun2022", "d3dx9", "xact"}    # only what the game imports


def test_a_generic_redist_bundle_offers_only_what_the_game_uses(tmp_path):
    """SteamRIP's _CommonRedist is the same bundle for every game: Avatar: Frontiers of Pandora (DirectX 12) was
    offered .NET 4.8, XNA, OpenAL and DirectX 9–11 extensions — and .NET failed to install."""
    e = game(tmp_path)
    redist = tmp_path / "Game" / "_CommonRedist"
    redist.mkdir()
    for n in ("dotNetFx40_Full_setup.exe", "dxwebsetup.exe", "oalinst.exe", "xnafx40_redist.msi"):
        (redist / n).write_bytes(b"MZ")
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-03 10:00:00", "GE", None, "x", 53, 2))
    (tmp_path / "Game" / "Game.exe").write_bytes(build_pe(["kernel32.dll", "d3d12.dll", "dxgi.dll"], 64))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    assert not any(f["key"] == "redist" for f in d["findings"])
    # an XNA game (a .NET program that uses OpenAL) gets exactly those
    (tmp_path / "Game" / "Game.exe").write_bytes(build_pe(["mscoree.dll"], 32, dotnet=True))
    (tmp_path / "Game" / "soft_oal.dll").write_bytes(build_pe(["kernel32.dll", "openal32.dll"], 32))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    f = next(f for f in d["findings"] if f["key"] == "redist")
    assert set(f["fix"]["value"].split()) == {"dotnet48", "xna40", "openal"}


def test_investigation_evidence_and_last_words(tmp_path):
    e = game(tmp_path)
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-03 10:00:00", "GE", None, "Proton: x", 53, 2))
    ev = {"label": "Current setup", "exit_code": 53, "seconds": 3, "text":
          "ProtonFixes[1] INFO: noise\n0124:err:module:import_dll Library VCRUNTIME140_1.dll (which is needed by "
          "L\"C:\\\\Game.exe\") not found\n0124:err:module:loader_init Importing dlls for L\"C:\\\\Game.exe\" failed\n"}
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE", evidence=ev).to_dict()
    assert d["findings"][0]["fix"]["value"] == "vcrun2022"
    texts = " ".join(s["text"] for s in d["reasoning"])
    assert "own test run" in texts and "Importing dlls" in texts and "noise" not in texts


async def test_adaptive_trials_test_what_a_failed_test_revealed():
    from ezgd.core.app_core import AppCore
    tested = []

    def run_trial(_eid, cand, n, _cancel):
        tested.append(cand["label"])
        lib.trial_evidence.setdefault(1, {})[n] = {"text": "x", "exit_code": 53, "seconds": 2, "label": cand["label"]}
        ok = "game DLLs" in cand["label"]
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "ran" if ok else "exited",
                "seconds": 45 if ok else 2, "detail": ""}
    finding = {"key": "x", "title": "Needs its own DLLs", "confidence": 0.8,
               "fix": {"kind": "enable_fix", "value": "game_dlls", "label": "Use the game DLLs"}}
    lib = SimpleNamespace(trial_evidence={}, run_trial=run_trial,
                          diagnose=lambda *a: {"findings": [finding, {"key": "w", "title": "Runtime", "confidence": 0.9,
                                                                     "fix": {"kind": "winetricks", "value": "vcrun2022",
                                                                             "label": "Install"}}]},
                          trial_candidates=lambda _e, fs, _g: [{"label": "Current", "overrides": {}}] + [
                              {"label": f["fix"]["label"] + " game DLLs", "overrides": {"compat": {"fixes": ["game_dlls"]}}}
                              for f in fs if f["fix"]["kind"] == "enable_fix"])
    import threading
    core = SimpleNamespace(library=lib, emit=lambda *a: None, _trial_cancel=threading.Event(), MAX_ADAPTIVE=4)
    for name in ("_key", "_learn_from_trial"):
        setattr(core, name, getattr(AppCore, name).__get__(core))
    repairs = []
    res = await AppCore._run_candidates(core, 1, [{"label": "Current", "overrides": {}},
                                                  {"label": "Use GE", "overrides": {"proton_version": "GE"}}],
                                        repairs=repairs)
    assert tested[:2] == ["Current", "Use the game DLLs game DLLs (found in test 1)"]   # evidence goes first
    assert res[1]["outcome"] == "ran" and repairs[0]["fix"]["value"] == "vcrun2022"


def test_ntsync_vkd3d_and_shader_cache_fixes(tmp_path):
    from ezgd.library import fixgen
    from ezgd.platform import syscheck
    e = game(tmp_path)
    (tmp_path / "Game" / "vkd3d-proton.cache").write_bytes(b"old cache")
    run = dg.LastRun(started="2026-10-10 10:00:00", text="ntsync: up and running.\nvkd3d-proton: init", exit_code=5,
                     seconds=3)
    c = dg.Context(e, Path(e.executable), tmp_path / "pfx", run, None, "", "", False, [], "GE", True, True, {},
                   system=syscheck.SystemFacts(gpus=[syscheck.GPU("NVIDIA GeForce RTX 4090", "1")]))
    gen = {g.gen_id: g for g in fixgen.generate(c, [], "no_start")}
    assert gen["sync:no-ntsync"].env == {"PROTON_NO_NTSYNC": "1"}
    assert gen["vkd3d:nodxr"].env == {"VKD3D_CONFIG": "nodxr"} and "vkd3d:no-upload-hvv" in gen
    cache = gen["cache:reset"]
    done = fixgen.apply_edits(cache.edits)
    assert not (tmp_path / "Game" / "vkd3d-proton.cache").exists()
    (tmp_path / "Game" / "vkd3d-proton.cache").write_bytes(b"rebuilt in the test")
    fixgen.revert_edits(done)
    assert (tmp_path / "Game" / "vkd3d-proton.cache").read_bytes() == b"old cache"      # the test left no trace
    [b] = fixgen.backup_and_apply(cache.edits)                                          # kept: moved aside
    assert not (tmp_path / "Game" / "vkd3d-proton.cache").exists() and Path(b).read_bytes() == b"old cache"


def test_unreal_crash_report_is_read(tmp_path):
    e = game(tmp_path)
    (tmp_path / "Game" / "Engine").mkdir()
    (tmp_path / "Game" / "Game" / "Binaries" / "Win64").mkdir(parents=True)
    crash = tmp_path / "Game" / "Game" / "Saved" / "Crashes" / "UECC-1"
    crash.mkdir(parents=True)
    (crash / "CrashContext.runtime-xml").write_text(
        "<FGenericCrashContext><RuntimeProperties><ErrorMessage>Fatal error: [File:D3D12Util.cpp] D3D Device Removed"
        " (DXGI_ERROR_DEVICE_HUNG)</ErrorMessage></RuntimeProperties></FGenericCrashContext>")
    run = dg.LastRun(started="2026-10-10 10:00:00", text="", exit_code=1, seconds=30)
    c = dg.Context(e, Path(e.executable), tmp_path / "pfx", run, "Unreal Engine", "", "", False, [], "GE", True,
                   True, {})
    [f] = dg.r_engine_crash(c)
    assert f.key == "ue-crash:gpu-lost" and f.fix.value == "-dx11" and "DEVICE_HUNG" in f.explanation


def test_test_copy_runs_but_real_start_fails_blames_the_overlay(tmp_path):
    from ezgd.core.app_core import AppCore
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "game-1.log").write_text(run_block("2026-10-12 10:00:00", "GE · Steam overlay (Shift+Tab)", None,
                                               "Proton: x", 1, 3))
    lib = SimpleNamespace(get=lambda i: SimpleNamespace(compat_config=SimpleNamespace(fixes=[])))
    core = SimpleNamespace(paths=SimpleNamespace(logs=logs), library=lib)
    d = AppCore._real_start_difference(core, 1, {"prefix": "a copy of the game's prefix"})
    assert d["fix"] == ("enable_fix", "no_steam_overlay")
    (logs / "game-1.log").write_text(run_block("2026-10-12 10:00:00", "GE", None, "Proton: x", 1, 3))
    d = AppCore._real_start_difference(core, 1, {"prefix": "a fresh prefix (your drive cannot make instant copies)"})
    assert "damaged" in d["text"] and "fix" not in d


async def test_failed_start_is_explained_in_the_notification():
    from ezgd.core.app_core import AppCore
    events = []

    async def diagnose_game(eid, symptom="", evidence=None):
        return {"findings": [{"key": "exit:dll", "title": "x", "confidence": 0.9, "fix": None},
                             {"key": "steam-ref-runtime", "title": "Runtimes missing", "confidence": 0.8,
                              "fix": {"kind": "winetricks", "value": "vcrun2022", "label": "Install them"}}]}
    core = SimpleNamespace(library=SimpleNamespace(get=lambda i: SimpleNamespace(title="Game")),
                           diagnose_game=diagnose_game, emit=lambda n, p: events.append((n, p)))
    await AppCore._explain_failure(core, 1, 2, 53)
    name, (title, text, extra) = events[-1]
    assert title == "Game stopped after 2s" and "Runtimes missing" in text and "Install them" in text
    assert extra == {"diagnose": 1}


async def test_preflight_keeps_only_problems_visible_before_starting(tmp_path):
    from ezgd.core.app_core import AppCore
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    e = SimpleNamespace(executable=str(exe), title="G", compat_config=SimpleNamespace(dll_overrides="", fixes=[]))
    calls = []

    async def diagnose_game(eid, symptom="", evidence=None):
        calls.append(eid)
        fx = {"kind": "winetricks", "value": "vcrun2022", "label": "Install"}
        return {"findings": [{"key": "redist", "title": "Runtimes", "confidence": 0.45, "fix": fx},
                             {"key": "graphics-crash", "title": "Crash", "confidence": 0.9, "fix": fx},
                             {"key": "predicted-dll:x.dll", "title": "Weak", "confidence": 0.3, "fix": fx}]}
    core = SimpleNamespace(library=SimpleNamespace(get=lambda i: e, prefix_dir=lambda i: tmp_path / "pfx"),
                           diagnose_game=diagnose_game, PREFLIGHT_KEYS=AppCore.PREFLIGHT_KEYS)
    out = await AppCore.preflight(core, 1)
    assert [f["key"] for f in out] == ["redist"]
    assert await AppCore.preflight(core, 1) == out and calls == [1]              # cached until something changes


def test_fix_already_applied_before_the_failed_run_is_not_offered_again(tmp_path):
    import os
    e = game(tmp_path, source="onlinefix")
    root = tmp_path / "Game"
    (root / "OnlineFix64.dll").write_bytes(b"MZ")
    b = root / ".ezgd-backup-20261012-211142"
    b.mkdir()
    os.utime(b, (1000, 1000))
    c = dg.Context(e, Path(e.executable), tmp_path / "pfx", dg.LastRun(started="x"), None, "", "", False, [], "GE",
                   True, True, {}, run_started=2000)
    assert dg._fix_applied_before_run(c)
    c.run_started = 500                                       # the fix came after that run: it may still help
    assert not dg._fix_applied_before_run(c)


def test_friendly_notes_and_wrapping_widgets(qtbot):
    from PySide6.QtWidgets import QPushButton, QWidget
    from ezgd.ui.pages.library import _friendly_notes
    from ezgd.ui.widgets.common import FlowLayout, PathLabel
    assert "not needed" in _friendly_notes("Not extracted automatically: G_Fix_Repair_Steam.rar")
    assert _friendly_notes("Not extracted automatically: Bonus.zip") == "Not extracted automatically: Bonus.zip"
    w = QWidget()
    qtbot.addWidget(w)
    f = FlowLayout(w)
    for i in range(5):
        f.addWidget(QPushButton(f"Button {i}"))
    assert f.heightForWidth(150) > f.heightForWidth(2000)                    # wraps when narrow
    p = PathLabel("/home/user/Games/Some Game/Some Game/Binaries/Win64/Game-Win64-Shipping.exe")
    qtbot.addWidget(p)
    p.resize(140, 20)
    p.show()
    assert "…" in p.text() and p.toolTip().endswith("Shipping.exe")


def test_onlinefix_loaders_next_to_unreals_real_game_are_found_and_fixed_first(tmp_path):
    """Expedition 33: Expedition33_Steam.exe (Unreal's launcher) starts Sandfall/Binaries/Win64/…-Shipping.exe, and
    OnlineFix's winmm.dll and UE4SS's dwmapi.dll sit there. Only the launcher's folder was searched, so Wine's own
    winmm/dwmapi were used, OnlineFix never loaded and the game crashed before its log — through every trial."""
    from ezgd.platform.proton import suggest_dll_overrides
    root = tmp_path / "Game"
    (root / "Engine").mkdir(parents=True)
    win64 = root / "Sandfall" / "Binaries" / "Win64"
    win64.mkdir(parents=True)
    (root / "Expedition33_Steam.exe").write_bytes(build_pe(["kernel32.dll"], 64))
    (win64 / "SandFall-Win64-Shipping.exe").write_bytes(build_pe(["kernel32.dll", "winmm.dll", "dwmapi.dll"], 64))
    for n in ("winmm.dll", "dwmapi.dll", "OnlineFix64.dll", "OnlineFix.ini"):
        (win64 / n).write_bytes(b"MZ")
    s = suggest_dll_overrides(root / "Expedition33_Steam.exe")
    assert s.found == ["dwmapi", "winmm"] and s.onlinefix
    e = LibraryEntry(title="E33", status=LibraryStatus.INSTALLED, id=1, executable=str(root / "Expedition33_Steam.exe"),
                     install_path=str(root), source="onlinefix")
    log = tmp_path / "game-1.log"
    log.write_text(run_block("2026-10-20 05:39:42", "GE", None, "x", 5, 11))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    f = next(f for f in d["findings"] if f["key"] == "advisor:dll-overrides")
    assert f["confidence"] >= 0.85 and f["fix"]["kind"] == "enable_fix" and f["fix"]["value"] == "game_dlls"
