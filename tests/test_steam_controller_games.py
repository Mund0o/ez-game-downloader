"""A Steam controller in every game the app starts (2026-10-28 deep dive). The real Steam client is never told
anything: a fake one answers in its controller logs the way Steam did when measured (a DualSense on Steam
1792618285, links sent in the background):

* ``forceinputappid/<the app's shortcut>`` → Steam's gamepad template for it and a virtual pad; the switch is
  logged with the id as a *signed* 32-bit number (``app: -1335484452`` for 2959482844);
* ``forceinputappid/480`` → Valve's own Steam Input API layout for Spacewar (every binding a Spacewar action):
  no virtual pad, nothing a game outside Spacewar can read — what OnlineFix games get by themselves;
* an id Steam does not know → nothing at all.
Each test failed before its fix."""

import time

import pytest

from ezgd.library import steamrun
from ezgd.platform import steam, steaminput, vdf

from test_release_fixes import core  # noqa: F401 — the shared AppCore fixture
from test_steaminput import ended, game, until

SPACEWAR = 480
DESKTOP = 413080


def signed(appid: int) -> int:
    return appid - 2 ** 32 if appid >= 2 ** 31 else appid


class FakeSteam:
    """Steam's controller logs, its shortcuts and its virtual pads, as measured."""

    def __init__(self, tmp_path, pads_for=None, known=None, connected=True):
        self.root = tmp_path / "Steam"
        (self.root / "logs").mkdir(parents=True)
        (self.root / "userdata" / "1234" / "config").mkdir(parents=True)
        self.log = self.root / "logs" / "controller.txt"
        self.log.write_text("[2026-10-28 18:00:00] Queueing activation for controller: 0 app: 413080\n")
        self.sent: list[str] = []
        self.pads_for = set(pads_for or ())       # layouts that make Steam's virtual pad
        self.known = known                        # ids Steam knows (None: every id)
        self.connected = connected                # a controller Steam drives is on
        self.active = DESKTOP
        self.shortcuts: dict = {}

    def opener(self, url: str) -> None:
        self.sent.append(url)
        appid = int(url.rsplit("/", 1)[1]) & 0xFFFFFFFF
        if not appid or not self.connected or (self.known is not None and appid not in self.known | {DESKTOP}):
            return
        self.active = appid
        with open(self.log, "a") as fh:
            fh.write(f"[2026-10-28 18:00:01] Queueing activation for controller: 0 app: {signed(appid)}\n")
        with open(self.root / "logs" / "controller_ui.txt", "a") as fh:
            fh.write(f"[2026-10-28 18:00:01] OnFocusWindowChanged URL Forcing to window type: k_EWindowTypeGame, "
                     f"{appid}\n")

    def pads(self, base=None) -> list[str]:
        return ["input90"] if self.active in self.pads_for else []

    def add_shortcut(self, appid: int, name: str, entry_id: int | None = None, exe: str = "/opt/ezgd/ezgd") -> None:
        opts = f"--play {entry_id}" if entry_id is not None else ""
        self.shortcuts[str(len(self.shortcuts))] = {"appid": vdf.Int32(signed(appid)), "AppName": name,
                                                    "Exe": f'"{exe}"', "LaunchOptions": opts}
        (self.root / "userdata" / "1234" / "config" / "shortcuts.vdf").write_bytes(
            vdf.binary_dumps({"shortcuts": self.shortcuts}))


@pytest.fixture
def steam_pc(core, tmp_path, monkeypatch):  # noqa: F811
    s = FakeSteam(tmp_path)
    monkeypatch.setattr(steaminput, "enabled", lambda: True)
    monkeypatch.setattr(steam, "is_steam_running", lambda: True)
    monkeypatch.setattr(core.library, "steam_root", lambda: s.root)
    monkeypatch.setattr(core.library.input_layout, "_open", s.opener)
    monkeypatch.setattr(steaminput, "virtual_pads", s.pads)
    events = []
    core.library.emit = lambda name, payload: events.append((name, payload))
    s.events = events
    s.valve = set()
    monkeypatch.setattr(steam, "valve_controllers", lambda base=None: set(s.valve))
    return core, s


def forced(s: FakeSteam) -> list[int]:
    return [int(u.rsplit("/", 1)[1]) for u in s.sent]


def warnings(s: FakeSteam) -> list[tuple]:
    return [p for n, p in s.events if n == "notify" and "gamepad" in (p[0] + p[1]).lower()]


# ------------------------------------------------------------------------- Steam's own way of writing app ids
def test_steam_writes_a_shortcuts_switch_as_a_signed_number(tmp_path):
    """Steam logs ``app: -1335484452`` when it switched to shortcut 2959482844 (and its decisions in
    controller_ui.txt unsigned). The quick menu's hold over a game Steam started waited for "2959482844" — which
    never comes — so it always sat out its whole wait (1.5 s) before the menu showed, and the game log said Steam
    "did not take" a layout it had taken."""
    s = FakeSteam(tmp_path, pads_for={2959482844})
    since = steaminput.log_size(s.root)
    s.opener("steam://forceinputappid/2959482844")
    assert steaminput.activations(s.root, since) == [(0, 2959482844)]
    assert steaminput.took(s.root, since, 2959482844, wait=1.0) is True
    f = steaminput.Forced(tmp_path / "marker", s.opener, root=lambda: s.root)
    t = time.monotonic()
    assert f.hold(5, 2959482844, wait=1.5)
    assert time.monotonic() - t < 0.5                      # (Steam answered at once)


# ------------------------------------------------------------------------- which layout a game gets
def test_a_steam_controller_gets_the_games_own_steam_layout(steam_pc, tmp_path):
    """Spacewar's layout (480) — what the app forced for every game Steam does not know — is Valve's own layout for
    Spacewar: no gamepad, nothing a game can read. A Steam Controller did nothing at all in an emulator or a repack.
    The game's own Steam shortcut has Steam's gamepad template (or the layout the player chose for it in Steam)."""
    core, s = steam_pc
    s.valve = {0x1142}                                     # the Steam Controller's receiver
    e = game(core, tmp_path, "Bloodborne")
    s.add_shortcut(3555500001, "Bloodborne", entry_id=e.id)
    s.pads_for = {3555500001}
    core.library.launch(e.id, confirmed=True)
    assert forced(s) == [3555500001]
    log_path = core.paths.logs / f"game-{e.id}.log"
    assert until(lambda: "ezgd:steam-input" in log_path.read_text(), 6)
    assert "game's own layout in Steam (3555500001)" in log_path.read_text() and "a gamepad" in log_path.read_text()
    ended(core, e.id)
    assert until(lambda: forced(s)[-1:] == [0])
    assert forced(s) == [3555500001, DESKTOP, 0]
    assert warnings(s) == []


def test_a_players_own_shortcut_for_the_game_counts_too(steam_pc, tmp_path):
    """A game the player added to Steam themselves (the app then adds no second entry): its layout is the one the
    player set up in Steam."""
    core, s = steam_pc
    s.valve = {0x1205}                                     # a Steam Deck
    e = game(core, tmp_path, "Elden Ring")
    s.add_shortcut(3555500002, "Elden Ring", exe=str(tmp_path / "games" / "Elden Ring" / "run.sh"))
    s.pads_for = {3555500002}
    core.library.launch(e.id, confirmed=True)
    assert forced(s) == [3555500002]
    ended(core, e.id)


def test_onlinefix_games_get_a_gamepad_from_a_steam_controller(steam_pc, tmp_path, monkeypatch):
    """OnlineFix games are Spacewar to Steam (they sign in as 480), so Steam gave them Spacewar's layout by itself —
    no gamepad from a Steam Controller. With one connected, the game's own layout is asked for too; without one the
    game reads its controller itself and Steam's choice is left alone, as before."""
    core, s = steam_pc
    e = game(core, tmp_path, "Slay the Spire 2")
    monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e: True)
    s.add_shortcut(3802265475, "Slay the Spire 2", entry_id=e.id)
    s.pads_for = {3802265475}
    core.library.launch(e.id, confirmed=True)
    assert s.sent == [] and e.id in core.library.input_layout.watched
    ended(core, e.id)
    s.valve = {0x1102}
    core.library.launch(e.id, confirmed=True)
    assert forced(s) == [3802265475]
    ended(core, e.id)
    assert until(lambda: forced(s)[-1:] == [0])


def test_a_game_not_in_steam_borrows_a_gamepad_layout_and_says_when_there_is_none(steam_pc, tmp_path):
    """Steam ignores a layout it does not know (measured: nothing logged), so a game that is not in Steam takes the
    layout of another of the app's Steam shortcuts (Steam's gamepad template unless the player changed it), and
    only then Spacewar's. A layout that makes no gamepad is not kept — and when none does, the player is told why
    the controller does nothing and what helps, instead of a dead controller."""
    core, s = steam_pc
    s.valve = {0x1142}
    e = game(core, tmp_path, "Not In Steam")
    other = game(core, tmp_path, "Other Game")
    s.add_shortcut(3555500003, "Other Game", entry_id=other.id)
    s.pads_for = {3555500003}
    core.library.launch(e.id, confirmed=True)
    assert forced(s) == [3555500003]
    ended(core, e.id)
    assert until(lambda: forced(s)[-1:] == [0])
    s.sent.clear()
    s.pads_for = set()                                     # (a layout without a gamepad — Spacewar's, say)
    core.library.launch(e.id, confirmed=True)
    assert until(lambda: forced(s)[:2] == [3555500003, SPACEWAR], 6)
    assert until(lambda: bool(warnings(s)), 6)
    title, text = warnings(s)[0][:2]
    assert "Not In Steam" in title and "Add" in text and "Steam" in text
    ended(core, e.id)


def test_a_layout_without_a_gamepad_is_not_kept(steam_pc, tmp_path):
    """The player gave the game's shortcut a layout without a gamepad (keyboard keys, say) in Steam: the next
    layout that has one is used instead, and the log says so."""
    core, s = steam_pc
    s.valve = {0x1142}
    e = game(core, tmp_path, "Keys Game")
    other = game(core, tmp_path, "Pad Game")
    s.add_shortcut(3555500004, "Keys Game", entry_id=e.id)
    s.add_shortcut(3555500005, "Pad Game", entry_id=other.id)
    s.pads_for = {3555500005}
    core.library.launch(e.id, confirmed=True)
    assert until(lambda: forced(s) == [3555500004, 3555500005], 6)
    log_path = core.paths.logs / f"game-{e.id}.log"
    assert until(lambda: "ezgd:steam-input" in log_path.read_text(), 6)
    assert "3555500004" in log_path.read_text() and "no gamepad" in log_path.read_text()
    ended(core, e.id)


def test_no_controller_on_keeps_the_first_layout(steam_pc, tmp_path):
    """The Steam Controller's receiver is plugged in but the controller is off: Steam switches nothing (nothing to
    judge), so the game's own layout stays asked for — Steam uses it once the controller is on."""
    core, s = steam_pc
    s.valve = {0x1142}
    s.connected = False
    e = game(core, tmp_path, "Off Game")
    s.add_shortcut(3555500006, "Off Game", entry_id=e.id)
    core.library.launch(e.id, confirmed=True)
    time.sleep(2.5)
    assert forced(s) == [3555500006] and warnings(s) == []
    ended(core, e.id)


def test_without_a_steam_controller_spacewar_keeps_steam_quiet(steam_pc, tmp_path):
    """Other controllers are read by the game itself; Spacewar's layout only stops Steam's desktop layout (stick =
    mouse) during the game and makes no second, virtual pad (that would be the same controller twice)."""
    core, s = steam_pc
    e = game(core, tmp_path, "Pad Game")
    s.add_shortcut(3555500007, "Pad Game", entry_id=e.id)
    core.library.launch(e.id, confirmed=True)
    assert forced(s) == [SPACEWAR]
    ended(core, e.id)
    assert warnings(s) == []


def test_a_proton_option_that_hides_steams_controller_is_named(steam_pc, tmp_path, monkeypatch):
    """GE-Proton and Proton-CachyOS ignore Steam's virtual controller with "Without Steam Input", "Controllers
    through SDL" and "Native Wayland" (PROTON_NO_STEAMINPUT, read in their winebus). With a Steam Controller the game
    then gets nothing: the player is told which option does it. "Without Steam Input" was looked for among the
    Fixes-screen switches instead of the Proton options, so it never counted."""
    from ezgd.library.models import CompatConfig
    from ezgd.platform import proton
    core, s = steam_pc
    s.valve = {0x1142}
    folder = tmp_path / "games" / "Windows Game"
    folder.mkdir(parents=True)
    exe = folder / "Game.exe"
    exe.write_bytes(b"MZ")
    e = core.library.import_folder("Windows Game", folder)
    core.library.set_executable(e.id, exe)
    e = core.library.get(e.id)
    e.compat = CompatConfig(options=["no_steaminput"]).to_dict()
    core.library.repo.update(e)
    s.add_shortcut(3555500008, "Windows Game", entry_id=e.id)
    s.pads_for = {3555500008}
    monkeypatch.setattr(proton, "proton_supports", lambda d, var: var == "PROTON_NO_STEAMINPUT" or None)
    def ids(plan):
        return [a for a, _what in plan["layouts"]]
    plan = core.library.controller_layouts(e, {"PROTON_NO_STEAMINPUT": "1"}, windows=True, proton_dir="/x")
    assert ids(plan) == [SPACEWAR] and "Without Steam Input" in plan["warn"]
    plan = core.library.controller_layouts(e, {"PROTON_PREFER_SDL": "1"}, windows=True, proton_dir="/x")
    assert ids(plan) == [SPACEWAR] and "Controllers through SDL" in plan["warn"]
    plan = core.library.controller_layouts(e, {}, windows=True, proton_dir="/x")
    assert ids(plan)[0] == 3555500008 and not plan["warn"]
    monkeypatch.setattr(proton, "proton_supports", lambda d, var: False)          # Valve's Proton: it uses the pad
    plan = core.library.controller_layouts(e, {"PROTON_NO_STEAMINPUT": "1"}, windows=True, proton_dir="/x")
    assert ids(plan)[0] == 3555500008 and not plan["warn"]
    s.valve = set()
    plan = core.library.controller_layouts(e, {"PROTON_NO_STEAMINPUT": "1"}, windows=True, proton_dir="/x")
    assert ids(plan) == [SPACEWAR] and not plan["warn"]          # (other controllers: read by the game)


def test_a_game_running_as_its_real_steam_app_keeps_steams_choice(steam_pc, tmp_path, monkeypatch):
    """A game that runs as a real Steam app (OnlineFix as the genuine app, or "Tell Proton the Steam App ID") gets
    that app's own layout from Steam — the developer's, which the game may read through Steam itself."""
    core, s = steam_pc
    s.valve = {0x1142}
    e = game(core, tmp_path, "Little Nightmares III")
    monkeypatch.setattr(core.library, "onlinefix_env", lambda _e: {"SteamAppId": "1392860"})
    core.library.launch(e.id, confirmed=True)
    assert s.sent == [] and e.id in core.library.input_layout.watched
    ended(core, e.id)


# ------------------------------------------------------------------------- play mode (Steam starts the game)
def test_play_mode_records_which_shortcut_steam_started(monkeypatch):
    monkeypatch.setenv("SteamGameId", str((3802265475 << 32) | 0x02000000))
    assert steamrun.started_shortcut() == 3802265475
    monkeypatch.setenv("SteamGameId", "1392860")
    assert steamrun.started_shortcut() is None             # (a real Steam app, not a shortcut)
    monkeypatch.delenv("SteamGameId")
    assert steamrun.started_shortcut() is None


def test_an_onlinefix_game_steam_started_keeps_its_shortcuts_layout(steam_pc, tmp_path, monkeypatch):
    """Measured in Steam's logs (Slay the Spire 2 through Steam): Steam readied the shortcut's gamepad template,
    then the game's window — Spacewar to Steam — switched it to Spacewar's layout: no gamepad for any controller
    Steam drives in that game (Steam hides the real one from games it starts). The app keeps the shortcut's layout
    while it runs — for a Valve controller, and for others when Steam Input is on for them in that game (Steam's
    opt-in mask at the start: 0x1000 is every app's, the low bits the other controllers' — measured: a DualSense got
    a layout with 1001, "empty.vdf" with 1000). Otherwise the game reads its controller itself and nothing changes."""
    core, s = steam_pc
    e = game(core, tmp_path, "Slay the Spire 2")
    monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e: True)

    def started_by_steam(mask: str) -> None:
        with open(s.log, "a") as fh:
            fh.write(f"[2026-10-28 18:00:05] Opted-in Controller Mask for AppId 3802265475: {mask}\n")
        monkeypatch.setattr(steamrun, "sessions", lambda _d: {e.id: {"started": time.time(),
                                                                     "shortcut": 3802265475}})
        core.library.reap()

    def ends() -> None:
        monkeypatch.setattr(steamrun, "sessions", lambda _d: {})
        core.library.reap()
    started_by_steam("1001")                               # Steam Input on for the PlayStation pad in this game
    assert forced(s) == [3802265475]
    ends()
    assert until(lambda: forced(s)[-1:] == [0])
    assert forced(s) == [3802265475, DESKTOP, 0]
    s.sent.clear()
    started_by_steam("1000")                               # off: the game reads the pad itself
    assert s.sent == [] and e.id in core.library.input_layout.watched
    ends()
    s.valve = {0x1142}                                     # a Steam Controller: always Steam's
    started_by_steam("1000")
    assert forced(s) == [3802265475]
    ends()
    assert until(lambda: forced(s)[-1:] == [0])
    s.sent.clear()
    s.valve = set()
    monkeypatch.setattr(core.library, "talks_to_steam_itself", lambda _e: False)    # any other game Steam started:
    monkeypatch.setattr(steamrun, "sessions", lambda _d: {e.id: {"started": time.time(), "shortcut": 3802265475}})
    core.library.reap()                                                             # Steam's own choice, as before
    assert s.sent == [] and e.id in core.library.input_layout.watched
    monkeypatch.setattr(steamrun, "sessions", lambda _d: {})
    core.library.reap()


# ------------------------------------------------------------------------- two games, switching between them
def test_switching_back_to_a_game_brings_its_layout_back(tmp_path):
    """Two games of the app, each with its own layout: the one played gets its own (switching back from the quick
    menu), and when one ends the other's stays — it was the last started's for both before."""
    s = FakeSteam(tmp_path, pads_for={3555500010, 3555500011})
    f = steaminput.Forced(tmp_path / "marker", s.opener, root=lambda: s.root)
    f.start(1, 3555500010)
    f.start(2, 3555500011)
    assert forced(s) == [3555500010, 3555500011]
    f.prefer(1)                                            # back to the first game
    assert forced(s)[-1] == 3555500010
    f.prefer(1)                                            # (already its: nothing sent)
    assert len(s.sent) == 3
    assert not f.end(1)                                    # it ends: the other one's layout
    assert forced(s)[-1] == 3555500011
    assert f.end(2)
    assert until(lambda: forced(s)[-1:] == [0])


# ------------------------------------------------------------------------- Fix my game's controller ideas
def test_controller_fixes_never_hide_steams_controller_from_a_steam_controller(monkeypatch):
    """"Controllers through SDL" and "Without Steam Input" make GE-Proton / Proton-CachyOS ignore Steam's virtual
    controller — the only way a Steam Controller reaches a game. They are not offered while one is connected."""
    from ezgd.library import fixgen
    monkeypatch.setattr(steam, "valve_controllers", lambda base=None: set())
    assert {o for f in fixgen._controller(None, "controller", [], False) for o in f.options} == \
        {"prefer_sdl", "no_steaminput", "no_hidraw"}
    monkeypatch.setattr(steam, "valve_controllers", lambda base=None: {0x1142})
    assert {o for f in fixgen._controller(None, "controller", [], False) for o in f.options} == {"no_hidraw"}


# ------------------------------------------------------------------------- Steam closed, or started late
def test_handing_back_never_starts_steam(tmp_path):
    """The link that hands the layout back went through ``steam -silent …``, which starts Steam when it is not
    running: a player who quit Steam during a game got it started again by the app when the game ended. Steam
    forgets a forced layout when it quits, so there is nothing to hand back then."""
    s = FakeSteam(tmp_path, pads_for={3555500020})
    up = {"steam": True}
    f = steaminput.Forced(tmp_path / "marker", s.opener, root=lambda: s.root, steam_up=lambda: up["steam"])
    f.start(1, 3555500020)
    assert forced(s) == [3555500020]
    up["steam"] = False                                    # Steam quit while the game ran
    assert f.end(1)
    time.sleep(0.3)
    assert forced(s) == [3555500020] and not f.marker.is_file()


def test_a_game_started_before_steam_gets_its_layout_once_steam_runs(steam_pc, tmp_path, monkeypatch):
    """Without Steam a Steam Controller is read directly; Steam started during the game takes it over with its
    desktop layout (stick = mouse). The game's layout is asked for once Steam runs."""
    core, s = steam_pc
    s.valve = {0x1142}
    up = {"steam": False}
    monkeypatch.setattr(steam, "is_steam_running", lambda: up["steam"])
    e = game(core, tmp_path, "Early Game")
    s.add_shortcut(3555500021, "Early Game", entry_id=e.id)
    s.pads_for = {3555500021}
    core.library.launch(e.id, confirmed=True)
    core.library.reap()
    assert s.sent == []
    up["steam"] = True                                     # Steam started
    core.library.reap()
    assert forced(s) == [3555500021]
    core.library.reap()
    assert forced(s) == [3555500021]                       # (once)
    ended(core, e.id)
