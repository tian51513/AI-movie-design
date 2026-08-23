# tests/test_storyboard_schema.py
import pytest
from pydantic import ValidationError

from comic_studio.engine.llm.storyboard import (
    ChunkStoryboard, ShotDraft, SPLIT_SYSTEM, build_split_user_prompt)


GOOD_SHOT = {
    "text_span": "林晨推开门", "description": "少年推开木门，庭院全景，晨光",
    "shot_type": "动作", "camera": {"景别": "全景", "机位": "平视", "运镜": "固定", "转场": "切"},
    "duration": 4.0, "workflow_type": "ref2va",
    "must_appear": ["林晨"], "must_keep": [], "may_change": [], "must_avoid": [],
    "character_ids": [1], "scene_ids": [], "prop_ids": [], "continue_prev": False,
}


def test_schema_parses_and_rejects():
    sb = ChunkStoryboard.model_validate({"shots": [GOOD_SHOT]})
    assert sb.shots[0].camera["景别"] == "全景"
    with pytest.raises(ValidationError):
        ChunkStoryboard.model_validate({"shots": []})          # 空序列拒绝
    with pytest.raises(ValidationError):
        ChunkStoryboard.model_validate({"shots": [{**GOOD_SHOT, "duration": "五秒"}]})


def test_system_prompt_pins_contract():
    for token in ("workflow_type", "continue_prev", "must_appear", "character_ids", "fl2v"):
        assert token in SPLIT_SYSTEM


def test_user_prompt_roster():
    from types import SimpleNamespace as NS
    rows = [NS(kind="character", id=1, name="林晨",
               appearance_json='{"detail": "黑发少年"}'),
            NS(kind="scene", id=2, name="庭院", appearance_json='{"detail": "古宅"}')]
    u = build_split_user_prompt("正文文本", rows)
    assert "id=1 林晨（黑发少年）" in u and "id=2 庭院（古宅）" in u and "正文文本" in u
