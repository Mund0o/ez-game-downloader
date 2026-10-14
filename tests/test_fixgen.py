"""The assistant's own fixes: built from evidence + symptom, reversible, tested before use, remembered when they work."""


from ezgd.library import diagnose as dg
from ezgd.library import fixgen
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.platform import syscheck
from pe_builder import build_pe


def ctx_for(tmp_path, *, engine_files=(), bits=64, log="", args="", gpus=None, compat=None):
    g = tmp_path / "Game"
    g.mkdir(parents=True, exist_ok=True)
    (g / "Game.exe").write_bytes(build_pe(["kernel32.dll"], bits))
    for f in engine_files:
        p = g / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"),
                     install_path=str(g), launch_args=args, compat=compat or {})
    run = dg.LastRun(started="2026-10-10 10:00:00", text=log)
    system = syscheck.SystemFacts(gpus=gpus or [])
    return dg.Context(e, g / "Game.exe", tmp_path / "pfx", run, dg.engine_of(g / "Game.exe"), "", "",
                      bits == 32, [], "GE-Proton", True, True, {}, system=system), e


TWO_GPUS = [syscheck.GPU("NVIDIA GeForce RTX 4090", "615.71"), syscheck.GPU("Intel(R) Graphics (RPL-S)", "Mesa")]


def test_with_env_respects_steam_syntax():
    assert fixgen.with_env("", {"A": "1"}) == "A=1 %command%"
    assert fixgen.with_env("gamemoderun %command% -x", {"A": "1"}) == "gamemoderun A=1 %command% -x"
    assert fixgen.with_env("-windowed", {"N": "a b"}) == "N='a b' %command% -windowed"


def test_gpu_fix_for_hybrid_graphics(tmp_path):
    c, e = ctx_for(tmp_path, gpus=TWO_GPUS)
    [g] = [f for f in fixgen.generate(c, [], "black_screen") if f.gen_id == "gpu:discrete"]
    assert g.env["DXVK_FILTER_DEVICE_NAME"] == "GeForce RTX 4090" and g.env["__NV_PRIME_RENDER_OFFLOAD"] == "1"
    o = g.overrides(e)
    assert o["launch_args"].startswith("DXVK_FILTER_DEVICE_NAME='GeForce RTX 4090'") and "%command%" in o["launch_args"]
    assert not [f for f in fixgen.generate(c, [], "no_sound") if f.gen_id == "gpu:discrete"]   # unrelated symptom
    c1, _ = ctx_for(tmp_path / "one", gpus=TWO_GPUS[:1])
    assert not [f for f in fixgen.generate(c1, [], "black_screen") if f.gen_id == "gpu:discrete"]


def test_injected_dll_and_renderers(tmp_path):
    c, e = ctx_for(tmp_path, engine_files=["UnityPlayer.dll", "dxgi.dll"], args="-force-d3d11")
    findings = [{"key": "graphics-crash", "evidence": ["crash address 0x1 is inside dxgi.dll (native)"]}]
    gen = {f.gen_id: f for f in fixgen.generate(c, findings, "")}
    assert "the crash happened inside it" in gen["disable:dxgi.dll"].why
    assert gen["disable:dxgi.dll"].overrides(e)["compat"]["dll_overrides"] == "dxgi=b"
    assert "renderer:Unity:-force-vulkan" in gen and "renderer:Unity:-force-d3d11" not in gen   # already used
    assert gen["renderer:Unity:-force-vulkan"].overrides(e)["launch_args"] == "-force-d3d11 -force-vulkan"


def test_unity_graphics_jobs_edit_is_reversible(tmp_path):
    c, e = ctx_for(tmp_path, engine_files=["UnityPlayer.dll"])
    boot = tmp_path / "Game" / "Game_Data" / "boot.config"
    boot.parent.mkdir()
    boot.write_text("gfx-enable-gfx-jobs=1\ngfx-enable-native-gfx-jobs=1\n")
    (tmp_path / "Game" / "Game_Data" / "globalgamemanagers").write_bytes(b"x")
    [g] = [f for f in fixgen.generate(c, [], "crash_later") if f.gen_id == "unity:gfx-jobs-off"]
    done = fixgen.apply_edits(g.edits)
    assert "gfx-enable-native-gfx-jobs=0" in boot.read_text() and "gfx-enable-gfx-jobs=1" in boot.read_text()
    fixgen.revert_edits(done)
    assert boot.read_text() == "gfx-enable-gfx-jobs=1\ngfx-enable-native-gfx-jobs=1\n"
    backups = fixgen.backup_and_apply(g.edits)
    assert "native-gfx-jobs=0" in boot.read_text() and "native-gfx-jobs=1" in open(backups[0]).read()


def test_edits_in_the_prefix_go_to_the_trial_copy(tmp_path):
    real, trial = tmp_path / "pfx", tmp_path / "trial"
    for root in (real, trial):
        (root / "cfg").mkdir(parents=True)
        (root / "cfg" / "a.ini").write_text("x=1\n")
    done = fixgen.apply_edits([{"path": str(real / "cfg" / "a.ini"), "kind": "keyval", "key": "x", "value": "2"}],
                              (real, trial))
    assert (real / "cfg" / "a.ini").read_text() == "x=1\n" and "x=2" in (trial / "cfg" / "a.ini").read_text()
    fixgen.revert_edits(done)


def test_audio_and_memory(tmp_path):
    c, e = ctx_for(tmp_path, engine_files=["xaudio2_7.dll"], bits=32, log="err: out of memory")
    gen = {f.gen_id: f for f in fixgen.generate(c, [], "no_sound")}
    assert gen["audio:latency"].env == {"PULSE_LATENCY_MSEC": "90"}
    assert gen["audio:native-xaudio"].dll_overrides == {"xaudio2_7": "n,b"}
    assert gen["mem:laa"].overrides(e)["compat"]["options"] == ["laa"]


def test_learned_fixes_are_tried_first(tmp_path):
    path = tmp_path / "learned.json"
    fixgen.remember(path, engine="Unity", problems=["symptom:black_screen"], gen_id="renderer:Unity:-force-glcore",
                    title="Hollow Knight")
    c, e = ctx_for(tmp_path, engine_files=["UnityPlayer.dll"])
    gen = fixgen.generate(c, [], "black_screen", fixgen.load_learned(path))
    assert gen[0].gen_id == "renderer:Unity:-force-glcore" and gen[0].learned_from == "Hollow Knight"


async def test_core_end_to_end(app_paths, tmp_path):
    import httpx
    from ezgd.core.app_core import AppCore
    core = AppCore(app_paths, transport=httpx.MockTransport(lambda r: httpx.Response(404)), enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        g = tmp_path / "games" / "U"
        (g / "U_Data").mkdir(parents=True)
        (g / "U.exe").write_bytes(build_pe(["kernel32.dll", "UnityPlayer.dll"]))
        (g / "UnityPlayer.dll").write_bytes(build_pe(["kernel32.dll"]))
        (g / "U_Data" / "boot.config").write_text("gfx-enable-native-gfx-jobs=1\n")
        e = core.library.import_folder("U", g)
        core.library.set_executable(e.id, g / "U.exe")
        d = await core.diagnose_game(e.id, "crash_later")
        gen = {x["gen_id"]: x for x in d["generated"]}
        assert "unity:gfx-jobs-off" in gen and "renderer:Unity:-force-vulkan" in gen
        assert "You reported: crashes after a while" in d["summary"]
        cands = core.library.trial_candidates(e.id, d["findings"], d["generated"])
        assert any(c.get("generated") for c in cands)
        msg = await core.apply_trial(e.id, gen["unity:gfx-jobs-off"]["overrides"], "unity:gfx-jobs-off",
                                     ["symptom:crash_later"])
        assert "boot.config.ezgd-backup-" in msg and "native-gfx-jobs=0" in (g / "U_Data" / "boot.config").read_text()
        learned = fixgen.load_learned(core.library.learned_path)
        assert learned[-1]["gen_id"] == "unity:gfx-jobs-off" and learned[-1]["engine"] == "Unity"
        msg = await core.apply_trial(e.id, gen["renderer:Unity:-force-vulkan"]["overrides"])
        assert core.library.get(e.id).launch_args == "-force-vulkan"
    finally:
        await core.shutdown()


def test_renderer_already_in_effect_via_fixes_is_not_offered(tmp_path):
    c, e = ctx_for(tmp_path, engine_files=["game.pck"], compat={"fixes": ["godot_vulkan"]})
    ids = {f.gen_id for f in fixgen.generate(c, [], "black_screen")}
    assert "renderer:Godot:--rendering-driver vulkan" not in ids and "renderer:Godot:--rendering-driver opengl3" in ids


def test_general_fixes_for_a_crashing_game(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 32)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    c, e = ctx_for(tmp_path, compat={"fixes": ["gamemode", "game_dlls"], "options": []})
    c.run.description = "GE-Proton10-1 · Steam overlay (Shift+Tab)"
    gen = {f.gen_id: f for f in fixgen.generate(c, [], "no_start")}
    assert gen["sync:no-fsync"].overrides(e)["compat"]["options"] == ["no_fsync"]
    assert gen["cpu:8"].env["WINE_CPU_TOPOLOGY"] == "8:0,1,2,3,4,5,6,7"
    assert gen["locale:en"].env["LC_ALL"] == "en_US.UTF-8"
    assert gen["toggle:no-overlay"].overrides(e)["compat"]["fixes"] == ["game_dlls", "gamemode", "no_steam_overlay"]
    assert "gamemode" not in gen["toggle:no-gamemode"].overrides(e)["compat"]["fixes"]
    assert gen["toggle:no-runtime"].overrides(e)["compat"]["use_runtime"] is False
    assert gen["toggle:no-protonfixes"].overrides(e)["compat"]["protonfixes"] is False
    # a game that runs fine and only has no sound gets none of these — unless the user asks to try everything
    calm = {f.gen_id for f in fixgen.generate(c, [], "no_sound")}
    assert "sync:no-fsync" not in calm and "cpu:8" not in calm
    deep = {f.gen_id for f in fixgen.generate(c, [], "no_sound", deep=True)}
    assert {"sync:no-fsync", "cpu:8", "toggle:no-runtime"} <= deep


def test_dx12_feature_level(tmp_path):
    c, e = ctx_for(tmp_path, log="vkd3d-proton: D3D12CreateDevice failed")
    gen = {f.gen_id: f for f in fixgen.generate(c, [{"key": "dx12", "evidence": []}], "no_start")}
    assert gen["vkd3d:fl12_0"].env == {"VKD3D_FEATURE_LEVEL": "12_0"}


def test_new_rules(tmp_path):
    c, e = ctx_for(tmp_path, log="ERROR: ld.so: object '/x/gameoverlayrenderer.so' segfault\n"
                                 "err:font:find_matching_face Arial not found\n"
                                 "Application tried to create a window, but no driver could be loaded.\n"
                                 "write failed: No space left on device\n")
    keys = {f.key: f for r in (dg.r_overlay, dg.r_fonts, dg.r_display, dg.r_disk) for f in r(c)}
    assert keys["overlay-crash"].fix.value == "no_steam_overlay"
    assert keys["fonts"].fix.kind == "winetricks" and keys["fonts"].fix.value == "corefonts"
    assert "display" in keys and keys["disk-space"].confidence >= 0.8
    (tmp_path / "pfx" / "pfx").mkdir(parents=True)
    (tmp_path / "pfx" / "pfx" / "winetricks.log").write_text("corefonts\n")
    assert not dg.r_fonts(c)                     # already installed: not offered again
