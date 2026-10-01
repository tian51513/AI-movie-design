# tests/test_musicstyles.py
"""音乐风格库（2026-10-01 官方 Music3 skill vendor）：templates/music_styles/
的 18 族索引 + 1000 卡扫描器——音乐库风格选择器的数据面。"""
import pytest


def test_list_families():
    from comic_studio.engine import musicstyles as MS
    fams = MS.list_families()
    assert len(fams) == 18
    by_id = {f["id"]: f for f in fams}
    assert by_id["cinematic-pop-ballad"]["name"] == "Cinematic Pop & Ballad"
    assert by_id["cinematic-pop-ballad"]["count"] == 54


def test_list_cards_and_get_card():
    from comic_studio.engine import musicstyles as MS
    cards = MS.list_cards("cinematic-pop-ballad")
    assert len(cards) == 54
    c = next(x for x in cards if x["id"] == "cinematic-ballad-ambient-pop_0001")
    assert c["style"] == "Cinematic Ballad / Ambient Pop"
    assert "70 BPM" in c["tempo"] and "Bb" in c["tempo"]
    assert c["file"] == "cinematic-ballad-ambient-pop_0001.txt"
    txt = MS.get_card("cinematic-pop-ballad", c["file"])
    assert txt.startswith("Global Metadata") and "Vocal Details" in txt


def test_path_safety_and_bad_family():
    from comic_studio.engine import musicstyles as MS
    with pytest.raises(ValueError):
        MS.get_card("cinematic-pop-ballad", "../../engine/db.py")
    with pytest.raises(ValueError):
        MS.list_cards("no-such-family")
    with pytest.raises(ValueError):
        MS.get_card("no-such-family", "x.txt")


def test_chinese_layer():
    """中文化（2026-10-01 用户需求）：18 族名静态中文；卡风格名词表机械合成
    （'/'→'·'、短语词典优先）；未收录词保留英文不硬翻。"""
    from comic_studio.engine import musicstyles as MS
    fams = {f["id"]: f for f in MS.list_families()}
    assert len([f for f in fams.values() if f.get("name_zh")]) == 18
    assert fams["cinematic-orchestral-epic"]["name_zh"] == "电影感管弦史诗"
    cards = {c["id"]: c for c in MS.list_cards("cinematic-pop-ballad")}
    assert cards["cinematic-ballad-ambient-pop_0001"]["style_zh"] \
        == "电影感抒情曲 · 氛围流行"
    # 短语词典优先（Hip Hop→嘻哈 不成「嘻哈哈」）
    assert "嘻哈" in MS.zh_style("East Asian Hip Hop") \
        and "哈" * 3 not in MS.zh_style("East Asian Hip Hop")
    # 未收录词保留英文原词
    assert "Zorblax" in MS.zh_style("Zorblax Pop")
