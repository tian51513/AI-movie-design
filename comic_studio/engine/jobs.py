# comic_studio/engine/jobs.py
"""job 记录（最小实现：状态记账；完整队列调度属计划 2）。"""
from .db import Database


def create_job(db: Database, project_id: int, jtype: str) -> int:
    conn = db.connect()
    cur = conn.execute(
        "INSERT INTO jobs (project_id, type, status, started_at) "
        "VALUES (?,?, 'running', datetime('now'))", (project_id, jtype))
    conn.commit()
    return cur.lastrowid


def finish_job(db: Database, job_id: int, error: str | None) -> None:
    conn = db.connect()
    conn.execute(
        "UPDATE jobs SET status=?, error=?, finished_at=datetime('now') WHERE id=?",
        ("failed" if error else "done", error, job_id))
    conn.commit()


def get_job(db: Database, job_id: int):
    return db.connect().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def latest_job(db: Database, project_id: int, jtype: str):
    return db.connect().execute(
        "SELECT * FROM jobs WHERE project_id=? AND type=? ORDER BY id DESC LIMIT 1",
        (project_id, jtype)).fetchone()
