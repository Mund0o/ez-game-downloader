"""How well games run on Linux (ProtonDB players' reports), for every card that shows a game: desktop grids, couch
mode's Store, game pages. One store for the whole app — asked in batches for what is on screen, answers cached for
the session (the index is offline, so this costs no network)."""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

LEVEL_COLORS = {"great": "#22c55e", "good": "#84cc16", "mixed": "#eab308", "bad": "#ef4444", "few": "#8c95ab"}


def group_key(g) -> str:
    return getattr(g, "key", None) or getattr(g, "dedup_key", "") or getattr(g, "id", "")


def _app_title(g) -> tuple[int | None, str]:
    """(Steam App ID, title) of a GameGroup / Game / LibraryEntry."""
    entries = getattr(g, "entries", None)
    if entries:
        app = next((e.steam_app_id for e in entries if getattr(e, "steam_app_id", None)), None)
        return app, getattr(g, "title", "") or entries[0].title
    return getattr(g, "steam_app_id", None), getattr(g, "title", "")


class Ratings(QObject):
    changed = Signal()

    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.known: dict[str, dict | None] = {}
        self._want: dict[str, tuple[int | None, str]] = {}
        self._asked: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)                  # many cards arrive at once: one question for all of them
        self._timer.timeout.connect(self._ask)

    def get(self, g) -> dict | None:
        """The rating if known (None: unknown or not asked yet — ``want`` it)."""
        return self.known.get(group_key(g))

    def want(self, games, app_ids: dict | None = None) -> None:
        """Look these games up (``app_ids``: key → an App ID found elsewhere, e.g. couch mode's title lookups)."""
        for g in games:
            k = group_key(g)
            if not k or k in self._asked:
                continue
            app, title = _app_title(g)
            if not app and app_ids:
                app = app_ids.get(k)
            self._want[k] = (app, title)
        if self._want and not self._timer.isActive():
            self._timer.start()

    def _ask(self) -> None:
        batch = list(self._want.items())[:400]
        for k, _ in batch:
            self._want.pop(k, None)
            self._asked.add(k)

        def got(res: list) -> None:
            for (k, _v), r in zip(batch, res):
                self.known[k] = r
            self.changed.emit()
            if self._want:
                self._timer.start()
        try:
            self.bridge.call(self.bridge.core.linux_ratings([v for _k, v in batch]), got,
                             lambda _e: None, owner=self)
        except RuntimeError:
            pass


_STORE: Ratings | None = None


def store(bridge) -> Ratings:
    global _STORE
    import shiboken6
    if _STORE is None or not shiboken6.isValid(_STORE) or _STORE.bridge is not bridge:
        _STORE = Ratings(bridge)
    return _STORE
