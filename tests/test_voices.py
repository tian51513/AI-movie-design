# tests/test_voices.py
"""角色音色预设表（2026-08-30 用户提供英文映射文档）。"""
import pytest

from comic_studio.engine.voices import (
    VOICE_PRESETS, assert_no_banned_words, voice_instruct, voice_timbre)


def test_fifteen_presets_with_gender_and_timbre():
    assert len(VOICE_PRESETS) == 15
    names = [p["name"] for p in VOICE_PRESETS]
    for want in ("萝莉", "高冷御姐", "正太", "大叔", "软萌甜妹", "深沉男声",
                 "浪漫女声", "文艺女生", "温柔少女", "播音男声", "播音女声",
                 "温柔淑女", "元气少女", "老年男声", "老年女声"):
        assert want in names, want
    assert {p["gender"] for p in VOICE_PRESETS} == {"male", "female"}
    assert all(3 <= len(p["timbre"].split(",")[0].split()) + len(
        p["timbre"].split(",")[1].split()) <= 12 for p in VOICE_PRESETS)


def test_voice_instruct_full_sentence_with_suffix():
    s = voice_instruct("高冷御姐")
    assert s.startswith("Voice: sultry seductive mature woman voice")  # D2 欲感版定稿
    assert "slow sensual speaking pace" in s  # 御姐专属慢速
    assert s.endswith("clear pronunciation, high fidelity audio, no background noise")
    assert voice_timbre("大叔") == "deep middle-aged man voice, rich gruff mature male voice"
    with pytest.raises(ValueError, match="未知音色"):
        voice_instruct("赛博朋克音")


def test_banned_acg_words_rejected():
    for bad in ("cute loli voice", "shota boy", "tsundere girl"):
        with pytest.raises(ValueError, match="禁词"):
            assert_no_banned_words(bad)
    assert_no_banned_words(voice_instruct("元气少女"))  # 预设本身无禁词


def test_match_voice_two_level():
    """两级匹配：合法建议优先；非法/缺失按性别×年龄落 8 档基线；无性别为空。"""
    from comic_studio.engine.voices import default_voice_for, match_voice
    assert match_voice("性别：女 年龄：8岁", "高冷御姐") == "高冷御姐"      # 建议优先
    assert match_voice("性别：女 年龄：8岁", "乱写的") == "萝莉"           # 兜底女童
    assert match_voice("性别：男 年龄：60岁", "") == "老年男声"
    assert match_voice("性别：女 年龄：35岁", "") == "温柔少女"            # 青年女
    assert match_voice("性别：女 年龄：45岁", "") == "温柔淑女"            # 中年女
    assert match_voice("性别：男 年龄：14岁", "") == "深沉男声"            # 青年男
    assert match_voice("性别：男 年龄：45岁", "") == "大叔"
    assert match_voice("无结构化外貌", "") == ""                            # 无性别不绑
    assert default_voice_for("女", 70) == "老年女声"
    assert default_voice_for("male", 10) == "正太"
