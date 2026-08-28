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


def test_cancel_project_jobs(tmp_path):
    """项目级停止（2026-08-28 需求）：pending→cancelled；running→attempts 打满
    （worker 异常返回时 retry_or_fail 不再重排，直接落 failed）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.jobs import cancel_project_jobs, enqueue_job, get_job
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "停务剧", "9:16", "t")["id"]
    p1 = enqueue_job(db, "gen_director", project_id=pid, payload={})
    p2 = enqueue_job(db, "gen_shot", project_id=pid, payload={"shot_id": 9})
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (p2,))
    conn.commit()
    r = cancel_project_jobs(db, pid)
    assert r == {"cancelled": 1, "stopping": 1}
    assert get_job(db, p1)["status"] == "cancelled"
    assert get_job(db, p2)["attempts"] >= 99  # running 不改状态（worker 收尾），只禁重排
