"""Release problems the assistant repairs: Steam-emulator setup, unapplied fix archives/folders, damaged files."""

import io
import zipfile

import httpx
import pytest

from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from ezgd.core.app_core import AppCore
from pe_builder import build_pe

VALVE_DLL = b"MZ" + b"\0" * 200 + b"SteamAPI_RestartAppIfNecessary\0Valve Corporation\0" + b"\0" * 200


@pytest.fixture
async def core(app_paths, tmp_path):
    c = AppCore(app_paths, transport=httpx.MockTransport(lambda r: httpx.Response(404)), enable_torrents=False)
    c.set_emitter(lambda *a: None)
    await c.start()
    c.settings.set("install_dir", str(tmp_path / "games"))
    c.settings.set("download_dir", str(tmp_path / "dl"))
    yield c
    await c.shutdown()


def install(core, tmp_path, files: dict, exe="Game/Game.exe", imports=("kernel32.dll",), app_id=1234560,
            source=None, log=""):
    root = tmp_path / "games" / "G"
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / exe).parent.mkdir(parents=True, exist_ok=True)
    (root / exe).write_bytes(build_pe(list(imports)))
    e = core.library.import_folder("G", root)
    core.library.set_executable(e.id, root / exe)
    core.library.update(e.id, steam_app_id=app_id)
    if source:
        e = core.library.get(e.id)
        e.source = source
        core.library.repo.update(e)
    if log:
        (core.paths.logs / f"game-{e.id}.log").write_text(log)
    return core.library.get(e.id), root


def by_key(d):
    return {f["key"]: f for f in d["findings"]}


async def test_valve_api_without_steam_appid(core, tmp_path):
    e, root = install(core, tmp_path, {"Game/steam_api64.dll": VALVE_DLL, "Game/Game.exe.1234.STEAMSTART": b"x"})
    f = by_key(await core.diagnose_game(e.id))["emu-appid-txt"]
    assert f["confidence"] >= 0.8 and ".STEAMSTART" in " ".join(f["evidence"])
    msg = await core.apply_fix(e.id, f["fix"]["kind"], f["fix"]["value"])
    assert (root / "Game" / "steam_appid.txt").read_text().strip() == "1234560" and "steam_appid.txt" in msg
    assert "emu-appid-txt" not in by_key(await core.diagnose_game(e.id))


async def test_codex_ini_and_goldberg(core, tmp_path):
    e, root = install(core, tmp_path, {"Game/steam_api64.dll": b"MZ codex", "Game/steam_emu.ini":
                                       b"[Settings]\nAppId=0\nUserName=Player\n"},
                      log="\n=== 2026-10-10 10:00:00 Proton\n[S_API FAIL] SteamAPI_Init() failed\n")
    f = by_key(await core.diagnose_game(e.id))["emu-codex-appid"]
    await core.apply_fix(e.id, f["fix"]["kind"], f["fix"]["value"])
    ini = (root / "Game" / "steam_emu.ini").read_text()
    assert "AppId=1234560" in ini and "UserName=Player" in ini
    assert list((root / "Game").glob("steam_emu.ini.ezgd-backup-*"))


async def test_goldberg_appid(core, tmp_path):
    e, root = install(core, tmp_path, {"Game/steam_api64.dll": b"MZ Goldberg emu", "Game/steam_settings/x.txt": b""})
    (core.paths.logs / f"game-{e.id}.log").write_text("\n=== 2026-10-10 10:00:00 Proton\nNo appID found\n")
    f = by_key(await core.diagnose_game(e.id))["emu-goldberg-appid"]
    await core.apply_fix(e.id, f["fix"]["kind"], f["fix"]["value"])
    assert (root / "Game" / "steam_settings" / "steam_appid.txt").read_text().strip() == "1234560"


async def test_emulator_in_the_wrong_folder(core, tmp_path):
    e, root = install(core, tmp_path, {"Engine/Steam/steam_api64.dll": b"MZ codex",
                                       "Engine/Steam/steam_emu.ini": b"AppId=1234560\n"},
                      exe="Game/Binaries/Win64/Game.exe", imports=("kernel32.dll", "steam_api64.dll"))
    f = by_key(await core.diagnose_game(e.id))["emu-location"]
    await core.apply_fix(e.id, f["fix"]["kind"], f["fix"]["value"])
    w = root / "Game" / "Binaries" / "Win64"
    assert (w / "steam_api64.dll").read_bytes() == b"MZ codex" and (w / "steam_emu.ini").exists()


async def test_creamapi_without_original_is_honest(core, tmp_path):
    e, _ = install(core, tmp_path, {"Game/steam_api64.dll": b"MZ", "Game/cream_api.ini": b"[config]\nappid=1234560\n"})
    f = by_key(await core.diagnose_game(e.id))["emu-cream-original"]
    assert f["fixable"] is False and f["fix"] is None


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


async def test_unapplied_fix_archive(core, tmp_path):
    e, root = install(core, tmp_path, {"Game/OnlineFix64.dll": b"old fix"})
    dl = tmp_path / "dl"
    dl.mkdir()
    fix = dl / "G_Fix_Repair_Steam_Generic.zip"
    fix.write_bytes(zip_bytes({"Game/OnlineFix64.dll": b"new fix dll", "Game/OnlineFix.ini": b"x=1"}))
    e.download_path = str(dl / "G.part1.rar")
    core.library.repo.update(e)
    f = by_key(await core.diagnose_game(e.id))[f"fix-archive:{fix.name}"]
    msg = await core.apply_fix(e.id, f["fix"]["kind"], f["fix"]["value"])
    assert (root / "Game" / "OnlineFix64.dll").read_bytes() == b"new fix dll" and "replaced" in msg
    [bdir] = list(root.glob(".ezgd-backup-*"))
    assert (bdir / "Game" / "OnlineFix64.dll").read_bytes() == b"old fix"
    assert f"fix-archive:{fix.name}" not in by_key(await core.diagnose_game(e.id))      # applied now


async def test_crack_folder(core, tmp_path):
    e, root = install(core, tmp_path, {"Crack/steam_api64.dll": b"MZ cracked one", "Game/steam_api64.dll": b"MZ valve",
                                       "README.txt": b"Copy the contents of the Crack folder to the install dir"})
    f = by_key(await core.diagnose_game(e.id))["crack-folder:Crack"]
    assert f["confidence"] >= 0.85
    await core.apply_fix(e.id, f["fix"]["kind"], f["fix"]["value"])
    assert (root / "Game" / "steam_api64.dll").read_bytes() == b"MZ cracked one"
    assert any((b / "steam_api64.dll").read_bytes() == b"MZ valve" for b in (root / "Game").glob(".ezgd-backup-*"))


async def test_onlinefix_fix_repair_offered(core, tmp_path):
    core.games.save(Game("onlinefix", "7", "G", "https://online-fix.me/games/x/7.html", download_methods=[
        DownloadMethod(MethodKind.DIRECT, "Pixeldrain — G_Fix_Repair.rar (fix files only)", "https://pixeldrain.com/u/x1",
                       supported=True, resolver="pixeldrain")]))
    e, root = install(core, tmp_path, {}, source="onlinefix",
                      log="\n=== 2026-10-10 10:00:00 Proton\nno appID found\n=== exited with code 1 after 4s\n")
    e.game_id = "onlinefix:7"
    core.library.repo.update(e)
    f = by_key(await core.diagnose_game(e.id))["onlinefix-fix-repair"]
    assert f["fix"] == {"kind": "download_fix", "value": "0", "label": "Download and apply the Fix Repair"}


async def test_integrity_through_core(core, tmp_path):
    arc = tmp_path / "dl" / "G.zip"
    arc.parent.mkdir()
    arc.write_bytes(zip_bytes({"G/Game.exe": build_pe(["kernel32.dll"]), "G/data.pak": b"P" * 4000}))
    core.games.save(Game("steamrip", "1", "G", "https://steamrip.com/g/"))
    r = await core.import_downloaded_file("steamrip:1", str(arc))
    import asyncio
    for _ in range(100):
        if core.library.get(r["entry_id"]).install_path and (tmp_path / "games" / "G" / "G" / "data.pak").exists():
            break
        await asyncio.sleep(0.05)
    e = core.library.get(r["entry_id"])
    core.library.set_executable(e.id, tmp_path / "games" / "G" / "G" / "Game.exe")
    (tmp_path / "games" / "G" / "G" / "data.pak").unlink()
    f = by_key(await core.diagnose_game(e.id))["integrity"]
    assert f["fix"]["kind"] == "repair_files" and f["title"] == "1 game file(s) are missing or damaged"
    msg = await core.apply_fix(e.id, "repair_files", "")
    assert "Restored 1 file" in msg and (tmp_path / "games" / "G" / "G" / "data.pak").read_bytes() == b"P" * 4000


async def test_fix_my_game_end_to_end(core, tmp_path, monkeypatch):
    import asyncio
    arc = tmp_path / "dl" / "G.zip"
    arc.parent.mkdir()
    arc.write_bytes(zip_bytes({"G/Game.exe": build_pe(["kernel32.dll"]), "G/data.pak": b"P" * 4000,
                               "G/steam_api64.dll": b"MZ codex", "G/steam_emu.ini": b"AppId=0\n"}))
    core.games.save(Game("steamrip", "2", "G", "https://steamrip.com/g2/"))
    r = await core.import_downloaded_file("steamrip:2", str(arc))
    game = tmp_path / "games" / "G" / "G"
    for _ in range(200):          # files appear one by one: wait until the extraction is recorded as finished
        if any(x.get("status") == "completed" for x in core.extraction_repo.for_library(r["entry_id"])) and \
                core.library.get(r["entry_id"]).executable:
            break                 # (and the import has chosen the exe: it wrote the entry last — it used to
        await asyncio.sleep(0.05)  # overwrite the exe set below now and then, a flaky test)
    await asyncio.sleep(0.2)
    e = core.library.get(r["entry_id"])
    core.library.set_executable(e.id, game / "Game.exe")
    core.library.update(e.id, steam_app_id=7777)
    (game / "data.pak").write_bytes(b"P" * 10)                                       # damaged
    (core.paths.logs / f"game-{e.id}.log").write_text(
        "\n=== 2026-10-10 10:00:00 Proton\n[S_API FAIL] SteamAPI_Init() failed\n=== exited with code 1 after 3s\n")
    events = []
    core.set_emitter(lambda n, p: events.append((n, p)))

    async def trials(entry_id, findings, generated=None, **_kw):
        return [{"label": "Current setup", "overrides": {}, "outcome": "crashed", "detail": "x"},
                {"label": "Start with DirectX 11", "overrides": {"launch_args": "-dx11"}, "outcome": "ran",
                 "detail": "kept running", "gen_id": None}]
    monkeypatch.setattr(core, "run_trials", trials)
    checks = []

    def second_check(entry_id, cand, n, cancel=None):         # the winner must pass twice
        checks.append(cand["label"])
        ok = bool(cand["overrides"])                         # the game as it is still crashes after the repairs
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "ran" if ok else "crashed",
                "detail": "ok" if ok else "x"}
    monkeypatch.setattr(core.library, "run_trial", second_check)
    v = await core.fix_my_game(e.id)
    # the repaired game is tested once as it is (root cause first), then the setups; the winner passes twice
    assert checks == ["Current setup", "Start with DirectX 11 (second check)"]
    assert v["status"] == "fixed" and v["kept"] == "Start with DirectX 11"
    assert (game / "data.pak").read_bytes() == b"P" * 4000                          # repaired
    assert "AppId=7777" in (game / "steam_emu.ini").read_text()                      # emulator fixed
    assert any("game file(s) are missing or damaged" in a for a in v["applied"])
    assert core.library.get(e.id).launch_args == "-dx11"
    steps = [p["step"] for n, p in events if n == "fixmygame"]
    assert steps[0] == "Checking the game" and "Testing" in steps
    await core.undo_fix_my_game(e.id, v["before"])
    assert core.library.get(e.id).launch_args == "" and (game / "data.pak").read_bytes() == b"P" * 4000
    st = await core.record_outcome(e.id, False)
    assert st == {"games": 1, "fixed": 0, "rate": 0}
    st = await core.record_outcome(e.id, True)                                       # latest answer counts
    assert st == {"games": 1, "fixed": 1, "rate": 100}

    async def nothing(entry_id, findings, generated=None, **_kw):
        return [{"label": "Current setup", "overrides": {}, "outcome": "crashed", "detail": "x"}]
    monkeypatch.setattr(core, "run_trials", nothing)
    monkeypatch.setattr(core.library, "run_trial", lambda eid, cand, n, cancel=None: {
        "label": cand["label"], "overrides": cand["overrides"], "outcome": "crashed", "detail": "x"})
    v = await core.fix_my_game(e.id)
    assert v["status"] == "not_fixed" and v["reason"]


def test_merge_overrides():
    from ezgd.library.manager import merge_overrides
    m = merge_overrides({"launch_args": "A=1 %command% -dx11"},
                        {"launch_args": "B=2 %command% -windowed", "proton_version": "GE-Proton11-7"})
    assert m == {"proton_version": "GE-Proton11-7", "launch_args": "A=1 B=2 %command% -dx11 -windowed"}
    m = merge_overrides({"compat": {"options": ["wined3d"], "fixes": [], "dll_overrides": "dxgi=b"}},
                        {"compat": {"options": ["laa"], "fixes": ["game_dlls"], "dll_overrides": "winmm=n,b"}})
    assert m["compat"]["options"] == ["laa", "wined3d"] and m["compat"]["dll_overrides"] == "dxgi=b;winmm=n,b"
    assert merge_overrides({"proton_version": "A"}, {"proton_version": "B"}) is None
    assert merge_overrides({"launch_args": "X=1 %command%"}, {"launch_args": "X=2 %command%"}) is None
    assert merge_overrides({"compat": {"dll_overrides": "d3d11=n"}}, {"compat": {"dll_overrides": "d3d11=b"}}) is None


async def test_fix_my_game_finds_a_combination(core, tmp_path, monkeypatch):
    e, root = install(core, tmp_path, {}, log="\n=== 2026-10-10 10:00:00 Proton\ncrash\n=== exited with code 1 after 2s\n")
    cands = [{"label": "Current setup", "overrides": {}},
             {"label": "Use GE-Proton11-7", "overrides": {"proton_version": "GE-Proton11-7"}},
             {"label": "Start with DirectX 11", "overrides": {"launch_args": "-dx11"}},
             {"label": "Larger audio buffer", "overrides": {"launch_args": "PULSE_LATENCY_MSEC=90 %command%"}}]
    monkeypatch.setattr(core.library, "trial_candidates", lambda *a, **k: cands)
    monkeypatch.setattr(core.library, "protons", lambda refresh=False: [type("P", (), {"name": "GE-Proton11-7"})()])

    def run_trial(entry_id, cand, n, cancel=None, **_kw):      # this "game" needs GE-Proton AND DirectX 11
        o = cand["overrides"]
        both = o.get("proton_version") == "GE-Proton11-7" and "-dx11" in (o.get("launch_args") or "")
        secs = {"Use GE-Proton11-7": 9, "Start with DirectX 11": 6}.get(cand["label"], 2)
        return {"label": cand["label"], "overrides": o, "outcome": "ran" if both else "crashed",
                "seconds": 45 if both else secs, "detail": "", "frames": True if both else None}
    monkeypatch.setattr(core.library, "run_trial", run_trial)
    monkeypatch.setattr(core, "ge_proton_status", lambda: _async({"newer_available": False}))
    v = await core.fix_my_game(e.id)
    assert v["status"] == "fixed" and v["kept"] == "Use GE-Proton11-7 + Start with DirectX 11"
    got = core.library.get(e.id)
    assert got.proton_version == "GE-Proton11-7" and "-dx11" in got.launch_args
    assert any(r.get("combined") for r in v["trials"])


async def _async(v):
    return v


async def test_fix_my_game_investigates_first_when_the_logs_say_little(core, tmp_path, monkeypatch):
    """One button: with no clear clue, "Fix my game" starts the game once with detailed logging (what the separate
    "Investigate" button did) and reasons over that run before repairing."""
    e, root = install(core, tmp_path, {}, log="\n=== 2026-10-10 10:00:00 Proton\nnothing useful\n"
                                               "=== exited with code 1 after 3s\n")
    events, started = [], []
    core.set_emitter(lambda n, p: events.append((n, p)))

    def run_trial(entry_id, cand, n, cancel=None, **_kw):
        started.append(cand["label"])
        core.library.trial_evidence.setdefault(entry_id, {})[n] = {
            "text": "0124:err:module:import_dll Library VCRUNTIME140_1.dll (which is needed by L\"C:\\\\G.exe\") not "
                    "found", "exit_code": 53, "seconds": 2, "label": cand["label"]}
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "exited", "seconds": 2,
                "detail": "exited with code 53 after 2s"}
    monkeypatch.setattr(core.library, "run_trial", run_trial)

    async def no_trials(*a, **k):
        return [{"label": "Current setup", "overrides": {}, "outcome": "exited", "detail": "x"}]
    monkeypatch.setattr(core, "run_trials", no_trials)
    tricks = []
    monkeypatch.setattr(core.library, "run_winetricks", lambda eid, verbs, wait=False: tricks.append(verbs) or 0)
    await core.fix_my_game(e.id)
    steps = [p["step"] for n, p in events if n == "fixmygame"]
    assert "Investigating" in steps and started[0] == "Current setup"
    assert tricks and tricks[0] == ["vcrun2022"]             # the investigation found the cause; it was repaired


async def test_fix_my_game_fixes_the_root_cause_instead_of_cycling_setups(core, tmp_path, monkeypatch):
    """The test run showed the game's own error ("Visual C++ 2015-2022 is required"): that is installed and the
    game tested again — no list of Protons and other setups (the user waited through all of them, 2026-10-14)."""
    e, root = install(core, tmp_path, {}, log="\n=== 2026-10-10 10:00:00 Proton\ncrash\n=== exited with code 41 after 2s\n")
    box = ('0204:trace:msgbox:MSGBOX_OnInit L"The following component(s) are required to run this program:\\n'
           'Microsoft Visual C++ 2015-2022 Redistributable (x64)"\n')
    installed: list = []
    tests: list = []

    def trial(entry_id, cand, n, cancel=None, **_kw):
        tests.append(cand["label"])
        ok = bool(installed)
        if not ok:
            core.library.trial_evidence.setdefault(entry_id, {})[n] = {
                "text": box, "exit_code": 41, "seconds": 2, "label": cand["label"], "overrides": cand["overrides"]}
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "ran" if ok else "crashed",
                "detail": "kept running" if ok else "the game showed an error"}

    async def no_broad_tests(*_a, **_kw):
        raise AssertionError("the setups list must not be needed")
    monkeypatch.setattr(core.library, "run_trial", trial)
    monkeypatch.setattr(core.library, "run_winetricks", lambda eid, verbs, wait=False: installed.extend(verbs) or 0)
    monkeypatch.setattr(core, "run_trials", no_broad_tests)
    v = await core.fix_my_game(e.id)
    assert installed == ["vcrun2022"]
    assert v["status"] == "fixed" and "Visual C++" in v["kept"]
    assert len(tests) == 3                                     # what does it do? → fixed → checked once more
