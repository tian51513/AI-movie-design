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
