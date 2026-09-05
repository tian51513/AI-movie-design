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
    """LLM 自选 4/6 也统一为项目段时长（显式 5；默认已改 0=动态）。"""
    from comic_studio.engine.llm.storyboard import split_storyboards
    from tests.test_storyboard_split import FakeLLM
    db, pid = _proj(tmp_path, default_shot_duration=5)
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


def test_create_with_durations(tmp_path):
    """创建项目即可指定段时长/总时长（2026-08-26 需求）。"""
    db, pid = _proj(tmp_path, default_shot_duration=6, target_duration=90)
    row = get_project(db, pid)
    assert row["default_shot_duration"] == 6 and row["target_duration"] == 90


def _split_shot(desc):
    return {"text_span": "推门", "description": desc, "shot_type": "动作",
            "camera": {"景别": "全景", "机位": "平视", "运镜": "固定", "转场": "切"},
            "duration": 5, "workflow_type": "ref2va",
            "must_appear": [], "must_keep": [], "may_change": [], "must_avoid": [],
            "character_ids": [1], "scene_ids": [], "prop_ids": [], "continue_prev": False}


def test_split_applies_target_redistribution(tmp_path):
    """创建时设了总时长 → 拆分镜后自动按镜数均摊（下限4s）。"""
    import json as _json
    from comic_studio.engine.llm.storyboard import split_storyboards
    from tests.test_storyboard_split import FakeLLM
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.shots import list_shots
    db, pid = _proj(tmp_path, target_duration=10)  # 2 镜 → 每镜 5s
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.parent.mkdir(parents=True, exist_ok=True)
    novel.write_text("甲" * 60 + "\n\n" + "乙" * 60, encoding="utf-8")
    two = _json.dumps({"shots": [_split_shot("甲镜"), _split_shot("乙镜")]},
                      ensure_ascii=False)
    fake = FakeLLM([two])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    assert [s["duration"] for s in list_shots(db, pid)] == [5.0, 5.0]


def test_patch_render_mode_batch_updates(tmp_path):
    """视频渲染模式项目级切换（2026-08-26）：改 render_mode → 全部镜 workflow_type 联动。"""
    from comic_studio.engine.shots import persist_shots, list_shots
    db, pid = _proj(tmp_path)
    persist_shots(db, pid, [
        _shot("a"), _shot("b"), _shot("c", 5)])  # 默认 ref2va
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.patch(f"/api/projects/{pid}", json={"render_mode": "t2v"})
        assert r.status_code == 200
        assert {s["workflow_type"] for s in list_shots(db, pid)} == {"t2v"}
        # 再切回 fl2v
        c.patch(f"/api/projects/{pid}", json={"render_mode": "fl2v"})
        assert {s["workflow_type"] for s in list_shots(db, pid)} == {"fl2v"}
        # 非法值 422
        assert c.patch(f"/api/projects/{pid}",
                       json={"render_mode": "xxx"}).status_code == 422


# ── 段时长 0 = LLM 动态估时（2026-09-05 需求）──

def test_split_dynamic_duration_when_segment_zero(tmp_path):
    """段时长=0 → 无对白镜用 LLM 估时（钳 4~15），不再统一段时长。"""
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.paths import data_to_abs
    from tests.test_storyboard_split import FakeLLM
    db, pid = _proj(tmp_path, default_shot_duration=0)
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.parent.mkdir(parents=True, exist_ok=True)
    novel.write_text("甲" * 60, encoding="utf-8")
    fake = FakeLLM([CHUNK.format(desc="甲", dur=9)])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    assert [s["duration"] for s in list_shots(db, pid)] == [9.0]
    # 越界照钳（schema 宽进 1~30，staging 钳 15）
    fake2 = FakeLLM([CHUNK.format(desc="乙", dur=30)])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake2)
    durs = [s["duration"] for s in list_shots(db, pid)]
    assert durs[-1] == 15.0


def test_patch_segment_zero_updates_field_only(tmp_path):
    """PATCH 段时长 0 = 只改字段（拆解时动态），不把存量镜时长清零。"""
    db, pid = _proj(tmp_path)
    persist_shots(db, pid, [_shot("a", 6)])
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.patch(f"/api/projects/{pid}", json={"default_shot_duration": 0})
    assert r.status_code == 200 and r.json()["default_shot_duration"] == 0
    assert [s["duration"] for s in list_shots(db, pid)] == [6.0]


def test_update_rejects_negative_segment(tmp_path):
    import pytest
    from comic_studio.engine.projects import update_video_params
    db, pid = _proj(tmp_path)
    with pytest.raises(ValueError):
        update_video_params(db, pid, default_shot_duration=-1)


def test_split_prompt_carries_duration_hint():
    """段时长基准进拆解上下文（此前规则 8 说「上下文给出」实际没给）。"""
    from comic_studio.engine.llm.storyboard import build_split_user_prompt
    p0 = build_split_user_prompt("正文", [], None, dur_hint=0)
    assert "无统一段时长" in p0
    p5 = build_split_user_prompt("正文", [], None, dur_hint=5)
    assert "统一段时长 5" in p5


def test_create_defaults_to_dynamic_durations(tmp_path):
    """2026-09-05 用户需求：创建项目默认 段时长=0/总时长=0（系统自动控制），
    人为可调。API 上传流与 from-theme 直建两路都默认 0。"""
    import io
    with TestClient(create_app(tmp_path / "t.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects", data={"name": "默认剧", "aspect_ratio": "16:9"},
                   files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")})
        assert r.status_code == 201
        assert r.json()["default_shot_duration"] == 0
        assert r.json()["target_duration"] == 0
        from comic_studio.engine.db import Database
        from comic_studio.engine.themes import list_themes
        tid = list_themes(Database(tmp_path / "t.db"))[0]["id"]
        r2 = c.post("/api/projects/from-theme", json={
            "theme_id": tid, "text": "字" * 120,
            "aspect_ratio": "16:9", "extra_prompt": ""})
        assert r2.status_code == 201
        assert r2.json()["default_shot_duration"] == 0
        assert r2.json()["target_duration"] == 0


def test_from_comic_accepts_durations(tmp_path):
    """M16（2026-09-05 审计）：漫画 tab 的段时长/总时长输入此前前端不发、
    后端不收——纯摆设。两路透传到项目字段。"""
    import io
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    with TestClient(create_app(tmp_path / "t.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-comic",
                   data={"name": "漫画参数剧", "aspect_ratio": "16:9",
                         "comic_mode": "motion_comic",
                         "default_shot_duration": 7, "target_duration": 0},
                   files={"images": ("p1.png", io.BytesIO(png), "image/png")})
        assert r.status_code == 201, r.text
        assert r.json()["default_shot_duration"] == 7
