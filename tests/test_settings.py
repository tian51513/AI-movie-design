# tests/test_settings.py
from comic_studio.engine.db import Database
from comic_studio.engine.settings import DEFAULT_SETTINGS, get_setting, set_setting


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_default_returned_without_write(tmp_path):
    db = _db(tmp_path)
    assert get_setting(db, "workers") == 1


def test_set_then_get_roundtrip(tmp_path):
    db = _db(tmp_path)
    set_setting(db, "workers", 2)
    assert get_setting(db, "workers") == 2


def test_mutating_result_does_not_pollute_defaults(tmp_path):
    db = _db(tmp_path)
    providers = get_setting(db, "llm_providers")
    providers["local"]["model"] = "changed"
    assert DEFAULT_SETTINGS["llm_providers"]["local"]["model"] != "changed"


def test_unknown_key_raises(tmp_path):
    import pytest
    db = _db(tmp_path)
    with pytest.raises(KeyError):
        get_setting(db, "nope")
