"""Couch mode's motion (2026-10-28, "the Store and Discover are jittery and laggy; the sound going side to side does
not work"): glides move by the clock on every frame, a frame asked for later never swallows the next one, and every
move that moved the selection is heard."""

from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401 — the shared main-window fixture


def test_a_later_frame_never_swallows_the_next_one(ui, qtbot):
    """Every move asks for a frame 255 ms later (the background follows the focus). One flag for all requests let
    that one swallow every glide's next frame: the Store's shelves moved one jump per press and stood still."""
    win, _core, _ = ui
    couch = _open_couch(win, qtbot)
    couch._pump.stop()                                   # (no frame pump: requests become timers)
    couch._schedule_frame(255)
    couch._schedule_frame(20)
    assert couch._frame_timer.isActive() and couch._frame_timer.remainingTime() <= 20
    couch._schedule_frame(255)                           # a later one does not push it back
    assert couch._frame_timer.remainingTime() <= 20
    couch._pump.start()
    couch._dirty = False
    couch._schedule_frame()                              # with the pump: the very next frame
    assert couch._dirty
    couch.force_close()


def test_glides_move_by_the_clock_not_per_frame(ui, qtbot):
    """The same glide reaches the same place in the same time at 60 and at 144 frames a second (a quarter of the
    way per frame was more than twice as fast on a 144 Hz screen, and lurched whenever a frame came late) — and a held
    direction slides evenly instead of pulsing with every repeat ("still laggy and jittery")."""
    win, _core, _ = ui
    couch = _open_couch(win, qtbot)

    def glide(fps: float, seconds: float) -> float:
        cur, t = 0.0, 1000.0
        couch._ease_at.clear()
        for _ in range(int(seconds * fps)):
            t += 1 / fps
            couch.frame_now = t
            cur = couch.ease("test", cur, 1000.0)
        return cur
    slow, fast = glide(60, 0.1), glide(144, 0.1)
    assert 300 < slow < 1000 and abs(slow - fast) < 0.06 * 1000
    assert glide(60, 2.0) == 1000.0                      # it arrives (and stops asking for frames)

    def held(fps: float, every: float) -> list[float]:
        """A direction held: one tile (300 px) further every ``every`` s — the movement of each frame."""
        cur, target, t, nxt, moves = 0.0, 0.0, 1000.0, 1000.0, []
        couch._ease_at.clear()
        for _ in range(int(1.4 * fps)):
            if t >= nxt:
                target += 300.0
                nxt += every
            t += 1 / fps
            couch.frame_now = t
            new = couch.ease("held", cur, target)
            moves.append(new - cur)
            cur = new
        return moves[int(0.25 * fps):int(1.2 * fps)]      # (while held: not the start, not the end)
    import statistics
    for every in (0.110, 0.055):                         # the controller's repeat, then its fast repeat
        m = held(120, every)
        assert statistics.pstdev(m) / statistics.mean(m) < 0.2       # even (a quarter per frame pulsed ±53 %)
    couch.frame_now = 0.0
    couch.force_close()


def test_left_and_right_in_discover_are_heard(ui, qtbot):
    """Discover keeps the focused tile of each shelf in a dict; only plain numbers counted as "the selection moved",
    so up / down clicked and left / right were silent."""
    from ezgd.ui import couch as C
    win, _core, _ = ui
    couch = _open_couch(win, qtbot)
    couch._switch_tab(C.T_STORE, force=True)
    store = couch.pages[C.T_STORE]
    before = couch._focus_key()
    store.cols[store.shelf] = store.cols.get(store.shelf, 0) + 1     # (what left / right do on a shelf)
    couch.sounds.played.clear()
    couch._sound("right", before)
    assert couch.sounds.played == ["move"]
    couch.sounds.played.clear()
    couch._sound("right", couch._focus_key())            # nothing moved: no tick
    assert couch.sounds.played == []
    couch.force_close()
