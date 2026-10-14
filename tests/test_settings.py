from pathlib import Path

import pytest

from ezgd.core.settings import Settings, SettingsStore

ROOT = Path(__file__).parent.parent


def test_defaults_and_persistence(db):
    s = SettingsStore(db)
    assert s.get("max_concurrent_downloads") == 3
    s.set("max_concurrent_downloads", 5)
    s.set("auto_extract", "false")
    s2 = SettingsStore(db)
    assert s2.get("max_concurrent_downloads") == 5
    assert s2.get("auto_extract") is False


def test_values_are_clamped_and_validated(db):
    s = SettingsStore(db)
    assert s.set("max_concurrent_downloads", 999) == 16
    assert s.set("torrent_listen_port", -4) == 0
    with pytest.raises(ValueError):
        s.set("theme_accent", "rainbow")
    with pytest.raises(KeyError):
        Settings.coerce("does_not_exist", 1)


def test_listeners_are_notified(db):
    s = SettingsStore(db)
    seen = []
    s.subscribe(lambda k, v: seen.append((k, v)))
    s.set("http_speed_limit_kbps", 512)
    s.set("http_speed_limit_kbps", 512)          # unchanged → no second notification
    assert seen == [("http_speed_limit_kbps", 512)]


def test_sample_config_imports_cleanly(db):
    s = SettingsStore(db)
    warnings = s.import_toml(ROOT / "config.sample.toml")
    assert warnings == []
    assert s.get("sources_enabled")["steamrip"] is True
    assert s.get("max_concurrent_downloads") == 3


def test_invalid_stored_value_is_ignored(db):
    db.execute("INSERT INTO settings (key, value, updated_at) VALUES ('theme_accent', '\"nope\"', 0)")
    s = SettingsStore(db)
    assert s.get("theme_accent") == "violet"


def test_sources_added_later_start_enabled(db):
    # a database saved before cracked-games.org existed only lists the two older sources
    SettingsStore(db).set("sources_enabled", {"onlinefix": True, "steamrip": False})
    s = SettingsStore(db)
    assert s.get("sources_enabled") == {"onlinefix": True, "steamrip": False, "crackedgames": True,
                                          "fitgirl": True, "ankergames": True}
