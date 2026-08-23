import pytest
from pydantic import ValidationError

from comic_studio.engine.llm.schemas import AssetsAnalysis


GOOD = {
    "characters": [{"name": "萧炎", "role": "主角",
                    "appearance": "黑发黑瞳少年，穿青色布衣", "tags": ["主角"]}],
    "scenes": [{"name": "乌坦城集市", "description": "喧嚣的东方古代集市", "tags": []}],
    "props": [{"name": "玄重尺", "description": "黑色巨型重剑", "tags": ["武器"]}],
}


def test_good_payload_parses():
    a = AssetsAnalysis.model_validate(GOOD)
    assert a.characters[0].appearance.startswith("黑发")


def test_missing_sections_rejected():
    with pytest.raises(ValidationError):
        AssetsAnalysis.model_validate({"characters": GOOD["characters"]})


def test_character_without_appearance_rejected():
    bad = {"characters": [{"name": "x"}], "scenes": [], "props": []}
    with pytest.raises(ValidationError):
        AssetsAnalysis.model_validate(bad)
