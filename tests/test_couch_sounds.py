"""Couch mode's sounds: a tick when the selector moves (only when it really moved), a blip when pressing into
something, quiet files, the setting, and a quieter intro."""

import wave

from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.ui import couch_intro, couch_sounds


def test_sound_files_are_short_and_quiet():
    for name in couch_sounds.NAMES:
        with wave.open(str(couch_sounds.SOUNDS / f"{name}.wav")) as w:
            assert w.getnchannels() == 2 and w.getsampwidth() == 2          # both ears (mono: left only)
            assert w.getnframes() / w.getframerate() < 0.2          # never in the way of the next press
            frames = w.readframes(w.getnframes())
        peak = max(abs(int.from_bytes(frames[i:i + 2], "little", signed=True)) for i in range(0, len(frames), 2))
        assert 0.15 < peak / 32767 < 0.35                            # around -12 dBFS: low, but there
    assert 0.1 <= couch_sounds.VOLUME <= 0.6
    assert couch_intro.VOLUME <= 0.4                                  # the intro was turned down


def test_moving_and_pressing_make_sounds(ui, qtbot):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    played = couch.sounds.played
    from ezgd.ui.couch import T_STORE
    couch.command("up")                                     # Home → the top bar: the selector moved
    assert played[-1:] == ["move"]
    n = len(played)
    couch.command("rb")                                     # next tab
    assert played[n:] == ["move"]
    couch._switch_tab(T_STORE, force=True)
    couch.zone = "top"
    couch.top_index = 0
    n = len(played)
    couch.command("left")                                   # already at the left end: nothing moved, no sound
    assert played[n:] == []
    couch.command("a")                                      # pressing into something
    assert played[n:] == ["select"]
    couch.actions.settings["couch_sounds"] = False          # switched off in Settings
    n = len(played)
    couch.command("right")
    couch.command("a")
    assert played[n:] == []
    couch.actions.settings["couch_sounds"] = True
    couch.force_close()


def test_sounds_are_mixed_into_one_stream():
    """One always-open stream (sounds were sometimes not heard as separate, freshly started streams)."""
    from array import array
    from ezgd.ui.couch_sounds import mix
    voices = []
    assert mix(voices, 4) == bytes(8)                                 # silence while nothing plays
    voices = [[array("h", [1000, 1000, 1000]), 0]]
    assert array("h", mix(voices, 2)).tolist() == [1000, 1000] and voices[0][1] == 2
    assert array("h", mix(voices, 2)).tolist() == [1000, 0] and voices == []      # finished: dropped
    voices = [[array("h", [30000, 5]), 0], [array("h", [30000, 5, 7]), 0]]
    assert array("h", mix(voices, 3)).tolist() == [32767, 10, 7]      # overlapping sounds add up (clipped)
    assert voices == []


def test_sounds_play_in_both_ears():
    """A one-channel stream reached PipeWire as "aux0" and was heard in the left ear only (a friend's headset): the
    stream is stereo, every sound the same on the left and the right, and reads always end on a whole frame."""
    from array import array
    mono = couch_sounds.load("select")
    both = couch_sounds.stereo(mono)
    assert len(both) == 2 * len(mono) and both[0::2] == mono and both[1::2] == mono
    s = couch_sounds._Stream(None, 2)
    s.add(array("h", [1, 1, 2, 2, 3, 3]))
    assert array("h", s.readData(6)).tolist() == [1, 1]              # 3 samples asked: one whole frame
    assert array("h", s.readData(8)).tolist() == [2, 2, 3, 3]
