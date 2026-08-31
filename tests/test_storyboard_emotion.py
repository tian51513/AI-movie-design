# tests/test_storyboard_emotion.py
"""A 级台词组拆镜（2026-09-01，借鉴「台词驱动无缝分镜」工程文档）：
连续同场景对白合并镜头组 + emotion/gesture/gaze/continuity 结构化字段。"""
import json
from types import SimpleNamespace as NS

from comic_studio.engine.assets import persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import Usage
from comic_studio.engine.llm.storyboard import SPLIT_SYSTEM, split_storyboards
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import list_shots

CHUNK = json.dumps({"shots": [{
    "text_span": "对话", "description": "林晨与白发少女对话", "shot_type": "对话",
    "camera": {"景别": "中景", "机位": "平视", "运镜": "固定", "转场": "无"},
    "duration": 12, "workflow_type": "fl2v",
    "must_appear": ["林晨"], "must_keep": [], "may_change": [], "must_avoid": [],
    "character_ids": [1], "scene_ids": [], "prop_ids": [], "continue_prev": False,
    "dialogue": [{"speaker": "林晨", "line": "你来了。"},
                 {"speaker": "白发少女", "line": "嗯。"}],
    "emotion": "平静", "gesture": "slightly raising one hand",
    "gaze": "looking at the girl", "continuity": "微变延续",
}]}, ensure_ascii=False)


class FakeLLM:
    model = "fake"
    def __init__(self, replies): self.replies = list(replies); self.n = 0
    def raw_chat(self, messages, temperature=0.3, max_tokens=None):
        r = self.replies[min(self.n, len(self.replies) - 1)]; self.n += 1
        return r, Usage(1, 2)


def _setup(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "林晨推开门。庭院里站着一个白发少女。")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    return db, pid


def test_split_persists_emotion_fields(tmp_path):
    db, pid = _setup(tmp_path)
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: FakeLLM([CHUNK]))
    row = list_shots(db, pid)[0]
    assert row["emotion"] == "平静"
    assert row["gesture"] == "slightly raising one hand"
    assert row["gaze"] == "looking at the girl"
    assert row["continuity"] == "微变延续"


def test_split_sanitizes_invalid_enum_and_clamps_duration(tmp_path):
    db, pid = _setup(tmp_path)
    bad = json.loads(CHUNK)
    bad["shots"][0].update(emotion="超然物外", continuity="随便", duration=25)
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: FakeLLM([json.dumps(bad, ensure_ascii=False)]))
    row = list_shots(db, pid)[0]
    assert row["emotion"] == "" and row["continuity"] == ""
    assert row["duration"] == 15.0  # 超上限机械钳到 15


def test_system_prompt_has_dialogue_grouping_contract():
    # 台词组打包规则 + 枚举字段契约进系统词（LLM 行为锚点）
    assert "3~8" in SPLIT_SYSTEM or "3-8" in SPLIT_SYSTEM
    for kw in ("平静", "嘶吼", "emotion", "gesture", "gaze", "continuity",
               "微变延续", "场景断点"):
        assert kw in SPLIT_SYSTEM, kw
