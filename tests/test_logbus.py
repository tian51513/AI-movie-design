# tests/test_logbus.py
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.logbus import emit, fetch_logs
from comic_studio.engine.projects import create_project


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def _pids(db, tmp_path, n=2):
    return [create_project(db, tmp_path / "data", f"p{i}", "9:16", "t")["id"] for i in range(n)]


def test_emit_and_fetch_roundtrip(tmp_path):
    db = _db(tmp_path)
    pid = _pids(db, tmp_path, 1)[0]
    emit(db, "analyze", "info", "分块 1/2 开始（100 字）", project_id=pid, data={"chars": 100})
    emit(db, "llm", "warn", "校验重试：JSON 解析失败", project_id=pid)
    rows = fetch_logs(db, project_id=pid)
    assert len(rows) == 2
    assert rows[0]["source"] == "analyze" and rows[0]["level"] == "info"
    assert rows[1]["level"] == "warn"


def test_after_cursor(tmp_path):
    db = _db(tmp_path)
    pid = _pids(db, tmp_path, 1)[0]
    emit(db, "system", "info", "a", project_id=pid)
    first = fetch_logs(db, pid)[0]["id"]
    emit(db, "system", "info", "b", project_id=pid)
    rows = fetch_logs(db, pid, after_id=first)
    assert [r["message"] for r in rows] == ["b"]


def test_project_isolation(tmp_path):
    db = _db(tmp_path)
    p1, p2 = _pids(db, tmp_path)
    emit(db, "system", "info", "mine", project_id=p1)
    emit(db, "system", "info", "other", project_id=p2)
    assert [r["message"] for r in fetch_logs(db, p1)] == ["mine"]


def test_invalid_level_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(AssertionError):
        emit(db, "system", "debug", "x", project_id=1)  # level 非法，断言先于 FK
