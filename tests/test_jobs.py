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


def test_attach_audit_snapshot(tmp_path):
    """P7-A 审计快照：最终提示词+完整工作流 JSON 落 jobs.snapshot_json。"""
    import json
    from comic_studio.engine.db import Database
    from comic_studio.engine.jobs import attach_snapshot, enqueue_job, get_job
    db = Database(tmp_path / "s.db"); db.migrate()
    jid = enqueue_job(db, "gen_shot", payload={"shot_id": 1})
    attach_snapshot(db, jid, prompt="林晨推门", workflow={"6": {"class_type": "CLIPTextEncode"}},
                    template_id="zimage_t2i")
    snap = json.loads(get_job(db, jid)["snapshot_json"])
    assert snap["prompt"] == "林晨推门"
    assert snap["template"] == "zimage_t2i"
    assert snap["workflow"]["6"]["class_type"] == "CLIPTextEncode"
