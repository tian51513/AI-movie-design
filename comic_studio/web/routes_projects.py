# comic_studio/web/routes_projects.py
"""项目 REST：创建（上传小说）、列表、详情。"""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..engine.projects import create_project, get_project, list_projects

router = APIRouter(prefix="/api/projects", tags=["projects"])

_PUBLIC_COLUMNS = ("id", "slug", "name", "aspect_ratio", "stage", "created_at")


def _public(row) -> dict:
    return {k: row[k] for k in _PUBLIC_COLUMNS}


@router.post("", status_code=201)
def create(request: Request, name: str = Form(...),
           aspect_ratio: str = Form(...), novel: UploadFile = File(...)):
    if aspect_ratio not in ("9:16", "16:9"):
        raise HTTPException(422, "aspect_ratio 只能是 9:16 或 16:9")
    try:
        text = novel.file.read().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "小说文件需为 UTF-8 编码（请转换后重新上传）")
    row = create_project(request.app.state.db, request.app.state.data_dir,
                         name, aspect_ratio, text)
    return _public(row)


@router.get("")
def listing(request: Request):
    return [_public(r) for r in list_projects(request.app.state.db)]


@router.get("/{project_id}")
def detail(request: Request, project_id: int):
    row = get_project(request.app.state.db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    return _public(row)
