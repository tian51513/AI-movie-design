# comic_studio/engine/assets.py
"""全局资产库：库为唯一存储，项目存引用（spec §4.1）。"""
import json
import sqlite3
from pathlib import Path

from .db import Database
from .paths import rel_to_data

_KINDS = ("character", "scene", "prop")


def _detail(item, kind: str) -> str:
    return getattr(item, "appearance", None) or getattr(item, "description", "") or ""


def persist_assets(db: Database, data_dir: Path, project_id: int, analysis) -> list[int]:
    conn = db.connect()
    ids: list[int] = []
    try:
        for kind in _KINDS:
            for item in getattr(analysis, f"{kind}s"):
                existing = conn.execute(
                    "SELECT id FROM assets WHERE kind=? AND name=? AND source_project=?",
                    (kind, item.name, project_id)).fetchone()
                if existing:  # 同项目重分析：跳过同名（回退语义由 stale 标记处理，计划 3）
                    ids.append(existing["id"])
                    continue
                cur = conn.execute(
                    "INSERT INTO assets (kind, name, appearance_json, tags_json, library_dir, source_project) "
                    "VALUES (?,?,?,?,?,?)",
                    (kind, item.name,
                     json.dumps({"detail": _detail(item, kind)}, ensure_ascii=False),
                     json.dumps(list(getattr(item, "tags", []) or []), ensure_ascii=False),
                     "", project_id))
                asset_id = cur.lastrowid
                lib_dir = Path(data_dir) / "library" / f"{kind}s" / str(asset_id)
                (lib_dir / "views").mkdir(parents=True, exist_ok=True)
                (lib_dir / "meta.json").write_text(json.dumps({
                    "name": item.name, "kind": kind,
                    "detail": _detail(item, kind),
                    "tags": list(getattr(item, "tags", []) or []),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                conn.execute("UPDATE assets SET library_dir=? WHERE id=?", (rel_to_data(data_dir, lib_dir), asset_id))
                conn.execute("INSERT OR IGNORE INTO project_assets (project_id, asset_id) VALUES (?,?)",
                             (project_id, asset_id))
                ids.append(asset_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ids


def list_project_assets(db: Database, project_id: int) -> list[sqlite3.Row]:
    return db.connect().execute(
        "SELECT a.* FROM assets a JOIN project_assets pa ON pa.asset_id=a.id "
        "WHERE pa.project_id=? ORDER BY a.kind, a.id", (project_id,)).fetchall()


def get_asset(db: Database, asset_id: int) -> sqlite3.Row | None:
    return db.connect().execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
