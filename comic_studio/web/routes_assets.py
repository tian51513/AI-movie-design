# comic_studio/web/routes_assets.py
"""项目资产视图（引用过滤，spec §4.1）。"""
import json

from fastapi import APIRouter, HTTPException, Request

from ..engine.assets import list_project_assets
from ..engine.projects import get_project

router = APIRouter(prefix="/api/projects/{project_id}/assets", tags=["assets"])


@router.get("")
def listing(request: Request, project_id: int):
    if get_project(request.app.state.db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    out = []
    for r in list_project_assets(request.app.state.db, project_id):
        out.append({
            "id": r["id"], "kind": r["kind"], "name": r["name"],
            "detail": json.loads(r["appearance_json"]).get("detail", ""),
            "tags": json.loads(r["tags_json"]),
        })
    return out
