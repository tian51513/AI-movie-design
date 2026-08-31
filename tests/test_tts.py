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


def test_voiced_tts_paths(tmp_path, monkeypatch):
    """配音期角色音色（2026-08-31 用户决策）：单说话人绑音色→VoiceClone 生成
    dialogue.mp3；多说话人/未绑→Edge-TTS 逐句；h3_native_voice 镜跳过并清残留。"""
    import sys, pathlib
    sys.path.insert(0, "tests")
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.assets import persist_assets
    from comic_studio.engine.shots import persist_shots, update_shot
    from comic_studio.engine import tts as T
    from comfy_mock import comfy_server

    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "配音剧", "9:16", "t")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="小雪", appearance="性别：女", tags=[])],
                      scenes=[], props=[]))
    # persist_assets 的 NS 不支持 voice——直接 SQL 补
    conn = db.connect()
    conn.execute("UPDATE assets SET voice='萝莉' WHERE name='小雪'")
    conn.commit()
    preset = tmp_path / "data" / "voices" / "presets" / "萝莉.flac"
    preset.parent.mkdir(parents=True)
    preset.write_bytes(b"fLaC")
    dlg = lambda sp, ln: {"dialogue": [{"speaker": sp, "line": ln}]}
    # persist_shots 按批次位置编号——一次调用三条（分三次调会全挤 seq=1）
    sid1, sid2, sid3 = persist_shots(db, pid, [
        NS(text_span="", description="a", shot_type="", camera={}, duration=5,
           workflow_type="fl2v", ledger=dlg("小雪", "哥哥你回来啦。"),
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None),
        NS(text_span="", description="b", shot_type="", camera={}, duration=5,
           workflow_type="fl2v",
           ledger={"dialogue": [{"speaker": "小雪", "line": "好。"},
                                 {"speaker": "路人", "line": "让开。"}]},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None),
        NS(text_span="", description="c", shot_type="", camera={}, duration=5,
           workflow_type="ref2va", ledger=dlg("小雪", "原声镜。"),
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])
    from comic_studio.engine.shots import get_shot
    led3 = json.loads(get_shot(db, sid3)["ledger_json"]); led3["h3_native_voice"] = True
    update_shot(db, sid3, {"ledger_json": json.dumps(led3, ensure_ascii=False)})
    stale3 = tmp_path / "data" / "projects" / "配音剧" / "shots" / "3" / "dialogue.mp3"
    stale3.parent.mkdir(parents=True); stale3.write_bytes(b"stale")

    edge_calls = []
    monkeypatch.setattr(T, "_tts_sync",
                        lambda text, voice, out: (edge_calls.append((text, voice)),
                                                  out.write_bytes(b"mp3")) or None)
    monkeypatch.setattr(T, "_to_mp3", lambda s, d: d.write_bytes(s.read_bytes()))

    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", pathlib.Path("templates/workflows"))
    with comfy_server("ok", audio=True) as m:
        T._comfy_client = lambda db: ComfyClient(m.base_url)
        out = T.generate_dialogue_audio(db, tmp_path / "data", pid)

    d1 = tmp_path / "data" / "projects" / "配音剧" / "shots" / "1" / "dialogue.mp3"
    assert d1.exists()                                   # 克隆生成
    wf = m.prompts[0]["prompt"]
    assert "哥哥你回来啦" in wf["77"]["inputs"]["target_text"]
    assert wf["77"]["inputs"]["ref_audio"] == ["79", 0]
    assert wf["79"]["inputs"]["start_index"] == 0        # 样本整段
    assert any(u.endswith(".flac") for u in m.audio_uploads)   # 上传的是音色样本
    # 镜2 多说话人 → Edge-TTS 两句
    assert [t for t, _ in edge_calls] == ["好。", "让开。"]
    # 镜3 native → 跳过 + 残留清理
    assert not stale3.exists()
    seqs = {r["seq"] for r in out}
    assert 3 not in seqs
