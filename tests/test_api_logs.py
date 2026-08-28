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


def test_logs_initial_fetch_desc_incremental_asc(tmp_path):
    """日志排序（2026-08-28 需求：时间降序）：首拉（after=0）取最新 N 条且倒序
    （最新在顶，旧库不再先看到 1000 条上古日志）；增量拉取仍升序供 unshift。"""
    from comic_studio.engine.logbus import emit as emit_log, fetch_logs
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "日志序剧", "9:16", "t")["id"]
    for i in range(5):
        emit_log(db, "test", "info", f"msg{i}", project_id=pid)
    first = fetch_logs(db, pid, after_id=0, limit=3)
    assert [r["message"] for r in first] == ["msg4", "msg3", "msg2"]  # 最新 3 条、倒序
    inc = fetch_logs(db, pid, after_id=first[0]["id"], limit=10)
    assert [r["message"] for r in inc] == []  # 无比 first[0] 更新的
    older = fetch_logs(db, pid, after_id=0, limit=10)
    assert [r["message"] for r in older][0] == "msg4"  # 首拉倒序：最新在顶
