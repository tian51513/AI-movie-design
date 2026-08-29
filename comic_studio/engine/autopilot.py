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
from .projects import get_project
from .queue.worker import register
from .settings import get_setting

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _has_active_job(db, project_id, jtype) -> bool:
    return db.connect().execute(
        "SELECT 1 FROM jobs WHERE project_id=? AND type=? AND status IN ('pending','running') LIMIT 1",
        (project_id, jtype)).fetchone() is not None


def _all_assets_have_sheets(db, data_dir, project_id) -> bool:
    from .pipeline_gates import has_views
    assets = list_project_assets(db, project_id)
    if not assets:
        return False
    for a in assets:
        views = data_to_abs(data_dir, a["library_dir"]) / "views"
        if not has_views(views):
            return False
    return True


def _shots_missing_prompt(db, project_id) -> int:
    conn = db.connect()
    return conn.execute(
        "SELECT COUNT(*) c FROM shots WHERE project_id=? AND disabled=0 "
        "AND (prompt IS '' OR prompt='' OR TRIM(prompt)='')",
        (project_id,)).fetchone()["c"]


def _all_shots_have_video(db, project_id) -> bool:
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=? AND disabled=0",
                         (project_id,)).fetchone()["c"]
    done = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=? AND disabled=0 "
                        "AND video_path IS NOT NULL",
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
        last = jobs_mod.latest_job(db, project_id, "analyze")
        if last is not None and last["status"] == "failed":
            # 失败不无限重烧（2026-08-25 真机：上下文爆掉后 autopilot 秒级重跑烧 token）
            return {"action": "wait", "detail": "上次分析失败，重试请手动发起"}
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
        has_shots = db.connect().execute(
            "SELECT 1 FROM shots WHERE project_id=? LIMIT 1",
            (project_id,)).fetchone() is not None
        if not has_shots:
            return {"action": "split", "detail": "开始分镜拆解"}
        # 桥接（2026-08-25 真机：拆完无桥接会无限重拆烧 token）：
        # 拆完 → 补提示词 → 齐全 → gate2（assets_ready → storyboard_ready）
        missing = _shots_missing_prompt(db, project_id)
        if missing > 0:
            if _has_active_job(db, project_id, "gen_prompt"):
                return {"action": "wait", "detail": "提示词生成中"}
            return {"action": "gen_prompts", "detail": f"缺 {missing} 条提示词"}
        return {"action": "gate2", "detail": "提示词齐全，过门2"}
    if stage == "storyboard_ready":
        total = db.connect().execute("SELECT COUNT(*) c FROM shots WHERE project_id=?",
                                     (project_id,)).fetchone()["c"]
        if total == 0:
            if _has_active_job(db, project_id, "split_storyboards"):
                return {"action": "wait", "detail": "分镜拆解中"}
            return {"action": "split", "detail": "无分镜，先拆解"}
        # P9 漫画感知（2026-08-29）：漫画项目提示词走 VLM 读图，不走文本 gen_prompt
        _cm = proj["comic_mode"] if "comic_mode" in proj.keys() else ""
        is_comic = _cm in ("motion_comic", "film_adaptation")
        prompt_job_type = "describe_shots" if is_comic else "gen_prompt"
        missing = _shots_missing_prompt(db, project_id)
        if missing > 0:
            if _has_active_job(db, project_id, prompt_job_type):
                return {"action": "wait", "detail": "提示词生成中" + ("（VLM 读图）" if is_comic else "")}
            if is_comic:
                return {"action": "describe_shots", "detail": f"缺 {missing} 条提示词（VLM 读图）"}
            return {"action": "gen_prompts", "detail": f"缺 {missing} 条提示词"}
        if _has_active_job(db, project_id, prompt_job_type):
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


def tick(db, data_dir, project_id) -> dict:
    """执行一轮决策。返回 {"action": ...}（wait/None 时仅返回）。"""
    act = next_action(db, data_dir, project_id)
    if act is None:
        return {"action": "none"}
    action = act["action"]
    if action == "done":
        # 全流程完成 → 自动关闭开关（真机 2026-08-25：完成后「停止自动」仍挂着）
        conn = db.connect()
        conn.execute("UPDATE projects SET autopilot=0 WHERE id=?", (project_id,))
        conn.commit()
        emit_log(db, "autopilot", "info", "autopilot：全流程完成，自动关闭", project_id=project_id)
        return act
    if action == "analyze":
        from .pipeline_jobs import enqueue_llm_job
        enqueue_llm_job(db, "analyze", project_id=project_id, payload={"project_id": project_id})
    elif action == "gen_refs":
        from .jobs import enqueue_job
        n = 0
        from .pipeline_gates import has_views
        for a in list_project_assets(db, project_id):
            views = data_to_abs(data_dir, a["library_dir"]) / "views"
            if has_views(views):
                continue
            enqueue_job(db, "gen_ref", project_id=project_id, asset_id=a["id"],
                        resource="gpu_comfy", payload={"asset_id": a["id"]})
            n += 1
        emit_log(db, "autopilot", "info", f"autopilot 入队 {n} 张参考图", project_id=project_id)
    elif action == "split":
        from .pipeline_jobs import enqueue_llm_job
        enqueue_llm_job(db, "split_storyboards", project_id=project_id, payload={"project_id": project_id})
    elif action == "describe_shots":
        # P9 漫画项目：VLM 读图生成提示词（一个 job 批量跑全部缺失的镜）
        from .jobs import enqueue_job
        enqueue_job(db, "describe_shots", project_id=project_id,
                    resource="gpu_llm_local", payload={"project_id": project_id})
        emit_log(db, "autopilot", "info", "autopilot 入队 VLM 读图（批量）",
                 project_id=project_id)
    elif action == "gen_prompts":
        from .pipeline_jobs import enqueue_llm_job
        conn = db.connect()
        queued = {r["shot_id"] for r in conn.execute(
            "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_prompt' AND shot_id IS NOT NULL AND status IN ('pending','running')")}
        n = 0
        from .shots import list_shots
        for s in list_shots(db, project_id):
            if s["disabled"] or (s["prompt"] or "").strip() or s["id"] in queued:
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
            if s["disabled"] or s["video_path"] or s["id"] in queued:
                continue
            enqueue_job(db, "gen_shot", project_id=project_id, shot_id=s["id"],
                        resource="gpu_comfy",
                        payload={"shot_id": s["id"], "template": pick_template_id(s)})
            n += 1
        emit_log(db, "autopilot", "info", f"autopilot 入队 {n} 镜渲染", project_id=project_id)
    elif action == "merge":
        # P6：合成前自动生成 TTS 配音 + SRT 字幕
        try:
            from .tts import generate_dialogue_audio
            from .subtitles import generate_srt
            audio_result = generate_dialogue_audio(db, data_dir, project_id)
            generate_srt(db, data_dir, project_id)
            if audio_result:
                emit_log(db, "autopilot", "info",
                         f"配音+字幕已生成（{len(audio_result)} 镜）",
                         project_id=project_id)
        except Exception as exc:
            emit_log(db, "autopilot", "warn",
                     f"TTS/字幕生成失败（{exc}），继续合成", project_id=project_id)
        from .jobs import enqueue_job
        enqueue_job(db, "merge", project_id=project_id, payload={"project_id": project_id})
    elif action.startswith("gate"):
        from .pipeline_gates import gate_pass
        try:
            gate_pass(db, data_dir, project_id, int(action[-1]), source="自动通过")
        except ValueError:
            pass  # 决策后瞬间条件失效（竞态）——留给下一轮 wait 循环
    return act


@register("autopilot_ping")
def _ping(db, data_dir, job, comfy):
    """占位 handler（autopilot 由巡检线程驱动，不进 job 队列）。"""
