"""项目执行日志：游标式增量接口（前端 1s 轮询追加）。"""
import json

from fastapi import APIRouter, HTTPException, Request

from ..engine.logbus import fetch_logs
from ..engine.projects import get_project

router = APIRouter(prefix="/api/projects/{project_id}/logs", tags=["logs"])


@router.get("")
def listing(request: Request, project_id: int, after: int = 0, limit: int = 200):
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    rows = fetch_logs(db, project_id, after, min(limit, 1000))
    return {
        "logs": [{
            "id": r["id"], "time": r["created_at"], "source": r["source"],
            "level": r["level"], "message": r["message"],
            "data": json.loads(r["data_json"]),
        } for r in rows],
        # 首拉（after=0）rows 为倒序，游标取最大 id
        "last_id": max((r["id"] for r in rows), default=after),
    }
