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


class FakeLLM:
    def __init__(self, reply): self.reply, self.calls = reply, []
    def raw_chat(self, messages, temperature=0.5):
        self.calls.append(messages); return self.reply, None


def test_suggest_caption_and_lyrics(tmp_path, monkeypatch):
    from comic_studio.engine.db import Database
    from comic_studio.engine import musiclib
    db = Database(tmp_path / "s.db"); db.migrate()
    fake = FakeLLM("Global Metadata: Cinematic lo-fi, 72 BPM, A minor.\n情绪走向：由静谧渐至温暖。")
    monkeypatch.setattr(musiclib, "client_for_task", lambda db, task: fake)
    cap = musiclib.suggest_caption(db, "雨夜重逢的校园恋爱短剧")
    assert cap.startswith("Global Metadata") and "雨夜" in fake.calls[0][-1]["content"]
    fake2 = FakeLLM("[主歌]\n风穿过走廊\n[副歌]\n我想再见你一面")
    monkeypatch.setattr(musiclib, "client_for_task", lambda db, task: fake2)
    assert "[副歌]" in musiclib.suggest_lyrics(db, "毕业季告别")
    with pytest.raises(ValueError, match="曲风"):
        monkeypatch.setattr(musiclib, "client_for_task",
                            lambda db, task: FakeLLM("  "))
        musiclib.suggest_caption(db)
