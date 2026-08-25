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


def enqueue_job(db, jtype, project_id=None, asset_id=None, shot_id=None,
                resource=None, payload=None) -> int:
    import json
    conn = db.connect()
    cur = conn.execute(
        "INSERT INTO jobs (project_id, shot_id, asset_id, type, resource, payload_json, status) "
        "VALUES (?,?,?,?,?,?, 'pending')",
        (project_id, shot_id, asset_id, jtype, resource,
         json.dumps(payload or {}, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


_RESOURCE_GROUP = {"gpu_comfy": "gpu", "gpu_llm_local": "gpu"}


def _group(res: str | None) -> str | None:
    return _RESOURCE_GROUP.get(res, res)


def claim_next_job(db, handler_types: tuple):
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    running_rows = conn.execute(
        "SELECT resource FROM jobs WHERE status='running' AND resource IS NOT NULL"
    ).fetchall()
    busy_groups = {_group(r["resource"]) for r in running_rows}
    # 所有组内资源字符串的并集（含自身 + 同组兄弟）
    blocked = {res for res in _RESOURCE_GROUP if _RESOURCE_GROUP[res] in busy_groups}
    blocked.update(r["resource"] for r in running_rows)
    type_ph = ",".join("?" * len(handler_types))
    if blocked:
        blocked_ph = ",".join("?" * len(blocked))
        sql = (f"SELECT * FROM jobs WHERE status='pending' AND type IN ({type_ph}) "
               f"AND (resource IS NULL OR resource NOT IN ({blocked_ph})) "
               "ORDER BY id LIMIT 1")
        params = (*handler_types, *blocked)
    else:
        sql = (f"SELECT * FROM jobs WHERE status='pending' AND type IN ({type_ph}) "
               "ORDER BY id LIMIT 1")
        params = handler_types
    row = conn.execute(sql, params).fetchone()
    if row is None:
        conn.execute("COMMIT")
        return None
    cur = conn.execute(
        "UPDATE jobs SET status='running', started_at=datetime('now'), "
        "attempts=attempts+1 WHERE id=? AND status='pending'", (row["id"],))
    conn.execute("COMMIT")
    if cur.rowcount != 1:
        return None
    return get_job(db, row["id"])


def retry_or_fail(db, job_id: int, error: str, max_attempts: int = 3,
                   consume_attempt: bool = True) -> str:
    conn = db.connect()
    job = get_job(db, job_id)
    if job["attempts"] < max_attempts:
        if not consume_attempt:
            # 回退本次 claim 的 attempts+1：不可达等可重试等待不计入尝试预算
            conn.execute("UPDATE jobs SET status='pending', error=?, attempts=attempts-1 WHERE id=?",
                         (error, job_id))
        else:
            conn.execute("UPDATE jobs SET status='pending', error=? WHERE id=?",
                         (error, job_id))
        conn.commit()
        return "pending"
    finish_job(db, job_id, error)
    return "failed"


def requeue_on_restart(db, requeue_types: tuple, exclude_ids=()) -> int:
    """重启对账：running → pending（重跑）。exclude_ids：等待接回的 job 不动（防双渲）。"""
    conn = db.connect()
    exclude = tuple(exclude_ids)
    marks = ",".join("?" * len(requeue_types))
    excl = f" AND id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
    cur = conn.execute(
        f"UPDATE jobs SET status='pending', started_at=NULL "
        f"WHERE status='running' AND type IN ({marks}) AND attempts < 3{excl}",
        requeue_types + exclude)
    fail_marks = ",".join("?" * len(exclude))
    fail_excl = f" AND id NOT IN ({fail_marks})" if exclude else ""
    conn.execute(
        f"UPDATE jobs SET status='failed', error='interrupted by restart', "
        f"finished_at=datetime('now') WHERE status='running'{fail_excl}", exclude)
    conn.commit()
    return cur.rowcount


def collect_reattach_candidates(db, jtype="gen_shot") -> list:
    """断点对账（spec §5）：收集 running 且带 comfy_prompt_id 的 job 行。

    注意：须在 requeue_on_restart 之前调用——requeue 会把 running 改 pending。
    """
    return db.connect().execute(
        "SELECT * FROM jobs WHERE status='running' AND type=? AND comfy_prompt_id IS NOT NULL",
        (jtype,)).fetchall()
