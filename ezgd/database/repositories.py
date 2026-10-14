"""Repositories: the only place that knows the SQL for each table."""

from __future__ import annotations

import json
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable

from ezgd.catalog.models import Game
from ezgd.database.db import Database
from ezgd.downloads.models import DownloadStatus, DownloadTask
from ezgd.library.models import LibraryEntry


class GameRepository:
    def __init__(self, db: Database):
        self.db = db

    def dedup_titles(self) -> list[tuple[str, str]]:
        return [(r["dedup_key"], r["title"]) for r in self.db.query("SELECT dedup_key, title FROM games")]

    def upsert_many(self, games: Iterable[Game], *, keep_details: bool = True) -> list[Game]:
        """Store listing records. Cached details (description, methods) survive a re-list."""
        out = []
        with self.db.transaction() as c:
            for g in games:
                if keep_details:
                    row = c.execute("SELECT * FROM games WHERE id = ?", (g.id,)).fetchone()
                    if row is not None:
                        g = Game.from_row(row).merge_listing(g)
                self._write(c, g)
                out.append(g)
        return out

    def save(self, game: Game) -> Game:
        with self.db.transaction() as c:
            self._write(c, game)
        return game

    @staticmethod
    def _write(c, g: Game) -> None:
        row = g.to_row()
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "id")
        c.execute(f"INSERT INTO games ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", row)

    def get(self, game_id: str) -> Game | None:
        row = self.db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
        return Game.from_row(row) if row else None

    def by_dedup_key(self, key: str) -> list[Game]:
        return [Game.from_row(r) for r in self.db.query("SELECT * FROM games WHERE dedup_key = ?", (key,))]

    def by_steam_app(self, app_id: int) -> list[Game]:
        return [Game.from_row(r) for r in self.db.query("SELECT * FROM games WHERE steam_app_id = ?", (app_id,))]

    def recent(self, sources: Iterable[str] | None = None, limit: int = 60, offset: int = 0) -> list[Game]:
        sql = "SELECT * FROM games"
        params: list = []
        if sources is not None:
            src = list(sources)
            if not src:
                return []
            sql += f" WHERE source IN ({','.join('?' * len(src))})"
            params += src
        sql += " ORDER BY COALESCE(published_at, updated_at, 0) DESC LIMIT ? OFFSET ?"
        return [Game.from_row(r) for r in self.db.query(sql, (*params, limit, offset))]

    def search_local(self, query: str, sources: Iterable[str] | None = None, limit: int = 200) -> list[Game]:
        """Offline fallback: LIKE search over cached titles."""
        like = f"%{query.strip()}%"
        sql = "SELECT * FROM games WHERE (title LIKE ? OR raw_title LIKE ?)"
        params: list = [like, like]
        if sources is not None:
            src = list(sources)
            if not src:
                return []
            sql += f" AND source IN ({','.join('?' * len(src))})"
            params += src
        sql += " ORDER BY COALESCE(published_at, 0) DESC LIMIT ?"
        return [Game.from_row(r) for r in self.db.query(sql, (*params, limit))]

    def count(self) -> int:
        return self.db.query_one("SELECT COUNT(*) FROM games")[0]

    # listings (picks, home rows) -------------------------------------------
    def save_listing(self, source: str, listing: str, games: list[Game]) -> None:
        now = time.time()
        stored = self.upsert_many(games)
        with self.db.transaction() as c:
            c.execute("DELETE FROM listings WHERE source = ? AND listing = ?", (source, listing))
            c.executemany("INSERT INTO listings (source, listing, position, game_id, fetched_at) VALUES (?,?,?,?,?)",
                          [(source, listing, i, g.id, now) for i, g in enumerate(stored)])

    def load_listing(self, source: str, listing: str) -> tuple[list[Game], float | None]:
        rows = self.db.query(
            "SELECT g.*, l.fetched_at AS l_fetched FROM listings l JOIN games g ON g.id = l.game_id "
            "WHERE l.source = ? AND l.listing = ? ORDER BY l.position", (source, listing))
        if not rows:
            return [], None
        return [Game.from_row(r) for r in rows], rows[0]["l_fetched"]

    def clear(self) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM listings")
            c.execute("DELETE FROM games")


@dataclass
class CachedResponse:
    url: str
    status: int
    etag: str | None
    last_modified: str | None
    content_type: str | None
    headers: dict
    body: bytes
    fetched_at: float
    expires_at: float

    @property
    def fresh(self) -> bool:
        return time.time() < self.expires_at


class HttpCacheRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, url: str) -> CachedResponse | None:
        r = self.db.query_one("SELECT * FROM http_cache WHERE url = ?", (url,))
        if not r:
            return None
        return CachedResponse(r["url"], r["status"], r["etag"], r["last_modified"], r["content_type"],
                              json.loads(r["headers"] or "{}"), bytes(r["body"]), r["fetched_at"], r["expires_at"])

    def put(self, c: CachedResponse) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO http_cache (url, status, etag, last_modified, content_type, headers, body, fetched_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET status=excluded.status, etag=excluded.etag, "
                "last_modified=excluded.last_modified, content_type=excluded.content_type, headers=excluded.headers, "
                "body=excluded.body, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                (c.url, c.status, c.etag, c.last_modified, c.content_type, json.dumps(c.headers), c.body,
                 c.fetched_at, c.expires_at))

    def touch(self, url: str, expires_at: float) -> None:
        self.db.execute("UPDATE http_cache SET expires_at = ?, fetched_at = ? WHERE url = ?",
                        (expires_at, time.time(), url))

    def clear(self) -> int:
        n = self.db.query_one("SELECT COUNT(*) FROM http_cache")[0]
        self.db.execute("DELETE FROM http_cache")
        return n

    def size_bytes(self) -> int:
        return self.db.query_one("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM http_cache")[0]

    def prune(self, max_age_s: float = 7 * 86400) -> None:
        self.db.execute("DELETE FROM http_cache WHERE fetched_at < ?", (time.time() - max_age_s,))


class SourceRepository:
    def __init__(self, db: Database):
        self.db = db

    def ensure(self, source_id: str, enabled: bool = True) -> None:
        self.db.execute("INSERT OR IGNORE INTO sources (id, enabled) VALUES (?, ?)", (source_id, int(enabled)))

    def set_status(self, source_id: str, status: str, message: str | None = None) -> None:
        now = time.time()
        col = "last_ok_at" if status == "ok" else "last_error_at"
        self.ensure(source_id)
        self.db.execute(f"UPDATE sources SET status = ?, status_message = ?, {col} = ? WHERE id = ?",
                        (status, message, now, source_id))

    def get(self, source_id: str) -> dict | None:
        r = self.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return dict(r) if r else None


class DownloadRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, task: DownloadTask) -> DownloadTask:
        task.created_at = task.updated_at = time.time()
        row = task.to_row()
        cols = ", ".join(row)
        with self.db.transaction() as c:
            cur = c.execute(f"INSERT INTO downloads ({cols}) VALUES ({', '.join('?' * len(row))})",
                            tuple(row.values()))
            task.id = cur.lastrowid
        return task

    def update(self, task: DownloadTask, *fields: str) -> None:
        task.updated_at = time.time()
        row = task.to_row()
        names = list(fields) + ["updated_at"] if fields else [k for k in row]
        sets = ", ".join(f"{k} = ?" for k in names)
        self.db.execute(f"UPDATE downloads SET {sets} WHERE id = ?", (*[row[k] for k in names], task.id))

    def get(self, task_id: int) -> DownloadTask | None:
        r = self.db.query_one("SELECT * FROM downloads WHERE id = ?", (task_id,))
        return DownloadTask.from_row(r) if r else None

    def all(self) -> list[DownloadTask]:
        return [DownloadTask.from_row(r) for r in self.db.query("SELECT * FROM downloads ORDER BY id")]

    def delete(self, task_id: int) -> None:
        self.db.execute("DELETE FROM downloads WHERE id = ?", (task_id,))

    def recent_completed(self, limit: int = 12) -> list[DownloadTask]:
        return [DownloadTask.from_row(r) for r in self.db.query(
            "SELECT * FROM downloads WHERE status = ? ORDER BY completed_at DESC LIMIT ?",
            (str(DownloadStatus.COMPLETED), limit))]


class ExtractionRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, download_id: int | None, library_id: int | None, archive: str, dest: str) -> int:
        now = time.time()
        with self.db.transaction() as c:
            cur = c.execute("INSERT INTO extractions (download_id, library_id, archive_path, dest_dir, status, "
                            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                            (download_id, library_id, archive, dest, "pending", now, now))
            return cur.lastrowid

    def update(self, ext_id: int, **values) -> None:
        values["updated_at"] = time.time()
        if "nested_archives" in values and not isinstance(values["nested_archives"], str):
            values["nested_archives"] = json.dumps(values["nested_archives"])
        sets = ", ".join(f"{k} = ?" for k in values)
        self.db.execute(f"UPDATE extractions SET {sets} WHERE id = ?", (*values.values(), ext_id))

    def get(self, ext_id: int) -> dict | None:
        r = self.db.query_one("SELECT * FROM extractions WHERE id = ?", (ext_id,))
        return dict(r) if r else None

    def interrupted(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM extractions WHERE status IN ('pending', 'running')")]

    def for_library(self, library_id: int) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM extractions WHERE library_id = ? ORDER BY id DESC", (library_id,))]


class LibraryRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, e: LibraryEntry) -> LibraryEntry:
        e.added_at = e.updated_at = time.time()
        row = e.to_row()
        with self.db.transaction() as c:
            cur = c.execute(f"INSERT INTO library ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                            tuple(row.values()))
            e.id = cur.lastrowid
        return e

    def update(self, e: LibraryEntry) -> None:
        e.updated_at = time.time()
        row = e.to_row()
        sets = ", ".join(f"{k} = ?" for k in row)
        self.db.execute(f"UPDATE library SET {sets} WHERE id = ?", (*row.values(), e.id))

    def get(self, entry_id: int) -> LibraryEntry | None:
        r = self.db.query_one("SELECT * FROM library WHERE id = ?", (entry_id,))
        return LibraryEntry.from_row(r) if r else None

    def by_download(self, download_id: int) -> LibraryEntry | None:
        r = self.db.query_one("SELECT * FROM library WHERE download_id = ?", (download_id,))
        return LibraryEntry.from_row(r) if r else None

    def all(self) -> list[LibraryEntry]:
        return [LibraryEntry.from_row(r) for r in self.db.query("SELECT * FROM library ORDER BY title COLLATE NOCASE")]

    def delete(self, entry_id: int) -> None:
        self.db.execute("DELETE FROM library WHERE id = ?", (entry_id,))

    def dedup_keys(self, statuses: Iterable[str] | None = None) -> set[str]:
        if statuses:
            st = list(statuses)
            rows = self.db.query(f"SELECT dedup_key FROM library WHERE status IN ({','.join('?' * len(st))})", st)
        else:
            rows = self.db.query("SELECT dedup_key FROM library")
        return {r[0] for r in rows}

    def duplicates_of(self, e: LibraryEntry) -> list[LibraryEntry]:
        rows = self.db.query(
            "SELECT * FROM library WHERE id IS NOT ? AND (dedup_key = ? OR (install_path IS NOT NULL AND install_path = ?))",
            (e.id, e.dedup_key, e.install_path))
        return [LibraryEntry.from_row(r) for r in rows]


class CreatedPathsRepository:
    """Every folder/file the app itself created (downloads, extracted games) — only these are ever offered as
    leftovers to delete."""

    def __init__(self, db: Database):
        self.db = db

    def add(self, path: Path | str, kind: str, game_id: str | None = None) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO created_paths (path, kind, game_id, created_at) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(path) DO UPDATE SET kind = excluded.kind, game_id = excluded.game_id",
                      (str(Path(path)), kind, game_id, time.time()))

    def paths(self) -> set[str]:
        return {r["path"] for r in self.db.query("SELECT path FROM created_paths")}

    def remove(self, path: Path | str) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM created_paths WHERE path = ?", (str(Path(path)),))


class CustomSourceRepository:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> list[dict]:
        rows = []
        for r in self.db.query("SELECT * FROM custom_sources ORDER BY added_at"):
            d = dict(r)
            d["enabled"] = bool(d["enabled"])
            d["details"] = json.loads(d.get("details") or "{}")
            rows.append(d)
        return rows

    def add(self, source_id: str, name: str, base_url: str, kind: str, details: dict) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO custom_sources (id, name, base_url, kind, enabled, added_at, details) "
                      "VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                      "base_url = excluded.base_url, kind = excluded.kind, details = excluded.details",
                      (source_id, name, base_url, kind, time.time(), json.dumps(details)))

    def remove(self, source_id: str) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM custom_sources WHERE id = ?", (source_id,))


class WishlistRepository:
    """Games the user wants that may not be available yet (see ezgd.catalog.wishlist)."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        try:
            d["matches"] = json.loads(d.get("matches") or "[]")
        except ValueError:
            d["matches"] = []
        d["coming_soon"] = bool(d.get("coming_soon"))
        return d

    def all(self) -> list[dict]:
        return [self._row(r) for r in self.db.query("SELECT * FROM wishlist ORDER BY added_at DESC")]

    def get(self, item_id: int) -> dict | None:
        r = self.db.query_one("SELECT * FROM wishlist WHERE id = ?", (item_id,))
        return self._row(r) if r else None

    def find(self, steam_app_id: int | None, dedup: str) -> dict | None:
        if steam_app_id:
            r = self.db.query_one("SELECT * FROM wishlist WHERE steam_app_id = ?", (steam_app_id,))
            if r:
                return self._row(r)
        r = self.db.query_one("SELECT * FROM wishlist WHERE dedup_key = ?", (dedup,))
        return self._row(r) if r else None

    def add(self, title: str, dedup: str, steam_app_id: int | None = None, cover_url: str | None = None,
            hero_url: str | None = None, release_text: str | None = None, coming_soon: bool = False,
            note: str | None = None) -> int:
        with self.db.transaction() as c:
            cur = c.execute(
                "INSERT INTO wishlist (title, dedup_key, steam_app_id, cover_url, hero_url, release_text, coming_soon,"
                " added_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, dedup, steam_app_id, cover_url, hero_url, release_text, int(bool(coming_soon)), time.time(),
                 note))
            return int(cur.lastrowid)

    def update(self, item_id: int, **values) -> None:
        if not values:
            return
        if "matches" in values:
            values["matches"] = json.dumps(values["matches"], ensure_ascii=False)
        if "coming_soon" in values:
            values["coming_soon"] = int(bool(values["coming_soon"]))
        allowed = {"title", "steam_app_id", "cover_url", "hero_url", "release_text", "coming_soon", "checked_at",
                   "status", "matches", "notified_at", "note"}
        bad = set(values) - allowed
        if bad:
            raise ValueError(f"unknown wishlist fields: {', '.join(sorted(bad))}")
        sets = ", ".join(f"{k} = ?" for k in values)
        self.db.execute(f"UPDATE wishlist SET {sets} WHERE id = ?", (*values.values(), item_id))

    def remove(self, item_id: int) -> None:
        self.db.execute("DELETE FROM wishlist WHERE id = ?", (item_id,))
