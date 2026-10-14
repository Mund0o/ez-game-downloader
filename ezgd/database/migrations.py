"""Versioned schema migrations.

Each migration runs inside one transaction and bumps ``PRAGMA user_version``.
Never edit a released migration; append a new one instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _sql(script: str) -> Callable[[sqlite3.Connection], None]:
    def run(conn: sqlite3.Connection) -> None:
        # executescript() would COMMIT implicitly; run statements one by one instead
        for stmt in _split(script):
            conn.execute(stmt)
    return run


def _split(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        joined = "\n".join(buf).strip()
        if joined.endswith(";") and sqlite3.complete_statement(joined):
            out.append(joined)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


V1 = """
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE sources (
    id             TEXT PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'unknown',
    status_message TEXT,
    last_ok_at     REAL,
    last_error_at  REAL
);

CREATE TABLE games (
    id                 TEXT PRIMARY KEY,
    source             TEXT NOT NULL,
    source_game_id     TEXT NOT NULL,
    title              TEXT NOT NULL,
    raw_title          TEXT,
    dedup_key          TEXT NOT NULL,
    url                TEXT NOT NULL,
    cover_url          TEXT,
    description        TEXT,
    genres             TEXT NOT NULL DEFAULT '[]',
    platforms          TEXT NOT NULL DEFAULT '[]',
    release_date       TEXT,
    version            TEXT,
    size_bytes         INTEGER,
    size_text          TEXT,
    steam_app_id       INTEGER,
    download_methods   TEXT,
    extra              TEXT NOT NULL DEFAULT '{}',
    published_at       REAL,
    updated_at         REAL,
    details_checked_at REAL,
    cached_at          REAL NOT NULL,
    UNIQUE (source, source_game_id)
);
CREATE INDEX idx_games_dedup ON games (dedup_key);
CREATE INDEX idx_games_source_published ON games (source, published_at DESC);
CREATE INDEX idx_games_title ON games (title COLLATE NOCASE);

CREATE TABLE listings (
    source     TEXT NOT NULL,
    listing    TEXT NOT NULL,
    position   INTEGER NOT NULL,
    game_id    TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (source, listing, position)
);

CREATE TABLE http_cache (
    url           TEXT PRIMARY KEY,
    status        INTEGER NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    content_type  TEXT,
    headers       TEXT NOT NULL DEFAULT '{}',
    body          BLOB NOT NULL,
    fetched_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
);

CREATE TABLE downloads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL CHECK (kind IN ('http', 'torrent')),
    title            TEXT NOT NULL,
    game_id          TEXT,
    source           TEXT,
    source_url       TEXT,
    uri              TEXT NOT NULL,
    referer          TEXT,
    dest_dir         TEXT NOT NULL,
    filename         TEXT,
    status           TEXT NOT NULL,
    status_detail    TEXT,
    error            TEXT,
    priority         INTEGER NOT NULL DEFAULT 0,
    total_bytes      INTEGER,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    expected_checksum TEXT,
    verification     TEXT NOT NULL DEFAULT 'none',
    etag             TEXT,
    last_modified    TEXT,
    info_hash        TEXT,
    torrent_file     TEXT,
    resume_data      BLOB,
    selected_files   TEXT,
    archive_password TEXT,
    auto_extract     INTEGER NOT NULL DEFAULT 1,
    extract_dest     TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    started_at       REAL,
    completed_at     REAL
);
CREATE INDEX idx_downloads_status ON downloads (status);

CREATE TABLE extractions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id     INTEGER REFERENCES downloads (id) ON DELETE SET NULL,
    library_id      INTEGER,
    archive_path    TEXT NOT NULL,
    dest_dir        TEXT NOT NULL,
    status          TEXT NOT NULL,
    progress        REAL NOT NULL DEFAULT 0,
    error           TEXT,
    files_count     INTEGER,
    bytes_total     INTEGER,
    nested_archives TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    completed_at    REAL
);

CREATE TABLE library (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    title                TEXT NOT NULL,
    dedup_key            TEXT NOT NULL,
    game_id              TEXT,
    source               TEXT,
    download_id          INTEGER REFERENCES downloads (id) ON DELETE SET NULL,
    cover_url            TEXT,
    download_path        TEXT,
    install_path         TEXT,
    executable           TEXT,
    executable_trusted   INTEGER NOT NULL DEFAULT 0,
    launch_method        TEXT NOT NULL DEFAULT 'auto',
    proton_version       TEXT,
    launch_args          TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL,
    steam_shortcut_appid INTEGER,
    notes                TEXT,
    added_at             REAL NOT NULL,
    updated_at           REAL NOT NULL,
    last_played_at       REAL,
    play_count           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_library_dedup ON library (dedup_key);

CREATE TABLE events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL,
    category TEXT NOT NULL,
    message  TEXT NOT NULL,
    ref      TEXT
);
CREATE INDEX idx_events_ts ON events (ts DESC);
"""


V2 = """
ALTER TABLE library ADD COLUMN steam_app_id INTEGER;
ALTER TABLE library ADD COLUMN compat TEXT NOT NULL DEFAULT '{}';
ALTER TABLE library ADD COLUMN playtime_seconds INTEGER NOT NULL DEFAULT 0;
"""


V3 = """
CREATE TABLE created_paths (
    path       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    game_id    TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE custom_sources (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    base_url   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    added_at   REAL NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}'
);
"""


V4 = """
ALTER TABLE library ADD COLUMN setup TEXT NOT NULL DEFAULT '{}';
"""


V5 = """
ALTER TABLE library ADD COLUMN release TEXT NOT NULL DEFAULT '{}';

CREATE TABLE wishlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    dedup_key     TEXT NOT NULL,
    steam_app_id  INTEGER,
    cover_url     TEXT,
    hero_url      TEXT,
    release_text  TEXT,
    coming_soon   INTEGER NOT NULL DEFAULT 0,
    added_at      REAL NOT NULL,
    checked_at    REAL,
    status        TEXT NOT NULL DEFAULT 'waiting',
    matches       TEXT NOT NULL DEFAULT '[]',
    notified_at   REAL,
    note          TEXT
);
CREATE UNIQUE INDEX idx_wishlist_app ON wishlist (steam_app_id) WHERE steam_app_id IS NOT NULL;
CREATE INDEX idx_wishlist_dedup ON wishlist (dedup_key);
"""


MIGRATIONS: list[Migration] = [
    Migration(1, "initial schema", _sql(V1)),
    Migration(2, "library: Steam App ID, Proton compatibility options, playtime", _sql(V2)),
    Migration(3, "paths created by the app; user-added sources", _sql(V3)),
    Migration(4, "library: installer (repack) state", _sql(V4)),
    Migration(5, "library: installed release (update checks); wishlist", _sql(V5)),
]

LATEST_VERSION = MIGRATIONS[-1].version
