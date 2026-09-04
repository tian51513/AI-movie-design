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


def test_backfill_alternates_speaker_and_reestimates_duration():
    """2026-09-03 武侠风云真机：①女主无提示回怼被就近规则派给前一句的男主
    ——中文对白交替惯例：相邻引号间无新说话人提示时轮换；②backfill 补录后
    时长没人回头重估（LLM 未见过对白，一律写 5）——补录后机械按文档公式
    句数×2.5 钳 4~15 重算。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.llm.storyboard import backfill_dialogue, reestimate_durations
    span = ('楚惊云冷笑着说道：“你知道为什么我会将主意打到她身上吗？”'
            '沈雪柔气得脸色发白。“无耻！你休得血口喷人！”')
    s = NS(text_span=span, ledger={}, duration=5.0)
    n = backfill_dialogue([s], ["楚惊云", "沈雪柔"])
    assert n == 2
    d = s.ledger["dialogue"]
    assert d[0]["speaker"] == "楚惊云"
    assert d[1]["speaker"] == "沈雪柔", "无新提示的相邻回怼应按交替惯例换人"
    # 时长机械重估（只管补录镜）：2 句 29 字 → ⌈29/4⌉+0.6 = 8.6（2026-09-05 B1：
    # 句数×2.5 无视句长，长句对白被截半——改字数/4+句间停顿 0.6s）
    reestimate_durations([s]); assert s.duration == 8.6
    s1 = NS(text_span="", ledger={"dialogue": [{"speaker": "x", "line": "一"}]},
            duration=5.0, dialogue_backfilled=True)
    s8 = NS(text_span="", ledger={"dialogue": [{"speaker": "x", "line": "字" * 10}
                                               for _ in range(8)]},
            duration=5.0, dialogue_backfilled=True)
    llm_filled = NS(text_span="", ledger={"dialogue": [{"speaker": "x", "line": "y"}]},
                    duration=12.0)  # LLM 自填对白→估时不覆盖
    s0 = NS(text_span="", ledger={"dialogue": []}, duration=6.0)
    reestimate_durations([s1, s8, llm_filled, s0])
    assert s1.duration == 4.0 and s8.duration == 15.0  # 1字钳下限；80字钳上限
    assert llm_filled.duration == 12.0 and s0.duration == 6.0


def test_system_prompt_duration_rules():
    """设计 B2（2026-09-05）：估时字数基准 + 无对白镜动作复杂度 + 打包字数预算。"""
    for kw in ("字数", "48", "打斗", "全景"):
        assert kw in SPLIT_SYSTEM, kw
