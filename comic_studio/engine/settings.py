# comic_studio/engine/settings.py
"""settings 表读写。默认值即产品默认行为（spec §2/§8.6/§9.1）。"""
import copy
import json

from .db import Database

DEFAULT_SETTINGS = {
    "workers": 1,
    # 类型→模板映射（spec §6.3）；t2v 可选，默认 None
    "template_map": {
        "character_views": "character_views",
        "t2i": "zimage_t2i",  # 主力：Z-Image Turbo（majicmix 版 t2i_ref 保留可切换）
        "ref2va": "h3_ref2va",
        "fl2v": "h3_fl2v",
        "t2v": "h3_t2v",
    },
    "llm_providers": {
        "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama",
                  "model": "qwen3:14b"},
        "online": {"base_url": "", "api_key": "", "model": ""},
    },
    # 任务路由（spec §9.1：轻活本地、重活线上）
    "llm_routing": {
        "extract_assets": "local",
        "fix_appearance": "local",
        "split_storyboards": "online",
        "gen_video_prompt": "online",
    },
    "comfy": {"base_url": "http://127.0.0.1:8188"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：dict 递归合并，标量/列表以 override 为准。"""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def get_setting(db: Database, key: str):
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    row = db.connect().execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return copy.deepcopy(DEFAULT_SETTINGS[key])
    stored = json.loads(row["value_json"])
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, dict) and isinstance(stored, dict):
        return _deep_merge(default, stored)
    return stored


def set_setting(db: Database, key: str, value) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()
