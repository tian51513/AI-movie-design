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


def test_wait_after_failed_analyze(tmp_path):
    """失败不无限重烧：最近一次 analyze 失败 → wait（真机 2026-08-25：失败即重跑烧 token）。"""
    db, pid = _proj(tmp_path)
    jid = jobs.enqueue_job(db, "analyze", project_id=pid, payload={"project_id": pid})
    jobs.finish_job(db, jid, "boom")
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "失败" in act["detail"]


def test_assets_without_sheets_go_gen_refs(tmp_path):
    """真机 bug（2026-08-25 验收）：any(glob(...) for e) 生成器恒真 → _all_assets_have_sheets
    恒 True → 永远选 gate1 → gate_pass 正确拒绝 → 吞异常死循环卡「过门1」。"""
    db, pid = _proj(tmp_path)
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="沈雪柔", appearance="黑发", tags=[])],
                      scenes=[], props=[]))
    set_stage(db, pid, "analyzed")
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "gen_refs"  # 无参考图：生成而非过门
    tick(db, tmp_path / "data", pid)
    row = db.connect().execute(
        "SELECT COUNT(*) c FROM jobs WHERE project_id=? AND type='gen_ref'",
        (pid,)).fetchone()
    assert row["c"] == 1  # 真的入队了


def test_assets_ready_bridge_after_split(tmp_path):
    """真机 bug（2026-08-25 项目4）：assets_ready 拆完分镜后无桥接 → 无限重拆。
    有分镜缺提示词 → gen_prompts；齐全 → gate2 过门 → storyboard_ready。"""
    from comic_studio.engine.projects import set_stage
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "assets_ready")
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="t2v", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="")])[0]
    assert next_action(db, tmp_path / "data", pid)["action"] == "gen_prompts"
    update_shot(db, sid, {"prompt": "提示词"})
    assert next_action(db, tmp_path / "data", pid)["action"] == "gate2"
    tick(db, tmp_path / "data", pid)
    assert get_project(db, pid)["stage"] == "storyboard_ready"


def test_autopilot_turns_off_when_done(tmp_path):
    """全流程完成（merged）→ tick 自动关 autopilot（真机：完成后「停止自动」仍可点）。"""
    from comic_studio.engine.projects import set_stage
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "merged")
    conn = db.connect()
    conn.execute("UPDATE projects SET autopilot=1 WHERE id=?", (pid,))
    conn.commit()
    assert next_action(db, tmp_path / "data", pid)["action"] == "done"
    tick(db, tmp_path / "data", pid)
    assert get_project(db, pid)["autopilot"] == 0


# ===== 2026-09-04 设计A：失败守卫 / 跨类型在飞感知 =====

def _shot(**kw):
    base = dict(text_span="", description="x", shot_type="", camera={},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="")
    base.update(kw)
    return NS(**base)


def test_wait_after_failed_split(tmp_path):
    """split 失败守卫：最新 split_storyboards failed 且无在飞 → wait 不自动重拆
    （泛化 2026-08-25 analyze 失败不重烧模式——否则每 3s 无限重拆烧 token）。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "assets_ready")
    jid = jobs.enqueue_job(db, "split_storyboards", project_id=pid,
                           payload={"project_id": pid})
    jobs.finish_job(db, jid, "boom")
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "失败" in act["detail"]


def test_wait_after_failed_describe_shots(tmp_path):
    """漫画读图失败守卫：describe_shots failed → wait 不自动重烧 VLM。"""
    db = Database(tmp_path / "c.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "漫画剧", "16:9", "正文",
                         comic_mode="motion_comic")["id"]
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [_shot()])
    jid = jobs.enqueue_job(db, "describe_shots", project_id=pid,
                           payload={"project_id": pid})
    jobs.finish_job(db, jid, "boom")
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "失败" in act["detail"]


def test_gen_prompts_skips_failed_shots_then_waits(tmp_path):
    """逐镜失败守卫：失败镜跳过不重烧、其余继续；全部失败 → wait + 一次性 error 日志。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    s1, s2 = persist_shots(db, pid, [_shot(), _shot()])
    j1 = jobs.enqueue_job(db, "gen_prompt", project_id=pid, shot_id=s1, payload={})
    jobs.finish_job(db, j1, "boom")
    assert next_action(db, tmp_path / "data", pid)["action"] == "gen_prompts"
    tick(db, tmp_path / "data", pid)
    pend = db.connect().execute(
        "SELECT id, shot_id FROM jobs WHERE type='gen_prompt' AND status='pending'").fetchall()
    assert [r["shot_id"] for r in pend] == [s2]  # 失败镜不入队
    for r in pend:
        jobs.finish_job(db, r["id"], "boom")  # 剩余也失败 → 全卡死
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "失败" in act["detail"]
    tick(db, tmp_path / "data", pid)   # 卡死 → error 一次性上报
    tick(db, tmp_path / "data", pid)   # 重复巡检不刷屏
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE source='autopilot' AND level='error'"
    ).fetchone()["c"]
    assert n == 1


def test_render_skips_failed_shots(tmp_path):
    """渲染逐镜失败守卫：失败镜不无限重渲染，其余继续入队。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    s1, s2 = persist_shots(db, pid, [_shot(prompt="甲"), _shot(prompt="乙")])
    j1 = jobs.enqueue_job(db, "gen_shot", project_id=pid, shot_id=s1,
                          resource="gpu_comfy", payload={"shot_id": s1})
    jobs.finish_job(db, j1, "boom")
    assert next_action(db, tmp_path / "data", pid)["action"] == "render"
    tick(db, tmp_path / "data", pid)
    rows = db.connect().execute(
        "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_shot' AND status='pending'"
    ).fetchall()
    assert [r["shot_id"] for r in rows] == [s2]


def test_gate3_waits_for_inflight_renders(tmp_path):
    """跨类型在飞感知：全部有视频但 gen_shot 仍在飞（重渲染）→ 不门3
    （2026-09-04 武侠风云真机竞态：门3 过了、镜15 还在重渲染，旧视频差点拼进成片）。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    sid = persist_shots(db, pid, [_shot(prompt="甲")])[0]
    update_shot(db, sid, {"video_path": "projects/自动剧/shots/1/video_v1.mp4"})
    jobs.enqueue_job(db, "gen_shot", project_id=pid, shot_id=sid, resource="gpu_comfy",
                     payload={"shot_id": sid})
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "渲染" in act["detail"]


def test_merge_waits_for_inflight_renders(tmp_path):
    """rendered 阶段：gen_shot 仍在飞（重渲染）→ 不合成（防旧视频拼进成片）。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "rendered")
    sid = persist_shots(db, pid, [_shot(prompt="甲")])[0]
    update_shot(db, sid, {"video_path": "projects/自动剧/shots/1/video_v1.mp4"})
    jobs.enqueue_job(db, "gen_shot", project_id=pid, shot_id=sid, resource="gpu_comfy",
                     payload={"shot_id": sid})
    assert next_action(db, tmp_path / "data", pid)["action"] == "wait"


def test_wait_after_failed_merge(tmp_path):
    """merge 失败守卫（2026-09-04 真机：字幕滤镜炸后 autopilot 每 3s 重烧合成）。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "rendered")
    sid = persist_shots(db, pid, [_shot(prompt="甲")])[0]
    update_shot(db, sid, {"video_path": "projects/自动剧/shots/1/video_v1.mp4"})
    jid = jobs.enqueue_job(db, "merge", project_id=pid, payload={"project_id": pid})
    jobs.finish_job(db, jid, "subtitles filter boom")
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "失败" in act["detail"]


# ===== 2026-09-05 审计高危修复：H1 漫改参考图 / H2 TTS 时序 =====

def test_film_adaptation_detects_empty_views_dir(tmp_path):
    """H1：persist_assets 恒建空 views 目录——目录存在≠有图，漫改参考图检查
    必须按文件判断（has_views），否则空参考图直进渲染永不补图。"""
    db = Database(tmp_path / "c.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "漫改剧", "16:9", "正文",
                         comic_mode="film_adaptation")["id"]
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [_shot(prompt="甲")])
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="沈雪柔", appearance="黑发", tags=[])],
                      scenes=[], props=[]))  # persist 建出空 views 目录
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "gen_refs", act


def test_tick_merge_no_longer_runs_tts_inline(tmp_path, monkeypatch):
    """H2a：merge 分支不再在巡检线程同步跑 TTS（曾卡停全部 autopilot 项目
    ~5min；且与手动合成行为不一致）——TTS/SRT 前置挪进 merge 任务本身。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "rendered")
    sid = persist_shots(db, pid, [_shot(prompt="甲")])[0]
    update_shot(db, sid, {"video_path": "projects/自动剧/shots/1/video_v1.mp4"})

    def _bomb(*a, **kw):
        raise AssertionError("巡检线程不得再内联跑 TTS")

    monkeypatch.setattr("comic_studio.engine.tts.generate_dialogue_audio", _bomb)
    act = tick(db, tmp_path / "data", pid)
    assert act["action"] == "merge"
    row = db.connect().execute(
        "SELECT type FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (pid,)).fetchone()
    assert row["type"] == "merge"
    # 内联跑过必留「TTS/字幕生成失败…继续合成」warn——零痕迹才算真没跑
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%继续合成%'").fetchone()["c"]
    assert n == 0


# ===== 2026-09-05 审计中危批次 A =====

def test_stale_prompts_count_as_missing(tmp_path):
    """M1：资产重生标 stale 的镜，autopilot 视为缺口重生（手动批量早已重生
    stale，autopilot 此前只查空提示词→渲染旧提示词）。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    sid = persist_shots(db, pid, [_shot(prompt="旧提示词")])[0]
    update_shot(db, sid, {"status": "stale"})
    assert next_action(db, tmp_path / "data", pid)["action"] == "gen_prompts"
    tick(db, tmp_path / "data", pid)
    row = db.connect().execute(
        "SELECT shot_id FROM jobs WHERE type='gen_prompt' AND status='pending'"
    ).fetchone()
    assert row and row["shot_id"] == sid


def test_all_disabled_shots_wait_not_loop(tmp_path):
    """M3：全部镜无效时明确 wait（此前 storyboard_ready 恒 render 刷日志、
    assets_ready 反复 gate2 静默吞异常——两个无信号死循环）。"""
    from comic_studio.engine.shots import set_disabled_batch
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    ids = persist_shots(db, pid, [_shot(prompt="甲"), _shot(prompt="乙")])
    set_disabled_batch(db, pid, ids, 1)
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "无效" in act["detail"]
    set_stage(db, pid, "assets_ready")
    act2 = next_action(db, tmp_path / "data", pid)
    assert act2["action"] == "wait" and "无效" in act2["detail"]


def test_describe_shots_resource_follows_routing(tmp_path, monkeypatch):
    """L13（2026-09-05 审计低危）：describe/extract 硬编码 gpu_llm_local——
    routing 配 online 时仍占 gpu 组过度互斥。改走 enqueue_llm_job 按 routing。"""
    from comic_studio.engine.settings import set_setting
    db = Database(tmp_path / "c.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "路由剧", "16:9", "正文",
                         comic_mode="motion_comic")["id"]
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [_shot()])
    set_setting(db, "llm_routing", {"describe_shot": "online"})
    act = tick(db, tmp_path / "data", pid)
    assert act["action"] == "describe_shots"
    row = db.connect().execute(
        "SELECT resource FROM jobs WHERE type='describe_shots' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["resource"] is None  # online 路由不再占 gpu 组


def test_stuck_reported_cleared_on_project_delete(tmp_path):
    """L9：_STUCK_REPORTED 对已删项目永不清（轻微泄漏）——删项目顺手清键。"""
    from fastapi.testclient import TestClient
    from comic_studio.engine import autopilot as AP
    from comic_studio.web.app import create_app
    db, pid = _proj(tmp_path)
    AP._STUCK_REPORTED[pid] = "上次分镜拆解失败，重试请手动发起"
    with TestClient(create_app(db_path=tmp_path / "s.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        assert c.delete(f"/api/projects/{pid}").status_code == 200
    assert pid not in AP._STUCK_REPORTED


def test_audio_project_waits_for_transcription(tmp_path):
    """终审 M-1 真机命中：源音频在、segments 未落 → 占位正文 20 字被分析成
    空资产。created 分支 wait 等转写；手动 /analyze 409。"""
    db, pid = _proj(tmp_path)
    adir = tmp_path / "data" / "projects" / "自动剧" / "audio"
    adir.mkdir(parents=True)
    (adir / "source.mp3").write_bytes(b"f")
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "转写" in act["detail"]
    import io
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    with TestClient(create_app(db_path=tmp_path / "s.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        assert c.post(f"/api/projects/{pid}/analyze").status_code == 409


def test_analyzed_with_zero_assets_waits_not_spins(tmp_path):
    """真机 16:03 空转：0 资产 → _all_assets_have_sheets 恒 False → gen_refs
    每 3s「入队 0 张」刷屏。空资产明确 wait。"""
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "analyzed")
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "资产" in act["detail"]
