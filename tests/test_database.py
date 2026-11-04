import sqlite3

import pytest

from ezgd.database.db import Database, DatabaseError, SchemaTooNewError
from ezgd.database.migrations import LATEST_VERSION, MIGRATIONS, Migration


def test_fresh_database_is_migrated(tmp_path):
    db = Database(tmp_path / "a.sqlite3").open()
    assert db.schema_version == LATEST_VERSION
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "games", "downloads", "extractions", "library", "http_cache", "events"} <= tables
    db.close()


def test_schema_newer_than_app_is_refused(tmp_path):
    path = tmp_path / "new.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    conn.close()
    with pytest.raises(SchemaTooNewError) as ei:
        Database(path).open()
    assert ei.value.found == LATEST_VERSION + 5


def test_corrupt_file_is_quarantined_and_recreated(tmp_path):
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"this is definitely not sqlite" * 100)
    db = Database(path).open()
    assert db.recovered_from is not None and db.recovered_from.exists()
    assert db.schema_version == LATEST_VERSION
    db.close()


def test_a_full_disk_or_a_lock_never_moves_the_database_aside(tmp_path, monkeypatch):
    # OperationalError is a DatabaseError too: a full disk (or a lock, no permission, an I/O error) had a good
    # database moved aside as "damaged" and the app start with an empty library
    path = tmp_path / "ezgd.sqlite3"
    Database(path).open().close()
    for msg in ("database or disk is full", "database is locked"):
        def failing(self, msg=msg):
            raise sqlite3.OperationalError(msg)
        monkeypatch.setattr(Database, "_check_integrity", failing)
        with pytest.raises(sqlite3.OperationalError):
            Database(path).open()
        assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))
    monkeypatch.undo()
    # a guest of the running app (play mode) never moves it aside, even when it is damaged
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"this is definitely not sqlite" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        Database(broken).open(recover=False)
    assert broken.exists() and not list(tmp_path.glob("broken.sqlite3.corrupt-*"))


def test_incremental_migration_with_backup(tmp_path):
    path = tmp_path / "m.sqlite3"
    Database(path).open().close()
    extra = list(MIGRATIONS) + [Migration(LATEST_VERSION + 1, "add test column",
                                          lambda c: c.execute("ALTER TABLE library ADD COLUMN test_col TEXT"))]
    db = Database(path, backup_dir=tmp_path / "bk", migrations=extra).open()
    assert db.schema_version == LATEST_VERSION + 1
    assert db.migrated_from == LATEST_VERSION
    cols = {r[1] for r in db.query("PRAGMA table_info(library)")}
    assert "test_col" in cols
    assert list((tmp_path / "bk").glob("pre-migration-v*.sqlite3"))
    db.close()


def test_failed_migration_rolls_back(tmp_path):
    path = tmp_path / "f.sqlite3"
    Database(path).open().close()

    def boom(c):
        c.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("migration bug")

    extra = list(MIGRATIONS) + [Migration(LATEST_VERSION + 1, "broken", boom)]
    with pytest.raises(RuntimeError):
        Database(path, migrations=extra).open()
    db = Database(path).open()
    assert db.schema_version == LATEST_VERSION
    assert not db.query("SELECT name FROM sqlite_master WHERE name='half_done'")
    db.close()


def test_transaction_rollback(db):
    with pytest.raises(ValueError):
        with db.transaction() as c:
            c.execute("INSERT INTO settings (key, value, updated_at) VALUES ('k', '1', 0)")
            raise ValueError("abort")
    assert db.query_one("SELECT * FROM settings WHERE key='k'") is None


def test_backup_validate_and_restore(db, tmp_path):
    db.execute("INSERT INTO settings (key, value, updated_at) VALUES ('before', '1', 0)")
    bk = db.backup(tmp_path / "backups" / "b.sqlite3")
    assert Database.validate_backup(bk) == LATEST_VERSION
    db.execute("INSERT INTO settings (key, value, updated_at) VALUES ('after', '1', 0)")
    db.restore(bk)
    keys = {r[0] for r in db.query("SELECT key FROM settings")}
    assert "before" in keys and "after" not in keys
    assert any(p.name.startswith("pre-restore") for p in db.list_backups())


def test_a_restore_that_fails_midway_leaves_the_database_as_it_was(db, tmp_path, monkeypatch):
    # the backup was copied straight over the live file: a full disk midway left it half copied (and closed)
    import shutil
    bk = db.backup(tmp_path / "backups" / "b.sqlite3")
    db.execute("INSERT INTO settings (key, value, updated_at) VALUES ('kept', '1', 0)")

    def full_disk(src, dst):
        open(dst, "wb").write(b"half")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(shutil, "copyfile", full_disk)
    with pytest.raises(OSError):
        db.restore(bk)
    assert "kept" in {r[0] for r in db.query("SELECT key FROM settings")}      # open, and as it was


def test_validate_backup_rejects_garbage(tmp_path):
    bad = tmp_path / "x.sqlite3"
    bad.write_bytes(b"garbage")
    with pytest.raises(DatabaseError):
        Database.validate_backup(bad)
    other = tmp_path / "other.sqlite3"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE t (x)")
    c.close()
    with pytest.raises(DatabaseError):
        Database.validate_backup(other)


def test_event_log_is_bounded(db):
    for i in range(30):
        db.log_event("info", "test", f"event {i}")
    assert db.query_one("SELECT COUNT(*) FROM events")[0] == 30
