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


def test_detect_no_false_positive_on_da_phrase_substrings():
    """「大X」缩写撞进常用词不算——2026-09-20 猫物语判例：「孤陋寡闻不大清楚」
    的子串「大清」曾把 44 万字的日本现代小说判成「中国清代」（唯一命中即胜出，
    时代限制句反向毒害全部图像提示词）。"""
    assert detect_era("我虽然孤陋寡闻不大清楚") == ""
    assert detect_era("他每天大清早就出门跑步") == ""
    assert detect_era("这件事我不大明白") == ""
    assert detect_era("她后来成了大明星") == ""
    assert detect_era("世界由五大元素构成") == ""
    assert detect_era("两大原因导致了失败") == ""
    assert detect_era("他是陆海空大元帅") == ""
    assert detect_era("高大汉子拦住了去路") == ""
    assert detect_era("祖孙五代同堂") == ""
    assert detect_era("移民国家的政策") == ""


def test_detect_genuine_abbreviated_mentions():
    """负向先行只挡常用词续字，真朝代缩写不受影响。"""
    assert detect_era("大清入关，剃发易服") == "中国清代"
    assert detect_era("大明王朝的官制") == "中国明代"
    assert detect_era("我大汉儿郎威武") == "中国汉代"
    assert detect_era("大唐贞观之治") == "中国唐代"
    assert detect_era("五代十国的乱世") == "中国五代十国时期"


def test_era_suffix_modern_not_forbidden():
    """现代/未来时代不得注「禁止现代元素」——2026-09-20 用户手改「日本现代」
    判例：该句对现代题材自相矛盾。改注禁古装与时代错位。"""
    from comic_studio.engine.era import era_suffix
    assert era_suffix("") == ""
    ancient = era_suffix("中国唐代")
    assert "时代风格：中国唐代" in ancient and "禁止现代元素" in ancient
    modern = era_suffix("日本现代")
    assert "时代风格：日本现代" in modern and "禁止现代元素" not in modern
    assert "禁止古装" in modern
    assert "禁止现代元素" not in era_suffix("近未来科幻")


def test_shot_context_modern_era_no_forbid_modern():
    shot = {"seq": 1, "shot_type": "常规", "duration": 5.0, "workflow_type": "ref2va",
            "description": "放学后的街道", "ledger_json": "{}", "id": 1,
            "camera_json": '{"景别":"中景"}'}
    proj = {"aspect_ratio": "16:9", "style": "", "era": "日本现代"}
    ctx = build_shot_context(shot, {}, proj)
    assert "时代风格：日本现代" in ctx and "禁止现代元素" not in ctx


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
