# comic_studio/web/routes_refs.py
"""参考图生成/队列/视图/门1 接口（spec §5 门1、§8 队列）。"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

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


@router.post("/api/assets/{asset_id}/gen", status_code=202)
def gen_asset(request: Request, asset_id: int):
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='gen_ref' AND asset_id=? AND status IN ('pending','running')",
        (asset_id,)).fetchone()
    if dup:
        raise HTTPException(409, "该资产的参考图生成已在队列中")
    jid = enqueue_job(db, "gen_ref", project_id=asset["source_project"],
                      asset_id=asset_id, resource="gpu_comfy",
                      payload={"asset_id": asset_id})
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
    views_dir = data_to_abs(request.app.state.data_dir, asset["library_dir"]) / "views"
    out = []
    if views_dir.is_dir():
        for f in sorted(views_dir.iterdir()):
            if f.suffix.lower() in VIEW_MEDIA_TYPES:
                # library_dir 形如 "library/characters/3"，静态挂载根即 library/，
                # URL 需去掉前导 "library/" 避免 /library/library/...
                rel = asset["library_dir"]
                rel = rel[len("library/"):] if rel.startswith("library/") else rel
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
