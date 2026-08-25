# tests/test_era.py
"""时代背景检测与注入（2026-08-25 需求：明确朝代 → 资产提示词自动加时代限制）。"""
from comic_studio.engine.era import ERA_SUFFIX, detect_era
from comic_studio.engine.genref import build_gen_prompt
from comic_studio.engine.prompts.gen import build_shot_context


def test_detect_common_dynasties():
    assert detect_era("话说大唐贞观年间，长安城内") == "中国唐代"
    assert detect_era("明朝永乐年间，燕王扫北") == "中国明代"
    assert detect_era("他穿越到了北宋的汴京") == "中国宋代"
    assert detect_era("民国二十三年的上海滩") == "中华民国时期"
    assert detect_era("大秦帝国，赳赳老秦") == "中国秦代"


def test_detect_no_false_positive_on_common_words():
    """裸朝代字不算（唐三/汉子/清明/元宝）——须带朝/代/大/南北东西等限定。"""
    assert detect_era("唐三藏带着老汉走过清明节的街道，捡了个元宝") == ""
    assert detect_era("现代都市白领的日常") == ""


def test_detect_most_frequent_wins():
    text = "唐朝旧事……大唐子民……宋代话说回来又是唐朝"  # 唐 3 次 vs 宋 1 次
    assert detect_era(text) == "中国唐代"


def test_gen_prompt_includes_era():
    asset = {"kind": "prop", "name": "肚兜", "source_project": 1, "id": 9,
             "appearance_json": '{"detail": "红绸缎面绣花"}'}
    prompt, _ = build_gen_prompt(asset, style="国风", era="中国唐代")
    assert "时代风格：中国唐代" in prompt
    prompt2, _ = build_gen_prompt(asset, style="国风", era="")
    assert "时代风格" not in prompt2


def test_shot_context_includes_era():
    shot = {"seq": 1, "shot_type": "常规", "duration": 5.0, "workflow_type": "ref2va",
            "description": "院子里对峙", "ledger_json": "{}", "id": 1,
            "camera_json": '{"景别":"中景"}'}
    proj = {"aspect_ratio": "16:9", "style": "", "era": "中国唐代"}
    ctx = build_shot_context(shot, {}, proj)
    assert "时代风格：中国唐代" in ctx and "禁止现代元素" in ctx
    ctx2 = build_shot_context(shot, {}, {"aspect_ratio": "16:9", "style": "", "era": ""})
    assert "未明确" in ctx2 and "禁止现代元素" not in ctx2  # 空时代=兜底行而非限制


def test_era_suffix_shape():
    assert "形制" in ERA_SUFFIX and "禁止现代元素" in ERA_SUFFIX
