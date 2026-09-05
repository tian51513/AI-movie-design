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
    if running and running["status"] in ("pending", "running"):
        # L15（2026-09-05 审计低危）：pending 也拦——autopilot 入队的待跑分析
        # 此前拦不住手动双跑（双份 LLM 成本）
        raise HTTPException(409, "分析正在进行中")
    if proj["stage"] != "created":
        raise HTTPException(409, f"阶段 {proj['stage']} 不允许重新分析（回退流程见后续计划）")
    # P10 守卫（终审 M-1 真机命中 2026-09-05）：源音频在、转写未落盘 →
    # 占位正文会被分析成空资产——409 等转写完成
    from ..engine.asr import load_segments
    from ..engine.paths import data_to_abs
    _adir = data_to_abs(request.app.state.data_dir, f"projects/{proj['slug']}/audio")
    _src = next(_adir.glob("source.*"), None) if _adir.is_dir() else None
    if _src is not None and load_segments(request.app.state.data_dir, proj["slug"]) is None:
        raise HTTPException(409, "音频转写尚未完成（正文还是占位文本）——等转写 job "
                                "结束后再分析；若转写失败请重新上传音频")
    job_id = jobs.create_job(db, project_id, "analyze")
    background.add_task(_run_analysis, db, request.app.state.data_dir, project_id, job_id)
    return {"job_id": job_id}


@router.get("/status")
def status(request: Request, project_id: int):
    row = jobs.latest_job(request.app.state.db, project_id, "analyze")
    if row is None:
        raise HTTPException(404, "尚无分析任务")
    return {"job_id": row["id"], "status": row["status"], "error": row["error"]}
