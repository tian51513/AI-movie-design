# tests/test_duration_control.py
"""时长控制（2026-08-26 需求）：拆分镜统一项目段时长；段时长/预设总时长项目级参数。"""
from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, get_project
from comic_studio.engine.shots import list_shots, persist_shots
from comic_studio.web.app import create_app
from types import SimpleNamespace as NS

CHUNK = """{{"shots":[{{
 "text_span":"推门","description":"{desc}","shot_type":"动作",
 "camera":{{"景别":"全景","机位":"平视","运镜":"固定","转场":"切"}},
 "duration":{dur},"workflow_type":"ref2va",
 "must_appear":[],"must_keep":[],"may_change":[],"must_avoid":[],
 "character_ids":[],"scene_ids":[],"prop_ids":[],"continue_prev":false}}]}}"""


def _proj(tmp_path, **kw):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "时长剧", "16:9", "正文", **kw)["id"]
    return db, pid


def _shot(sid_desc="a", dur=5):
    return NS(text_span="", description=sid_desc, shot_type="", camera={},
              duration=dur, workflow_type="t2v", ledger={},
              character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)


def test_split_uses_uniform_project_duration(tmp_path):
    """LLM 自选 4/6 也统一为项目段时长（默认 5）。"""
    from comic_studio.engine.llm.storyboard import split_storyboards
    from tests.test_storyboard_split import FakeLLM
    db, pid = _proj(tmp_path)
    fake = FakeLLM([CHUNK.format(desc="甲", dur=4)])  # LLM 给 4s 也统一为项目 5s
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    durs = [s["duration"] for s in list_shots(db, pid)]
    assert durs == [5.0]


def test_patch_segment_duration_applies_uniform(tmp_path):
    db, pid = _proj(tmp_path)
    persist_shots(db, pid, [_shot("a", 4), _shot("b", 6)])
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.patch(f"/api/projects/{pid}",
                    json={"default_shot_duration": 8})
    assert r.status_code == 200
    assert [s["duration"] for s in list_shots(db, pid)] == [8.0, 8.0]


def test_patch_target_duration_redistributes(tmp_path):
    """预设总时长按镜数均摊（下限 4s），并同步段时长。"""
    db, pid = _proj(tmp_path)
    persist_shots(db, pid, [_shot(f"s{i}", 5) for i in range(6)])
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.patch(f"/api/projects/{pid}", json={"target_duration": 60})
        assert r.status_code == 200
        assert [s["duration"] for s in list_shots(db, pid)] == [10.0] * 6
        assert r.json()["default_shot_duration"] == 10
        # 总时长过小 → 均摊钳到 4s 下限
        c.patch(f"/api/projects/{pid}", json={"target_duration": 12})
        assert [s["duration"] for s in list_shots(db, pid)] == [4.0] * 6
