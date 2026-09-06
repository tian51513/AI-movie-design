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
    assert set(PROMPT_MODES) == {"A", "B", "C", "D", "E"}
    d = PROMPT_MODES["D"]["spec"]
    assert "[Shot" in d and "<Subject" in d and "<d>Chinese</d>" in d
    b = PROMPT_MODES["B"]["spec"]
    assert "高密度" in b or "足够详细" in b
    c = PROMPT_MODES["C"]["spec"]
    assert "站位" in c and "间距" in c
    for spec in PROMPT_MODES.values():
        assert "服装" in spec["spec"]
    with pytest.raises(ValueError):
        mode_spec("F")


def test_generate_uses_project_mode(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "模式剧", "16:9", "t", prompt_mode="A")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    captured = []

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured.append(messages[0]["content"])
            return "林晨推开木门，晨光，推进镜头，写实。", Usage(1, 1)

    generate_video_prompt(db, sid, FakeLLM(), backend="h3")
    assert PROMPT_MODES["A"]["spec"][:30] in captured[-1]
    # 显式 mode 覆盖项目设置：首次 D 调用用 D 规程；结构失败后的重试换
    # 紧凑骨架系统词（2026-09-02 真机 job 38401：9B × 全量规程三次全缺段）
    with pytest.raises(RuntimeError):
        generate_video_prompt(db, sid, FakeLLM(), backend="h3", mode="D")
    assert PROMPT_MODES["D"]["spec"][:30] in captured[1]   # D 首调=全量 D 规程
    assert "subject_definitions:" in captured[-1]          # 重试=紧凑骨架


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


def test_audio_protocol_in_all_modes():
    """音频协议（2026-08-30 用户实测格式）：四模式统一带 soundscape 白名单、
    non_diegetic_music 默认 N/A（按分镜可写配乐）、同步声音标注、台词口型。"""
    from comic_studio.engine.prompts.modes import PROMPT_MODES
    for key, m in PROMPT_MODES.items():
        if key == "E":
            continue  # E 英文控制式走 Preserve/Add 句式，见独立断言
        spec = m["spec"]
        assert "overall_soundscape" in spec, key
        assert "non_diegetic_music" in spec and "N/A" in spec, key
        assert "无关声源" in spec, key          # 广播/嘈杂人群等禁令
        assert "同步声音" in spec, key           # 人物非语言声标注
        assert "口型" in spec, key               # 台词口型同步
        assert "无对白" in spec, key             # 无台词镜明示


def test_mode_e_english_control_spec():
    """模式 E（2026-08-30 用户实测英文控制式，杂音主因修复）：固定句式齐全。"""
    from comic_studio.engine.prompts.modes import PROMPT_MODES, mode_spec
    assert "E" in PROMPT_MODES
    spec = mode_spec("E")
    for phrase in ("EXACT starting key frame", "global character design reference",
                   "says in natural Mandarin", "<d>[Mandarin Chinese]",
                   "No subtitles, logos, watermarks", "Preserve"):
        assert phrase in spec, phrase
    # E 不用分节标题/音频字段（区别于 B/C/D 骨架）
    assert "subject_definitions:" not in spec


def test_new_project_defaults_to_mode_d(tmp_path):
    """2026-08-31 实测回调：语言与杂音无关（有对白即抑制），默认回 D 中文。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    row = create_project(db, tmp_path / "d", "D默认剧", "9:16", "t")
    assert row["prompt_mode"] == "D"


def test_e_mode_audio_line_pins_no_music():
    """2026-09-05 提案落地：E 音频行为句尾固定 No background music——与结尾
    负面清单呼应，更早锚定抑制配乐。"""
    spec = PROMPT_MODES["E"]["spec"]
    assert "No background music." in spec


def test_heal_strips_ad_fields_from_e_prompt():
    """heal ⑨（2026-09-05 提案落地）：E 模式混入 A-D 字段段整段删除（小模型
    混合结构时保 E 五段纯度，不转换避免半结构产物）；A-D 模式字段不动。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.prompts.gen import heal_h3_prompt
    mixed = ("subject_definitions:\n<Subject 1> 是来自 <Picture 1> 的人物，外观由该图提供\n\n"
             "<Picture 1> is the global character design reference.\n"
             "[Shot 1] 5-second continuous cinematic shot. 雨夜庭院，林晚缓缓抬头。\n"
             "overall_soundscape: 雨声，音量轻微\n\n"
             "No dialogue, no humming, no speech.\n"
             "Preserve rain sound. Add fabric movement.\n"
             "No subtitles, logos, watermarks, or text.")
    shot = NS(ledger_json="{}")
    out, fixes = heal_h3_prompt(mixed, shot, mode="E")
    assert "subject_definitions" not in out and "overall_soundscape" not in out
    assert "global character design reference" in out   # E 内容保留
    assert "[Shot 1]" in out
    assert any("清除混入字段" in f for f in fixes)
    out_d, _ = heal_h3_prompt(mixed, shot, mode="D")
    assert "subject_definitions" in out_d               # A-D 模式不动


def test_heal_weaves_dialogue_into_prompt():
    """2026-09-06 有声2 真机：上下文带台词+系统词有格式，本地模型仍不写
    <d> 对白段；heal 旧兜底只补空壳 <d>Chinese</d>（无用）。机械逐句织入。"""
    import json as _json
    from comic_studio.engine.prompts.gen import heal_h3_prompt
    shot = {"ledger_json": _json.dumps({
        "dialogue": [{"speaker": "少芬妈妈", "line": "来，把这碗汤喝了。"},
                      {"speaker": "女婿", "line": "好的。"}]}, ensure_ascii=False)}
    text = ("subject_definitions:\n少芬妈妈 是来自 <Picture 1> 的人物\n"
            "summary:一句话：本镜核心\n"
            "overall_soundscape:无对白、无哼唱\nnon_diegetic_music: N/A")
    out, fixes = heal_h3_prompt(text, shot, max_pics=2)
    assert "<d>[Mandarin Chinese]来，把这碗汤喝了。</d>" in out
    assert "<d>[Mandarin Chinese]好的。</d>" in out
    assert any("织入" in f for f in fixes)
    # 已含 <d> 的提示词不重复织
    out2, _ = heal_h3_prompt(
        "已有 <d>[Mandarin Chinese]台词</d> 的提示词 overall_soundscape:无",
        shot, max_pics=2)
    assert out2.count("<d>") == 1
