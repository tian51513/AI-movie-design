# tests/test_prompt_gen.py
import json
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import persist_shots
from comic_studio.engine.prompts.gen import (
    LTX_SYSTEM, build_h3_system, build_shot_context, generate_video_prompt,
    validate_h3)
from comic_studio.engine.llm.provider import Usage


def test_h3_system_contains_rules_and_pipeline_note():
    s = build_h3_system()
    assert "官方" in s or "限制" in s            # vendored 规则已拼入
    assert "非交互" in s and "不要输出" in s    # 流水线适配说明


def test_shot_context_binds_assets_and_style():
    shot = {"seq": 3, "description": "庭院对话", "duration": 5.0,
            "ledger_json": json.dumps({"must_appear": ["林晨"], "must_keep": [],
                                       "may_change": [], "must_avoid": ["换装"],
                                       "assets": {"characters": [1], "scenes": [], "props": []}}),
            "shot_type": "", "camera_json": '{"景别":"中景"}', "workflow_type": "ref2va"}
    assets = {1: {"kind": "character", "name": "林晨",
                  "appearance_json": '{"detail":"黑发少年"}'}}
    proj = {"aspect_ratio": "9:16", "style": "真人电影"}
    ctx = build_shot_context(shot, assets, proj)
    for token in ("镜头 3", "庭院对话", "林晨", "黑发少年", "真人电影", "9:16", "5.0", "禁止"):
        assert token in ctx, token


def test_validate_h3_accepts_reasonable_prompt():
    ok, msg = validate_h3("林晨在庭院中推开木门，晨光洒入，镜头缓慢推进，写实画面。", 5, "9:16", 0, 0)
    assert ok is True, msg


def test_validate_h3_rejects_overlong():
    ok, msg = validate_h3("推门。" * 4000, 5, "9:16", 0, 0)
    assert ok is False


def test_generate_h3_prompt_with_fake_client(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "p", "9:16", "t",
                         style="真人电影")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="推门",
        shot_type="", camera={}, duration=5.0, workflow_type="ref2va",
        ledger={}, character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "林晨在庭院中推开木门，晨光洒入，镜头缓慢推进，写实画面。", Usage(10, 20)

    out = generate_video_prompt(db, sid, FakeLLM(), backend="h3", mode="A")
    assert "推" in out and len(out) < 2000


def test_generate_retries_on_validation_failure_then_ok(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="x",
        shot_type="", camera={}, duration=5.0, workflow_type="ref2va",
        ledger={}, character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    replies = iter(["占位 可自行补充", "林晨推开木门，晨光，推进镜头，写实。"])

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return next(replies), Usage(1, 1)

    out = generate_video_prompt(db, sid, FakeLLM(), backend="h3", mode="A")
    assert "木门" in out  # 第二次（带校验错误反馈）通过


def test_structure_check_by_mode():
    """A: 结构校验入重试环（2026-08-25）——B/C/D 缺必需分段要拦，A/None 放行。"""
    from comic_studio.engine.prompts.gen import structure_check
    prose = "[Shot 2] 近景仰视推镜：一段散文描述，无任何分节。"
    structured = ("subject_definitions:\n<Subject 1> 来自 <Picture 1>\nsummary:\n概要\n"
                  "retention_analysis:\n保持\n\ndetailed_description:\n[Shot 1] 描述\n"
                  "overall_soundscape:\n风声\nnon_diegetic_music:\n无")
    assert structure_check(prose, "A") == (True, "")
    assert structure_check(prose, None) == (True, "")
    ok, msg = structure_check(prose, "D")
    assert not ok and "subject_definitions" in msg
    assert structure_check(structured, "D") == (True, "")
    assert structure_check(structured.upper().replace(" ", ""), "C")[0]  # 大小写宽容


def test_generate_retries_on_missing_structure(tmp_path):
    """真机 2026-08-25：D 模式产出散文（规范要求骨架但旧无校验）→ 应带原因重试。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="庭院",
        shot_type="", camera={}, duration=5.0, workflow_type="ref2va",
        ledger={}, character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    structured = ("subject_definitions:\n<Subject 1> 来自 <Picture 1>\nsummary:\n参考生成\n"
                  "retention_analysis:\n保持服装\ndetailed_description:\n[Shot 1] 林晨推开木门，晨光，推进镜头，写实。\n"
                  "overall_soundscape:\n风声\nnon_diegetic_music:\n无")
    replies = iter(["[Shot 1] 林晨推开木门，晨光，推进镜头，写实的散文一段。", structured])

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return next(replies), Usage(1, 1)

    out = generate_video_prompt(db, sid, FakeLLM(), backend="h3")  # 默认 D
    assert "subject_definitions" in out  # 散文被拦后重试，结构版通过


def test_mode_specs_embed_skeleton():
    """B: 结构化模式规范内嵌完整填空骨架（few-shot），不再只是'要求列出'。"""
    from comic_studio.engine.prompts.modes import mode_spec
    for m in ("B", "C", "D"):
        spec = mode_spec(m)
        for section in ("subject_definitions:", "retention_analysis:",
                        "detailed_description:", "non_diegetic_music:"):
            assert section in spec, (m, section)
    assert "subject_definitions" not in mode_spec("A")  # A 散文明确不用结构
