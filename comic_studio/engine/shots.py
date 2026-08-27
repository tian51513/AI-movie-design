# comic_studio/engine/shots.py
"""分镜仓库：替换式重拆、白名单更新、资产重生联动 stale（spec §5/§4.2）。"""
import json
import sqlite3

from .db import Database

_CAMERA_FIELDS = ("景别", "机位", "运镜", "转场")


def persist_shots(db: Database, project_id: int, drafts: list) -> list[int]:
    conn = db.connect()
    # 先清 jobs.shot_id 引用再删旧镜（jobs.shot_id 外键 REFERENCES shots(id)，
    # FK=ON 下直接 DELETE 被 IntegrityError 拦下——2026-08-27 真机 job 653；
    # 任务行保留作审计，只解除引用）
    conn.execute("UPDATE jobs SET shot_id=NULL WHERE project_id=? AND shot_id IS NOT NULL",
                 (project_id,))
    conn.execute("DELETE FROM shots WHERE project_id=?", (project_id,))
    ids = []
    seq_to_id = {}
    seq = 0
    for d in drafts:
        seq += 1
        ledger = dict(getattr(d, "ledger", {}) or {})
        ledger["assets"] = {"characters": list(getattr(d, "character_ids", []) or []),
                            "scenes": list(getattr(d, "scene_ids", []) or []),
                            "props": list(getattr(d, "prop_ids", []) or [])}
        depends_on = getattr(d, "depends_on", None)
        cur = conn.execute(
            "INSERT INTO shots (project_id, seq, text_span, description, shot_type, "
            "camera_json, duration, workflow_type, ledger_json, depends_on, prompt) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, seq, getattr(d, "text_span", ""), getattr(d, "description", ""),
             getattr(d, "shot_type", ""), json.dumps(getattr(d, "camera", {}) or {}, ensure_ascii=False),
             float(getattr(d, "duration", 5)), getattr(d, "workflow_type", "ref2va"),
             json.dumps(ledger, ensure_ascii=False), depends_on,
             getattr(d, "prompt", "")))
        shot_id = cur.lastrowid
        ids.append(shot_id)
        seq_to_id[seq] = shot_id
    # Convert sequence-based dependencies to shot IDs
    for shot_seq, depends_on_seq in [(d["seq"], d["depends_on"]) for d in
                                       conn.execute("SELECT seq, depends_on FROM shots WHERE project_id=?",
                                                   (project_id,)).fetchall()]:
        if depends_on_seq is not None and isinstance(depends_on_seq, int) and depends_on_seq in seq_to_id:
            conn.execute("UPDATE shots SET depends_on=? WHERE project_id=? AND seq=?",
                        (seq_to_id[depends_on_seq], project_id, shot_seq))
    conn.commit()
    return ids


def list_shots(db: Database, project_id: int) -> list[sqlite3.Row]:
    return db.connect().execute(
        "SELECT * FROM shots WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()


def get_shot(db: Database, shot_id: int) -> sqlite3.Row | None:
    return db.connect().execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()


_UPDATE_WHITELIST = {"description", "shot_type", "camera_json", "duration",
                     "workflow_type", "ledger_json", "prompt", "status",
                     "video_path"}


def update_shot(db: Database, shot_id: int, fields: dict) -> None:
    bad = set(fields) - _UPDATE_WHITELIST
    if bad:
        raise ValueError(f"非法字段: {sorted(bad)}")
    conn = db.connect()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE shots SET {sets} WHERE id=?", (*fields.values(), shot_id))
    conn.commit()


def set_disabled_batch(db: Database, project_id: int, ids: list[int], disabled: int) -> int:
    """批量置生效/无效（2026-08-27 需求）：无效镜不进门禁计数/渲染/合成。返回受影响行数。"""
    if not ids:
        return 0
    conn = db.connect()
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE shots SET disabled=? WHERE project_id=? AND id IN ({marks})",
        (1 if disabled else 0, project_id, *ids))
    conn.commit()
    return cur.rowcount


def delete_shots_batch(db: Database, project_id: int, ids: list[int]) -> int:
    """批量删除分镜；引用被删镜的 depends_on 一并清空（避免悬挂链接）。"""
    if not ids:
        return 0
    conn = db.connect()
    marks = ",".join("?" * len(ids))
    # 先清引用：帧链（depends_on 指向将删镜）与任务行（jobs.shot_id 外键同上）
    conn.execute(f"UPDATE shots SET depends_on=NULL WHERE depends_on IN ({marks})", ids)
    conn.execute(f"UPDATE jobs SET shot_id=NULL WHERE shot_id IN ({marks})", ids)
    cur = conn.execute(f"DELETE FROM shots WHERE project_id=? AND id IN ({marks})",
                       (project_id, *ids))
    conn.commit()
    return cur.rowcount


def mark_stale_for_asset(db: Database, asset_id: int) -> int:
    """资产参考图重生后，引用它的分镜标 stale（spec §5 回退规则，不自动重跑）。"""
    n = 0
    conn = db.connect()
    for shot in conn.execute("SELECT id, ledger_json FROM shots").fetchall():
        try:
            ledger = json.loads(shot["ledger_json"] or "{}")
        except json.JSONDecodeError:
            continue
        assets = ledger.get("assets", {})
        if any(asset_id in (assets.get(k) or []) for k in ("characters", "scenes", "props")):
            conn.execute("UPDATE shots SET status='stale' WHERE id=?", (shot["id"],))
            n += 1
    conn.commit()
    return n
