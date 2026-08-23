# tests/test_jobs.py
from comic_studio.engine.db import Database
from comic_studio.engine.jobs import create_job, finish_job, get_job, latest_job
from comic_studio.engine.projects import create_project


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_create_finish_roundtrip(tmp_path):
    db = _db(tmp_path)
    create_project(db, tmp_path / "data", "t", "9:16", "x")
    jid = create_job(db, project_id=1, jtype="analyze")
    assert get_job(db, jid)["status"] == "running"
    finish_job(db, jid, error=None)
    assert get_job(db, jid)["status"] == "done"
    jid2 = create_job(db, project_id=1, jtype="analyze")
    finish_job(db, jid2, error="boom")
    assert get_job(db, jid2)["status"] == "failed"
    assert latest_job(db, 1, "analyze")["error"] == "boom"
