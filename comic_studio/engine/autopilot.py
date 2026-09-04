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


def _missing_prompt_shot_ids(db, project_id) -> list:
    """缺提示词的生效镜 id 列表（2026-09-04 设计A2：逐镜失败守卫需要镜级粒度）。"""
    conn = db.connect()
    return [r["id"] for r in conn.execute(
        "SELECT id FROM shots WHERE project_id=? AND disabled=0 "
        "AND (prompt IS '' OR prompt='' OR TRIM(prompt)='') ORDER BY seq",
        (project_id,)).fetchall()]


def _shots_missing_video_ids(db, project_id) -> set:
    conn = db.connect()
    return {r["id"] for r in conn.execute(
        "SELECT id FROM shots WHERE project_id=? AND disabled=0 "
        "AND video_path IS NULL", (project_id,)).fetchall()}


def _latest_failed(db, project_id, jtype) -> bool:
    """批次型失败守卫（泛化 2026-08-25 analyze 模式）：最新 job 已 failed
    且无在飞 → 不自动重烧，等手动重发解除。"""
    last = jobs_mod.latest_job(db, project_id, jtype)
    return last is not None and last["status"] == "failed"


def _failed_shot_ids(db, project_id, jtype) -> set:
    """逐镜失败守卫：该镜此类型最新 job 已 failed → 暂不重烧该镜（其余镜照常推进）。
    最新状态按 job id 序取每镜最后一条；有更新的 pending/running 自然覆盖。"""
    rows = db.connect().execute(
        "SELECT shot_id, status FROM jobs WHERE project_id=? AND type=? "
        "AND shot_id IS NOT NULL ORDER BY id", (project_id, jtype)).fetchall()
    latest = {r["shot_id"]: r["status"] for r in rows}  # 后写覆盖=每镜最新
    return {sid for sid, st in latest.items() if st == "failed"}


def _prompt_gap(db, project_id) -> dict | None:
    """缺提示词时的决策（novel 两阶段共用）：在飞→wait；部分失败→跳过失败镜
    继续入队；全部失败→wait 等手动重发。返回 None=不缺。"""
    missing = _missing_prompt_shot_ids(db, project_id)
    if not missing:
        return None
    if _has_active_job(db, project_id, "gen_prompt"):
        return {"action": "wait", "detail": "提示词生成中"}
    stuck = set(missing) & _failed_shot_ids(db, project_id, "gen_prompt")
    if stuck and len(stuck) == len(missing):
        return {"action": "wait",
                "detail": f"提示词生成失败 {len(stuck)} 条，重试请手动发起"}
    if stuck:
        return {"action": "gen_prompts",
                "detail": f"缺 {len(missing) - len(stuck)} 条提示词（另 {len(stuck)} 条失败待手动）"}
    return {"action": "gen_prompts", "detail": f"缺 {len(missing)} 条提示词"}


def _render_gap(db, project_id) -> dict | None:
    """渲染决策（两流程共用）：缺视频→render（跳过失败镜）；全有视频但
    gen_shot 在飞（重渲染）→ wait（2026-09-04 设计A3 竞态：门3 别抢在
    重渲染前过）。返回 None=可过门3。"""
    if not _all_shots_have_video(db, project_id):
        if _has_active_job(db, project_id, "gen_shot"):
            return {"action": "wait", "detail": "渲染中"}
        missing = _shots_missing_video_ids(db, project_id)
        stuck = missing & _failed_shot_ids(db, project_id, "gen_shot")
        if stuck and len(stuck) == len(missing):
            return {"action": "wait",
                    "detail": f"渲染失败 {len(stuck)} 镜，重试请手动发起"}
        if stuck:
            return {"action": "render", "detail": f"批量渲染（{len(stuck)} 镜失败待手动）"}
        return {"action": "render", "detail": "批量渲染"}
    if _has_active_job(db, project_id, "gen_shot"):
        return {"action": "wait", "detail": "重渲染在飞，收尾后过门3"}
    return None


def _merge_gap(db, project_id) -> dict | None:
    """合成前检查：merge 在飞→wait；gen_shot 在飞（重渲染）→ wait 等收尾
    （防旧视频拼进成片）；上次 merge 失败 → wait 不自动重烧。None=可合成。"""
    if _has_active_job(db, project_id, "merge"):
        return {"action": "wait", "detail": "合成中"}
    if _has_active_job(db, project_id, "gen_shot"):
        return {"action": "wait", "detail": "重渲染在飞，收尾后再合成"}
    if _latest_failed(db, project_id, "merge"):
        return {"action": "wait", "detail": "上次合成失败，重试请手动发起"}
    return None


def _all_shots_have_video(db, project_id) -> bool:
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=? AND disabled=0",
                         (project_id,)).fetchone()["c"]
    done = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=? AND disabled=0 "
                        "AND video_path IS NOT NULL",
                        (project_id,)).fetchone()["c"]
    return total > 0 and total == done


def next_action(db, data_dir, project_id) -> dict:
    """纯决策：按项目类型分发到对应流程。返回 {"action": str, "detail": str} 或 None。"""
    proj = get_project(db, project_id)
    if proj is None:
        return None
    _cm = proj["comic_mode"] if "comic_mode" in proj.keys() else ""
    if _cm in ("motion_comic", "film_adaptation"):
        return _comic_flow(db, data_dir, project_id, proj)
    return _novel_flow(db, data_dir, project_id, proj)


def _novel_flow(db, data_dir, project_id, proj) -> dict:
    """小说/主题项目流程：
    created → analyze → analyzed → gen_refs → gate1 → assets_ready
    → split → gen_prompts → gate2 → storyboard_ready
    → render → gate3 → rendered → merge → merged"""
    stage = proj["stage"]
    if stage == "merged":
        return {"action": "done", "detail": "已成片"}
    if stage == "created":
        if _has_active_job(db, project_id, "analyze"):
            return {"action": "wait", "detail": "分析进行中"}
        last = jobs_mod.latest_job(db, project_id, "analyze")
        if last is not None and last["status"] == "failed":
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
            if _latest_failed(db, project_id, "split_storyboards"):
                return {"action": "wait", "detail": "上次分镜拆解失败，重试请手动发起"}
            return {"action": "split", "detail": "开始分镜拆解"}
        gap = _prompt_gap(db, project_id)
        if gap:
            return gap
        return {"action": "gate2", "detail": "提示词齐全，过门2"}
    if stage == "storyboard_ready":
        total = db.connect().execute("SELECT COUNT(*) c FROM shots WHERE project_id=?",
                                     (project_id,)).fetchone()["c"]
        if total == 0:
            if _has_active_job(db, project_id, "split_storyboards"):
                return {"action": "wait", "detail": "分镜拆解中"}
            if _latest_failed(db, project_id, "split_storyboards"):
                return {"action": "wait", "detail": "上次分镜拆解失败，重试请手动发起"}
            return {"action": "split", "detail": "无分镜，先拆解"}
        gap = _prompt_gap(db, project_id)
        if gap:
            return gap
        if _has_active_job(db, project_id, "gen_prompt"):
            return {"action": "wait", "detail": "提示词生成中"}  # 重生成在飞（stale）
        rgap = _render_gap(db, project_id)
        if rgap:
            return rgap
        return {"action": "gate3", "detail": "全部有视频，过门3"}
    if stage == "rendered":
        mgap = _merge_gap(db, project_id)
        if mgap:
            return mgap
        return {"action": "merge", "detail": "开始合成成片"}
    return {"action": "wait", "detail": f"未知阶段 {stage}"}


def _comic_flow(db, data_dir, project_id, proj) -> dict:
    """漫画项目流程（P9 2026-08-29 重构：读图顺便提取角色）：
    导入时直达 storyboard_ready
    → describe_shots（VLM 读所有页 + 顺手提取角色 + 生成提示词 + 绑定）
    → [漫改] gen_refs（角色参考图）
    → gate2 → render（fl2v 翻页 或 ref2va 角色动画）
    → gate3 → rendered → TTS+字幕 → merge → merged"""
    stage = proj["stage"]
    _cm = proj["comic_mode"] if "comic_mode" in proj.keys() else ""
    is_film = _cm == "film_adaptation"

    if stage == "merged":
        return {"action": "done", "detail": "已成片"}
    if stage == "storyboard_ready":
        total = db.connect().execute("SELECT COUNT(*) c FROM shots WHERE project_id=?",
                                     (project_id,)).fetchone()["c"]
        if total == 0:
            return {"action": "wait", "detail": "无分镜（漫画导入异常）"}

        # ① VLM 读图（含角色提取——2026-08-29 用户需求重构：一遍完成）
        missing = _missing_prompt_shot_ids(db, project_id)
        if missing:
            if _has_active_job(db, project_id, "describe_shots"):
                return {"action": "wait", "detail": f"VLM 读图+角色提取中（缺 {len(missing)} 镜）"}
            if _latest_failed(db, project_id, "describe_shots"):
                return {"action": "wait", "detail": "上次 VLM 读图失败，重试请手动发起"}
            return {"action": "describe_shots",
                    "detail": f"缺 {len(missing)} 条提示词（VLM 读图+提取角色）"}
        if _has_active_job(db, project_id, "describe_shots"):
            return {"action": "wait", "detail": "VLM 读图收尾中"}

        # ② 漫改模式：角色参考图（读图提取的角色可能还没有图）
        if is_film:
            from .assets import list_project_assets
            from .paths import data_to_abs
            missing_refs = []
            for a in list_project_assets(db, project_id):
                if a["kind"] == "character":
                    views = data_to_abs(data_dir, a["library_dir"]) / "views"
                    main = data_to_abs(data_dir, a["library_dir"]) / "main.png"
                    if not views.is_dir() and not main.exists():
                        missing_refs.append(a["id"])
            if missing_refs:
                if _has_active_job(db, project_id, "gen_ref"):
                    return {"action": "wait", "detail": f"角色参考图生成中（{len(missing_refs)} 个）"}
                return {"action": "gen_refs", "detail": f"漫改：生成 {len(missing_refs)} 个角色参考图"}

        # ③ 渲染 → 门3 → 合成（失败守卫/在飞感知同小说流 _render_gap/_merge_gap）
        rgap = _render_gap(db, project_id)
        if rgap:
            return rgap
        return {"action": "gate3", "detail": "全部有视频，过门3"}
    if stage == "rendered":
        mgap = _merge_gap(db, project_id)
        if mgap:
            return mgap
        return {"action": "merge", "detail": "开始合成成片"}
    return {"action": "wait", "detail": f"漫画项目不应处于阶段 {stage}"}


_STUCK_REPORTED: dict = {}  # project_id → 已上报的卡死 detail（一次性 error，3s 巡检不刷屏）


def tick(db, data_dir, project_id) -> dict:
    """执行一轮决策。返回 {"action": ...}（wait/None 时仅返回）。"""
    act = next_action(db, data_dir, project_id)
    if act is None:
        return {"action": "none"}
    action = act["action"]
    # 失败守卫卡死（2026-09-04 设计A2）：detail 带「失败」的 wait 首次出现报一条
    # error 日志；手动重发后决策不再卡死 → 键清除（再卡重新上报）
    if action == "wait" and "失败" in (act.get("detail") or ""):
        if _STUCK_REPORTED.get(project_id) != act["detail"]:
            emit_log(db, "autopilot", "error",
                     f"autopilot 卡住：{act['detail']}（手动处理后可续跑）",
                     project_id=project_id)
            _STUCK_REPORTED[project_id] = act["detail"]
        return act
    _STUCK_REPORTED.pop(project_id, None)
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
        try:
            from .settings import ensure_comfy_configured
            ensure_comfy_configured(db)  # 配置门禁（2026-09-01 事故）：空地址不入队
        except ValueError as exc:
            emit_log(db, "autopilot", "error", f"autopilot 跳过参考图入队：{exc}",
                     project_id=project_id)
            return act
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
    elif action == "extract_comic_characters":
        # 漫改模式：VLM 提取角色资产
        from .jobs import enqueue_job
        enqueue_job(db, "extract_comic_characters", project_id=project_id,
                    resource="gpu_llm_local", payload={"project_id": project_id})
        emit_log(db, "autopilot", "info", "autopilot 入队角色提取（漫改模式）",
                 project_id=project_id)
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
        failed = _failed_shot_ids(db, project_id, "gen_prompt")  # 失败镜不重烧
        n = 0
        from .shots import list_shots
        for s in list_shots(db, project_id):
            if (s["disabled"] or (s["prompt"] or "").strip()
                    or s["id"] in queued or s["id"] in failed):
                continue
            enqueue_llm_job(db, "gen_prompt", project_id=project_id, shot_id=s["id"],
                              payload={"shot_id": s["id"]})
            n += 1
        emit_log(db, "autopilot", "info", f"autopilot 入队 {n} 条提示词生成", project_id=project_id)
    elif action == "render":
        from .jobs import enqueue_job
        from .rendershot import pick_template_id
        try:
            from .settings import ensure_comfy_configured
            ensure_comfy_configured(db)  # 配置门禁（2026-09-01 事故）：空地址不入队
        except ValueError as exc:
            emit_log(db, "autopilot", "error", f"autopilot 跳过渲染入队：{exc}",
                     project_id=project_id)
            return act
        conn = db.connect()
        queued = {r["shot_id"] for r in conn.execute(
            "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_shot' AND shot_id IS NOT NULL AND status IN ('pending','running')")}
        failed = _failed_shot_ids(db, project_id, "gen_shot")  # 失败镜不重烧
        n = 0
        from .shots import list_shots
        for s in list_shots(db, project_id):
            if (s["disabled"] or s["video_path"]
                    or s["id"] in queued or s["id"] in failed):
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
