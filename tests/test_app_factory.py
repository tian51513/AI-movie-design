from fastapi.testclient import TestClient

from comic_studio.web.app import create_app


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
    from comic_studio.engine.db import Database
    conn = Database(db_path).connect()
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 0  # 表已存在
