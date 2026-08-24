# comic_studio/engine/autopilot.py
"""autopilot 决策引擎：项目全自动免门禁跑完管线（spec §5 一键出片，2026-08-24 用户需求）。

纯决策函数 next_action（可测）+ 执行函数 tick（入队/过门禁）。
幂等：每轮先查状态再决定动作，已完成步骤自动跳过。
"""
import json

from . import jobs as jobs_mod
from .assets import list_project_assets
from .llm.storyboard import split_storyboards
from .logbus import emit as emit_log
from .paths import data_to_abs
from .projects import get_project, set_stage
from .queue.worker import register
from .settings import get_setting

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _has_active_job(db, project_id, jtype) -> bool:
    return db.connect().execute(
        "SELECT 1 FROM jobs WHERE project_id=? AND type=? AND status IN ('pending','running') LIMIT 1",
        (project_id, jtype)).fetchone() is not None


def _all_assets_have_sheets(db, data_dir, project_id) -> bool:
    assets = list_project_assets(db, project_id)
    if not assets:
        return False
    for a in assets:
        views = data_to_abs(data_dir, a["library_dir"]) / "views"
        if not any(views.glob(f"*{e}") for e in IMAGE_EXTS):
            return False
    return True


def _shots_missing_prompt(db, project_id) -> int:
    conn = db.connect()
    return conn.execute(
        "SELECT COUNT(*) c FROM shots WHERE project_id=? AND (prompt IS '' OR prompt='' OR TRIM(prompt)='')",
        (project_id,)).fetchone()["c"]


def _all_shots_have_video(db, project_id) -> bool:
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=?",
                         (project_id,)).fetchone()["c"]
    done = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=? AND video_path IS NOT NULL",
                         (project_id,)).fetchone()["c"]
    return total > 0 and total == done


def next_action(db, data_dir, project_id) -> dict:
    """纯决策：返回 {"action": str, "detail": str} 或 None（项目不存在）。"""
    proj = get_project(db, project_id)
    if proj is None:
        return None
    stage = proj["stage"]
    if stage == "merged":
        return {"action": "done", "detail": "已成片"}
    if stage == "created":
        if _has_active_job(db, project_id, "analyze"):
            return {"action": "wait", "detail": "分析进行中"}
        return {"action": "analyze", "detail": "开始资产分析"}
    if stage == "analyzed":
        if _all_assets_have_sheets(db, data_dir, project_id):
            return {"action": "gate1", "detail": "资产齐全，过门1"}
        if _has_active_job(db, project_id, "gen_ref"):
            return {"action": "wait", "detail": "参考图生成中"}
        return {"action": "gen_refs", "detail": "批量生成参考图"}
    if stage == "assets_ready":
        if _has_active_job(db, project_id, "split_storyboards"):
            return {"action": "wait", "detail": "分镜拆解中"}
        return {"action": "split", "detail": "开始分镜拆解"}
    if stage == "storyboard_ready":
        total = db.connect().execute("SELECT COUNT(*) c FROM shots WHERE project_id=?",
                                     (project_id,)).fetchone()["c"]
        if total == 0:
            if _has_active_job(db, project_id, "split_storyboards"):
                return {"action": "wait", "detail": "分镜拆解中"}
            return {"action": "split", "detail": "无分镜，先拆解"}
        missing = _shots_missing_prompt(db, project_id)
        if missing > 0:
            if _has_active_job(db, project_id, "gen_prompt"):
                return {"action": "wait", "detail": "提示词生成中"}
            return {"action": "gen_prompts", "detail": f"缺 {missing} 条提示词"}
        if _has_active_job(db, project_id, "gen_prompt"):
            return {"action": "wait", "detail": "提示词生成中"}
        if not _all_shots_have_video(db, project_id):
            if _has_active_job(db, project_id, "gen_shot"):
                return {"action": "wait", "detail": "渲染中"}
            return {"action": "render", "detail": "批量渲染"}
        return {"action": "gate3", "detail": "全部有视频，过门3"}
    if stage == "rendered":
        if _has_active_job(db, project_id, "merge"):
            return {"action": "wait", "detail": "合成中"}
        return {"action": "merge", "detail": "开始合成成片"}
    return {"action": "wait", "detail": f"未知阶段 {stage}"}


def _pass_gate(db, data_dir, project_id, n: int) -> None:
    """门禁自动通过——条件与手动 gate 端点一致，不满足时留给下一轮（wait 循环）。"""
    proj = get_project(db, project_id)
    stage = proj["stage"]
    if n == 1 and stage == "analyzed" and _all_assets_have_sheets(db, data_dir, project_id):
        set_stage(db, project_id, "assets_ready")
        emit_log(db, "system", "info", "autopilot：门1 自动通过（analyzed → assets_ready）", project_id=project_id)
    elif n == 2 and stage == "storyboard_ready" and _shots_missing_prompt(db, project_id) == 0:
        set_stage(db, project_id, "storyboard_ready")
        emit_log(db, "system", "info", "autopilot：门2 检查（storyboard_ready）", project_id=project_id)
        # stage2 由 render 阶段在 all_shots 有 video 后 pass gate3——门2 在此模型中
        # 通过条件为提示词齐全（gate2→stage 仍为 storyboard_ready 渲染入口，不需要 set）
    elif n == 3 and stage == "storyboard_ready" and _all_shots_have_video(db, project_id):
        set_stage(db, project_id, "rendered")
        emit_log(db, "system", "info", "autopilot：门3 自动通过（storyboard_ready → rendered）", project_id=project_id)


def tick(db, data_dir, project_id) -> dict:
    """执行一轮决策。返回 {"action": ...}（wait/None 时仅返回）。"""
    act = next_action(db, data_dir, project_id)
    if act is None:
        return {"action": "none"}
    action = act["action"]
    if action == "analyze":
        from .pipeline_jobs import enqueue_llm_job
        enqueue_llm_job(db, "split_storyboards", project_id=project_id, payload={"project_id": project_id})
    elif action == "gen_refs":
        from .jobs import enqueue_job
        n = 0
        for a in list_project_assets(db, project_id):
            views = data_to_abs(data_dir, a["library_dir"]) / "views"
            if any(views.glob(f"*{e}") for e in IMAGE_EXTS):
                continue
            enqueue_job(db, "gen_ref", project_id=project_id, asset_id=a["id"],
                        resource="gpu_comfy", payload={"asset_id": a["id"]})
            n += 1
        emit_log(db, "autopilot", "info", f"autopilot 入队 {n} 张参考图", project_id=project_id)
    elif action == "split":
        from .pipeline_jobs import enqueue_llm_job
        enqueue_llm_job(db, "split_storyboards", project_id=project_id, payload={"project_id": project_id})
    elif action == "gen_prompts":
        from .pipeline_jobs import enqueue_llm_job
        conn = db.connect()
        queued = {r["shot_id"] for r in conn.execute(
            "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_prompt' AND shot_id IS NOT NULL AND status IN ('pending','running')")}
        n = 0
        from .shots import list_shots
        for s in list_shots(db, project_id):
            if (s["prompt"] or "").strip() or s["id"] in queued:
                continue
            enqueue_llm_job(db, "gen_prompt", project_id=project_id, shot_id=s["id"],
                              payload={"shot_id": s["id"]})
            n += 1
        emit_log(db, "autopilot", "info", f"autopilot 入队 {n} 条提示词生成", project_id=project_id)
    elif action == "render":
        from .jobs import enqueue_job
        from .rendershot import pick_template_id
        conn = db.connect()
        queued = {r["shot_id"] for r in conn.execute(
            "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_shot' AND shot_id IS NOT NULL AND status IN ('pending','running')")}
        n = 0
        from .shots import list_shots
        for s in list_shots(db, project_id):
            if s["video_path"] or s["id"] in queued:
                continue
            enqueue_job(db, "gen_shot", project_id=project_id, shot_id=s["id"],
                        resource="gpu_comfy",
                        payload={"shot_id": s["id"], "template": pick_template_id(s)})
            n += 1
        emit_log(db, "autopilot", "info", f"autopilot 入队 {n} 镜渲染", project_id=project_id)
    elif action == "merge":
        from .jobs import enqueue_job
        enqueue_job(db, "merge", project_id=project_id, payload={"project_id": project_id})
    elif action in ("gate1", "gate3"):
        _pass_gate(db, data_dir, project_id, int(action[-1]))
    elif action == "gate2":
        emit_log(db, "autopilot", "info", "autopilot：门2 条件已满足（提示词齐全）", project_id=project_id)
    return act


@register("autopilot_ping")
def _ping(db, data_dir, job, comfy):
    """占位 handler（autopilot 由巡检线程驱动，不进 job 队列）。"""
