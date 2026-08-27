# comic_studio/web/routes_shots.py
"""分镜 REST：拆解发起/状态、列表、编辑、提示词重生/批量、门2（spec §5 门2）。"""
import json

from fastapi import APIRouter, Body, HTTPException, Request

from ..engine import jobs
from ..engine.jobs import enqueue_job
from ..engine.pipeline_gates import GATE_STAGES, GateStageError, gate_pass
from ..engine.pipeline_jobs import enqueue_llm_job
from ..engine.projects import get_project, set_stage
from ..engine.shots import (delete_shots_batch, get_shot, list_shots,
                            set_disabled_batch, update_shot)
from ..engine.logbus import emit as emit_log
from ..engine.rendershot import pick_template_id, shot_versions
from ..engine.projects import get_project

router = APIRouter(tags=["shots"])


def _last_job(db, shot_id, jtype):
    row = db.connect().execute(
        "SELECT status, started_at, finished_at FROM jobs "
        "WHERE shot_id=? AND type=? ORDER BY id DESC LIMIT 1",
        (shot_id, jtype)).fetchone()
    if row is None:
        return None
    elapsed = None
    if row["started_at"]:
        end = row["finished_at"] or "now"
        end = "datetime('now')" if end == "now" else f"'{end}'"
        q = f"SELECT CAST((julianday({end}) - julianday('{row['started_at']}')) * 86400 AS INTEGER) e"
        elapsed = db.connect().execute(q).fetchone()["e"]
    return {"status": row["status"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "elapsed_s": elapsed}


def _shot_public(r, versions=None, db=None, data_dir=None, slug=None):
    vp = r["video_path"]
    # fl2v 关键帧 URL（存在才有）
    kf_urls = {}
    if data_dir and slug:
        from ..engine.paths import data_to_abs as _dta
        shot_dir = _dta(data_dir, f"projects/{slug}/shots/{r['seq']}")
        for phase in ("start", "end"):
            kf = shot_dir / f"kf_{phase}.png"
            if kf.exists():
                kf_urls[f"kf_{phase}_url"] = (
                    f"/media/projects/{slug}/shots/{r['seq']}/kf_{phase}.png"
                    f"?v={int(kf.stat().st_mtime)}")
    return {"id": r["id"], "seq": r["seq"], "description": r["description"],
            "shot_type": r["shot_type"], "camera": json.loads(r["camera_json"] or "{}"),
            "ledger": json.loads(r["ledger_json"] or "{}"), "duration": r["duration"],
            "workflow_type": r["workflow_type"], "prompt": r["prompt"],
            "status": r["status"], "depends_on": r["depends_on"],
            "video_url": f"/media/{vp}" if vp else None,
            "disabled": bool(r["disabled"]),
            "versions": versions if versions is not None else [],
            "selected": vp.rsplit("/", 1)[-1] if vp else None,
            **kf_urls,
            "render_job": _last_job(db, r["id"], "gen_shot") if db is not None else None,
            "prompt_job": _last_job(db, r["id"], "gen_prompt") if db is not None else None}


@router.post("/api/projects/{project_id}/auto-bind")
def auto_bind(request: Request, project_id: int):
    """角色自动补绑：扫描描述文本，提到的角色补绑到 ledger（修已有项目漏绑）。"""
    from ..engine.llm.storyboard import auto_bind_characters
    n = auto_bind_characters(request.app.state.db, project_id)
    return {"bound": n}


@router.post("/api/projects/{project_id}/split-storyboards", status_code=202)
def start_split(request: Request, project_id: int, body: dict | None = Body(default=None)):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "assets_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能拆分镜（需 assets_ready）")
    running = jobs.latest_job(db, project_id, "split_storyboards")
    if running and running["status"] in ("pending", "running"):
        raise HTTPException(409, "分镜拆解正在进行中")
    body = body or {}
    target = body.get("target_count")
    if target is not None and (not isinstance(target, int) or target < 1):
        raise HTTPException(422, "target_count 需为 ≥1 的整数（不传则自动拆分）")
    payload = {"project_id": project_id}
    if target:
        payload["target_count"] = target
    cf, ct = body.get("chapter_from"), body.get("chapter_to")
    if cf is not None or ct is not None:
        try:
            cf, ct = int(cf or 1), int(ct or cf or 1)
        except (TypeError, ValueError):
            raise HTTPException(422, "chapter_from/chapter_to 需为整数章号")
        if cf < 1 or ct < cf:
            raise HTTPException(422, "章节范围非法（需 1 ≤ from ≤ to）")
        payload["chapter_range"] = [cf, ct]
    jid = enqueue_llm_job(db, "split_storyboards", project_id=project_id,
                          payload=payload)
    return {"job_id": jid}


@router.post("/api/projects/{project_id}/shots/batch")
def shots_batch(request: Request, project_id: int, body: dict = Body(...)):
    """批量处理分镜（2026-08-27 需求）：disable/enable 置无效/生效，delete 删除。"""
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    action = body.get("action")
    ids = body.get("ids") or []
    if action not in ("disable", "enable", "delete"):
        raise HTTPException(422, "action 只支持 disable/enable/delete")
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise HTTPException(422, "ids 需为整数数组")
    if not ids:
        return {"updated": 0, "deleted": 0}
    if action == "delete":
        n = delete_shots_batch(db, project_id, ids)
        emit_log(db, "storyboard", "info", f"批量删除分镜 {n} 条",
                 project_id=project_id)
        return {"deleted": n}
    n = set_disabled_batch(db, project_id, ids, 1 if action == "disable" else 0)
    label = "无效（渲染/合成将跳过）" if action == "disable" else "生效"
    emit_log(db, "storyboard", "info", f"批量置{label}分镜 {n} 条", project_id=project_id)
    return {"updated": n}


@router.get("/api/projects/{project_id}/split-storyboards/status")
def split_status(request: Request, project_id: int):
    row = jobs.latest_job(request.app.state.db, project_id, "split_storyboards")
    if row is None:
        raise HTTPException(404, "尚无拆解任务")
    return {"job_id": row["id"], "status": row["status"], "error": row["error"]}


@router.get("/api/projects/{project_id}/shots")
def listing(request: Request, project_id: int):
    proj = get_project(request.app.state.db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    return [_shot_public(
        r, versions=shot_versions(request.app.state.data_dir, proj["slug"], r["seq"]),
        db=request.app.state.db,
        data_dir=request.app.state.data_dir, slug=proj["slug"])
        for r in list_shots(request.app.state.db, project_id)]


@router.post("/api/shots/{shot_id}/version", status_code=200)
def select_version(request: Request, shot_id: int, body: dict = Body(...)):
    db = request.app.state.db
    shot = get_shot(db, shot_id)
    if shot is None:
        raise HTTPException(404, "分镜不存在")
    proj = get_project(db, shot["project_id"])
    versions = shot_versions(request.app.state.data_dir, proj["slug"], shot["seq"])
    file = str(body.get("file", ""))
    if file not in versions:
        raise HTTPException(422, f"版本不存在: {file}，可选 {versions}")
    rel = f"projects/{proj['slug']}/shots/{shot['seq']}/{file}"
    update_shot(db, shot_id, {"video_path": rel})
    return _shot_public(get_shot(db, shot_id), versions=versions)


@router.patch("/api/shots/{shot_id}")
def patch_shot(request: Request, shot_id: int, body: dict = Body(...)):
    db = request.app.state.db
    if get_shot(db, shot_id) is None:
        raise HTTPException(404, "分镜不存在")
    fields = {}
    if "camera" in body:
        fields["camera_json"] = json.dumps(body["camera"], ensure_ascii=False)
    if "duration" in body:
        v = body["duration"]
        if not isinstance(v, (int, float)) or v < 1 or v > 15:
            raise HTTPException(422, "duration 须为 1~15 的数字")
        fields["duration"] = v
    for k in ("description", "shot_type", "workflow_type"):
        if k in body:
            fields[k] = body[k]
    if "prompt" in body:
        fields["prompt"] = str(body["prompt"])
        fields["status"] = "ready" if str(body["prompt"]).strip() else "pending"
    if not fields:
        raise HTTPException(422, "无可更新字段")
    update_shot(db, shot_id, fields)
    return _shot_public(get_shot(db, shot_id))


@router.post("/api/shots/{shot_id}/regen-prompt", status_code=202)
def regen_prompt(request: Request, shot_id: int, body: dict | None = Body(default=None)):
    db = request.app.state.db
    shot = get_shot(db, shot_id)
    if shot is None:
        raise HTTPException(404, "分镜不存在")
    body = body or {}
    force = body.get("force")
    if shot["prompt"].strip() and not force:
        raise HTTPException(409, "已有提示词，force=true 才会重生")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='gen_prompt' AND shot_id=? "
        "AND status IN ('pending','running')", (shot_id,)).fetchone()
    if dup and not force:
        raise HTTPException(409, "该镜头提示词生成已在队列")
    jid = enqueue_llm_job(db, "gen_prompt", project_id=shot["project_id"],
                          shot_id=shot_id, payload={"shot_id": shot_id})
    return {"job_id": jid}


@router.post("/api/projects/{project_id}/generate-prompts", status_code=202)
def gen_batch(request: Request, project_id: int):
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    queued = {r["shot_id"] for r in db.connect().execute(
        "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_prompt' "
        "AND shot_id IS NOT NULL AND status IN ('pending','running')")}
    n = 0
    for s in list_shots(db, project_id):
        if (s["prompt"] or "").strip() or s["id"] in queued:
            continue
        enqueue_llm_job(db, "gen_prompt", project_id=project_id,
                        shot_id=s["id"], payload={"shot_id": s["id"]})
        n += 1
    return {"enqueued": n}


@router.post("/api/projects/{project_id}/gate2")
def gate2(request: Request, project_id: int):
    return _gate_resp(request, project_id, 2)


def _gate_resp(request: Request, project_id: int, n: int):
    """门2/3 转调 engine.gate_pass；409=阶段不符 / 422=缺件 / 404=无项目。"""
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    try:
        gate_pass(db, request.app.state.data_dir, project_id, n)
    except GateStageError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    _, to = GATE_STAGES[n]
    return {"stage": to}


@router.post("/api/shots/{shot_id}/render", status_code=202)
def render_shot(request: Request, shot_id: int, body: dict | None = Body(default=None)):
    db = request.app.state.db
    shot = get_shot(db, shot_id)
    if shot is None:
        raise HTTPException(404, "分镜不存在")
    if not (shot["prompt"] or "").strip():
        raise HTTPException(422, "提示词为空，无法渲染")
    body = body or {}
    force = body.get("force")
    if shot["disabled"]:
        raise HTTPException(422, "镜头已标无效（先在分镜列表恢复生效，或 force 批量渲染时不会包含它）")
    if shot["video_path"] and not force:
        raise HTTPException(409, "已有视频，force=true 才会重渲")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='gen_shot' AND shot_id=? "
        "AND status IN ('pending','running')", (shot_id,)).fetchone()
    if dup and not force:
        raise HTTPException(409, "该镜头渲染已在队列")
    jid = enqueue_job(db, "gen_shot", project_id=shot["project_id"],
                      shot_id=shot_id, resource="gpu_comfy",
                      payload={"shot_id": shot_id,
                               "template": pick_template_id(shot)})
    return {"job_id": jid}


@router.post("/api/projects/{project_id}/render", status_code=202)
def render_batch(request: Request, project_id: int):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "storyboard_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能渲染（需 storyboard_ready）")
    queued = {r["shot_id"] for r in db.connect().execute(
        "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_shot' "
        "AND shot_id IS NOT NULL AND status IN ('pending','running')")}
    n = 0
    skipped = 0
    skipped_disabled = 0
    for s in list_shots(db, project_id):
        if s["disabled"]:
            skipped_disabled += 1
            continue
        if not (s["prompt"] or "").strip():
            skipped += 1
            continue
        if s["video_path"] or s["id"] in queued:
            continue
        enqueue_job(db, "gen_shot", project_id=project_id, shot_id=s["id"],
                    resource="gpu_comfy", payload={"shot_id": s["id"],
                                            "template": pick_template_id(s)})
        n += 1
    result = {"enqueued": n}
    if skipped:
        result["skipped_no_prompt"] = skipped
    if skipped_disabled:
        result["skipped_disabled"] = skipped_disabled
    return result


@router.post("/api/projects/{project_id}/gate3")
def gate3(request: Request, project_id: int):
    return _gate_resp(request, project_id, 3)


@router.post("/api/projects/{project_id}/render-director", status_code=202)
def render_director(request: Request, project_id: int):
    """P7-D 整段快车道：全部生效镜打包一次提交导演台（段间 latent 连贯）。
    v1 产出整片直达 merged，不混 TTS/字幕。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "storyboard_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能整段渲染（需 storyboard_ready）")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='gen_director' AND project_id=? "
        "AND status IN ('pending','running')", (project_id,)).fetchone()
    if dup:
        raise HTTPException(409, "整段渲染已在队列")
    jid = enqueue_job(db, "gen_director", project_id=project_id,
                      resource="gpu_comfy", payload={"project_id": project_id})
    return {"job_id": jid}


@router.get("/api/projects/{project_id}/chapters")
def chapters_listing(request: Request, project_id: int):
    """P7-E：章节结构（创建时正则识别，供拆分镜按章选择）。"""
    proj = get_project(request.app.state.db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    chapters = json.loads(proj["chapters_json"] or "[]") if "chapters_json" in proj.keys() else []
    return [{"idx": c["idx"], "title": c["title"], "chars": c["end"] - c["start"]}
            for c in chapters]


@router.get("/api/jobs/{job_id}/snapshot")
def job_snapshot(request: Request, job_id: int):
    """P7-A 审计快照查看：任务实际提交的提示词与完整工作流 JSON。"""
    row = request.app.state.db.connect().execute(
        "SELECT id, type, status, snapshot_json FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    if not row["snapshot_json"]:
        raise HTTPException(404, "该任务无快照（旧任务或非 ComfyUI 任务）")
    return {"id": row["id"], "type": row["type"], "status": row["status"],
            "snapshot": json.loads(row["snapshot_json"])}
