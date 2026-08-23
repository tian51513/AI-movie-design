"""结构化执行日志总线：全管线统一埋点入口（analyze/llm/comfy/merge/system）。

Plan 2 的 ComfyUI 执行日志走同一 emit 接口，前端面板零改动接入。
"""
import json

from .db import Database

LEVELS = ("info", "warn", "error")


def emit(db: Database, source: str, level: str, message: str, *,
         project_id: int | None = None, job_id: int | None = None,
         data: dict | None = None) -> None:
    assert level in LEVELS, f"level 只能是 {LEVELS}"
    conn = db.connect()
    conn.execute(
        "INSERT INTO logs (project_id, job_id, source, level, message, data_json) "
        "VALUES (?,?,?,?,?,?)",
        (project_id, job_id, source, level, message,
         json.dumps(data or {}, ensure_ascii=False)))
    conn.commit()


def fetch_logs(db: Database, project_id: int, after_id: int = 0,
               limit: int = 200) -> list:
    """游标式增量读取：after_id 之后的日志，按 id 升序。"""
    return db.connect().execute(
        "SELECT * FROM logs WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
        (project_id, after_id, limit)).fetchall()
