# comic_studio/web/routes_refs.py
"""参考图生成/队列/视图/门1 接口（spec §5 门1、§8 队列）。"""
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile

from ..engine.assets import get_asset, list_project_assets
from ..engine.jobs import enqueue_job
from ..engine.paths import data_to_abs
from ..engine.pipeline_gates import GateStageError, gate_pass
from ..engine.projects import get_project
from ..engine.settings import get_setting

router = APIRouter(tags=["refs"])

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIEW_MEDIA_TYPES = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
                    ".mp4": "video", ".webm": "video", ".mov": "video",
                    ".mp3": "audio", ".wav": "audio", ".ogg": "audio"}


def _has_views(views_dir: Path) -> bool:
    """Check if views_dir contains any image files."""
    if not views_dir.is_dir():
        return False
    return any(f for ext in IMAGE_EXTS for f in views_dir.glob(f"*{ext}"))


@router.post("/api/assets/{asset_id}/main-image")
def upload_main_image(request: Request, asset_id: int,
                      file: UploadFile = File(...)):
    """人工上传主图（替换生成版，存 library/<dir>/main.png；jpg/webp 也存为
    main.png，ComfyUI 按内容读取）。上传后可点「三视图」从新主图重派生。"""
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(422, f"只接受图片文件（png/jpg/jpeg/webp），得到 {ext or '无后缀'}")
    data = file.file.read(20 * 1024 * 1024 + 1)  # 上限 20MB（防超大文件塞盘）
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(422, "图片超过 20MB 上限")
    dest = data_to_abs(request.app.state.data_dir, asset["library_dir"]) / "main.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    from ..engine.logbus import emit as emit_log
    emit_log(db, "comfy", "info", f"资产「{asset['name']}」主图已人工上传",
             project_id=asset["source_project"])
    return {"path": f"{asset['library_dir']}/main.png"}
@router.post("/api/assets/{asset_id}/gen", status_code=202)
def gen_asset(request: Request, asset_id: int, body: dict | None = Body(default=None)):
    """重新生成参考图。stage：all=主图+三视图两段（默认）；main=仅主图；
    views=仅从现有主图重派生三视图。"""
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    stage = (body or {}).get("stage") or "all"
    if stage not in ("all", "main", "views"):
        raise HTTPException(422, "stage 只能是 all/main/views")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='gen_ref' AND asset_id=? AND status IN ('pending','running')",
        (asset_id,)).fetchone()
    if dup:
        raise HTTPException(409, "该资产的参考图生成已在队列中")
    jid = enqueue_job(db, "gen_ref", project_id=asset["source_project"],
                      asset_id=asset_id, resource="gpu_comfy",
                      payload={"asset_id": asset_id, "stage": stage})
    return {"job_id": jid}


@router.post("/api/projects/{project_id}/generate-refs", status_code=202)
def gen_batch(request: Request, project_id: int):
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    queued = {r["asset_id"] for r in db.connect().execute(
        "SELECT DISTINCT asset_id FROM jobs WHERE type='gen_ref' "
        "AND asset_id IS NOT NULL AND status IN ('pending','running')")}
    n = 0
    for a in list_project_assets(db, project_id):
        views = data_to_abs(request.app.state.data_dir, a["library_dir"]) / "views"
        if _has_views(views) or a["id"] in queued:
            continue
        enqueue_job(db, "gen_ref", project_id=project_id, asset_id=a["id"],
                    resource="gpu_comfy", payload={"asset_id": a["id"]})
        n += 1
    return {"enqueued": n}


@router.delete("/api/projects/{project_id}/queue")
def clear_queue(request: Request, project_id: int):
    """一键清空队列/取消任务：pending/running → cancelled；
    running 的 gen_shot 先向 ComfyUI 发 /interrupt（掐断在跑的渲染）。"""
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    running_shots = db.connect().execute(
        "SELECT id FROM jobs WHERE project_id=? AND status='running' "
        "AND type='gen_shot'", (project_id,)).fetchall()
    if running_shots:
        base_url = (get_setting(db, "comfy") or {}).get("base_url")
        if base_url:
            from ..engine.comfy.client import ComfyClient
            try:
                ComfyClient(base_url).interrupt()
            except Exception:
                pass  # ComfyUI 不可达也不阻塞取消（本地行已无效）
    conn = db.connect()
    cur = conn.execute(
        "UPDATE jobs SET status='cancelled', error='手动取消', "
        "finished_at=datetime('now') "
        "WHERE project_id=? AND status IN ('pending','running')", (project_id,))
    conn.commit()
    from ..engine.logbus import emit as emit_log
    emit_log(db, "system", "warn", f"手动清空队列：取消 {cur.rowcount} 个任务",
             project_id=project_id)
    return {"cancelled": cur.rowcount}


@router.get("/api/projects/{project_id}/queue")
def queue_status(request: Request, project_id: int):
    db = request.app.state.db
    conn = db.connect()
    counts = {"running": 0, "pending": 0, "failed": 0}
    for r in conn.execute("SELECT status, COUNT(*) c FROM jobs WHERE project_id=? "
                          "GROUP BY status", (project_id,)):
        if r["status"] in counts:
            counts[r["status"]] = r["c"]
    jobs = [{"id": r["id"], "type": r["type"], "status": r["status"], "error": r["error"],
             "asset_id": r["asset_id"]} for r in conn.execute(
        "SELECT * FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 20", (project_id,))]
    comfy_ok = False
    try:
        from ..engine.comfy.client import ComfyClient
        ComfyClient(get_setting(db, "comfy")["base_url"], timeout=2).health()
        comfy_ok = True
    except Exception:
        pass
    return {**counts, "jobs": jobs, "comfy_ok": comfy_ok}


@router.get("/api/assets/{asset_id}/views")
def views(request: Request, asset_id: int):
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    lib_abs = data_to_abs(request.app.state.data_dir, asset["library_dir"])
    views_dir = lib_abs / "views"
    # library_dir 形如 "library/characters/3"，静态挂载根即 library/，
    # URL 需去掉前导 "library/" 避免 /library/library/...
    rel = asset["library_dir"]
    rel = rel[len("library/"):] if rel.startswith("library/") else rel
    out = []
    main = lib_abs / "main.png"
    if main.exists():  # 主图（两段式第一段/人工上传）——列表首位展示
        out.append({"name": "主图 main", "type": "image",
                    "url": f"/library/{rel}/main.png?v={int(main.stat().st_mtime)}"})
    if views_dir.is_dir():
        for f in sorted(views_dir.iterdir()):
            if f.suffix.lower() in VIEW_MEDIA_TYPES:
                out.append({"name": f.stem, "type": VIEW_MEDIA_TYPES[f.suffix.lower()],
                            "url": f"/library/{rel}/views/{f.name}?v={int(f.stat().st_mtime)}"})
    return out


@router.post("/api/projects/{project_id}/gate1")
def gate1(request: Request, project_id: int):
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    try:
        gate_pass(db, request.app.state.data_dir, project_id, 1)
    except GateStageError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"stage": "assets_ready"}


@router.post("/api/shots/{shot_id}/keyframe")
def upload_keyframe(request: Request, shot_id: int,
                    file: UploadFile = File(...), phase: str = Query("start")):
    """人工上传关键帧（phase=start/end → kf_start.png / kf_end.png）。"""
    from ..engine.shots import get_shot as _gs
    from ..engine.projects import get_project as _gp
    db = request.app.state.db
    shot = _gs(db, shot_id)
    if shot is None:
        raise HTTPException(404, "分镜不存在")
    if phase not in ("start", "end"):
        raise HTTPException(422, "phase 只能是 start/end")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(422, "只接受图片文件")
    proj = _gp(db, shot["project_id"])
    dest = data_to_abs(request.app.state.data_dir,
                       f"projects/{proj['slug']}/shots/{shot['seq']}") / f"kf_{phase}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = file.file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(422, "图片超过 20MB 上限")
    dest.write_bytes(data)
    from ..engine.logbus import emit as emit_log
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 关键帧（{phase}）已人工上传",
             project_id=shot["project_id"])
    return {"path": str(dest.relative_to(request.app.state.data_dir))}


@router.post("/api/shots/{shot_id}/regen-keyframes", status_code=202)
def regen_keyframes(request: Request, shot_id: int):
    """触发关键帧重新生成（走队列，角色主图锚定 + 前镜尾帧参考）。"""
    from ..engine.shots import get_shot as _gs
    db = request.app.state.db
    shot = _gs(db, shot_id)
    if shot is None:
        raise HTTPException(404, "分镜不存在")
    # 删旧关键帧让 ensure_keyframes 重生成
    from ..engine.projects import get_project as _gp
    from ..engine.paths import data_to_abs as _dta
    proj = _gp(db, shot["project_id"])
    kd = _dta(request.app.state.data_dir,
              f"projects/{proj['slug']}/shots/{shot['seq']}")
    for f in ("kf_start.png", "kf_end.png"):
        (kd / f).unlink(missing_ok=True)
    # 入队 gen_shot 任务（render 时自动补关键帧）
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "gen_shot", project_id=shot["project_id"],
                      shot_id=shot_id, resource="gpu_comfy",
                      payload={"shot_id": shot_id})
    return {"job_id": jid}
