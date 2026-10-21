"""The screen stays on while a game runs and while couch mode is open — a controller is not activity to the desktop
(a friend's Bloodborne dimmed and locked mid-game). A fake systemd-inhibit stands in: never a real hold in tests."""

import os
import time

import pytest

from ezgd.platform import awake

from test_release_fixes import core  # noqa: F401 — the shared AppCore fixture


@pytest.fixture
def fake_inhibit(tmp_path, monkeypatch):
    """systemd-inhibit that notes its arguments and runs the command (sleep infinity) like the real one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    script = bin_dir / "systemd-inhibit"
    script.write_text(f'#!/bin/sh\necho "$@" >> {calls}\nwhile [ "${{1#--}}" != "$1" ]; do shift; done\nexec "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("EZGD_AWAKE", "1")
    monkeypatch.setattr(awake, "_shared", None)
    yield calls
    awake.shared().release_all()


def test_holds_are_named_and_end_with_their_helper(fake_inhibit):
    k = awake.KeepAwake()
    assert k.hold("game:1", "Bloodborne is running")
    p = k._holds["game:1"][0]
    time.sleep(0.2)
    assert "--what=idle" in fake_inhibit.read_text() and "Bloodborne is running" in fake_inhibit.read_text()
    assert k.hold("game:1", "Bloodborne is running") and k._holds["game:1"][0] is p      # held once
    k.sync({"game:2": "Other is running"}, "game:")
    assert set(k.held()) == {"game:2"} and p.poll() is not None                         # game 1 ended: released
    k.hold("couch", "Couch mode is open")
    k.sync({}, "game:")
    assert set(k.held()) == {"couch"}                                                    # couch mode is not a game
    k.release_all()
    assert k.held() == {}


def test_nothing_is_held_when_switched_off(fake_inhibit, monkeypatch):
    monkeypatch.setenv("EZGD_AWAKE", "0")
    assert not awake.KeepAwake().hold("game:1", "x")


async def test_a_running_game_keeps_the_screen_on(core, tmp_path, fake_inhibit):  # noqa: F811
    folder = tmp_path / "games" / "B"
    folder.mkdir(parents=True)
    script = folder / "run.sh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    e = core.library.import_folder("Bloodborne", folder)
    core.library.set_executable(e.id, script)
    core.library.launch(e.id, confirmed=True)
    core._keep_awake()
    assert awake.shared().held() == {f"game:{e.id}": "Bloodborne is running"}
    core.library._suspended[e.id] = {"at": time.monotonic(), "pids": []}               # paused from the quick menu
    core._keep_awake()
    assert awake.shared().held() == {}
    core.library._suspended.pop(e.id)
    core.settings.set("keep_awake", False)
    core._keep_awake()
    assert awake.shared().held() == {}
    core.settings.set("keep_awake", True)
    core._keep_awake()
    assert awake.shared().held()
    core.library.stop(e.id)
    for _ in range(50):
        core.library.reap()
        if not core.library.is_running(e.id):
            break
        time.sleep(0.1)
    core._keep_awake()
    assert awake.shared().held() == {}
