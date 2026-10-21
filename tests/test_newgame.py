"""A newly installed game is checked once, hidden, before anyone presses Play (the real KWin is never touched)."""

from test_release_fixes import core, install  # noqa: F401 — the shared AppCore fixture
from test_tracer import wine_log


async def test_a_new_game_is_checked_out_of_sight(core, tmp_path, monkeypatch):  # noqa: F811
    from ezgd.platform import hidden
    e, root = install(core, tmp_path, {"Game/dwmapi.dll": b"MZ", "Game/ue4ss/UE4SS.dll": b"MZ"}, exe="Game/Game.exe")
    folder = root / "Game"
    monkeypatch.setattr(hidden, "available", lambda: True)
    monkeypatch.setattr(core, "_memory_path", lambda: tmp_path / "mem.json")
    hidden_flags = []
    options = []

    def trial(entry_id, cand, n, cancel=None, **kw):
        hidden_flags.append(kw.get("hidden"))
        options.append(kw)
        compat = (cand["overrides"] or {}).get("compat") or core.library.get(entry_id).compat
        own = "dwmapi=n,b" in (compat.get("dll_overrides") or "")
        core.library.trial_evidence.setdefault(entry_id, {})[n] = {
            "text": wine_log(folder, "Game.exe", own, crash=not own, menu=own), "exit_code": None if own else 5,
            "seconds": 45 if own else 10, "label": cand["label"], "overrides": cand["overrides"]}
        return {"label": cand["label"], "overrides": cand["overrides"], "outcome": "ran" if own else "crashed",
                "stuck": False, "progress": [], "detail": "it showed its menu" if own else "stopped after 10s"}
    monkeypatch.setattr(core.library, "run_trial", trial)
    await core.check_new_game(e.id)
    st = await core.new_game_check(e.id)
    assert st["status"] == "fixed" and "dwmapi" in st["detail"], st
    assert hidden_flags == [True]                                   # one start, out of sight — no investigation
    assert options[0]["seconds"] <= 25 and options[0]["early"]      # short, and done at the menu
    assert "dwmapi=n,b" in core.library.get(e.id).compat["dll_overrides"]
    await core.check_new_game(e.id)                                 # checked once only
    assert (await core.new_game_check(e.id))["status"] == "fixed"


async def test_the_check_waits_while_a_game_is_played(core, tmp_path, monkeypatch):  # noqa: F811
    from ezgd.platform import hidden
    e, _root = install(core, tmp_path, {}, exe="Game/Game.exe")
    monkeypatch.setattr(hidden, "available", lambda: True)
    monkeypatch.setattr(core.library, "running_ids", lambda: [99])
    monkeypatch.setattr(core, "NEW_GAME_WAIT", 0)
    started = []
    monkeypatch.setattr(core.library, "run_trial", lambda *a, **k: started.append(1))
    assert await core.check_new_game(e.id) is None
    assert not started and (await core.new_game_check(e.id))["status"] == "skipped"
    core.settings.set("check_new_games", False)
    core.settings.settings.check_new_games = False
    e2, _ = install(core, tmp_path / "b", {}, exe="Game/Game.exe")
    assert await core.check_new_game(e2.id) is None and (await core.new_game_check(e2.id)) == {}


async def test_a_working_new_game_is_ready_after_one_short_start(core, tmp_path, monkeypatch):  # noqa: F811
    from ezgd.platform import hidden
    e, _root = install(core, tmp_path, {}, exe="Game/Game.exe")
    monkeypatch.setattr(hidden, "available", lambda: True)
    starts = []

    def trial(entry_id, cand, n, cancel=None, **kw):
        starts.append(kw)
        return {"label": cand["label"], "overrides": {}, "outcome": "ran", "stuck": False, "picture": "menu",
                "progress": ["game loaded"], "detail": "it showed its menu after 16s"}
    monkeypatch.setattr(core.library, "run_trial", trial)
    await core.check_new_game(e.id)
    st = await core.new_game_check(e.id)
    assert st["status"] == "ready" and len(starts) == 1 and st["seconds"] < 30, st
