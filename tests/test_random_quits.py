"""A game that quits at random, later than a test lasts (Ghost Recon Wildlands: from 5 s to 4 minutes in): its recent
starts say how long to watch, every test of that Fix my game may run that long — and still ends as soon as the game
fails. A 45-second run that "kept running" proved nothing there, and once a lucky run was taken for a fix."""

from ezgd.library import history

from test_release_fixes import core, install  # noqa: F401 — the shared AppCore fixture

LOG = """
=== 2026-10-24 10:00:00 Proton-GE Latest
=== exited with code 255 after 70s
=== 2026-10-24 11:00:00 Proton-GE Latest
=== exited with code 255 after 240s
=== 2026-10-24 12:00:00 Proton-GE Latest
=== exited with code -15 after 900s
=== 2026-10-24 13:00:00 Proton-GE Latest
=== exited with code 0 after 300s
=== 2026-10-24 14:00:00 Proton-GE Latest
=== exited with code 255 after 12s
=== 2026-10-24 15:00:00 Proton-GE Latest · diagnostic run
=== exited with code 1 after 100s
"""


def test_quits_later_than_a_test_are_found_in_the_games_history(tmp_path):
    log = tmp_path / "game.log"
    log.write_text(LOG)
    # stopped by the player (-15), quit normally (0), within the test (12 s), diagnostic runs: not counted
    assert history.late_quits(history.runs(log), after=45) == [70, 240]
    assert history.late_quits(history.runs(log), after=45, until=200) == [70]


async def test_every_test_watches_longer_and_says_why(core, tmp_path):  # noqa: F811
    e, _root = install(core, tmp_path, {}, log=LOG)
    assert core.library.trial_watch == 0
    with core._watching_longer(e.id):
        assert core.library.trial_watch == 240 * 1.5 + 30
        assert "quit by themselves after 70 s, 4 min" in core._watch_note() and "6.5 min" in core._watch_note()
        with core._watching_longer(e.id):                 # nested (Try fixes inside Fix my game): the outer wins
            assert core.library.trial_watch == 390
    assert core.library.trial_watch == 0 and core._watch_note() == ""
    (core.paths.logs / f"game-{e.id}.log").write_text("=== 2026-10-24 10:00:00 Proton\n=== exited with code 0 after "
                                                       "600s\n")
    with core._watching_longer(e.id):
        assert core.library.trial_watch == 0               # no quits of its own: normal tests


def test_a_watched_test_sees_the_quit_a_short_one_misses(core, tmp_path, monkeypatch):  # noqa: F811
    folder = tmp_path / "games" / "Q"
    folder.mkdir(parents=True)
    script = folder / "run.sh"
    script.write_text("#!/bin/sh\nsleep 4\nexit 1\n")
    script.chmod(0o755)
    e = core.library.import_folder("Q", folder)
    core.library.set_executable(e.id, script)
    monkeypatch.setattr(core.library, "TRIAL_SECONDS", 2)
    monkeypatch.setattr(core.library, "SCREEN_LOOKS", ())
    assert core.library.run_trial(e.id, {"label": "short", "overrides": {}}, 1)["outcome"] == "ran"
    core.library.trial_watch = 15
    try:
        r = core.library.run_trial(e.id, {"label": "watched", "overrides": {}}, 2)
    finally:
        core.library.trial_watch = 0
    assert r["outcome"] == "exited" and r["seconds"] < 12, r       # it ended when the game quit, not at 15 s
