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
    # 首答超长（>7000 字符上限）——自愈修不了长度，必须走 LLM 重试路径
    #（占位语类问题已被 P7-C 自愈接管，不再触发重试）
    replies = iter(["超长" * 4000, "林晨推开木门，晨光，推进镜头，写实。"])

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


def test_shot_context_carries_prev_continuity(tmp_path):
    """连贯性③（2026-08-26）：上下文带上一镜信息+延续约束——
    姿态/位置/服装默认延续上镜结尾，仅明确写出变化才变。"""
    from comic_studio.engine.shots import persist_shots
    db = Database(tmp_path / "s3.db"); db.migrate()
    pid = create_project(db, tmp_path / "d3", "连贯剧", "9:16", "t")["id"]
    drafts = [NS(text_span="", description="林晨坐着喝茶", shot_type="", camera={},
                 duration=5.0, workflow_type="ref2va", ledger={},
                 character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
                 prompt=""),
              NS(text_span="", description="林晨继续说话", shot_type="", camera={},
                 duration=5.0, workflow_type="ref2va", ledger={},
                 character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
                 prompt="")]
    sids = persist_shots(db, pid, drafts)
    captured = {}

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured["user"] = messages[-1]["content"]
            return ("subject_definitions:\\n<Subject 1>\\nsummary:\\n x\\n"
                    "retention_analysis:\\n x\\ndetailed_description:\\n x\\n"
                    "overall_soundscape:\\n x\\nnon_diegetic_music:\\n 无"), Usage(1, 1)

    out = generate_video_prompt(db, sids[1], FakeLLM(), backend="h3")
    assert "林晨坐着喝茶" in captured["user"]      # 上一镜描述入上下文
    assert "延续上一镜" in captured["user"]         # 延续约束
    assert "明确写出" in captured["user"]


def test_context_uses_slot_map_not_asset_ids(tmp_path):
    """真机 2026-08-26：<Picture 70>=资产id 照抄——角色锚定全失效。
    上下文必须给显式槽位表（<Picture 1/2>=具体内容），并禁用资产 id。"""
    from comic_studio.engine.shots import persist_shots
    db = Database(tmp_path / "s4.db"); db.migrate()
    pid = create_project(db, tmp_path / "d4", "槽位剧", "9:16", "t")["id"]
    from comic_studio.engine.assets import persist_assets
    persist_assets(db, tmp_path / "d4", pid,
                   NS(characters=[NS(name="林医生", appearance="黑框眼镜", tags=[])],
                      scenes=[], props=[]))
    aid = __import__("comic_studio.engine.assets", fromlist=["list_project_assets"]) \
        .list_project_assets(db, pid)[0]["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="问诊", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va",
        ledger={}, character_ids=[aid], scene_ids=[], prop_ids=[],
        depends_on=None)])[0]
    from comic_studio.engine.prompts.gen import generate_video_prompt
    captured = {}
    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured["user"] = messages[-1]["content"]
            captured["system"] = messages[0]["content"]
            return ("subject_definitions:\n<Subject 1>\nsummary:\n x\n"
                    "retention_analysis:\n x\ndetailed_description:\n x\n"
                    "overall_soundscape:\n x\nnon_diegetic_music:\n 无"), Usage(1, 1)
    generate_video_prompt(db, sid, FakeLLM(), backend="h3")
    ctx = captured["user"]
    assert "<Picture 1> = 林医生（角色三视图" in ctx         # 显式槽位表
    assert "严禁" in ctx and "资产 id" in ctx             # 禁用规则
    assert f"id={aid} " not in ctx                        # 不再出现裸 id 绑定行
    assert "<d>Chinese" in captured["system"]           # 对白标记指引在系统词/骨架


def test_skeleton_has_dialogue_tag_example():
    from comic_studio.engine.prompts.modes import mode_spec
    assert "<d>Chinese" in mode_spec("D")   # 骨架含对白标记示例


def test_context_carries_verbatim_dialogue(tmp_path):
    """台词链路：ledger.dialogue → 上下文'逐字使用'行 → 视频说原话。"""
    db = Database(tmp_path / "s5.db"); db.migrate()
    pid = create_project(db, tmp_path / "d5", "台词剧", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="问诊", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va",
        ledger={"dialogue": [{"speaker": "林医生", "line": "哪里不舒服？"}],
                "assets": {}},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    from comic_studio.engine.prompts.gen import generate_video_prompt
    captured = {}
    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured["user"] = messages[-1]["content"]
            return ("subject_definitions:\n x\nsummary:\n x\nretention_analysis:\n x\n"
                    "detailed_description:\n x\noverall_soundscape:\n x\n"
                    "non_diegetic_music:\n 无"), Usage(1, 1)
    generate_video_prompt(db, sid, FakeLLM(), backend="h3")
    assert "哪里不舒服？" in captured["user"]
    assert "逐字使用" in captured["user"] and "林医生" in captured["user"]


def test_picture_reference_validation():
    """真机 2026-08-26：<Picture 70>=资产 id——机械拦截只允许 1/2。"""
    from comic_studio.engine.prompts.gen import _check_picture_refs
    ok, msg = _check_picture_refs("subject_definitions:\n是来自 <Picture 1> 的人物")
    assert ok
    ok, msg = _check_picture_refs("subject_definitions:\n是来自 <Picture 70> 的人物")
    assert not ok and "70" in msg
    ok, msg = _check_picture_refs("无任何图片引用的纯文本")
    assert ok  # 无引用不拦


def test_heal_h3_prompt_common_fixes():
    """P7-C 提示词 token 自愈（借鉴 Director reinforce）：机械可修的不退回 LLM。"""
    from comic_studio.engine.prompts.gen import heal_h3_prompt
    shot = {"ledger_json": '{"dialogue":[{"speaker":"林晨","line":"你好"}]}',
            "duration": 5}
    # ① 有对白但缺 <d>Chinese</d> → 补；② <Picture 9> 超界 → 删；
    # ③ 重复约束行去重；④ 占位语删除
    bad = ("subject_definitions: 林晨\nsummary: 林晨推门。\n"
           "<Picture 9> 的画面主体身份明确。林晨说：你好。\n"
           "禁止出现：多余手指。禁止出现：多余手指。\n（可自行补充细节）")
    healed, fixes = heal_h3_prompt(bad, shot, max_pics=2)
    assert "<d>Chinese</d>" in healed
    assert "<Picture 9>" not in healed
    assert healed.count("禁止出现：多余手指。") == 1
    assert "可自行补充" not in healed
    assert len(fixes) >= 4
    # 无对白不补 <d>；无问题的文本原样返回
    ok_text = "summary: 空镜。subject_definitions: 无。"
    h2, f2 = heal_h3_prompt(ok_text, {"ledger_json": "{}"}, max_pics=2)
    assert h2 == ok_text and f2 == []


def test_generate_prompt_uses_healed_version(tmp_path):
    """自愈成功 → 不再消耗 LLM 重试次数（raw_chat 只调一次）。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="对话",
        shot_type="", camera={}, duration=5.0, workflow_type="ref2va",
        ledger={"dialogue": [{"speaker": "林晨", "line": "你好"}]},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    calls = []

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            calls.append(1)
            return ("林晨在庭院对话，中景固定镜头。<Picture 9> 的主体清晰。"
                    "林晨说：你好。（可自行补充细节）"), Usage(1, 1)

    out = generate_video_prompt(db, sid, FakeLLM(), backend="h3", mode="A")
    assert len(calls) == 1  # 自愈生效，没有第二次 LLM 调用
    assert "<d>Chinese</d>" in out and "<Picture 9>" not in out
    assert "可自行补充" not in out
    """自愈成功 → 不再消耗 LLM 重试次数。"""


def test_generate_retry_with_thinking_squeeze_on_truncation(tmp_path):
    """思考模型 × 16k ctx（真机 2026-08-28 job 682：同输入两截断一成功）：
    raw_chat 抛长度截断 → 追加「压缩思考直接输出」反馈再试，不盲重试。"""
    from comic_studio.engine.llm.provider import LLMError
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="推门",
        shot_type="", camera={}, duration=5.0, workflow_type="ref2va",
        ledger={}, character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    seen_feedback = []
    replies = iter([None, "林晨推开木门，晨光，推进镜头，写实。"])

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            r = next(replies)
            if r is None:
                raise LLMError("输出被长度上限截断（finish_reason=length）：请减小单次输入")
            if any("压缩思考" in m.get("content", "") or "直接输出" in m.get("content", "")
                   for m in messages if m.get("role") == "user"):
                seen_feedback.append(1)
            return r, Usage(1, 1)

    out = generate_video_prompt(db, sid, FakeLLM(), backend="h3", mode="A")
    assert "木门" in out and seen_feedback, "第二次调用应带压缩思考反馈"
