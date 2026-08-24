# tests/test_api_merge.py
"""一键出片后端接口（计划5B 任务4）：autopilot 开关/详情动作、merge 发起与列表。"""
from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, set_stage
from comic_studio.web.app import create_app


def _client(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "一键剧", "16:9", "正文")["id"]
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    return db, pid, TestClient(app)


def test_autopilot_patch_and_detail_action(tmp_path):
    db, pid, c = _client(tmp_path)
    with c:
        r = c.patch(f"/api/projects/{pid}", json={"autopilot": True})
        assert r.status_code == 200 and r.json()["autopilot"] == 1
        d = c.get(f"/api/projects/{pid}").json()
        assert d["autopilot"] == 1
        assert d["autopilot_action"]["action"] == "analyze"  # 当前动作角标
        r = c.patch(f"/api/projects/{pid}", json={"autopilot": False})
        assert r.json()["autopilot"] == 0
        d = c.get(f"/api/projects/{pid}").json()
        assert "autopilot_action" not in d  # 关闭时不计算


def test_merge_guard_dedupe_and_listing(tmp_path):
    db, pid, c = _client(tmp_path)
    with c:
        # 非 rendered 阶段 → 409
        assert c.post(f"/api/projects/{pid}/merge").status_code == 409
        set_stage(db, pid, "rendered")
        r = c.post(f"/api/projects/{pid}/merge")
        assert r.status_code == 202 and "job_id" in r.json()
        # 队列去重 → 409
        assert c.post(f"/api/projects/{pid}/merge").status_code == 409
        # 产物列表：output 目录扫描
        assert c.get(f"/api/projects/{pid}/merges").json() == []
        out_dir = tmp_path / "data" / "projects" / "一键剧" / "output"
        out_dir.mkdir(parents=True)
        (out_dir / "ep001.mp4").write_bytes(b"x")
        merges = c.get(f"/api/projects/{pid}/merges").json()
        assert len(merges) == 1 and merges[0]["file"] == "ep001.mp4"
        assert merges[0]["url"].startswith("/media/")
