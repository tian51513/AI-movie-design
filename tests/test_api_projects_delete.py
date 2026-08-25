# tests/test_api_projects_delete.py
"""项目删除（2026-08-25 需求）：行/磁盘目录清理、任务取消、全局资产库保留。"""
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine import jobs
from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting
from comic_studio.engine.shots import persist_shots
from comic_studio.web.app import create_app
from comfy_mock import comfy_server


def _client(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "删除剧", "16:9", "正文")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                      scenes=[], props=[]))
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="t2v", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="p")])
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    return db, pid, TestClient(app)


def _counts(db, pid):
    conn = db.connect()
    return {t: conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE project_id=?",
                            (pid,)).fetchone()["c"]
            for t in ("jobs", "shots", "project_assets", "logs")}


def test_delete_project_cleans_rows_and_disk(tmp_path):
    db, pid, c = _client(tmp_path)
    jobs.enqueue_job(db, "gen_shot", project_id=pid, payload={})
    with c:
        r = c.delete(f"/api/projects/{pid}")
        assert r.status_code == 200 and r.json()["deleted"] == pid
        assert c.get(f"/api/projects/{pid}").status_code == 404
    assert (tmp_path / "data" / "projects" / "删除剧").exists() is False
    assert all(v == 0 for v in _counts(db, pid).values())
    row = db.connect().execute("SELECT COUNT(*) c FROM projects WHERE id=?",
                               (pid,)).fetchone()["c"]
    assert row == 0


def test_delete_keeps_global_library(tmp_path):
    """资产库是全局的：删项目不动 data/library（其他项目可能复用）。"""
    db, pid, c = _client(tmp_path)
    lib_dirs = [a["library_dir"] for a in list_project_assets(db, pid)]
    assert lib_dirs
    with c:
        assert c.delete(f"/api/projects/{pid}").status_code == 200
    from pathlib import Path
    for ld in lib_dirs:
        assert (tmp_path / "data" / ld).exists(), f"全局资产被误删: {ld}"


def test_delete_cancels_and_interrupts_running(tmp_path):
    with comfy_server("ok") as mock:
        db, pid, c = _client(tmp_path)
        set_setting(db, "comfy", {"base_url": mock.base_url})
        with c:
            jobs.create_job(db, project_id=pid, jtype="gen_shot")  # lifespan 后造 running
            r = c.delete(f"/api/projects/{pid}")
        assert r.status_code == 200
        assert mock.interrupts == 1  # 在跑渲染向 ComfyUI 发了 interrupt
        left = db.connect().execute(
            "SELECT COUNT(*) c FROM jobs WHERE project_id=?", (pid,)).fetchone()["c"]
        assert left == 0  # 任务行随项目清掉


def test_delete_missing_404(tmp_path):
    _, _, c = _client(tmp_path)
    with c:
        assert c.delete("/api/projects/999").status_code == 404


def test_delete_project_with_depends_on_chain(tmp_path):
    """真机 2026-08-25：镜间 depends_on 自引用链——单条 DELETE 逐行 FK 检查会违约 500。"""
    db, pid, c = _client(tmp_path)
    from comic_studio.engine.shots import persist_shots
    ids = persist_shots(db, pid, [
        NS(text_span="", description="a", shot_type="", camera={}, duration=5.0,
           workflow_type="t2v", ledger={}, character_ids=[], scene_ids=[],
           prop_ids=[], depends_on=None, prompt="p"),
        NS(text_span="", description="b", shot_type="", camera={}, duration=5.0,
           workflow_type="t2v", ledger={}, character_ids=[], scene_ids=[],
           prop_ids=[], depends_on=None, prompt="p"),
    ])
    from comic_studio.engine.shots import update_shot  # noqa: F401
    conn = db.connect()
    conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (ids[0], ids[1]))
    conn.commit()  # 镜2 依赖 镜1（接力链；update_shot 不开放该字段，直连建链）
    with c:
        r = c.delete(f"/api/projects/{pid}")
    assert r.status_code == 200


def test_delete_project_with_job_logs(tmp_path):
    """真机 2026-08-25：logs.job_id 引用 jobs——jobs 先删会 FK 违约（顺序修正回归）。"""
    db, pid, c = _client(tmp_path)
    jid = jobs.enqueue_job(db, "gen_shot", project_id=pid, payload={})
    from comic_studio.engine.logbus import emit as emit_log
    emit_log(db, "comfy", "info", "带 job_id 的日志", project_id=pid, job_id=jid)
    with c:
        assert c.delete(f"/api/projects/{pid}").status_code == 200
    assert all(v == 0 for v in _counts(db, pid).values())
