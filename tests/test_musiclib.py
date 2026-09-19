# tests/test_musiclib.py
"""音乐库（2026-09-19 spec）：music3 模板 / 库 CRUD / LLM 建议 / gen_music /
API / 合成混入。本文件随任务逐段追加。"""
import json
from pathlib import Path

import pytest

from comic_studio.engine.workflows.registry import scan_templates


def test_music3_template():
    reg = scan_templates(Path("templates/workflows"))
    t = reg["music3"]
    assert t.type == "music"
    wf = t.api_json()
    assert wf["37:13"]["class_type"] == "MiniMaxMusic3TextEncode"
    assert wf["35"]["class_type"] == "SaveAudioAdvanced"
    assert t.inject_prompt.node == "37:13" and t.inject_prompt.field == "caption"
    for key, node, field in (("seed", "37:38", "seed"),
                             ("max_duration", "37:13", "max_duration"),
                             ("lyrics", "37:13", "lyrics")):
        ip = t.inject_params[key]
        assert (ip.node, ip.field) == (node, field), key
    assert {s.label for s in t.models} == {"unet", "clip", "vae"}


def test_migrate_43_music_table(tmp_path):
    from comic_studio.engine.db import Database
    db = Database(tmp_path / "s.db"); db.migrate()
    conn = db.connect()
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(music_library)")]
    assert {"id", "name", "caption", "lyrics", "seed", "duration",
            "origin", "path", "created_at"} <= set(cols)
    pcols = [r["name"] for r in conn.execute("PRAGMA table_info(projects)")]
    assert "bgm_music_id" in pcols and "bgm_volume" in pcols


def test_library_crud_roundtrip(tmp_path):
    from comic_studio.engine.db import Database
    from comic_studio.engine.musiclib import (delete_music, list_music,
                                              save_to_library)
    db = Database(tmp_path / "s.db"); db.migrate()
    src = tmp_path / "draft.mp3"; src.write_bytes(b"mp3data")
    entry = save_to_library(db, tmp_path, src, "夜晚钢琴", "Lo-fi 钢琴 70BPM",
                            "", 42, 120)
    assert entry["id"] and (tmp_path / "music/custom/夜晚钢琴.mp3").exists()
    assert list_music(db)[0]["name"] == "夜晚钢琴"
    with pytest.raises(ValueError, match="已存在"):
        save_to_library(db, tmp_path, src, "夜晚钢琴", "", "", 1, 60)
    delete_music(db, tmp_path, entry["id"])
    assert list_music(db) == [] and not (tmp_path / "music/custom/夜晚钢琴.mp3").exists()
    with pytest.raises(ValueError, match="音乐不存在"):
        delete_music(db, tmp_path, entry["id"])
