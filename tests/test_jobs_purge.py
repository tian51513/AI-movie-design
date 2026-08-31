# tests/test_jobs_purge.py
"""jobs 历史清理（2026-09-01 用户决策）：done/failed/cancelled 超 N 天删除——
3.6 万失败行把失败计数撑到 35246 且拖库；pending/running/近期记录保留。"""
from fastapi.testclient import TestClient

from comic_studio.engine import jobs
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.web.app import create_app


def _client(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "清史剧", "16:9", "t")["id"]
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    return db, pid, TestClient(app)


def _job(db, pid, status, finished_at):
    jid = jobs.enqueue_job(db, "gen_shot", project_id=pid)
    conn = db.connect()
    conn.execute("UPDATE jobs SET status=?, finished_at=? WHERE id=?",
                 (status, finished_at, jid))
    conn.commit()
    return jid


def test_purge_removes_old_finished_keeps_rest(tmp_path):
    db, pid, c = _client(tmp_path)
    old_fail = _job(db, pid, "failed", "2026-08-01 00:00:00")
    old_done = _job(db, pid, "done", "2026-08-01 00:00:00")
    old_cancel = _job(db, pid, "cancelled", "2026-08-01 00:00:00")
    new_fail = _job(db, pid, "failed", "2099-01-01 00:00:00")  # 远未来=一定保留
    pending = jobs.enqueue_job(db, "gen_shot", project_id=pid)
    with c:
        r = c.post("/api/jobs/purge?days=7")
        assert r.status_code == 200, r.text
        assert r.json()["purged"] == 3
        remain = {row["id"] for row in db.connect().execute(
            "SELECT id FROM jobs")}
        assert remain == {new_fail, pending}


def test_purge_days_clamped(tmp_path):
    db, pid, c = _client(tmp_path)
    with c:
        assert c.post("/api/jobs/purge?days=0").status_code == 422
        assert c.post("/api/jobs/purge?days=400").status_code == 422
