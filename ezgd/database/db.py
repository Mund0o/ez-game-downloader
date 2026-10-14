"""Thread-safe SQLite wrapper with migrations, integrity recovery and backups."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from ezgd.database.migrations import MIGRATIONS, Migration

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    pass


class SchemaTooNewError(DatabaseError):
    """The database was written by a newer version of the application."""

    def __init__(self, found: int, supported: int):
        super().__init__(f"Database schema v{found} is newer than this application supports (v{supported}).")
        self.found = found
        self.supported = supported


class Database:
    """One shared connection guarded by a re-entrant lock.

    Statements are short; long-running work (downloads, extraction) never holds
    the lock across I/O. WAL mode keeps readers and the single writer from
    blocking each other on disk.
    """

    def __init__(self, path: Path | str, *, backup_dir: Path | None = None,
                 migrations: Sequence[Migration] = MIGRATIONS):
        self.path = Path(path) if str(path) != ":memory:" else None
        self._raw_path = str(path)
        self.backup_dir = backup_dir
        self._migrations = list(migrations)
        self._latest = self._migrations[-1].version if self._migrations else 0
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.recovered_from: Path | None = None   # set when a corrupt file was moved aside
        self.migrated_from: int | None = None

    # ------------------------------------------------------------------ open
    def open(self) -> "Database":
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connect()
            self._check_integrity()
        except sqlite3.DatabaseError as exc:
            if not self.path:
                raise
            log.error("Database %s is unreadable (%s); moving it aside", self.path, exc)
            self._quarantine()
            self._connect()
        self._migrate()
        return self

    def _connect(self) -> None:
        conn = sqlite3.connect(self._raw_path, check_same_thread=False, isolation_level=None,
                               timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if self.path:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        self._conn = conn

    def _check_integrity(self) -> None:
        row = self.conn.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise sqlite3.DatabaseError(f"integrity check failed: {row[0] if row else 'no result'}")

    def _quarantine(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        assert self.path
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                os.replace(src, Path(str(target) + suffix))
        self.recovered_from = target

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("database is not open")
        return self._conn

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.schema_version
        if current > self._latest:
            raise SchemaTooNewError(current, self._latest)
        if current == self._latest:
            return
        if current > 0 and self.path and self.backup_dir:
            self.backup(self.backup_dir / f"pre-migration-v{current}-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
        self.migrated_from = current
        for m in self._migrations:
            if m.version <= current:
                continue
            log.info("Applying database migration v%d: %s", m.version, m.description)
            with self._lock:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    m.apply(self.conn)
                    self.conn.execute(f"PRAGMA user_version = {int(m.version)}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    if self.path:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------ statements
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; rolls back on any exception. Re-entrant."""
        with self._lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def log_event(self, level: str, category: str, message: str, ref: str | None = None) -> None:
        with self.transaction() as c:
            c.execute("INSERT INTO events (ts, level, category, message, ref) VALUES (?, ?, ?, ?, ?)",
                      (time.time(), level, category, message[:2000], ref))
            # keep the table bounded
            c.execute("DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET 5000)")

    # --------------------------------------------------------------- backups
    def backup(self, target: Path) -> Path:
        """Consistent online copy via the SQLite backup API."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with self._lock:
            dst = sqlite3.connect(str(tmp))
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
        os.replace(tmp, target)
        log.info("Database backed up to %s", target)
        return target

    def list_backups(self) -> list[Path]:
        if not self.backup_dir or not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)

    def prune_backups(self, keep: int = 10) -> None:
        for old in self.list_backups()[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    @staticmethod
    def validate_backup(path: Path) -> int:
        """Return the schema version of a backup file, raising if it is unusable."""
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise DatabaseError(f"backup failed integrity check: {ok}")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"settings", "downloads", "library"} <= names:
                raise DatabaseError("file does not look like an EZ Game Downloader database")
            return version
        except sqlite3.DatabaseError as exc:
            raise DatabaseError(f"not a valid database: {exc}") from exc
        finally:
            conn.close()

    def restore(self, backup_path: Path) -> None:
        """Replace the live database with ``backup_path`` (current file is backed up first)."""
        if not self.path:
            raise DatabaseError("cannot restore an in-memory database")
        version = self.validate_backup(backup_path)
        if version > self._latest:
            raise SchemaTooNewError(version, self._latest)
        if self.backup_dir:
            self.backup(self.backup_dir / f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
        with self._lock:
            self.close()
            for suffix in ("-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    p.unlink()
            shutil.copyfile(backup_path, self.path)
            self._connect()
            self._check_integrity()
            self._migrate()
