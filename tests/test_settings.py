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
    # merge_xfade/merge_grade：C6 段间交叉淡化/统一调色（2026-09-01，默认关）
    # page_redraw_denoise：动态漫整页重绘重绘幅度（2026-09-09，决策 8）
    # h3_sla_*：H3 SLA 注意力三键（2026-09-10，默认开/0.9 本机验证/64 音频安全）
    assert get_setting(db, "comfy") == {"base_url": "http://127.0.0.1:8188",
                                        "min_free_vram_gb": 8,
                                        "merge_xfade": False,
                                        "merge_grade": False,
                                        "director_batch_frames": 512, "page_ref_denoise": 0.9,
                                        "director_clear_vram": False,
                                        "director_export_source": False,
                                        "director_batch_relay": True,
                                        "director_mix": True,
                                        "mute_quiet_shots": False,
                                        "page_redraw_denoise": 1.0,
                                        "page_redraw_h3_duration": 2,
                                        "h3_sla_enabled": True,
                                        "h3_sla_sparsity": 0.9,
                                        "h3_sla_block_size": "64"}


def test_page_redraw_defaults(tmp_path):
    """动态漫整页重绘（2026-09-09 建；2026-09-11 v3 线稿 ControlNet 锁结构，denoise 1.0）。"""
    db = _db(tmp_path)
    assert get_setting(db, "template_map")["page_redraw"] == "zimage_page_redraw"
    assert get_setting(db, "comfy")["page_redraw_denoise"] == 1.0

