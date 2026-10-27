"""Following the error: the investigator reads a run, asks why it ended, tests what that points at and follows the
next error (Expedition 33: OnlineFix's and UE4SS's loaders replaced by Wine's → a crash before the game's log)."""

from pathlib import Path

from ezgd.library import tracer

from test_release_fixes import core, install  # noqa: F401 — the shared AppCore fixture


def win(p: Path) -> str:
    """A Linux path as Wine logs it (drive letter, doubled backslashes)."""
    return "Z:" + str(p).replace("/", "\\\\")


def wine_log(folder: Path, exe: str, own_dll: bool, *, crash: bool = True, menu: bool = False,
             before_crash: str = "") -> str:
    """A diagnostic run's Wine log, shaped like the real one."""
    out = [f'0134:trace:loaddll:build_module Loaded L"{win(folder / exe)}" at 0000000140000000: native',
           '0134:trace:loaddll:build_module Loaded L"C:\\\\windows\\\\system32\\\\kernel32.dll" at 00006FFFFFE70000: '
           'builtin',
           f'0134:trace:loaddll:build_module Loaded L"{win(folder / "dwmapi.dll")}" at 0000000180000000: '
           + ("native" if own_dll else "builtin")]
    if own_dll:
        out.append(f'0134:trace:loaddll:build_module Loaded L"{win(folder / "ue4ss" / "UE4SS.dll")}" at '
                   '0000000190000000: native')
    out.append('0134:trace:loaddll:build_module Loaded L"C:\\\\Program Files (x86)\\\\Steam\\\\steamclient64.dll" at '
               '00000001A0000000: native')
    if before_crash:
        out.append(before_crash.rstrip("\n"))
    if crash:
        out += ["0134:trace:seh:dispatch_exception code=c0000005 (EXCEPTION_ACCESS_VIOLATION) flags=0 "
                "addr=0000000140001234",
                "0134:trace:seh:dispatch_exception  info[0]=0000000000000000",
                "0134:trace:seh:dispatch_exception  info[1]=0000000000000000",
                f'0134:warn:seh:virtual_unwind backtrace: 0000000140001234: L"{exe}" + 0000000000001234.',
                "0134:trace:seh:_XcptFilter (c0000005,0000000000C9D9B0)"]
    if menu:
        out.append("LogLoad: Took 2.5 seconds to LoadMap(/Game/Maps/MainMenu)")
    return "\n".join(out) + "\n"


def game(tmp_path) -> Path:
    folder = tmp_path / "games" / "G" / "Game"
    (folder / "ue4ss").mkdir(parents=True)
    (folder / "Game.exe").write_bytes(b"MZ")
    (folder / "dwmapi.dll").write_bytes(b"MZ")
    (folder / "ue4ss" / "UE4SS.dll").write_bytes(b"MZ")
    return folder


def test_the_trace_names_the_cause_and_predicts_the_fix(tmp_path):
    folder = game(tmp_path)
    t = tracer.trace(wine_log(folder, "Game.exe", own_dll=False), folder / "Game.exe", exit_code=5, seconds=10)
    assert [r.name for r in t.replaced] == ["dwmapi.dll"] and t.replaced[0].loader.startswith("UE4SS")
    assert t.replaced[0].payload_missing == ["ue4ss.dll"]
    assert t.crash is not None and t.crash.certain and t.crash.null and t.crash.module == "Game.exe"
    chain, suspects = tracer.follow(t)
    text = " ".join(chain)
    assert "access violation reading address 0" in text and "dwmapi.dll is UE4SS" in text
    assert "ue4ss.dll never loaded" in text
    top = suspects[0]
    assert top.confidence >= 0.9 and top.fix.dll_overrides == {"dwmapi": "n,b"}
    assert top.expect == {"native": ["dwmapi.dll"], "loads": ["ue4ss.dll"]}

    # the test with the change: the prediction came true, the game still crashes → kept, the next error followed
    after = tracer.trace(wine_log(folder, "Game.exe", own_dll=True), folder / "Game.exe", exit_code=5, seconds=12)
    v, why = tracer.verdict(t, after, top.expect)
    assert v == "confirmed" and "dwmapi.dll loaded" in why
    # … and with a change that did nothing, nothing is kept
    v, _ = tracer.verdict(t, t, top.expect)
    assert v == "no-change"
    # into the game: fixed
    ok = tracer.trace(wine_log(folder, "Game.exe", own_dll=True, crash=False, menu=True), folder / "Game.exe",
                      ran=True, seconds=45)
    assert tracer.verdict(t, ok, top.expect)[0] == "fixed"


async def test_fix_my_game_follows_the_error_instead_of_testing_a_list(core, tmp_path, monkeypatch):  # noqa: F811
    """The run crashes because Wine replaced the game's own loader: the investigator tests exactly that, sees the
    game get into its menu, checks once more and keeps it — no list of Protons and setups."""
    e, root = install(core, tmp_path, {"Game/dwmapi.dll": b"MZ", "Game/ue4ss/UE4SS.dll": b"MZ"},
                      log="\n=== 2026-10-20 05:39:42 Proton\n=== exited with code 5 after 10s\n")
    folder = root / "Game"
    tests: list[str] = []

    def trial(entry_id, cand, n, cancel=None, **_kw):
        tests.append(cand["label"])
        compat = (cand["overrides"] or {}).get("compat") or core.library.get(entry_id).compat
        own = "dwmapi=n,b" in (compat.get("dll_overrides") or "")
        core.library.trial_evidence.setdefault(entry_id, {})[n] = {
            "text": wine_log(folder, "Game.exe", own, crash=not own, menu=own), "exit_code": None if own else 5,
            "seconds": 45 if own else 10, "label": cand["label"], "overrides": cand["overrides"]}
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "ran" if own else "crashed",
                "stuck": False, "progress": ["Proton started"] + (["game loaded"] if own else []),
                "detail": "kept running for 45s, got to the game" if own else "stopped after 10s: access violation"}

    async def no_list(*_a, **_kw):
        raise AssertionError("the list of setups must not be needed")
    monkeypatch.setattr(core.library, "run_trial", trial)
    monkeypatch.setattr(core, "run_trials", no_list)
    events = []
    monkeypatch.setattr(core, "emit", lambda name, payload: events.append((name, payload)))
    v = await core.fix_my_game(e.id)
    assert v["status"] == "fixed", v
    assert "dwmapi.dll" in v["kept"]
    assert "dwmapi=n,b" in core.library.get(e.id).compat["dll_overrides"]      # kept for real after two passes
    assert tests[-1] == "Second check" and len(tests) <= 4
    steps = [p["step"] for n, p in events if n == "fixmygame"]
    assert "Following the error 1" in steps
    assert any("dwmapi.dll is UE4SS" in i for i in v["investigation"])


async def test_it_follows_the_next_error_after_a_fix_that_was_right(core, tmp_path, monkeypatch):  # noqa: F811
    """Two problems in a row: the loader (crash) and then a missing runtime (the game says so). The first change is
    kept because its prediction came true, and the error it uncovers is followed next."""
    e, root = install(core, tmp_path, {"Game/dwmapi.dll": b"MZ", "Game/ue4ss/UE4SS.dll": b"MZ"},
                      log="\n=== 2026-10-20 05:39:42 Proton\n=== exited with code 5 after 10s\n")
    folder = root / "Game"
    box = '0134:trace:msgbox:MSGBOX_OnInit L"VCRUNTIME140_1.dll was not found. Reinstalling may fix this."\n'
    tests: list[str] = []

    def trial(entry_id, cand, n, cancel=None, **_kw):
        tests.append(cand["label"])
        ov = cand["overrides"] or {}
        own = "dwmapi=n,b" in ((ov.get("compat") or core.library.get(entry_id).compat).get("dll_overrides") or "")
        vc = "vcrun2022" in (ov.get("winetricks") or [])
        ok = own and vc
        text = wine_log(folder, "Game.exe", own, crash=not own, menu=ok) + (box if own and not vc else "")
        core.library.trial_evidence.setdefault(entry_id, {})[n] = {
            "text": text, "exit_code": None if ok else 1, "seconds": 45 if ok else 8, "label": cand["label"],
            "overrides": ov}
        return {"label": cand["label"], "overrides": ov, "outcome": "ran" if ok else "crashed", "stuck": False,
                "progress": ["Proton started"], "detail": "kept running" if ok else "stopped"}

    async def no_list(*_a, **_kw):
        raise AssertionError("the list of setups must not be needed")
    installed: list = []
    monkeypatch.setattr(core.library, "run_trial", trial)
    monkeypatch.setattr(core.library, "run_winetricks", lambda eid, verbs, wait=False: installed.extend(verbs) or 0)
    monkeypatch.setattr(core, "run_trials", no_list)
    v = await core.fix_my_game(e.id)
    assert v["status"] == "fixed", v
    assert "dwmapi.dll" in v["kept"] and "Visual C++" in v["kept"]
    assert installed == ["vcrun2022"]                          # into the real prefix only after both passes
    assert "dwmapi=n,b" in core.library.get(e.id).compat["dll_overrides"]
    assert len(v["investigation"]) == 2 and "VCRUNTIME140_1.dll" in v["investigation"][1]


def reg_lines(key: str, found: bool) -> str:
    return (f'0134:trace:reg:NtOpenKeyEx (0x28,L"{key}",20019,0x31fed8)\n'
            f'0134:trace:reg:NtOpenKeyEx <- {"0x3c" if found else "(nil)"}\n')


def test_what_it_looked_for_before_it_died(tmp_path):
    """warn+file and +reg record what the game looked for in vain; a .reg the release ships that creates the
    missing key is the suspect, with the prediction that the key is found next time."""
    folder = game(tmp_path)
    (folder / "install.reg").write_text('Windows Registry Editor Version 5.00\r\n\r\n'
                                        '[HKEY_LOCAL_MACHINE\\SOFTWARE\\Sandfall\\Expedition33]\r\n"Path"="C:\\\\x"\r\n')
    miss = ('0134:warn:file:NtCreateFile L"\\\\??\\\\Z:\\\\games\\\\G\\\\Game\\\\Config\\\\Engine.ini" not found '
            '(c0000034)\n'
            '0134:warn:file:NtCreateFile L"\\\\??\\\\C:\\\\windows\\\\system32\\\\foo.dll" not found (c0000034)\n')
    text = wine_log(folder, "Game.exe", own_dll=True, crash=False) + miss \
        + reg_lines("\\Registry\\Machine\\Software\\Sandfall\\Expedition33", False) \
        + reg_lines("AppDefaults\\Game.exe", False) + wine_log(folder, "Game.exe", own_dll=True).split("\n", 5)[5]
    t = tracer.trace(text, folder / "Game.exe", exit_code=5, seconds=10)
    assert [p for _t, p, _a in t.files_missing] == ["Z:\\games\\G\\Game\\Config\\Engine.ini"]    # DLL search: noise
    assert [k for _t, k, _a in t.keys_missing] == ["Software\\Sandfall\\Expedition33"]           # Wine's own: noise
    chain, suspects = tracer.follow(t)
    text = " ".join(chain)
    assert "Engine.ini" in text and "Software\\Sandfall\\Expedition33" in text
    s = next(s for s in suspects if s.key == "reg:install.reg")
    assert s.confidence >= 0.85 and s.changes(None)["registry"] == [str(folder / "install.reg")]
    fixed = tracer.trace(text.replace("<- (nil)", "<- 0x3c"), folder / "Game.exe", exit_code=5, seconds=10)
    fixed.keys_traced, fixed.keys_missing = True, []
    assert tracer.verdict(t, fixed, s.expect)[0] == "confirmed"


def test_solved_crashes_are_recognised_in_another_game(tmp_path):
    """The same failure in a different game (the same loader replaced by Wine's): what fixed it is tried first.
    Changes that did not help a game are not tested on it again."""
    folder = game(tmp_path)
    mem = tmp_path / "crash_memory.json"
    t = tracer.trace(wine_log(folder, "Game.exe", own_dll=False), folder / "Game.exe", exit_code=5, seconds=10)
    _chain, sus = tracer.follow(t)
    tracer.remember(mem, t, "Unreal Engine", "Expedition 33", [sus[0].fix.to_dict()], {"compat": {}}, [sus[0].title])
    other = tmp_path / "other"
    other_folder = game(other)
    t2 = tracer.trace(wine_log(other_folder, "Game.exe", own_dll=False), other_folder / "Game.exe", exit_code=5,
                      seconds=9)
    got = tracer.recall(mem, t2, "Unreal Engine", "Another Game")
    assert got and got[0].confidence >= 0.85 and got[0].fix.dll_overrides == {"dwmapi": "n,b"}
    assert "Expedition 33" in got[0].why
    tracer.remember_no_help(mem, t2, "Unreal Engine", "Another Game", ["finding:x"])
    assert tracer.no_help(mem, t2, "Unreal Engine", "Another Game") == {"finding:x"}
    assert tracer.no_help(mem, t2, "Unreal Engine", "A Third Game") == set()


def proton(tmp_path, name: str, version: str):
    from ezgd.platform.steam import ProtonInstall
    d = tmp_path / "compatibilitytools.d" / name
    d.mkdir(parents=True)
    (d / "proton").write_text(f'#!/usr/bin/env python3\nCURRENT_PREFIX_VERSION="{version}"\n')
    return ProtonInstall(name, d)


def test_a_switch_of_proton_is_remembered_for_other_games(tmp_path):
    """GE-Proton fixed a game: other games with the same failure try "a GE-Proton" — the one installed here, by
    whatever name — not the folder name it had in the first game (it may not exist, or be another computer's)."""
    folder = game(tmp_path)
    mem = tmp_path / "crash_memory.json"
    ge, cachy = proton(tmp_path, "Proton-GE Latest", "GE-Proton11-7"), proton(tmp_path, "cachy", "CachyOS-11.0-100")
    assert (tracer.proton_kind(ge), tracer.proton_kind(cachy)) == ("ge", "cachyos")
    fix = tracer.proton_fix(ge)
    assert fix["proton_kind"] == "ge" and fix["label"] == "Use GE-Proton"
    t = tracer.trace(wine_log(folder, "Game.exe", own_dll=False), folder / "Game.exe", exit_code=5, seconds=10)
    tracer.remember(mem, t, "Unreal Engine", "First Game", [fix], {"proton_version": "Proton-GE Latest"},
                    ["Use Proton-GE Latest"])
    other = game(tmp_path / "other")
    t2 = tracer.trace(wine_log(other, "Game.exe", own_dll=False), other / "Game.exe", exit_code=5, seconds=9)
    mine = proton(tmp_path / "here", "GE-Proton11-9", "GE-Proton11-9")
    got = [s for s in tracer.recall(mem, t2, "Unreal Engine", "Another Game", protons=[cachy, mine], current=cachy)
           if s.fix is not None and s.fix.proton_kind]
    from ezgd.library.models import LibraryEntry, LibraryStatus
    entry = LibraryEntry(title="Another Game", status=LibraryStatus.INSTALLED, id=2, executable=str(other / "Game.exe"))
    assert got and got[0].changes(entry) == {"proton_version": "GE-Proton11-9"} and got[0].title == "Use GE-Proton11-9"
    # nothing to test when the game already runs on that kind, or no Proton of that kind is installed
    for protons, current in (([cachy, mine], mine), ([cachy], cachy)):
        assert not any(s.fix is not None and s.fix.proton_kind for s in
                       tracer.recall(mem, t2, "Unreal Engine", "Another Game", protons=protons, current=current))
    # shared fixes keep the kind (and only known kinds)
    assert tracer._clean_fix(fix)["proton_kind"] == "ge"
    assert tracer._clean_fix({**fix, "proton_kind": "/home/x/evil"}) is None
    assert "proton_kind" not in tracer._clean_fix({**fix, "proton_kind": ""})


def test_a_run_without_evidence_is_not_remembered_and_a_no_takes_it_back(tmp_path):
    """2026-10-25 a "fix" kept on a run that simply kept going (no crash, no error: an empty fingerprint) was wrong
    (Wildlands → Proton-CachyOS) — such runs are not remembered; and "Did it work? No" forgets what the run kept."""
    import time
    folder = game(tmp_path)
    mem = tmp_path / "crash_memory.json"
    ran = tracer.trace(wine_log(folder, "Game.exe", own_dll=True, crash=False), folder / "Game.exe", ran=True,
                       seconds=45)
    tracer.remember(mem, ran, "", "Wildlands", [], {"proton_version": "cachy"}, ["Use cachy"])
    assert not mem.exists() or tracer._load_memory(mem)["solved"] == []
    t = tracer.trace(wine_log(folder, "Game.exe", own_dll=False), folder / "Game.exe", exit_code=5, seconds=10)
    tracer.remember(mem, t, "", "Old", [], {"proton_version": "x"}, ["x"])
    since = time.time()
    tracer.remember(mem, t, "", "Wildlands", [], {"proton_version": "cachy"}, ["Use cachy"])
    assert tracer.forget(mem, "Wildlands", since) == 1
    assert [r["game"] for r in tracer._load_memory(mem)["solved"]] == ["Old"]


def test_a_failing_run_is_compared_with_the_one_that_worked(tmp_path):
    folder = game(tmp_path)
    ok = tracer.trace(wine_log(folder, "Game.exe", own_dll=True, crash=False, menu=True), folder / "Game.exe",
                      ran=True, seconds=45)
    good = tracer.summary(ok)
    assert good["own"]["dwmapi.dll"] == "native" and "steamclient64.dll" in good["notable"]
    bad = tracer.trace(wine_log(folder, "Game.exe", own_dll=False), folder / "Game.exe", exit_code=5, seconds=10)
    lines, sus = tracer.compare(good, bad)
    assert "loaded its own dwmapi.dll" in " ".join(lines)
    assert sus[0].fix.dll_overrides == {"dwmapi": "n,b"} and sus[0].confidence >= 0.9


def test_long_logs_keep_their_end(tmp_path):
    """The crash is at the end of a run: a 20 MB log is read as its start and its end, not its first 8 MB."""
    from ezgd.library.manager import _read_run
    log = tmp_path / "trial.log"
    with open(log, "w") as fh:
        fh.write("=== header\n")
        start = fh.tell()
        fh.write("first line\n" + ("x" * 120 + "\n") * 170_000 + "0134:trace:seh:dispatch_exception code=c0000005\n")
    text = _read_run(log, start)
    assert text.startswith("first line") and text.rstrip().endswith("code=c0000005")
    deep = tmp_path / "deep.log"
    deep.write_text("0134:trace:reg:NtQueryValueKey (0x38,L\"x\",2,0x0,1)\n" + reg_lines("Software\\A", False))
    assert "NtQueryValueKey" not in _read_run(deep, 0, condense=True)


async def test_a_fix_is_trimmed_to_what_it_needs(core, tmp_path, monkeypatch):  # noqa: F811
    """Two changes were kept on the way (each came true as predicted), but the game only needs the second: the
    first is tested away and not saved. The investigation is written to a report."""
    e, root = install(core, tmp_path, {"Game/dwmapi.dll": b"MZ", "Game/ue4ss/UE4SS.dll": b"MZ",
                                       "Game/install.reg": b"[HKEY_LOCAL_MACHINE\\SOFTWARE\\Vendor\\G]\r\n"},
                      log="\n=== 2026-10-20 05:39:42 Proton\n=== exited with code 5 after 10s\n")
    folder = root / "Game"
    labels: list[str] = []

    def trial(entry_id, cand, n, cancel=None, **_kw):
        labels.append(cand["label"])
        ov = cand["overrides"] or {}
        own = "dwmapi=n,b" in ((ov.get("compat") or core.library.get(entry_id).compat).get("dll_overrides") or "")
        reg = bool(ov.get("registry"))
        text = wine_log(folder, "Game.exe", own, crash=not reg, menu=reg,
                        before_crash="" if reg else reg_lines("\\Registry\\Machine\\Software\\Vendor\\G", False))
        core.library.trial_evidence.setdefault(entry_id, {})[n] = {
            "text": text, "exit_code": None if reg else 5, "seconds": 45 if reg else 10, "label": cand["label"],
            "overrides": ov}
        return {"label": cand["label"], "overrides": ov, "outcome": "ran" if reg else "crashed", "stuck": False,
                "progress": [], "detail": "kept running" if reg else "stopped"}

    async def no_list(*_a, **_kw):
        raise AssertionError("the list of setups must not be needed")
    imported: list = []
    monkeypatch.setattr(core.library, "run_trial", trial)
    monkeypatch.setattr(core.library, "import_reg", lambda eid, prefix, files, p=None: imported.append(files))
    monkeypatch.setattr(core, "run_trials", no_list)
    monkeypatch.setattr(core, "_memory_path", lambda: tmp_path / "mem.json")
    v = await core.fix_my_game(e.id)
    assert v["status"] == "fixed", v
    assert any(lb.startswith("Without: Load the game's own dwmapi.dll") for lb in labels)
    assert "dwmapi" not in (core.library.get(e.id).compat.get("dll_overrides") or "")     # trimmed away
    assert imported and imported[-1][0].endswith("install.reg")                           # kept for real
    report = Path(v["report"]).read_text()
    assert "Following the error" in report and "not needed" in report
    # and the next game with the same missing key: the crash memory suggests the same import first
    assert any(r["labels"] for r in __import__("json").loads((tmp_path / "mem.json").read_text())["solved"])


async def test_a_closer_look_finds_what_the_normal_run_could_not_see(core, tmp_path, monkeypatch):  # noqa: F811
    """Nothing in the normal run points anywhere (a crash in the game's code, nothing replaced): before giving up
    the investigator records one run with registry lookups — the missing key leads to the release's .reg. A test
    that is still starting up at 45 s gets a longer run before it counts as stuck."""
    e, root = install(core, tmp_path, {"Game/install.reg": b"[HKEY_LOCAL_MACHINE\\SOFTWARE\\Vendor\\G]\r\n"},
                      log="\n=== 2026-10-20 05:39:42 Proton\n=== exited with code 5 after 10s\n")
    folder = root / "Game"
    calls: list[tuple[str, dict]] = []

    def trial(entry_id, cand, n, cancel=None, **kw):
        calls.append((cand["label"], kw))
        reg = bool((cand["overrides"] or {}).get("registry"))
        slow = reg and not kw.get("seconds")               # with the fix it needs a long first start
        keys = reg_lines("\\Registry\\Machine\\Software\\Vendor\\G", False) if kw.get("deep") and not reg else ""
        core.library.trial_evidence.setdefault(entry_id, {})[n] = {
            "text": wine_log(folder, "Game.exe", True, crash=not reg, menu=reg and not slow, before_crash=keys),
            "exit_code": None if reg else 5, "seconds": 45 if reg else 10, "label": cand["label"],
            "overrides": cand["overrides"]}
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "ran" if reg else "crashed",
                "stuck": slow, "seconds": 45, "progress": [], "detail": "kept running" if reg else "stopped"}

    async def no_list(*_a, **_kw):
        raise AssertionError("the list of setups must not be needed")
    monkeypatch.setattr(core.library, "run_trial", trial)
    monkeypatch.setattr(core.library, "import_reg", lambda *a, **k: None)
    monkeypatch.setattr(core, "run_trials", no_list)
    monkeypatch.setattr(core, "_memory_path", lambda: tmp_path / "mem.json")
    v = await core.fix_my_game(e.id)
    assert v["status"] == "fixed", v
    assert ("Looking closer", {"deep": True}) in calls
    assert any(label.endswith("(longer)") and kw.get("seconds") for label, kw in calls)


def test_the_proton_build_that_worked_is_offered_back(tmp_path):
    """It worked with GE-Proton11-5; "Proton-GE Latest" became 11-7 in place, and 11-5 is still installed under
    its own name: going back to it is the fix offered."""
    import json
    from types import SimpleNamespace
    from ezgd.library import diagnose as dg, history
    from ezgd.library.models import LibraryEntry, LibraryStatus
    from pe_builder import build_pe
    g = tmp_path / "Game"
    g.mkdir()
    (g / "Game.exe").write_bytes(build_pe(["kernel32.dll"], 64))
    old = tmp_path / "GE-Proton11-5"
    old.mkdir()
    (old / "version").write_text("1786437966 GE-Proton11-5\n")
    protons = [SimpleNamespace(name="Proton-GE Latest", path=tmp_path / "latest"),
               SimpleNamespace(name="GE-Proton11-5", path=old)]

    def run(when, build, code, secs):
        snap = {"proton": "Proton-GE Latest", "proton_build": build}
        return (f"\n=== {when} Proton-GE Latest\n{history.SETUP_MARK}{json.dumps(snap)}\n"
                f"=== exited with code {code} after {secs}s\n")
    log = tmp_path / "game-1.log"
    log.write_text(run("2026-10-18 10:00:00", "1786437966 GE-Proton11-5", 0, 3600)
                   + run("2026-10-20 10:00:00", "1790000000 GE-Proton11-7", 53, 3))
    e = LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"),
                     install_path=str(g))
    d = dg.diagnose(e, tmp_path / "pfx", log, protons=protons, proton_name="Proton-GE Latest").to_dict()
    f = next(f for f in d["findings"] if f["key"] == "regression")
    assert json.loads(f["fix"]["value"]) == {"proton_version": "GE-Proton11-5"}


def test_a_closer_look_is_reduced_to_what_was_missing(tmp_path):
    """+reg and trace+file record every lookup (hundreds of MB for a big game). What did not exist is worked out
    from the test copy itself — case-insensitively, as Windows does — and only that is kept (warn+file cannot be
    used: GE-Proton's unterminated warning made Wine abort)."""
    import os
    from ezgd.library.manager import _essentials, _mark_missing_files
    (tmp_path / "pfx" / "dosdevices").mkdir(parents=True)
    (tmp_path / "pfx" / "drive_c" / "Game").mkdir(parents=True)
    (tmp_path / "pfx" / "drive_c" / "Game" / "Real.ini").write_text("x")
    os.symlink("../drive_c", tmp_path / "pfx" / "dosdevices" / "c:")

    def req(p: str, disp: int = 1) -> str:
        return (f'0134:trace:file:NtCreateFile handle=0x30f6c8 access=80100080 name=L"\\\\??\\\\{p}" objattr=00000040 '
                f"root=(nil) sec=(nil) io=0x30f6f0 alloc_size=(nil) attr=00000080 sharing=00000007 disp={disp} "
                "options=00000060 ea=(nil).0x00000000\n")
    text = (req("C:\\\\Game\\\\real.INI") + req("C:\\\\Game\\\\Config\\\\Engine.ini")
            + req("C:\\\\Game\\\\new.sav", disp=5) + "0134:trace:file:RtlGetFullPathName_U (x)\n"
            + reg_lines("\\Registry\\Machine\\Software\\Vendor\\G", False) + reg_lines("Software\\Wine", True)
            + "0134:trace:loaddll:build_module Loaded L\"C:\\\\Game\\\\x.dll\" at 0000000180000000: native\n")
    out = _mark_missing_files(text, tmp_path)
    assert 'ezgd:missing-file L"C:\\Game\\Config\\Engine.ini"' in out and "real.INI" not in out
    assert "new.sav" not in out                                   # created, not opened: not a miss
    assert 'ezgd:missing-key L"\\Registry\\Machine\\Software\\Vendor\\G"' in out
    assert "trace:reg" not in out and "trace:file" not in out and "trace:loaddll" in out
    t = tracer.Trace()
    tracer._lookups(out, t, [])
    assert t.keys_traced and [k for _t, k, _a in t.keys_missing] == ["Software\\Vendor\\G"]
    middle = ("0134:trace:seh:noise\n" * 1000) + "0134:trace:loaddll:build_module Loaded L\"x\" at 0: native\n"
    assert _essentials(middle).count("\n") == 1


# ------------------------------------------------------------------ games that just quit (#3)
def relay_run(folder: Path, child_ok: bool = True, quitter: str | None = None) -> str:
    """A closer-look run: a launcher starts the real game; Wine traced who started what and who quit."""
    exe = win(folder / "Game.exe")
    out = [f'00e4:trace:loaddll:build_module Loaded L"{exe}" at 0000000140000000: native',
           f'00e4:Call KERNEL32.CreateProcessW(140002000 L"{win(folder / "Bin" / "Real-Win64-Shipping.exe")}",'
           '00000000,00000000,00000000,00000000,00000000,00000000,00000000,0010ff10,0010fef0) ret=140001097',
           f'00e4:Ret  KERNEL32.CreateProcessW() retval={"00000001" if child_ok else "00000000"} ret=140001097',
           "00e4:Call KERNEL32.ExitProcess(00000000) ret=14000109f"]
    if quitter:
        out += [f'00f0:trace:loaddll:build_module Loaded L"{win(folder / "Bin" / "Real-Win64-Shipping.exe")}" at '
                "0000000150000000: native",
                f'00f0:trace:loaddll:build_module Loaded L"{win(folder / "Bin" / quitter)}" at 0000000180000000: native',
                "00f0:Call KERNEL32.ExitProcess(00000001) ret=180001234"]
    return "\n".join(out) + "\n"


def test_a_launcher_that_starts_the_real_game_and_quits(tmp_path):
    folder = tmp_path / "games" / "L"
    (folder / "Bin").mkdir(parents=True)
    (folder / "Game.exe").write_bytes(b"MZ")
    (folder / "Bin" / "Real-Win64-Shipping.exe").write_bytes(b"MZ")
    t = tracer.trace(relay_run(folder, child_ok=False), folder / "Game.exe", exit_code=0, seconds=2)
    assert t.launches[0]["module"] == "Game.exe" and t.launches[0]["ok"] is False
    chain, sus = tracer.follow(t)
    text = " ".join(chain)
    assert "tried to start" in text and "failed" in text and "it is a launcher" in text
    assert sus[0].title == "Start Real-Win64-Shipping.exe directly"
    assert sus[0].changes(None) == {"executable": str(folder / "Bin" / "Real-Win64-Shipping.exe")}


def test_who_ended_the_game_is_named(tmp_path):
    folder = tmp_path / "games" / "Q"
    (folder / "Bin").mkdir(parents=True)
    (folder / "Game.exe").write_bytes(b"MZ")
    (folder / "Bin" / "Real-Win64-Shipping.exe").write_bytes(b"MZ")
    t = tracer.trace(relay_run(folder, quitter="steam_api64.dll"), folder / "Game.exe", exit_code=1, seconds=4)
    quit_ = [x for x in t.exits if x["module"] == "steam_api64.dll"]
    assert quit_ and quit_[0]["code"] == 1
    text = " ".join(tracer.follow(t)[0])
    assert "steam_api64.dll ended the game — it called ExitProcess(1)" in text and "Steam is not running" in text


def test_the_games_own_log_is_quoted_when_it_failed(tmp_path):
    folder = game(tmp_path)
    extra = ("ezgd-gamelog: /x/Saved/Logs/Game.log\nLoading level\n"
             "Fatal error: Failed to create the D3D12 device (DXGI_ERROR_UNSUPPORTED)\n")
    text = wine_log(folder, "Game.exe", own_dll=True, crash=False) + "[S_API FAIL] SteamAPI_Init() failed; no appID\n"
    t = tracer.trace(text, folder / "Game.exe", exit_code=1, seconds=3, extra=extra)
    assert t.game_errors and "D3D12" in t.game_errors[0][1] and t.steam_fail
    chain, sus = tracer.follow(t)
    joined = " ".join(chain)
    assert "Game.log" in joined and "SteamAPI_Init() failed" in joined
    assert any(s.fix is not None and s.fix.args == "-dx11" for s in sus)        # DirectX 12 failing → DX11
    ok = tracer.trace(text, folder / "Game.exe", ran=True, seconds=45, extra=extra)
    assert "Game.log" not in " ".join(tracer.follow(ok)[0])                     # a run that worked: not quoted
    good = tracer.summary(t)
    assert "Game.log" not in " ".join(tracer.follow(t, None, good)[0])          # it said that when it worked too


def test_the_games_logs_are_found_in_any_engine(tmp_path):
    import os
    import time as _t
    from ezgd.library.manager import GAMELOG, _mark_missing_files, _read_run, run_logs
    folder = game(tmp_path)
    (folder / "console.log").write_text("map loaded\nERROR: could not open file\n")
    (folder / "System.Console.dll").write_bytes(b"MZ binary")                     # not a log
    users = tmp_path / "pfx" / "pfx" / "drive_c" / "users" / "steamuser"
    (users / "AppData" / "LocalLow" / "Dev" / "Game").mkdir(parents=True)
    (users / "AppData" / "LocalLow" / "Dev" / "Game" / "Player.log").write_text("NullReferenceException\n")
    (users / "Temp").mkdir(parents=True)
    (users / "Temp" / "dd_vcredist_amd64.log").write_text("installer\n")         # an installer's, not the game's
    old = folder / "old.log"
    old.write_text("from last week\n")
    os.utime(old, (_t.time() - 86400, _t.time() - 86400))
    text = run_logs(folder / "Game.exe", tmp_path / "pfx", _t.time() - 60)
    names = sorted(b.split("\n", 1)[0].rsplit("/", 1)[-1] for b in text.split(GAMELOG)[1:])
    assert names == ["Player.log", "console.log"]
    deep = tmp_path / "deep.log"
    deep.write_text("00e4:Call window proc 00006FFFE9D93A00 (hwnd=1)\n00e4:Call PE DLL (proc=1)\n"
                    "00e4:Call KERNEL32.ExitProcess(00000001) ret=14000109f\n")
    kept = _read_run(deep, 0, condense=True)
    assert "window proc" not in kept and "PE DLL" not in kept and "ExitProcess" in kept
    _ = _mark_missing_files
