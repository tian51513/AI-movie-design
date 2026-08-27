# tests/test_shot_status.py
"""分镜生效/无效状态（2026-08-27 需求）：无效镜不进门禁计数/渲染/合成，可批量处理与删除。"""
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, get_project, set_stage
from comic_studio.engine.shots import (delete_shots_batch, list_shots,
                                       persist_shots, set_disabled_batch)
from comic_studio.web.app import create_app


def _draft(desc="推门", prompt="p", **kw):
    base = dict(text_span="", description=desc, shot_type="", camera={},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
                prompt=prompt)
    base.update(kw)
    return NS(**base)


def _db3(tmp_path, prompt="p"):
    """3 镜项目，返回 (db, pid, ids)。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "状态剧", "16:9", "t")["id"]
    ids = persist_shots(db, pid, [_draft(prompt=prompt) for _ in range(3)])
    return db, pid, ids


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                  start_workers=False))


def test_disabled_column_defaults_and_batch_ops(tmp_path):
    db, pid, ids = _db3(tmp_path)
    assert all(s["disabled"] == 0 for s in list_shots(db, pid))
    assert set_disabled_batch(db, pid, ids[:2], 1) == 2
    flags = {s["id"]: s["disabled"] for s in list_shots(db, pid)}
    assert flags[ids[0]] == 1 and flags[ids[1]] == 1 and flags[ids[2]] == 0
    assert set_disabled_batch(db, pid, [ids[0]], 0) == 1


def test_delete_batch_clears_dangling_depends_on(tmp_path):
    db, pid, ids = _db3(tmp_path)
    conn = db.connect()
    conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (ids[0], ids[1]))
    conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (ids[1], ids[2]))
    conn.commit()
    assert delete_shots_batch(db, pid, [ids[1]]) == 1
    left = {s["id"]: s for s in list_shots(db, pid)}
    assert set(left) == {ids[0], ids[2]}
    assert left[ids[2]]["depends_on"] is None  # 引用被删镜的链接已清


def test_gate2_gate3_skip_disabled_shots(tmp_path):
    from comic_studio.engine.pipeline_gates import gate_pass
    db, pid, ids = _db3(tmp_path, prompt="")  # 全部无提示词
    set_stage(db, pid, "assets_ready")
    with pytest.raises(ValueError, match="缺提示词"):
        gate_pass(db, tmp_path / "data", pid, 2)
    from comic_studio.engine.shots import update_shot
    update_shot(db, ids[0], {"prompt": "提示词"})
    set_disabled_batch(db, pid, [ids[1], ids[2]], 1)  # 无提示词的两镜标无效
    gate_pass(db, tmp_path / "data", pid, 2)  # 无效镜不阻塞门2
    assert get_project(db, pid)["stage"] == "storyboard_ready"
    # 门3：生效镜有视频、无效镜没有 → 应通过
    update_shot(db, ids[0], {"video_path": "projects/状态剧/shots/1/video_v1.mp4"})
    gate_pass(db, tmp_path / "data", pid, 3)
    assert get_project(db, pid)["stage"] == "rendered"


def test_batch_endpoint_render_skip_and_single_block(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "批处剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]
        db = c.app.state.db
        ids = persist_shots(db, pid, [_draft() for _ in range(3)])
        set_stage(db, pid, "storyboard_ready")
        # 批量无效
        r = c.post(f"/api/projects/{pid}/shots/batch",
                   json={"action": "disable", "ids": [ids[1], ids[2]]})
        assert r.status_code == 200 and r.json()["updated"] == 2
        # 无效镜单渲被拒
        assert c.post(f"/api/shots/{ids[1]}/render").status_code == 422
        # 批量渲染跳过无效镜
        r = c.post(f"/api/projects/{pid}/render").json()
        assert r["enqueued"] == 1 and r["skipped_disabled"] == 2
        # 批量删除
        r = c.post(f"/api/projects/{pid}/shots/batch",
                   json={"action": "delete", "ids": [ids[2]]})
        assert r.json()["deleted"] == 1
        assert len(list_shots(db, pid)) == 2
        # 恢复生效
        r = c.post(f"/api/projects/{pid}/shots/batch",
                   json={"action": "enable", "ids": [ids[1]]})
        assert r.json()["updated"] == 1
        assert {s["disabled"] for s in list_shots(db, pid)} == {0}
        # 非法 action
        assert c.post(f"/api/projects/{pid}/shots/batch",
                      json={"action": "boom", "ids": [ids[0]]}).status_code == 422


def test_split_endpoint_accepts_target_count(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "定数剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]
        set_stage(c.app.state.db, pid, "assets_ready")
        r = c.post(f"/api/projects/{pid}/split-storyboards", json={"target_count": 12})
        assert r.status_code == 202
        from comic_studio.engine import jobs as J
        row = J.latest_job(c.app.state.db, pid, "split_storyboards")
        payload = json.loads(row["payload_json"] or "{}")
        assert payload.get("target_count") == 12
        # 不带 body 也照旧
        c.app.state.db.connect().execute(
            "UPDATE jobs SET status='done' WHERE id=?", (row["id"],))
        c.app.state.db.connect().commit()
        r = c.post(f"/api/projects/{pid}/split-storyboards")
        assert r.status_code == 202


def test_autopilot_counts_ignore_disabled(tmp_path):
    from comic_studio.engine.autopilot import _all_shots_have_video, _shots_missing_prompt
    from comic_studio.engine.shots import update_shot
    db, pid, ids = _db3(tmp_path, prompt="")
    update_shot(db, ids[0], {"prompt": "提示词", "video_path": "x.mp4"})
    set_disabled_batch(db, pid, [ids[1], ids[2]], 1)
    assert _shots_missing_prompt(db, pid) == 0  # 无效镜不算缺提示词
    assert _all_shots_have_video(db, pid) is True  # 无效镜不算缺视频
    set_disabled_batch(db, pid, [ids[1]], 0)
    assert _shots_missing_prompt(db, pid) == 1


def test_merge_guard_skips_disabled(tmp_path, monkeypatch):
    """无效镜缺视频不应阻塞合成；concat 也只吃生效镜。"""
    from comic_studio.engine import merge as M
    from comic_studio.engine.merge import merge_project
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.shots import update_shot
    db, pid, ids = _db3(tmp_path)
    set_stage(db, pid, "rendered")
    video = data_to_abs(tmp_path / "data", f"projects/状态剧/shots/{ids[0]}/video_v1.mp4")
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"fake")
    update_shot(db, ids[0], {"video_path": f"projects/状态剧/shots/{ids[0]}/video_v1.mp4"})
    set_disabled_batch(db, pid, [ids[1], ids[2]], 1)  # 无视频的两镜标无效
    seen = []

    def _fake_normalize(src, dst, w, h, fps):
        seen.append(("norm", Path(src).name))
        Path(dst).write_bytes(b"v")
        return Path(dst)

    def _fake_concat(parts, out):
        seen.append(("concat", len(parts)))
        Path(out).write_bytes(b"v")
        return Path(out)

    monkeypatch.setattr(M, "normalize", _fake_normalize)
    monkeypatch.setattr(M, "concat", _fake_concat)
    merge_project(db, tmp_path / "data", pid)  # 修复前：无效镜缺视频 → raise"无法合成"
    assert seen == [("norm", "video_v1.mp4"), ("concat", 1)]  # 只吃生效镜


def test_replace_and_delete_clear_job_references(tmp_path):
    """FK 修复（2026-08-27 真机 job 653：重拆 DELETE shots 被 jobs.shot_id 外键拦下）：
    替换/删除分镜前先把引用 job 的 shot_id 置 NULL（保留任务行作审计）。"""
    from comic_studio.engine.jobs import enqueue_job
    from comic_studio.engine.shots import persist_shots
    db, pid, ids = _db3(tmp_path)
    enqueue_job(db, "gen_prompt", project_id=pid, shot_id=ids[0], payload={"shot_id": ids[0]})
    enqueue_job(db, "gen_shot", project_id=pid, shot_id=ids[1], resource="gpu_comfy",
                payload={"shot_id": ids[1]})
    # 替换式重拆（DELETE+INSERT）不再抛 IntegrityError
    persist_shots(db, pid, [_draft(prompt="p")])
    row = db.connect().execute(
        "SELECT COUNT(*) c FROM jobs WHERE shot_id IS NOT NULL").fetchone()["c"]
    assert row == 0  # 引用已清
    # 批量删除路径同样安全
    new_ids = [s["id"] for s in list_shots(db, pid)]
    enqueue_job(db, "gen_prompt", project_id=pid, shot_id=new_ids[0], payload={})
    assert delete_shots_batch(db, pid, new_ids) == 1
    assert db.connect().execute(
        "SELECT COUNT(*) c FROM jobs WHERE shot_id IS NOT NULL").fetchone()["c"] == 0
