# tests/test_autopilot.py
from types import SimpleNamespace as NS

from comic_studio.engine import jobs
from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.autopilot import next_action, tick
from comic_studio.engine.db import Database
from comic_studio.engine.paths import data_to_abs
from comic_studio.engine.projects import create_project, get_project, set_stage
from comic_studio.engine.shots import list_shots, persist_shots, update_shot


def _proj(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "自动剧", "16:9", "正文文本")["id"]
    return db, pid


def test_action_by_stage(tmp_path):
    db, pid = _proj(tmp_path)
    assert next_action(db, tmp_path / "data", pid)["action"] == "analyze"
    set_stage(db, pid, "analyzed")
    assert next_action(db, tmp_path / "data", pid)["action"] == "gen_refs"
    set_stage(db, pid, "assets_ready")
    assert next_action(db, tmp_path / "data", pid)["action"] == "split"


def test_gate1_when_all_assets_have_sheets(tmp_path):
    db, pid = _proj(tmp_path)
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])], scenes=[], props=[]))
    for a in list_project_assets(db, pid):
        views = data_to_abs(tmp_path / "data", a["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
    set_stage(db, pid, "analyzed")
    assert next_action(db, tmp_path / "data", pid)["action"] == "gate1"
    tick(db, tmp_path / "data", pid)
    assert get_project(db, pid)["stage"] == "assets_ready"
    set_stage(db, pid, "storyboard_ready")
    assert next_action(db, tmp_path / "data", pid)["action"] == "split"


def test_render_then_gate3_then_merge(tmp_path):
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    ids = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="", camera={},
        duration=5.0, workflow_type="ref2va", ledger={}, character_ids=[],
        scene_ids=[], prop_ids=[], depends_on=None, prompt="提示词")])
    update_shot(db, ids[0], {"video_path": "projects/自动剧/shots/1/video_v1.mp4", "status": "rendered"})
    assert next_action(db, tmp_path / "data", pid)["action"] == "gate3"
    tick(db, tmp_path / "data", pid)
    assert get_project(db, pid)["stage"] == "rendered"
    assert next_action(db, tmp_path / "data", pid)["action"] == "merge"


def test_wait_when_jobs_active(tmp_path):
    db, pid = _proj(tmp_path)
    jobs.enqueue_job(db, "analyze", project_id=pid, payload={"project_id": pid})
    jobs.create_job(db, project_id=pid, jtype="analyze")
    assert next_action(db, tmp_path / "data", pid)["action"] == "wait"


def test_gen_prompts_when_missing(tmp_path):
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="", camera={},
        duration=5.0, workflow_type="ref2va", ledger={}, character_ids=[],
        scene_ids=[], prop_ids=[], depends_on=None, prompt="")])
    assert next_action(db, tmp_path / "data", pid)["action"] == "gen_prompts"
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="", camera={},
        duration=5.0, workflow_type="ref2va", ledger={}, character_ids=[],
        scene_ids=[], prop_ids=[], depends_on=None, prompt="有提示词")])
    # 门2 是检查型（提示词齐全即开始渲染，不转 stage）→ 下一步 render
    assert next_action(db, tmp_path / "data", pid)["action"] == "render"


def test_tick_analyze_enqueues_analyze_job(tmp_path):
    """analyze 动作入队 analyze 类型（经 worker 跑 analyze_project），
    而非 split_storyboards——否则跳过分析卡死 created。"""
    db = Database(tmp_path / "t.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "分析剧", "16:9", "正文")["id"]
    act = tick(db, tmp_path / "data", pid)
    assert act["action"] == "analyze"
    row = db.connect().execute(
        "SELECT type, status FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (pid,)).fetchone()
    assert row["type"] == "analyze" and row["status"] == "pending"


def test_autopilot_once_ticks_enabled_only(tmp_path):
    """巡检单轮：只动 autopilot=1 的项目；tick 异常不冒泡（记日志继续）。"""
    from comic_studio.web.app import _autopilot_once
    db, pid = _proj(tmp_path)
    conn = db.connect()
    conn.execute("UPDATE projects SET autopilot=1 WHERE id=?", (pid,))
    conn.commit()
    assert _autopilot_once(db, tmp_path / "data") == 1
    row = db.connect().execute(
        "SELECT type FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (pid,)).fetchone()
    assert row["type"] == "analyze"
    # 关掉开关后不再产生新动作
    conn.execute("UPDATE projects SET autopilot=0, stage='created' WHERE id=?", (pid,))
    conn.execute("DELETE FROM jobs WHERE project_id=?", (pid,))
    conn.commit()
    assert _autopilot_once(db, tmp_path / "data") == 0
