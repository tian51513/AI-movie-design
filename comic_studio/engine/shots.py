# comic_studio/engine/shots.py
"""分镜仓库：替换式重拆、白名单更新、资产重生联动 stale（spec §5/§4.2）。"""
import json
import sqlite3

from .db import Database

_CAMERA_FIELDS = ("景别", "机位", "运镜", "转场")


def persist_shots(db: Database, project_id: int, drafts: list) -> list[int]:
    conn = db.connect()
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
            "camera_json, duration, workflow_type, ledger_json, depends_on) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, seq, getattr(d, "text_span", ""), getattr(d, "description", ""),
             getattr(d, "shot_type", ""), json.dumps(getattr(d, "camera", {}) or {}, ensure_ascii=False),
             float(getattr(d, "duration", 5)), getattr(d, "workflow_type", "ref2va"),
             json.dumps(ledger, ensure_ascii=False), depends_on))
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
                     "workflow_type", "ledger_json", "prompt", "status"}


def update_shot(db: Database, shot_id: int, fields: dict) -> None:
    bad = set(fields) - _UPDATE_WHITELIST
    if bad:
        raise ValueError(f"非法字段: {sorted(bad)}")
    conn = db.connect()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE shots SET {sets} WHERE id=?", (*fields.values(), shot_id))
    conn.commit()


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
