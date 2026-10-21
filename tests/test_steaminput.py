"""A game started here that never talks to Steam gets a game controller layout, not Steam's desktop one (a friend's
Bloodborne: the controller moved the mouse). The real Steam client is never told anything: a fake one answers in
its controller log the way Steam does."""

import time

import pytest

from ezgd.platform import steam, steaminput

from test_release_fixes import core  # noqa: F401 — the shared AppCore fixture


def fake_steam(tmp_path, answer: int | None = 480):
    """A Steam folder whose controller log switches to ``answer`` when a layout is forced (None: no controller)."""
    root = tmp_path / "Steam"
    (root / "logs").mkdir(parents=True)
    log = root / "logs" / "controller.txt"
    log.write_text("[2026-10-21 07:14:42] Queueing activation for controller: 0 app: 413080\n")
    sent = []

    def opener(url):
        sent.append(url)
        appid = int(url.rsplit("/", 1)[1])
        if answer is not None:
            with open(log, "a") as fh:
                fh.write(f"[2026-10-21 10:00:00] Queueing activation for controller: 0 app: "
                         f"{answer if appid else 413080}\n")
    return root, sent, opener


def test_switches_are_read_from_steams_log(tmp_path):
    root, sent, opener = fake_steam(tmp_path)
    since = steaminput.log_size(root)
    assert steaminput.activations(root, 0) == [(0, 413080)]
    f = steaminput.Forced(tmp_path / "marker", opener)
    f.start(1)
    assert steaminput.took(root, since, 480, wait=0.5) is True
    f.start(2)                                         # a second game: already forced (sent again: harmless)
    assert not f.end(1) and f.marker.is_file()         # one still runs
    assert f.end(2) and sent[-1] == "steam://forceinputappid/0" and not f.marker.is_file()
    root2, _s, _o = fake_steam(tmp_path / "b", answer=None)
    assert steaminput.took(root2, steaminput.log_size(root2), 480, wait=0.3) is None      # no controller


@pytest.fixture
def steam_here(core, tmp_path, monkeypatch):  # noqa: F811
    root, sent, opener = fake_steam(tmp_path)
    monkeypatch.setattr(steaminput, "enabled", lambda: True)
    monkeypatch.setattr(steam, "is_steam_running", lambda: True)
    monkeypatch.setattr(core.library, "steam_root", lambda: root)
    monkeypatch.setattr(core.library.input_layout, "_open", opener)
    return core, sent


def game(core, tmp_path, title="Bloodborne"):  # noqa: F811
    folder = tmp_path / "games" / title
    folder.mkdir(parents=True)
    script = folder / "run.sh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    e = core.library.import_folder(title, folder)
    core.library.set_executable(e.id, script)
    return e


def ended(core, eid):  # noqa: F811
    core.library.stop(eid)
    for _ in range(50):
        core.library.reap()
        if not core.library.is_running(eid):
            return
        time.sleep(0.1)


async def test_a_game_that_never_talks_to_steam_gets_a_game_layout(steam_here, tmp_path):
    core, sent = steam_here
    e = game(core, tmp_path)
    core.library.launch(e.id, confirmed=True)
    assert sent == ["steam://forceinputappid/480"]
    log_path = core.paths.logs / f"game-{e.id}.log"
    for _ in range(40):
        if "ezgd:steam-input" in log_path.read_text():
            break
        time.sleep(0.25)
    assert "ezgd:steam-input Steam switched the controller to the game layout" in log_path.read_text()
    ended(core, e.id)
    assert sent[-1] == "steam://forceinputappid/0"


async def test_left_alone_when_steam_switches_by_itself_or_it_is_off(steam_here, tmp_path, monkeypatch):
    core, sent = steam_here
    e = game(core, tmp_path, "OnlineFix game")
    monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e: True)    # signs in to Steam as 480
    core.library.launch(e.id, confirmed=True)
    ended(core, e.id)
    monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e: False)
    core.settings.set("steam_input_layout", False)
    core.library.launch(e.id, confirmed=True)
    ended(core, e.id)
    assert sent == []


async def test_a_layout_left_forced_is_handed_back(core, tmp_path, monkeypatch):  # noqa: F811
    """The app crashed while a game had the game layout: the next start gives the choice back to Steam (else the
    controller stays a gamepad on the desktop)."""
    from ezgd.library.manager import LibraryManager
    (core.paths.data / "steam-input-forced").write_text("480")
    sent = []
    monkeypatch.setattr(steaminput, "enabled", lambda: True)
    monkeypatch.setattr(steam, "open_steam_url", sent.append)
    LibraryManager(core.library.repo, core.library.games, core.library.settings, core.paths, lambda n, p: None)
    assert sent == ["steam://forceinputappid/0"] and not (core.paths.data / "steam-input-forced").exists()
