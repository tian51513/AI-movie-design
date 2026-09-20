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


@router.post("/dedup-assets")
def dedup_assets_route(request: Request, project_id: int):
    """LLM 资产查重合并（2026-09-20 用户需求：四层/四楼教室类同物异名清理，
    存量项目免重分析）。同步单次 LLM 调用，秒级返回。"""
    if get_project(request.app.state.db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.llm.analyze import dedup_project_assets, make_client_factory
    from ..engine.llm.provider import LLMError
    try:
        client = make_client_factory(request.app.state.db)("extract_assets")
        merged = dedup_project_assets(request.app.state.db, request.app.state.data_dir,
                                      project_id, client)
    except LLMError as e:
        raise HTTPException(502, f"资产查重 LLM 调用失败：{e}")
    return {"merged": merged}


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
            "voice": r["voice"] if "voice" in r.keys() else "",
            "source_project": r["source_project"],
        })
    return out
