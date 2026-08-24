# comic_studio/web/routes_projects.py
"""项目 REST：创建（上传小说）、列表、详情。"""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..engine.projects import create_project, get_project, list_projects

router = APIRouter(prefix="/api/projects", tags=["projects"])

_PUBLIC_COLUMNS = ("id", "slug", "name", "aspect_ratio", "stage", "created_at", "style",
                    "video_megapixels", "video_multiple", "video_speed", "default_shot_duration",
                    "prompt_mode", "lora_realism", "autopilot")


def _public(row) -> dict:
    return {k: row[k] for k in _PUBLIC_COLUMNS}


@router.post("", status_code=201)
def create(request: Request, name: str = Form(...),
           aspect_ratio: str = Form(...), novel: UploadFile = File(...),
           style: str = Form(""), video_megapixels: float = Form(0.4),
           video_multiple: int = Form(32), video_speed: str = Form("标准"),
           default_shot_duration: float = Form(5.0),
           prompt_mode: str = Form("D"), lora_realism: float = Form(0.75)):
    if aspect_ratio not in ("9:16", "16:9"):
        raise HTTPException(422, "aspect_ratio 只能是 9:16 或 16:9")
    try:
        text = novel.file.read().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "小说文件需为 UTF-8 编码（请转换后重新上传）")
    row = create_project(request.app.state.db, request.app.state.data_dir,
                         name, aspect_ratio, text, style=style,
                         video_megapixels=video_megapixels, video_multiple=video_multiple,
                         video_speed=video_speed, default_shot_duration=default_shot_duration,
                         prompt_mode=prompt_mode, lora_realism=lora_realism)
    return _public(row)


@router.get("")
def listing(request: Request):
    out = []
    for r in list_projects(request.app.state.db):
        item = _public(r)
        if r["autopilot"]:
            from ..engine.autopilot import next_action
            item["autopilot_action"] = next_action(
                request.app.state.db, request.app.state.data_dir, r["id"])
        out.append(item)
    return out


@router.get("/{project_id}")
def detail(request: Request, project_id: int):
    row = get_project(request.app.state.db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    out = _public(row)
    if row["autopilot"]:
        from ..engine.autopilot import next_action
        out["autopilot_action"] = next_action(
            request.app.state.db, request.app.state.data_dir, project_id)
    return out


@router.patch("/{project_id}")
def patch_style(request: Request, project_id: int, body: dict):
    from pydantic import BaseModel

    class StylePatch(BaseModel):
        style: str = ""

    db = request.app.state.db
    row = get_project(db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")

    # Handle style parameter
    if "style" in body:
        patch = StylePatch.model_validate(body)
        conn = db.connect()
        conn.execute("UPDATE projects SET style=? WHERE id=?", (patch.style.strip(), project_id))
        conn.commit()

    # Handle autopilot switch (一键出片)
    if "autopilot" in body:
        on = 1 if body["autopilot"] else 0
        conn = db.connect()
        conn.execute("UPDATE projects SET autopilot=? WHERE id=?", (on, project_id))
        conn.commit()

    # Handle video parameters (composable with style)
    if any(k in body for k in ("video_megapixels", "video_multiple", "video_speed", "default_shot_duration", "prompt_mode", "lora_realism")):
        try:
            from ..engine.projects import update_video_params
            kwargs = {}
            if "video_megapixels" in body:
                kwargs["video_megapixels"] = body["video_megapixels"]
            if "video_multiple" in body:
                kwargs["video_multiple"] = body["video_multiple"]
            if "video_speed" in body:
                kwargs["video_speed"] = body["video_speed"]
            if "default_shot_duration" in body:
                kwargs["default_shot_duration"] = body["default_shot_duration"]
            if "prompt_mode" in body:
                kwargs["prompt_mode"] = body["prompt_mode"]
            if "lora_realism" in body:
                kwargs["lora_realism"] = body["lora_realism"]
            row = update_video_params(db, project_id, **kwargs)
        except ValueError as e:
            raise HTTPException(422, str(e))

    return _public(get_project(db, project_id))
