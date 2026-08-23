# tests/test_api_logs.py
from fastapi.testclient import TestClient

from comic_studio.engine.logbus import emit
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False))


def test_logs_404_unknown_project(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/projects/999/logs").status_code == 404


def test_logs_returns_structured_rows_and_cursor(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "p", "aspect_ratio": "9:16"},
                     files={"novel": ("n.txt", __import__("io").BytesIO("t".encode()), "text/plain")}).json()["id"]
        emit(c.app.state.db, "analyze", "info", "入库 1 角色", project_id=pid, data={"n": 1})
        body = c.get(f"/api/projects/{pid}/logs").json()
        assert body["last_id"] == body["logs"][0]["id"]
        row = body["logs"][0]
        assert set(row) == {"id", "time", "source", "level", "message", "data"}
        assert row["source"] == "analyze" and row["data"] == {"n": 1}
        # 游标增量
        emit(c.app.state.db, "llm", "warn", "重试", project_id=pid)
        delta = c.get(f"/api/projects/{pid}/logs?after={body['last_id']}").json()
        assert len(delta["logs"]) == 1 and delta["logs"][0]["message"] == "重试"
