# tests/test_api_queue_clear.py
"""一键清空队列/取消任务（2026-08-25 需求）：pending→cancelled；
running gen_shot 先 ComfyUI /interrupt 再取消；worker 不得复活已取消任务。"""
from fastapi.testclient import TestClient

from comic_studio.engine import jobs
from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting
from comic_studio.web.app import create_app
from comfy_mock import comfy_server


def _client(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "清队剧", "16:9", "t")["id"]
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    return db, pid, TestClient(app)


def test_clear_queue_cancels_pending_and_interrupts_running(tmp_path):
    with comfy_server("ok") as mock:
        db, pid, c = _client(tmp_path)
        set_setting(db, "comfy", {"base_url": mock.base_url})
        for i in range(3):
            jobs.enqueue_job(db, "gen_shot", project_id=pid,
                             payload={"shot_id": None})
        with c:
            # running 须在 lifespan 之后造（启动 requeue 会把遗留 running 翻 pending）
            jobs.create_job(db, project_id=pid, jtype="gen_shot")
            r = c.delete(f"/api/projects/{pid}/queue")
        assert r.status_code == 200 and r.json()["cancelled"] == 4
        assert mock.interrupts == 1  # running 的向 ComfyUI 发了 interrupt
        left = db.connect().execute(
            "SELECT COUNT(*) c FROM jobs WHERE project_id=? AND status='pending'",
            (pid,)).fetchone()["c"]
        assert left == 0
        # 已取消的不被 claim 复活
        from comic_studio.engine.queue.worker import HANDLERS  # 注册触发
        row = jobs.claim_next_job(db, ("gen_shot", "gen_ref", "merge"))
        assert row is None


def test_retry_or_fail_never_resurrects_cancelled(tmp_path):
    """worker 还持有被取消的 job 时（poll 中），结束后不得把它改回 pending。"""
    db, pid, c = _client(tmp_path)
    jid = jobs.enqueue_job(db, "gen_shot", project_id=pid, payload={})
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
    conn.commit()
    assert retry_guard(db, jid) == "cancelled"


def retry_guard(db, jid):
    from comic_studio.engine.jobs import retry_or_fail
    return retry_or_fail(db, jid, "boom")


def test_clear_queue_noop_when_empty(tmp_path):
    db, pid, c = _client(tmp_path)
    with c:
        r = c.delete(f"/api/projects/{pid}/queue")
    assert r.status_code == 200 and r.json()["cancelled"] == 0
