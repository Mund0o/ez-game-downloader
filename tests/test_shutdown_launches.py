"""Shutting down: nothing new is started, and a running "Fix my game" is cancelled before the database closes.
Seen in logs: a test start launched a game after "shutting down" (it ran on with nothing to stop it), and in a
friend's log fix_my_game's next step failed with "database is not open"."""

import asyncio
import sys
import threading

import pytest

from test_features_v2 import core  # noqa: F401 — the AppCore fixture

from ezgd.library.manager import LibraryError
from ezgd.platform import launcher


async def test_nothing_starts_once_the_app_is_closing(core, tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(launcher, "spawn", lambda plan, log=None: spawned.append(plan) or object())
    plan = launcher.LaunchPlan(["true"], str(tmp_path), {}, "test")
    core.library._spawn(plan)
    assert len(spawned) == 1

    async def long_job():
        core._stops_with_the_app()                    # as fix_my_game / run_trials do
        await asyncio.sleep(60)
    job = asyncio.create_task(long_job())
    await asyncio.sleep(0)
    ev = core._trial_cancel = threading.Event()
    await core.shutdown()
    await asyncio.sleep(0)
    assert job.cancelled() and ev.is_set() and core.library.closing
    with pytest.raises(LibraryError, match="closing"):
        core.library._spawn(plan)
    assert len(spawned) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Linux launch semantics")
async def test_a_test_start_while_closing_is_an_error_not_a_game(core, tmp_path):
    from ezgd.library.models import LibraryEntry, LibraryStatus
    folder = tmp_path / "g"
    folder.mkdir()
    exe = folder / "run.sh"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    e = core.library.repo.insert(LibraryEntry(title="T", status=LibraryStatus.INSTALLED, install_path=str(folder),
                                              executable=str(exe), executable_trusted=True))
    core.library.closing = True
    with pytest.raises(LibraryError, match="closing"):
        core.library.launch(e.id, confirmed=True)
    assert not core.library.is_running(e.id)
    core.library.closing = False
