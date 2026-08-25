# tests/test_reattach.py
"""断点对账（spec §5）：重启时按 comfy_prompt_id 查 ComfyUI /history，
已完成直接下载落盘，不重渲。"""
import json
from types import SimpleNamespace as NS

from comic_studio.engine import jobs
from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, get_project, set_stage
from comic_studio.engine.shots import get_shot, persist_shots, update_shot
from comfy_mock import comfy_server


def _proj_with_shot(tmp_path, client_id):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "对账剧", "16:9", "t")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="t2v", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
        prompt="提示词")])[0]
    update_shot(db, sid, {"prompt": "提示词"})
    return db, pid, sid, client_id


def test_collect_reattach_candidates(tmp_path):
    db, pid, sid, cid = _proj_with_shot(tmp_path, "cs-job-7")
    jobs.create_job(db, project_id=pid, jtype="gen_shot")
    conn = db.connect()
    jid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    conn.execute("UPDATE jobs SET shot_id=?, comfy_prompt_id=?, status='running' WHERE id=?",
                 (sid, "p1", jid))
    conn.commit()
    rows = jobs.collect_reattach_candidates(db)
    assert len(rows) == 1 and rows[0]["comfy_prompt_id"] == "p1"
    # requeue 之后收集为空（调用顺序契约）
    jobs.requeue_on_restart(db, ("gen_shot",))
    assert jobs.collect_reattach_candidates(db) == []


def _simulate_crash(tmp_path, db, pid, sid, comfy):
    """正常渲染一镜 → 模拟进程中断：job 卡 running、shot 状态回退、产物丢失。"""
    from comic_studio.engine.rendershot import render_shot
    job_id = jobs.enqueue_job(db, "gen_shot", project_id=pid, shot_id=sid,
                              payload={"shot_id": sid})
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=?",
                 (job_id,))
    conn.commit()
    render_shot(db, tmp_path / "data", sid, comfy, job_id=job_id)
    update_shot(db, sid, {"status": "pending", "video_path": None})
    for f in (tmp_path / "data" / "projects" / "对账剧" / "shots" / "1").glob("video*.mp4"):
        f.unlink()
    return jobs.get_job(db, job_id)


def test_reattach_downloads_without_resubmit(tmp_path):
    from comic_studio.engine.rendershot import reattach
    with comfy_server(mode="ok", video=True) as mock:
        comfy = ComfyClient(mock.base_url)
        db, pid, sid, _ = _proj_with_shot(tmp_path, "cs-shot-1")
        row = _simulate_crash(tmp_path, db, pid, sid, comfy)
        n_prompts = len(mock.prompts)
        dest = reattach(db, tmp_path / "data", row, comfy)
        assert dest is not None and dest.exists()
        shot = get_shot(db, sid)
        assert shot["status"] == "rendered"
        assert shot["video_path"].endswith("video_v1.mp4")
        assert len(mock.prompts) == n_prompts  # 未重提交 /prompt


def test_reattach_returns_none_when_history_empty(tmp_path):
    from comic_studio.engine.rendershot import reattach
    with comfy_server(mode="hang") as mock:  # history 恒空=未完成
        comfy = ComfyClient(mock.base_url)
        db, pid, sid, _ = _proj_with_shot(tmp_path, "cs-shot-1")
        jobs.create_job(db, project_id=pid, jtype="gen_shot")
        conn = db.connect()
        conn.execute(
            "UPDATE jobs SET shot_id=?, comfy_prompt_id='p-hang' WHERE project_id=?",
            (sid, pid))
        conn.commit()
        row = jobs.collect_reattach_candidates(db)[0]
        assert reattach(db, tmp_path / "data", row, comfy) is None
        assert get_shot(db, sid)["status"] == "pending"  # 留给 requeue 重渲


def test_try_reattach_marks_job_done(tmp_path):
    from comic_studio.web.app import _try_reattach
    with comfy_server(mode="ok", video=True) as mock:
        comfy = ComfyClient(mock.base_url)
        db, pid, sid, _ = _proj_with_shot(tmp_path, "cs-shot-1")
        row = _simulate_crash(tmp_path, db, pid, sid, comfy)
        from comic_studio.engine.settings import set_setting
        set_setting(db, "comfy", {"base_url": mock.base_url})
        rows = jobs.collect_reattach_candidates(db)
        done, waiting = _try_reattach(db, tmp_path / "data", rows)
        assert done == 1 and waiting == []
        assert jobs.get_job(db, row["id"])["status"] == "done"
        assert get_shot(db, sid)["video_path"].endswith("video_v1.mp4")


def test_try_reattach_unreachable_falls_back(tmp_path):
    """ComfyUI 不可达：不标 done，行留给 requeue_on_restart 重渲。"""
    from comic_studio.web.app import _try_reattach
    db, pid, sid, _ = _proj_with_shot(tmp_path, "cs-shot-1")
    jobs.create_job(db, project_id=pid, jtype="gen_shot")
    conn = db.connect()
    conn.execute("UPDATE jobs SET comfy_prompt_id='p-x' WHERE project_id=?", (pid,))
    conn.commit()
    from comic_studio.engine.settings import set_setting
    set_setting(db, "comfy", {"base_url": "http://127.0.0.1:1"})  # 不可达
    rows = jobs.collect_reattach_candidates(db)
    done, waiting = _try_reattach(db, tmp_path / "data", rows)
    assert done == 0 and waiting == []
    assert jobs.get_job(db, rows[0]["id"])["status"] == "running"


def test_reattach_wait_for_inflight_prompt(tmp_path):
    """重启时 prompt 仍在 ComfyUI 队列/执行中 → 不重渲：保持 running、跳过 requeue，
    由后台等待线程接回（wait_and_collect → 落盘）。"""
    from comic_studio.web.app import _try_reattach
    from comic_studio.engine.rendershot import reattach_wait
    from comic_studio.engine.settings import set_setting
    db, pid, sid, _ = _proj_with_shot(tmp_path, "cs-shot-1")
    jobs.enqueue_job(db, "gen_shot", project_id=pid, shot_id=sid,
                     payload={"shot_id": sid})
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='running', comfy_prompt_id='p-live' "
                 "WHERE project_id=?", (pid,))
    conn.commit()
    # 场景一：history 无产物但 prompt 在队 → 等待接回（不判死、requeue 跳过）
    with comfy_server(mode="hang", queue_running=["p-live"]) as mock:
        set_setting(db, "comfy", {"base_url": mock.base_url})
        rows = jobs.collect_reattach_candidates(db)
        done, waiting = _try_reattach(db, tmp_path / "data", rows)
        assert done == 0 and waiting == [rows[0]["id"]]
        jobs.requeue_on_restart(db, ("gen_shot",), exclude_ids=waiting)
        assert jobs.get_job(db, rows[0]["id"])["status"] == "running"
    # 场景二：等待线程视角——ComfyUI 跑完（history 有产物）→ 落盘且不重提交
    with comfy_server(mode="ok", video=True) as mock:
        comfy = ComfyClient(mock.base_url)
        n_prompts = len(mock.prompts)
        dest = reattach_wait(db, tmp_path / "data",
                             jobs.get_job(db, rows[0]["id"]), comfy)
        assert dest is not None and dest.exists()
        assert get_shot(db, sid)["video_path"].endswith("video_v1.mp4")
        assert len(mock.prompts) == n_prompts
