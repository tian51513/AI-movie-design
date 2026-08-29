# comic_studio/web/routes_assets.py
"""项目资产视图（引用过滤，spec §4.1）。"""
import json

from fastapi import APIRouter, HTTPException, Request

from ..engine.assets import list_project_assets
from ..engine.projects import get_project

router = APIRouter(prefix="/api/projects/{project_id}/assets", tags=["assets"])


@router.post("/purge-comic")
def purge_comic_route(request: Request, project_id: int):
    """清理读图提取的资产（2026-08-29 动态漫误提取善后）：
    删资产行 + library 目录 + 分镜 ledger 角色绑定；LLM 分析资产不动。"""
    from ..engine.comic import purge_comic_assets
    n = purge_comic_assets(request.app.state.db, request.app.state.data_dir, project_id)
    return {"purged": n}


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
