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
