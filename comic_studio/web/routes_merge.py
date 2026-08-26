# comic_studio/web/routes_merge.py
"""成片合成 REST（计划5B 任务4）：发起 merge 任务与产物列表（spec §10）。"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..engine.projects import get_project

router = APIRouter(tags=["merge"])


@router.post("/api/projects/{project_id}/merge", status_code=202)
def start_merge(request: Request, project_id: int):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "rendered":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能合成（需 rendered）")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='merge' AND project_id=? "
        "AND status IN ('pending','running')", (project_id,)).fetchone()
    if dup:
        raise HTTPException(409, "合成任务已在队列")
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "merge", project_id=project_id,
                      payload={"project_id": project_id})
    return {"job_id": jid}


@router.get("/api/projects/{project_id}/merges")
def list_merges(request: Request, project_id: int):
    proj = get_project(request.app.state.db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    out_dir = Path(request.app.state.data_dir) / "projects" / proj["slug"] / "output"
    out = []
    if out_dir.is_dir():
        for f in sorted(out_dir.glob("ep*.mp4")):
            rel = f.relative_to(Path(request.app.state.data_dir)).as_posix()
            out.append({"file": f.name, "url": f"/media/{rel}"})
    return out


@router.post("/api/projects/{project_id}/tts", status_code=200)
def generate_tts(request: Request, project_id: int):
    """P6：一键生成 TTS 配音 + SRT 字幕（同步执行，通常 < 10 秒）。"""
    from ..engine.tts import generate_dialogue_audio
    from ..engine.subtitles import generate_srt
    from ..engine.projects import get_project as _gp
    db = request.app.state.db
    if _gp(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    try:
        audio = generate_dialogue_audio(db, request.app.state.data_dir, project_id)
        srt = generate_srt(db, request.app.state.data_dir, project_id)
        return {"shots_with_dialogue": len(audio), "srt": str(srt)}
    except Exception as exc:
        raise HTTPException(502, f"TTS/字幕生成失败：{exc}")
