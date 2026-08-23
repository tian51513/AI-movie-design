from fastapi.testclient import TestClient

from comic_studio.web.app import create_app
from comic_studio.engine.db import Database
from comic_studio.engine.jobs import create_job, get_job
from comic_studio.engine.projects import create_project


def test_health(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}


def test_migrations_applied_on_startup(tmp_path):
    db_path = tmp_path / "t.db"
    app = create_app(db_path=db_path, data_dir=tmp_path / "data")
    with TestClient(app):
        pass
    conn = Database(db_path).connect()
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 0  # 表已存在


def test_restart_cancels_running_jobs(tmp_path):
    """重启后 running job 应被标记为 failed（BackgroundTasks 随进程消亡）。"""
    db_path = tmp_path / "t.db"
    data_dir = tmp_path / "data"
    # 第一个 app 生命周期：手动插入一个 running job
    app1 = create_app(db_path=db_path, data_dir=data_dir)
    with TestClient(app1):
        db = app1.state.db
        create_project(db, data_dir, "p", "9:16", "x")
        jid = create_job(db, project_id=1, jtype="analyze")
        assert get_job(db, jid)["status"] == "running"
    # 第二个 app 生命周期（模拟重启）：running job 应变为 failed
    app2 = create_app(db_path=db_path, data_dir=data_dir)
    with TestClient(app2):
        job = get_job(app2.state.db, jid)
        assert job["status"] == "failed"
        assert "interrupted by restart" in job["error"]
