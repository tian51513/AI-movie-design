# comic_studio/web/routes_analyze.py
"""分析接口：后台执行 + 状态轮询（spec §5 门禁前的自动化阶段）。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..engine import jobs
from ..engine.llm.analyze import analyze_project
from ..engine.logbus import emit as emit_log
from ..engine.projects import get_project

router = APIRouter(prefix="/api/projects/{project_id}/analyze", tags=["analyze"])


def _run_analysis(db, data_dir, project_id: int, job_id: int) -> None:
    try:
        analyze_project(db, data_dir, project_id)
        jobs.finish_job(db, job_id, None)
    except Exception as e:  # job 层兜底，错误明细进库（spec §11）
        emit_log(db, "analyze", "error", f"分析失败：{type(e).__name__}: {e}",
                 project_id=project_id, job_id=job_id)
        jobs.finish_job(db, job_id, f"{type(e).__name__}: {e}")


@router.post("", status_code=202)
def start(request: Request, project_id: int, background: BackgroundTasks):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    running = jobs.latest_job(db, project_id, "analyze")
    if running and running["status"] == "running":
        raise HTTPException(409, "分析正在进行中")
    if proj["stage"] != "created":
        raise HTTPException(409, f"阶段 {proj['stage']} 不允许重新分析（回退流程见后续计划）")
    job_id = jobs.create_job(db, project_id, "analyze")
    background.add_task(_run_analysis, db, request.app.state.data_dir, project_id, job_id)
    return {"job_id": job_id}


@router.get("/status")
def status(request: Request, project_id: int):
    row = jobs.latest_job(request.app.state.db, project_id, "analyze")
    if row is None:
        raise HTTPException(404, "尚无分析任务")
    return {"job_id": row["id"], "status": row["status"], "error": row["error"]}
