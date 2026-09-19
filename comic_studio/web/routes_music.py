# comic_studio/web/routes_music.py
"""音乐库 API（2026-09-19 BGM spec）：Music3 生成入队 → staging 试听 →
入库/放弃 → 库列表/删除 + LLM 曲风/歌词建议。

- generate 入队 202（仓库入队端点惯例，同 routes_merge/routes_refs/routes_shots）；
  在飞互斥（pending/running gen_music）409；caption 空 422
- save 从该 job 快照回读 staging 相对路径——路径穿越防线同 use-kf：
  解析后必须落在 data 根下且文件在盘，否则 409；重名 409 / 非法名 422
"""
import json
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request

from ..engine import musiclib
from ..engine.jobs import enqueue_job, get_job
from ..engine.paths import data_to_abs

router = APIRouter()


def _job_id_of(body: dict) -> int:
    try:
        return int(body.get("job_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "job_id 需为数字")


def _staging_of(db, data_dir, job_id: int) -> Path:
    """从 gen_music job 快照解析 staging 绝对路径。
    job 不存在 / 无快照 / 解析越界（穿越防线）一律 409。"""
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(409, f"任务不存在: {job_id}")
    try:
        snap = json.loads(job["snapshot_json"] or "{}")
    except ValueError:
        snap = {}
    rel = str((snap.get("workflow") or {}).get("staging") or "")
    if not rel:
        raise HTTPException(409, "该任务没有 staging 快照（未生成或生成中）")
    p = data_to_abs(data_dir, rel).resolve()
    if not p.is_relative_to(Path(data_dir).resolve()):
        raise HTTPException(409, "staging 路径越界")
    return p


@router.post("/api/music/generate", status_code=202)
def generate(request: Request, body: dict = Body(...)):
    """入队 gen_music（Music3 样曲 → staging）。caption 必填；单飞互斥。"""
    db = request.app.state.db
    caption = str(body.get("caption") or "").strip()
    if not caption:
        raise HTTPException(422, "caption 必填（曲风描述）")
    seed = body.get("seed") or 0
    duration = body.get("duration")
    try:
        seed = int(seed)
        if duration is not None:
            duration = float(duration)
    except (TypeError, ValueError):
        raise HTTPException(422, "seed/duration 需为数字")
    conn = db.connect()
    if conn.execute(
            "SELECT 1 FROM jobs WHERE type='gen_music' "
            "AND status IN ('pending','running') LIMIT 1").fetchone():
        raise HTTPException(409, "已有音乐生成任务在排队/执行，请等待完成后再试")
    payload = {"caption": caption,
               "lyrics": str(body.get("lyrics") or "").strip(), "seed": seed,
               "genre": str(body.get("genre") or "").strip(),
               "voice": str(body.get("voice") or "").strip()}
    if duration is not None:
        payload["duration"] = duration
    jid = enqueue_job(db, "gen_music", project_id=None, resource="gpu_comfy",
                      payload=payload)
    return {"job_id": jid}


@router.get("/api/music")
def list_all(request: Request):
    """库清单 + 最近 gen_music 任务的 staging 样曲（文件在盘才列）。"""
    db = request.app.state.db
    data_dir = Path(request.app.state.data_dir)
    staging = []
    rows = db.connect().execute(
        "SELECT id, status, snapshot_json FROM jobs WHERE type='gen_music' "
        "ORDER BY id DESC LIMIT 5").fetchall()
    for r in rows:
        try:
            snap = json.loads(r["snapshot_json"] or "{}")
        except ValueError:
            continue
        rel = str((snap.get("workflow") or {}).get("staging") or "")
        if rel and data_to_abs(data_dir, rel).is_file():
            staging.append({"job_id": r["id"], "status": r["status"],
                            "path": rel})
    return {"music": musiclib.list_music(db), "staging": staging}


@router.post("/api/music/save", status_code=201)
def save(request: Request, body: dict = Body(...)):
    """staging 样曲确认入库（复制进 music/custom）。重名 409 / 非法名 422。"""
    db = request.app.state.db
    data_dir = request.app.state.data_dir
    p = _staging_of(db, data_dir, _job_id_of(body))
    if not p.is_file():
        raise HTTPException(409, "staging 文件不存在（可能已被清理）")
    try:
        entry = musiclib.save_to_library(
            db, data_dir, p, str(body.get("name") or ""),
            caption=str(body.get("caption") or ""),
            lyrics=str(body.get("lyrics") or ""),
            seed=body.get("seed") or 0,
            duration=body.get("duration") or 120)
    except ValueError as exc:
        msg = str(exc)
        if "已存在" in msg:
            raise HTTPException(409, msg)  # 重名
        raise HTTPException(422, msg)  # 非法字符/空名等校验类
    return {**entry, "url": f"/media/{entry['path']}"}


@router.post("/api/music/discard", status_code=204)
def discard(request: Request, body: dict = Body(...)):
    """放弃 staging 样曲（删文件，不入库）。文件已不在 → 409。"""
    db = request.app.state.db
    p = _staging_of(db, request.app.state.data_dir, _job_id_of(body))
    if not p.is_file():
        raise HTTPException(409, "staging 文件不存在（可能已被清理）")
    p.unlink()


@router.delete("/api/music/{music_id}", status_code=204)
def delete_music_route(request: Request, music_id: int):
    """删除库内音乐（元数据 + 文件）。不存在 → 404。"""
    try:
        musiclib.delete_music(request.app.state.db,
                              request.app.state.data_dir, music_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/music/suggest-caption")
def suggest_caption(request: Request, body: dict | None = Body(default=None)):
    """LLM 曲风描述建议（Music3 caption 格式）。失败 502 包 detail。"""
    try:
        _b = body or {}
        text = musiclib.suggest_caption(request.app.state.db,
                                        str(_b.get("hint") or ""),
                                        genre=str(_b.get("genre") or ""),
                                        voice=str(_b.get("voice") or ""),
                                        lyrics=str(_b.get("lyrics") or ""))
    except Exception as e:
        raise HTTPException(502, f"曲风建议失败：{e}")
    return {"text": text}


@router.post("/api/music/suggest-lyrics")
def suggest_lyrics(request: Request, body: dict | None = Body(default=None)):
    """LLM 歌词建议（[主歌]/[副歌] 结构）。失败 502 包 detail。"""
    try:
        _b = body or {}
        text = musiclib.suggest_lyrics(request.app.state.db,
                                       str(_b.get("hint") or ""),
                                       base_lyrics=str(_b.get("lyrics") or ""),
                                       duration=_b.get("duration"),
                                       genre=str(_b.get("genre") or ""),
                                       voice=str(_b.get("voice") or ""))
    except Exception as e:
        raise HTTPException(502, f"歌词建议失败：{e}")
    return {"text": text}
