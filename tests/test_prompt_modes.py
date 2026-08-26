# tests/test_prompt_modes.py
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import Usage
from comic_studio.engine.projects import create_project
from comic_studio.engine.prompts.gen import generate_video_prompt
from comic_studio.engine.prompts.modes import PROMPT_MODES, mode_spec
from comic_studio.engine.shots import persist_shots


def test_four_modes_exist_and_pin_lessons():
    assert set(PROMPT_MODES) == {"A", "B", "C", "D"}
    d = PROMPT_MODES["D"]["spec"]
    assert "[Shot" in d and "<Subject" in d and "<d>Chinese</d>" in d
    b = PROMPT_MODES["B"]["spec"]
    assert "高密度" in b or "足够详细" in b
    c = PROMPT_MODES["C"]["spec"]
    assert "站位" in c and "间距" in c
    for spec in PROMPT_MODES.values():
        assert "服装" in spec["spec"]
    with pytest.raises(ValueError):
        mode_spec("E")


def test_generate_uses_project_mode(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "模式剧", "16:9", "t", prompt_mode="A")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    captured = {}

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured["system"] = messages[0]["content"]
            return "林晨推开木门，晨光，推进镜头，写实。", Usage(1, 1)

    generate_video_prompt(db, sid, FakeLLM(), backend="h3")
    assert PROMPT_MODES["A"]["spec"][:30] in captured["system"]
    # 显式 mode 覆盖项目设置（D 的结构校验会拒散文回复——system 已捕获即达成测试目的）
    with pytest.raises(RuntimeError):
        generate_video_prompt(db, sid, FakeLLM(), backend="h3", mode="D")
    assert PROMPT_MODES["D"]["spec"][:30] in captured["system"]


def test_t2v_rich_prompt_spec():
    """t2v 富结构化模板（2026-08-26 用户需求）：纯文生视频无参考图，
    提示词是唯一约束——七段结构化格式。"""
    from comic_studio.engine.prompts.modes import mode_spec
    spec = mode_spec("D")  # D 是默认，t2v 时生成器自动附加富模板
    # 验证核心分段已在骨架
    for section in ("subject_definitions:", "detailed_description:",
                    "overall_soundscape:", "non_diegetic_music:"):
        assert section in spec


def test_t2v_context_adds_rich_template():
    """workflow_type=t2v 时上下文附加密富模板要求。"""
    from comic_studio.engine.prompts.gen import build_shot_context
    shot = {"seq": 1, "description": "对话", "duration": 5.0,
            "shot_type": "", "camera_json": "{}", "workflow_type": "t2v",
            "ledger_json": "{}"}
    proj = {"aspect_ratio": "16:9", "style": "写实", "era": ""}
    ctx = build_shot_context(shot, {}, proj)
    assert "文生视频" in ctx or "纯文" in ctx
    assert "无图片参考" in ctx  # 槽位表应显示无参考
