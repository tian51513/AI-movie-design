# comic_studio/web/routes_shots.py
"""分镜 REST：拆解发起/状态、列表、编辑、提示词重生/批量、门2（spec §5 门2）。"""
import json

from fastapi import APIRouter, Body, HTTPException, Request

from ..engine import jobs
from ..engine.jobs import enqueue_job
from ..engine.pipeline_jobs import enqueue_llm_job
from ..engine.projects import get_project, set_stage
from ..engine.shots import get_shot, list_shots, update_shot
from ..engine.logbus import emit as emit_log
from ..engine.rendershot import pick_template_id

router = APIRouter(tags=["shots"])


def _shot_public(r):
    vp = r["video_path"]
    return {"id": r["id"], "seq": r["seq"], "description": r["description"],
            "shot_type": r["shot_type"], "camera": json.loads(r["camera_json"] or "{}"),
            "ledger": json.loads(r["ledger_json"] or "{}"), "duration": r["duration"],
            "workflow_type": r["workflow_type"], "prompt": r["prompt"],
            "status": r["status"], "depends_on": r["depends_on"],
            "video_url": f"/media/{vp}" if vp else None}


@router.post("/api/projects/{project_id}/split-storyboards", status_code=202)
def start_split(request: Request, project_id: int):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "assets_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能拆分镜（需 assets_ready）")
    running = jobs.latest_job(db, project_id, "split_storyboards")
    if running and running["status"] in ("pending", "running"):
        raise HTTPException(409, "分镜拆解正在进行中")
    jid = enqueue_llm_job(db, "split_storyboards", project_id=project_id,
                          payload={"project_id": project_id})
    return {"job_id": jid}


@router.get("/api/projects/{project_id}/split-storyboards/status")
def split_status(request: Request, project_id: int):
    row = jobs.latest_job(request.app.state.db, project_id, "split_storyboards")
    if row is None:
        raise HTTPException(404, "尚无拆解任务")
    return {"job_id": row["id"], "status": row["status"], "error": row["error"]}


@router.get("/api/projects/{project_id}/shots")
def listing(request: Request, project_id: int):
    if get_project(request.app.state.db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    return [_shot_public(r) for r in list_shots(request.app.state.db, project_id)]


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
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "assets_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能过门2（需 assets_ready）")
    shots = list_shots(db, project_id)
    if not shots:
        raise HTTPException(422, "尚无分镜，请先拆分镜")
    missing = [s["seq"] for s in shots if not (s["prompt"] or "").strip()]
    if missing:
        raise HTTPException(422, f"缺提示词的镜头: {missing}")
    set_stage(db, project_id, "storyboard_ready")
    emit_log(db, "system", "info", "阶段流转 assets_ready → storyboard_ready（门2 确认）",
             project_id=project_id)
    return {"stage": "storyboard_ready"}


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
    for s in list_shots(db, project_id):
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
    return result


@router.post("/api/projects/{project_id}/gate3")
def gate3(request: Request, project_id: int):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "storyboard_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能过门3（需 storyboard_ready）")
    shots = list_shots(db, project_id)
    if not shots:
        raise HTTPException(422, "尚无分镜，请先拆分镜")
    missing = [s["seq"] for s in shots if not s["video_path"]]
    if missing:
        raise HTTPException(422, f"缺视频的镜头: {missing}")
    set_stage(db, project_id, "rendered")
    emit_log(db, "system", "info", "阶段流转 storyboard_ready → rendered（门3 确认）",
             project_id=project_id)
    return {"stage": "rendered"}
