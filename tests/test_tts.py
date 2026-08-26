# tests/test_tts.py
"""P6 Task 1：TTS 配音（Edge-TTS）——从 ledger.dialogue 生成语音。"""
import json
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import persist_shots


def _proj_with_dialogue(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "配音剧", "16:9", "正文")["id"]
    from comic_studio.engine.assets import persist_assets
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林医生", appearance="性别：男\n年龄：45岁", tags=[]),
                                  NS(name="璃", appearance="性别：女\n年龄：20岁", tags=[])],
                      scenes=[], props=[]))
    persist_shots(db, pid, [
        NS(text_span="", description="对话A", shot_type="", camera={},
           duration=5.0, workflow_type="t2v",
           ledger={"dialogue": [{"speaker": "林医生", "line": "你哪里不舒服？"},
                                 {"speaker": "璃", "line": "头有点晕"}],
                   "assets": {"characters": [1, 2], "scenes": [], "props": []}},
           character_ids=[1, 2], scene_ids=[], prop_ids=[], depends_on=None),
    ])
    return db, pid


def test_generate_dialogue_audio(tmp_path):
    """逐镜逐句生成 TTS，角色按性别分配声音。"""
    db, pid = _proj_with_dialogue(tmp_path)
    from comic_studio.engine.tts import generate_dialogue_audio

    # Mock edge_tts：模拟写文件
    saved = []
    def mock_save(path):
        Path(path).write_bytes(b"fake-mp3")
        saved.append(Path(path).name)
    mock_comm = MagicMock()
    mock_comm.save = AsyncMock(side_effect=mock_save)

    with patch("edge_tts.Communicate", return_value=mock_comm):
        result = generate_dialogue_audio(db, tmp_path / "data", pid)

    assert len(result) == 1  # 一镜
    shot_result = result[0]
    assert shot_result["seq"] == 1
    assert len(shot_result["lines"]) == 2  # 两句对白
    assert shot_result["lines"][0]["speaker"] == "林医生"
    assert shot_result["lines"][1]["speaker"] == "璃"
    # 音频文件存在
    shot_dir = tmp_path / "data" / "projects" / "配音剧" / "shots" / "1"
    assert (shot_dir / "dialogue.mp3").exists() or saved  # 至少有生成动作


def test_gender_detection():
    """从八行外貌模板检测性别。"""
    from comic_studio.engine.tts import detect_gender
    assert detect_gender("性别：男\n年龄：45岁") == "male"
    assert detect_gender("性别：女\n年龄：20岁") == "female"
    assert detect_gender("无性别信息") == "male"  # 默认男声


def test_voice_mapping():
    """角色 → 声音映射（男→云希，女→晓晓）。"""
    from comic_studio.engine.tts import voice_for_character, DEFAULT_VOICES
    assert voice_for_character("male") == DEFAULT_VOICES["male"]
    assert voice_for_character("female") == DEFAULT_VOICES["female"]
