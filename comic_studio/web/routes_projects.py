# comic_studio/web/routes_projects.py
"""项目 REST：创建（上传小说）、列表、详情。"""
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..engine.projects import create_project, get_project, list_projects

router = APIRouter(prefix="/api/projects", tags=["projects"])

_PUBLIC_COLUMNS = ("id", "slug", "name", "aspect_ratio", "stage", "created_at", "style", "era",
                    "video_megapixels", "video_multiple", "video_speed", "default_shot_duration",
                    "prompt_mode", "lora_realism", "autopilot")


@router.delete("/{project_id}")
def delete_project(request: Request, project_id: int):
    """删除项目：行（jobs/shots/关联/日志/项目）+ 磁盘 projects/<slug>/ 全清；
    在跑渲染发 interrupt；全局资产库（data/library）保留——其他项目可能复用。"""
    db = request.app.state.db
    row = get_project(db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    running = db.connect().execute(
        "SELECT 1 FROM jobs WHERE project_id=? AND status='running' "
        "AND type='gen_shot' LIMIT 1", (project_id,)).fetchone()
    if running:
        from ..engine.settings import get_setting
        base_url = (get_setting(db, "comfy") or {}).get("base_url")
        if base_url:
            from ..engine.comfy.client import ComfyClient
            try:
                ComfyClient(base_url).interrupt()
            except Exception:
                pass  # ComfyUI 不可达不阻塞删除
    conn = db.connect()
    try:
        # 删除顺序按外键依赖：logs(job_id→jobs) 先于 jobs；
        # jobs(shot_id→shots) 先于 shots；shots 自引用链按叶子序（见下）
        conn.execute("DELETE FROM logs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        # 镜间接力链：先删叶子（无人 depends_on 它的镜）再循环——单条 DELETE 会被
        # 自引用 FK 逐行检查卡住（真机 2026-08-25 Internal Server Error）
        for _ in range(1000):
            cur = conn.execute(
                "DELETE FROM shots WHERE project_id=? AND id NOT IN ("
                "SELECT depends_on FROM shots WHERE project_id=? AND depends_on IS NOT NULL)",
                (project_id, project_id))
            if cur.rowcount == 0:
                break
        # 全局资产保留（library 跨项目复用），仅清来源引用
        conn.execute("UPDATE assets SET source_project=NULL WHERE source_project=?",
                     (project_id,))
        for sql in ("DELETE FROM project_assets WHERE project_id=?",
                    "DELETE FROM projects WHERE id=?"):
            conn.execute(sql, (project_id,))
        conn.commit()
    except Exception:
        conn.rollback()  # 失败必须回滚——否则持锁把 worker 线程锁死（真机教训）
        raise
    import shutil
    shutil.rmtree(Path(request.app.state.data_dir) / "projects" / row["slug"],
                  ignore_errors=True)
    return {"deleted": project_id}


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

    # Handle era override (时代背景；检测错了可手动纠正，空串=清除)
    if "era" in body:
        conn = db.connect()
        conn.execute("UPDATE projects SET era=? WHERE id=?",
                     (str(body["era"] or "").strip(), project_id))
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
