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


def test_partial_stored_providers_merge_with_defaults(tmp_path):
    """存储部分 llm_providers 时，未存储的 key（如 online）应保留默认值。"""
    db = _db(tmp_path)
    set_setting(db, "llm_providers", {"local": {"base_url": "http://my:8080/v1",
                                                  "api_key": "mykey", "model": "mymodel"}})
    val = get_setting(db, "llm_providers")
    assert val["local"]["model"] == "mymodel"
    assert val["online"]["model"] == ""  # 默认值保留


def test_data_dir_removed_from_defaults(tmp_path):
    """data_dir 不再是合法 setting key。"""
    import pytest
    db = _db(tmp_path)
    with pytest.raises(KeyError):
        get_setting(db, "data_dir")


def test_unknown_key_raises(tmp_path):
    import pytest
    db = _db(tmp_path)
    with pytest.raises(KeyError):
        get_setting(db, "nope")


def test_comfy_setting_default(tmp_path):
    db = _db(tmp_path)
    # min_free_vram_gb：LLM 让位后的显存门槛（2026-08-28，12GB 共享决策）
    assert get_setting(db, "comfy") == {"base_url": "http://127.0.0.1:8188",
                                        "min_free_vram_gb": 8,
                                        "director_batch_frames": 512,
                                        "director_clear_vram": False,
                                        "director_export_source": False,
                                        "director_batch_relay": True,
                                        "director_mix": True}
