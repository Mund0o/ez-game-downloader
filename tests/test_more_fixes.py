"""The grounded fixes added on 2026-10-13: Proton switches checked against the real Proton build, components tested
in a copy of the prefix first, heap / DirectX 8 / 32-bit / old-Windows / controller fixes, NTFS and heap rules."""

import json
from types import SimpleNamespace

from ezgd.library import diagnose as dg
from ezgd.library import fixgen, history
from ezgd.library.manager import merge_overrides
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.platform import pe as pe_mod
from ezgd.platform.proton import proton_supports
from pe_builder import build_pe


def proton(tmp_path, *vars_):
    d = tmp_path / "Proton-X"
    d.mkdir(exist_ok=True)
    (d / "proton").write_text("CURRENT_PREFIX_VERSION=\"GE-Proton11-7\"\n" + "\n".join(f'env["{v}"]' for v in vars_))
    return d


def ctx(tmp_path, imports=("kernel32.dll",), bits=64, subsystem=(6, 0), log="", code=5, secs=3, pdir=None,
        path_name="Game"):
    g = tmp_path / path_name
    g.mkdir(parents=True, exist_ok=True)
    (g / "Game.exe").write_bytes(build_pe(list(imports), bits, subsystem=subsystem))
    e = LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"), install_path=str(g))
    run = dg.LastRun(started="2026-10-13 10:00:00", text=log, exit_code=code, seconds=secs)
    return dg.Context(e, g / "Game.exe", tmp_path / "pfx", run, None, "", "", bits == 32, [], "GE", True, True, {},
                      proton_dir=pdir), e


def test_pe_reports_the_windows_version_it_was_built_for(tmp_path):
    f = tmp_path / "old.exe"
    f.write_bytes(build_pe(["d3d8.dll"], 32, subsystem=(5, 1)))
    info = pe_mod.read_pe(f)
    assert info.subsystem_version == (5, 1) and info.is32 and "d3d8.dll" in info.imports


def test_proton_switches_are_checked_against_the_build(tmp_path):
    d = proton(tmp_path, "PROTON_HEAP_DELAY_FREE")
    assert proton_supports(d, "PROTON_HEAP_DELAY_FREE") is True
    assert proton_supports(d, "PROTON_ENABLE_NVAPI") is False          # gone from current Proton builds
    assert proton_supports(d, "DXVK_HUD") is True and proton_supports(None, "PROTON_X") is None
    c, _ = ctx(tmp_path, pdir=d, log="err:heap:RtlFreeHeap invalid block")
    ids = {f.gen_id for f in fixgen.generate(c, [], "no_start")}
    assert "heap:delay-free" in ids
    assert "sync:no-ntsync" not in ids                                   # this build has no PROTON_NO_NTSYNC


def test_old_32bit_directx8_game_gets_its_fixes(tmp_path):
    c, e = ctx(tmp_path, imports=("kernel32.dll", "d3d8.dll"), bits=32, subsystem=(5, 1))
    gen = {f.gen_id: f for f in fixgen.generate(c, [], "no_start")}
    assert gen["d3d8:dxvk"].options == ["dxvk_d3d8"] and gen["wow64:on"].options == ["wow64"]
    assert gen["winver:winxp"].overrides(e)["winetricks"] == ["winxp"]
    assert "winver:win7" in gen


def test_controller_symptom_and_long_paths(tmp_path):
    c, _ = ctx(tmp_path)
    ids = {f.gen_id for f in fixgen.generate(c, [], "controller")}
    assert {"input:sdl", "input:no-steaminput", "input:no-hidraw"} <= ids
    c2, _ = ctx(tmp_path, path_name="Spiele/" + "Ü" * 5 + "/" + "x" * 120)
    assert "drive:game" in {f.gen_id for f in fixgen.generate(c2, [], "no_start")}


def test_heap_exit_code_and_rule(tmp_path):
    assert history.explain_exit(0xC0000374)[0] == "heap" and history.explain_exit(0x74)[0] == "heap"
    c, _ = ctx(tmp_path, log="0124:err:heap:validate_free invalid pointer")
    [f] = dg.r_heap(c)
    assert f.fix.value == "opt:heap_delay_free"


def test_filesystem_rule(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="ntfs3\n"))
    c, _ = ctx(tmp_path)
    [f] = dg.r_filesystem(c)
    assert "NTFS" in f.title and not f.fixable
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="btrfs\n"))
    assert not dg.r_filesystem(c)


def test_components_are_tested_in_a_copy_and_combined_safely():
    assert merge_overrides({"winetricks": ["vcrun2022"]}, {"winetricks": ["d3dx9"]}) == {"winetricks": ["vcrun2022",
                                                                                                         "d3dx9"]}
    assert merge_overrides({"winetricks": ["winxp"]}, {"winetricks": ["win7"]}) is None   # two Windows versions


def test_trial_installs_into_the_copy_and_keep_installs_for_real(tmp_path, monkeypatch, app_paths, db, settings):
    from ezgd.database.repositories import GameRepository, LibraryRepository
    from ezgd.library.manager import LibraryManager
    lib = LibraryManager(LibraryRepository(db), GameRepository(db), settings, app_paths, lambda *a: None)
    g = tmp_path / "G"
    g.mkdir()
    (g / "G.exe").write_bytes(build_pe(["kernel32.dll"]))
    e = lib.repo.insert(LibraryEntry(title="G", status=LibraryStatus.INSTALLED, executable=str(g / "G.exe"),
                                     install_path=str(g)))
    copies = []
    monkeypatch.setattr(lib, "_trial_prefix", lambda eid, n, p=None: (tmp_path / "copy", "a copy of the game's prefix"))
    monkeypatch.setattr("ezgd.library.manager.prefix_ready", lambda p: True)
    monkeypatch.setattr(lib, "_winetricks_into", lambda eid, verbs, prefix, log, p=None: copies.append((verbs, prefix))
                        or 1)
    monkeypatch.setattr(lib, "_winetricks_failure", lambda *a: "the installer could not be downloaded")
    r = lib.run_trial(e.id, {"label": "VC++", "overrides": {"winetricks": ["vcrun2022"]}}, 3)
    assert copies == [(["vcrun2022"], tmp_path / "copy")]                # never the real prefix during a test
    assert r["outcome"] == "error" and "could not be downloaded" in r["detail"]
    cands = lib.trial_candidates(e.id, [{"key": "x", "title": "t", "confidence": 0.6, "fix": {
        "kind": "winetricks", "value": "vcrun2022", "label": "Install VC++"}}], [])
    assert any(c["overrides"] == {"winetricks": ["vcrun2022"]} for c in cands)
    cands = lib.trial_candidates(e.id, [{"key": "x", "title": "t", "confidence": 0.9, "fix": {
        "kind": "winetricks", "value": "dotnet48", "label": "Install .NET"}}], [])
    assert not any("winetricks" in c["overrides"] for c in cands)        # far too slow for a test run
    _ = json
