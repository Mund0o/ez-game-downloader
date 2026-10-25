"""A game started here that never talks to Steam gets a game controller layout, not Steam's desktop one (a friend's
Bloodborne: the controller moved the mouse). The real Steam client is never told anything: a fake one answers in
its controller log the way Steam does."""

import time

import pytest

from ezgd.library import steamrun
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
                         f"{appid or 413080}\n")
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


def test_the_game_layout_comes_back_after_the_steam_button(tmp_path, monkeypatch):
    """Steam drops a forced layout when its Steam button is pressed: the chord layout, then the desktop one — the
    virtual pad goes and the game has no controller (a friend's Dark Souls III). The desktop layout is asked for
    while the quick menu is open (its keys drive the menu — Steam does not always fall back to them by itself), and
    the game layout again as soon as it closes."""
    root, sent, opener = fake_steam(tmp_path)
    monkeypatch.setattr(steaminput.Forced, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(steaminput.Forced, "RETRY_SECONDS", 0.1)
    log = root / "logs" / "controller.txt"
    desktop = "steam://forceinputappid/413080"
    game_layout = "steam://forceinputappid/480"

    def steam_switches(*apps):
        with open(log, "a") as fh:
            for a in apps:
                fh.write(f"[2026-10-23 03:19:49] Queueing activation for controller: 0 app: {a}\n")
    f = steaminput.Forced(tmp_path / "marker", opener, root=lambda: root)
    f.start(1)
    assert sent == [game_layout]
    time.sleep(0.2)
    f.hold(True)                                       # the quick menu opened with the Steam button
    assert sent == [game_layout, desktop]              # the menu's keys come from the desktop layout
    steam_switches(443510, 413080)
    time.sleep(0.3)
    assert sent == [game_layout, desktop]              # nothing forced while it is open
    f.hold(False)                                      # closed: the game layout again (and Steam takes it)
    deadline = time.monotonic() + 3
    while sent[-1] != game_layout and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sent[-1] == game_layout
    time.sleep(0.3)
    assert sent[-1] == game_layout and sent.count(desktop) == 1
    n = len(sent)
    steam_switches(443510, 413080)                     # a tap without the menu: back by itself
    while len(sent) == n and time.monotonic() < deadline + 3:
        time.sleep(0.02)
    assert sent[-1] == game_layout and sent.count(desktop) == 1
    time.sleep(0.2)
    n = len(sent)
    steam_switches(4000)                               # a game Steam started itself: its layout is left alone
    time.sleep(0.3)
    assert len(sent) == n
    assert f.end(1) and sent[-1] == "steam://forceinputappid/0"


def test_a_line_steam_is_still_writing_is_read_next_time(tmp_path):
    root, _sent, _opener = fake_steam(tmp_path)
    log = root / "logs" / "controller.txt"
    pos = steaminput.log_size(root)
    with open(log, "a") as fh:
        fh.write("[x] Queueing activation for controller: 0 app: 480\n[x] Queueing activation for contr")
    got, pos = steaminput.read_activations(root, pos)
    assert got == [(0, 480)]
    with open(log, "a") as fh:
        fh.write("oller: 0 app: 413080\n")
    assert steaminput.read_activations(root, pos)[0] == [(0, 413080)]


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


async def test_a_game_started_through_steam_gets_the_game_layout(steam_here, tmp_path, monkeypatch):
    """Steam starts only this app's shortcut (play mode), not the game: its layout for the shortcut is not the
    game's, and the quick menu needs the desktop one — so play-mode games are forced like direct ones."""
    core, sent = steam_here
    e = game(core, tmp_path)
    monkeypatch.setattr(steamrun, "sessions", lambda _d: {e.id: {"started": time.time()}})
    core.library.reap()
    assert sent == ["steam://forceinputappid/480"]
    core.library.input_hold(True)
    assert sent[-1] == "steam://forceinputappid/413080"
    core.library.input_hold(False)
    assert sent[-1] == "steam://forceinputappid/480"
    monkeypatch.setattr(steamrun, "sessions", lambda _d: {})
    core.library.reap()
    assert sent[-1] == "steam://forceinputappid/0"


async def test_a_game_steam_started_that_talks_to_steam_is_left_to_steam(steam_here, tmp_path, monkeypatch):
    """OnlineFix games sign in to Steam themselves: Steam knows the game and its layout, so play mode does not
    override it. The quick menu still needs the desktop layout, and hands the choice back afterwards."""
    core, sent = steam_here
    e = game(core, tmp_path, "OnlineFix game")
    monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e: True)
    monkeypatch.setattr(steamrun, "sessions", lambda _d: {e.id: {"started": time.time()}})
    core.library.reap()
    assert sent == []
    core.library.input_hold(True)
    assert sent[-1] == "steam://forceinputappid/413080"
    core.library.input_hold(False)
    assert sent[-1] == "steam://forceinputappid/0"
    monkeypatch.setattr(steamrun, "sessions", lambda _d: {})
    core.library.reap()


async def test_the_quick_menu_over_a_game_left_to_steam_hands_the_layout_back(steam_here, tmp_path, monkeypatch):
    core, sent = steam_here
    e = game(core, tmp_path, "OnlineFix game")
    monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e: True)
    marker = core.paths.data / "steam-input-forced"
    core.library.launch(e.id, confirmed=True)
    assert sent == []                                  # the game layout is Steam's to choose
    core.library.input_hold(True)
    assert sent[-1] == "steam://forceinputappid/413080" and marker.is_file()
    assert core.library.input_layout.active            # (the app must hand this back if it quits now)
    core.library.input_hold(False)
    assert sent[-1] == "steam://forceinputappid/0"     # not 480: Steam chooses again
    assert not marker.exists() and not core.library.input_layout.active
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
