"""The on-screen keyboard's keys: every controller has Backspace (B), Space (X / Y) and Enter (Start); a Steam
controller sent as keys (A = Enter, B = Esc) types instead of finishing at once."""

from __future__ import annotations

import types

from PySide6.QtCore import Qt

from ezgd.ui.couch_pages import Keyboard


def _kb(text: str = ""):
    host = types.SimpleNamespace(keyboard=None)
    done = []
    kb = Keyboard(host, "Message", done.append, text=text, symbols=True, on_cancel=lambda: done.append(None))
    host.keyboard = kb
    return kb, host, done


def test_controller_shortcuts():
    kb, host, done = _kb()
    kb.row, kb.col = 1, 0
    kb.command("a")
    kb.command("x")                                       # Space
    kb.command("a")
    assert kb.text == "q q"
    kb.command("b")                                       # Backspace
    assert kb.text == "q " and host.keyboard is kb
    kb.command("start")                                   # Enter
    assert done == ["q"] and host.keyboard is None


def test_b_on_an_empty_box_closes_it():
    kb, host, done = _kb()
    kb.command("b")
    assert host.keyboard is None and done == [None]


def test_steam_controller_keys_type():
    kb, host, done = _kb()
    kb.row, kb.col = 1, 2
    assert kb.key(Qt.Key_Return, "\r", "a", True) == "a"          # A (Enter): the key in focus, not "send"
    assert kb.key(Qt.Key_Space, " ", "x", True) == "x"            # X (Space)
    assert kb.key(Qt.Key_Return, "\r", "a", True, repeat=True) is None    # held A: one letter
    assert kb.text == "e " and host.keyboard is kb
    assert kb.key(Qt.Key_Escape, "\x1b", "b", True) == "b"        # B (Esc): Backspace
    assert kb.text == "e" and host.keyboard is kb
    assert kb.key(Qt.Key_Tab, "\t", "start", True) == "start"     # Start: Enter
    assert done == ["e"]


def test_a_pad_without_steam_mapping_and_a_real_keyboard():
    kb, host, done = _kb()
    kb.row, kb.col = 1, 0
    kb.key(Qt.Key_Return, "\r", None, True)                       # a pad that sends Enter: types
    assert kb.text == "q" and host.keyboard is kb
    kb.key(Qt.Key_W, "w", None, False)                            # a real keyboard
    assert kb.typed and kb.text == "qw"
    kb.key(Qt.Key_Backspace, "\b", None, False, repeat=True)      # held Backspace keeps deleting
    assert kb.text == "q"
    kb.key(Qt.Key_Return, "\r", None, False)                      # its Enter: done
    assert done == ["q"]
