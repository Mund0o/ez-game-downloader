"""A game started here that never talks to Steam gets a game controller layout, not Steam's desktop one (a friend's
Bloodborne: the controller moved the mouse). The real Steam client is never told anything: a fake one answers in
its controller log the way Steam does."""

import time

import pytest

from ezgd.library import steamrun
from ezgd.platform import steam, steaminput

from test_release_fixes import core  # noqa: F401 — the shared AppCore fixture


def fake_steam(tmp_path, answer: int | None = 480):
    """A Steam folder whose controller log switches to a forced layout the way Steam does (measured 2026-10-27:
    ``/0`` switches nothing by itself). ``answer`` None: no controller connected, nothing is logged."""
    root = tmp_path / "Steam"
    (root / "logs").mkdir(parents=True)
    log = root / "logs" / "controller.txt"
    log.write_text("[2026-10-21 07:14:42] Queueing activation for controller: 0 app: 413080\n")
    sent = []

    def opener(url):
        sent.append(url)
        appid = int(url.rsplit("/", 1)[1])
        if answer is not None and appid:
            with open(log, "a") as fh:
                fh.write(f"[2026-10-21 10:00:00] Queueing activation for controller: 0 app: {appid}\n")
            with open(root / "logs" / "controller_ui.txt", "a") as fh:
                fh.write(f"[2026-10-21 10:00:00] OnFocusWindowChanged URL Forcing to window type: "
                         f"k_EWindowTypeGame, {appid}\n")
    return root, sent, opener


def until(cond, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


def test_switches_are_read_from_steams_log(tmp_path):
    root, sent, opener = fake_steam(tmp_path)
    since = steaminput.log_size(root)
    assert steaminput.activations(root, 0) == [(0, 413080)]
    f = steaminput.Forced(tmp_path / "marker", opener)
    f.start(1)
    assert steaminput.took(root, since, 480, wait=0.5) is True
    f.start(2)                                         # a second game: already forced (sent again: harmless)
    assert not f.end(1) and f.marker.is_file()         # one still runs
    assert f.end(2, desktop=False) and sent[-1] == "steam://forceinputappid/0" and not f.marker.is_file()
    root2, _s, _o = fake_steam(tmp_path / "b", answer=None)
    assert steaminput.took(root2, steaminput.log_size(root2), 480, wait=0.3) is None      # no controller


def test_handing_back_goes_through_the_desktop_layout(tmp_path):
    """``/0`` alone leaves the forced game layout until an X11 focus change (the Steam Controller stayed a gamepad
    on the desktop): the desktop layout is forced first and released once Steam switched to it — never before (links
    reach Steam asynchronously)."""
    root, sent, opener = fake_steam(tmp_path)
    log = root / "logs" / "controller.txt"
    late = []

    def slow(url):                                     # Steam takes a moment to switch to the desktop layout
        if url.endswith("/413080"):
            late.append(url)
            sent.append(url)
        else:
            opener(url)
    f = steaminput.Forced(tmp_path / "marker", slow, root=lambda: root)
    f.ACK_SECONDS = 2.0
    f.start(1)
    assert f.end(1)
    time.sleep(0.3)
    assert sent == ["steam://forceinputappid/480", "steam://forceinputappid/413080"] and f.marker.is_file()
    with open(log, "a") as fh:
        fh.write("[x] Queueing activation for controller: 0 app: 413080\n")
    assert until(lambda: sent[-1] == "steam://forceinputappid/0") and not f.marker.is_file()
    sent.clear()
    f.start(2)                                         # a game starts while the hand-back waits for Steam:
    f.end(2)
    f.start(3)
    with open(log, "a") as fh:
        fh.write("[x] Queueing activation for controller: 0 app: 413080\n")
    time.sleep(0.5)
    assert "steam://forceinputappid/0" not in sent and f.active      # its layout is not released under it
    assert f.end(3, desktop=False) and sent[-1] == "steam://forceinputappid/0"


def test_a_forced_layout_is_kept_through_the_steam_button_and_the_menu(tmp_path, monkeypatch, caplog):
    """Measured on Steam: a forced layout survives focus changes, the Steam button (chord 443510, then the forced
    layout again) and Big Picture. The app adds nothing there — asking again only when Steam lost it (a restart:
    the desktop layout while the game runs) — and logs every choice Steam made."""
    import logging
    caplog.set_level(logging.INFO, logger="ezgd.platform.steaminput")
    root, sent, opener = fake_steam(tmp_path)
    monkeypatch.setattr(steaminput.Forced, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(steaminput.Forced, "RETRY_SECONDS", 0.1)
    log = root / "logs" / "controller.txt"
    game_layout = "steam://forceinputappid/480"

    def steam_switches(*apps):
        with open(log, "a") as fh:
            for a in apps:
                fh.write(f"[2026-10-23 03:19:49] Queueing activation for controller: 0 app: {a}\n")
    f = steaminput.Forced(tmp_path / "marker", opener, root=lambda: root)
    f.start(1)
    assert sent == [game_layout]
    time.sleep(0.2)
    steam_switches(443510, 480)                        # the Steam button: Steam goes back to the forced layout
    time.sleep(0.3)
    assert sent == [game_layout]
    assert until(lambda: "Steam switched the controller to the 443510 (Steam button) layout" in caplog.text)
    assert "Steam controller layout: URL Forcing to window type: k_EWindowTypeGame, 480" in caplog.text
    steam_switches(413080)                             # Steam restarted: the force is gone — asked for again
    assert until(lambda: len(sent) == 2) and sent[-1] == game_layout
    time.sleep(0.3)
    assert len(sent) == 2                              # (Steam took it)
    steam_switches(4000)                               # a game Steam started itself: its layout is left alone
    time.sleep(0.3)
    assert len(sent) == 2
    assert f.end(1, desktop=False) and sent[-1] == "steam://forceinputappid/0"


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
    assert "ezgd:steam-input Steam switched the controller to Spacewar's layout (480)" in log_path.read_text()
    ended(core, e.id)                                  # the last game: back through the desktop layout
    assert until(lambda: sent[-1] == "steam://forceinputappid/0")
    assert sent == ["steam://forceinputappid/480", "steam://forceinputappid/413080", "steam://forceinputappid/0"]


async def test_a_game_started_through_steam_is_left_to_steam(steam_here, tmp_path, monkeypatch):
    """Steam knows a game it started from the app's shortcut and gives it the shortcut's layout (Steam's gamepad
    template, or the player's) by focus, its overlay included — nothing is forced, Steam's choices are logged."""
    core, sent = steam_here
    for title, onlinefix in (("Shortcut game", False), ("OnlineFix game", True)):
        e = game(core, tmp_path, title)
        monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e, v=onlinefix: v)
        monkeypatch.setattr(steamrun, "sessions", lambda _d, i=e.id: {i: {"started": time.time()}})
        core.library.reap()
        assert sent == [] and e.id in core.library.input_layout.watched
        monkeypatch.setattr(steamrun, "sessions", lambda _d: {})
        core.library.reap()
        assert sent == [] and e.id not in core.library.input_layout.watched


async def test_play_mode_leaves_the_apps_frozen_games_and_layout_alone(core, tmp_path, monkeypatch):  # noqa: F811
    """Play mode (a game Steam starts) runs next to the app: it must not thaw the game the quick menu froze to switch
    games, nor hand back the layout the app forced."""
    from ezgd.library.manager import LibraryManager
    marker = core.paths.data / "steam-input-forced"
    marker.write_text("480")
    note = core.library.running_dir / "suspended-5.json"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text('{"pids": [999999]}')
    sent = []
    monkeypatch.setattr(steaminput, "enabled", lambda: True)
    monkeypatch.setattr(steam, "open_steam_url", sent.append)
    LibraryManager(core.library.repo, core.library.games, core.library.settings, core.paths, lambda n, p: None,
                   recover=False)
    assert sent == [] and marker.exists() and note.exists()


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
    """The app crashed while a game had the game layout: the next start gives the choice back to Steam, through the
    desktop layout (else the controller stays a gamepad on the desktop)."""
    from ezgd.library.manager import LibraryManager
    (core.paths.data / "steam-input-forced").write_text("480")
    sent = []
    monkeypatch.setattr(steaminput, "enabled", lambda: True)
    monkeypatch.setattr(steam, "open_steam_url", sent.append)
    monkeypatch.setattr(steam, "is_steam_running", lambda: True)
    LibraryManager(core.library.repo, core.library.games, core.library.settings, core.paths, lambda n, p: None)
    assert until(lambda: sent[-1:] == ["steam://forceinputappid/0"], 6)
    assert sent == ["steam://forceinputappid/413080", "steam://forceinputappid/0"]
    assert not (core.paths.data / "steam-input-forced").exists()
    (core.paths.data / "steam-input-forced").write_text("480")      # Steam is not running: it forgot it already
    sent.clear()
    monkeypatch.setattr(steam, "is_steam_running", lambda: False)
    LibraryManager(core.library.repo, core.library.games, core.library.settings, core.paths, lambda n, p: None)
    time.sleep(0.3)
    assert sent == [] and not (core.paths.data / "steam-input-forced").exists()   # (and Steam is not started)
