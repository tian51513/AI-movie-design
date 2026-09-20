# comic_studio/engine/assets.py
"""全局资产库：库为唯一存储，项目存引用（spec §4.1）。"""
import json
import sqlite3
from pathlib import Path

from .db import Database
from .paths import data_to_abs, rel_to_data

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
                if lib_dir.exists():
                    # id 复用防幽灵图（2026-08-29 真机：删行/回滚后 id 复用，
                    # 新资产继承了旧目录残留的 main.png/views——清空重建）
                    import shutil
                    shutil.rmtree(lib_dir)
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


def delete_asset(db: Database, data_dir: Path, asset_id: int) -> str | None:
    """删除资产并清整条 FK/引用链（2026-09-20 用户需求：乱码名/误提取资产清理）。
    返回资产名（不存在返回 None）。链：project_assets 绑定 / jobs.asset_id 引用
    （任务行保留作审计，同 shots.py 删镜判例）/ shots.ledger 各 kind 绑定列表 /
    磁盘 library 目录（含参考图）。"""
    import shutil
    asset = get_asset(db, asset_id)
    if asset is None:
        return None
    conn = db.connect()
    conn.execute("DELETE FROM project_assets WHERE asset_id=?", (asset_id,))
    conn.execute("UPDATE jobs SET asset_id=NULL WHERE asset_id=?", (asset_id,))
    # 分镜 ledger 解绑（characters/scenes/props 三列表同口径）
    from .shots import list_shots
    for sh in list_shots(db, asset["source_project"]):
        led = json.loads(sh["ledger_json"] or "{}")
        groups = led.get("assets") or {}
        touched = False
        for kind in _KINDS:
            ids = groups.get(f"{kind}s") or []
            if asset_id in ids:
                groups[f"{kind}s"] = [i for i in ids if i != asset_id]
                touched = True
        if touched:
            conn.execute("UPDATE shots SET ledger_json=? WHERE id=?",
                         (json.dumps(led, ensure_ascii=False), sh["id"]))
    conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    conn.commit()
    if asset["library_dir"]:
        lib = data_to_abs(Path(data_dir), asset["library_dir"])
        if lib.exists():
            shutil.rmtree(lib)
    return asset["name"]
