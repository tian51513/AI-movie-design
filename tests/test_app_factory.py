from fastapi.testclient import TestClient

from comic_studio.web.app import create_app
from comic_studio.engine.db import Database
from comic_studio.engine.jobs import create_job, get_job
from comic_studio.engine.projects import create_project


def test_health(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False)
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}


def test_frontend_assets_no_cache_header(tmp_path):
    """前端资源必须 no-cache（2026-09-01 事故：无 Cache-Control → 浏览器启发式
    缓存旧 app.js，新 HTML+旧 JS 混跑出现怪相；no-cache 带 ETag 重验证零成本）。"""
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False)
    with TestClient(app) as client:
        for path in ("/", "/static/app.js"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers.get("cache-control") == "no-cache", path


def test_migrations_applied_on_startup(tmp_path):
    db_path = tmp_path / "t.db"
    app = create_app(db_path=db_path, data_dir=tmp_path / "data", start_workers=False)
    with TestClient(app):
        pass
    conn = Database(db_path).connect()
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 0  # 表已存在


def test_restart_cancels_running_jobs(tmp_path):
    """重启后 analyze running→failed，gen_ref running→pending（requeue）。"""
    db_path = tmp_path / "t.db"
    data_dir = tmp_path / "data"
    app1 = create_app(db_path=db_path, data_dir=data_dir, start_workers=False)
    with TestClient(app1):
        db = app1.state.db
        create_project(db, data_dir, "p", "9:16", "x")
        jid_a = create_job(db, project_id=1, jtype="analyze")
        jid_g = create_job(db, project_id=1, jtype="gen_ref")
        assert get_job(db, jid_a)["status"] == "running"
        assert get_job(db, jid_g)["status"] == "running"
    app2 = create_app(db_path=db_path, data_dir=data_dir, start_workers=False)
    with TestClient(app2):
        assert get_job(app2.state.db, jid_a)["status"] == "failed"
        assert get_job(app2.state.db, jid_g)["status"] == "pending"
