# tests/test_storyboard_continuity.py
"""B 级连贯分发（2026-09-01）：continuity 继承类机械校准 workflow_type→fl2v；
同一延续组视频 seed 继承（组首随机，组内 +3，场景断点重开）——防画风漂移。"""
import json
from types import SimpleNamespace as NS

from comic_studio.engine.assets import persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import Usage
from comic_studio.engine.llm.storyboard import split_storyboards
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import list_shots


def _shot(seq_guard, desc, *, continuity, wf="ref2va", dialogue=None):
    s = {
        "text_span": "段", "description": desc, "shot_type": "对话",
        "camera": {}, "duration": 8, "workflow_type": wf,
        "must_appear": [], "must_keep": [], "may_change": [], "must_avoid": [],
        "character_ids": [1], "scene_ids": [], "prop_ids": [],
        "continue_prev": False, "dialogue": dialogue or [],
        "emotion": "平静", "gesture": "calm stillness", "gaze": "forward",
        "continuity": continuity}
    return s


class FakeLLM:
    model = "fake"
    def __init__(self, replies): self.replies = list(replies); self.n = 0
    def raw_chat(self, messages, temperature=0.3, max_tokens=None):
        r = self.replies[min(self.n, len(self.replies) - 1)]; self.n += 1
        return r, Usage(1, 2)


def _setup(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "长文本")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    return db, pid


def test_continuity_inherits_workflow_and_group_seed(tmp_path):
    db, pid = _setup(tmp_path)
    reply = json.dumps({"shots": [
        # 镜1：场景断点（新组首）ref2va
        _shot(1, "新场景开场", continuity="场景断点", wf="ref2va"),
        # 镜2：继承上镜，LLM 却给了 ref2va → 机械校准 fl2v；同组 seed=组首+3
        _shot(2, "延续对话", continuity="全程继承", wf="ref2va",
              dialogue=[{"speaker": "林晨", "line": "好。"}]),
        # 镜3：微变延续 → fl2v + 组内 seed 再 +3
        _shot(3, "微变", continuity="微变延续", wf="fl2v",
              dialogue=[{"speaker": "林晨", "line": "嗯。"}]),
        # 镜4：断点 → 新组首（新随机 seed），workflow 尊重原值
        _shot(4, "切到次日", continuity="场景断点", wf="ref2va"),
    ]}, ensure_ascii=False)
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: FakeLLM([reply]))
    rows = list_shots(db, pid)
    assert [r["workflow_type"] for r in rows] == ["ref2va", "fl2v", "fl2v", "ref2va"]
    s = [r["seed"] for r in rows]
    assert all(x is not None and x > 0 for x in s), s
    assert s[1] == s[0] + 3 and s[2] == s[0] + 6      # 延续组继承 +3/镜
    assert s[3] != s[0] and s[3] not in (s[1], s[2])  # 断点重开新组


def test_t2v_never_overridden(tmp_path):
    db, pid = _setup(tmp_path)
    reply = json.dumps({"shots": [
        _shot(1, "新场景", continuity="场景断点", wf="t2v"),
        _shot(2, "延续", continuity="全程继承", wf="t2v"),  # LLM 判断 t2v 尊重
    ]}, ensure_ascii=False)
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: FakeLLM([reply]))
    rows = list_shots(db, pid)
    assert [r["workflow_type"] for r in rows] == ["t2v", "t2v"]


def test_rendershot_uses_stored_seed(tmp_path):
    """rendershot 视频渲染优先用库里组 seed（无则随机兜底）。"""
    from comic_studio.engine.rendershot import _video_seed
    assert _video_seed({"seed": 12345}) == 12345
    s = _video_seed({})
    assert isinstance(s, int) and 0 <= s < 2 ** 31
