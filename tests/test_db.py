# tests/test_db.py
import sqlite3
from pathlib import Path

from comic_studio.engine.db import Database, MIGRATIONS


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "studio.db")
    db.migrate()
    return db


def test_migrate_creates_all_tables(tmp_path):
    db = _db(tmp_path)
    conn = db.connect()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"schema_version", "projects", "assets", "project_assets",
                "shots", "jobs", "endpoints", "settings", "llm_calls", "logs"}
    assert expected <= tables


def test_migrate_idempotent(tmp_path):
    db = _db(tmp_path)
    db.migrate()  # 第二次不报错、不重复
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == len(MIGRATIONS)


def test_connection_pragmas(tmp_path):
    db = _db(tmp_path)
    conn = db.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_is_thread_local(tmp_path):
    import threading
    db = _db(tmp_path)
    c1 = db.connect()
    holder = {}
    threading.Thread(target=lambda: holder.setdefault("c", db.connect())).start()
    import time; time.sleep(0.1)
    assert holder["c"] is not c1



def test_migrate_from_any_partial_version(tmp_path, monkeypatch):
    """回归：迁移列表只能末尾追加——从任意历史版本升级到最新都必须成功。

    复现 2026-08-23 事故：style 迁移被插到列表中间，历史库（已应用到
    旧的第 9 条）升级时位置错位 → "table logs already exists"。
    """
    from comic_studio.engine import db as dbmod
    FULL = list(dbmod.MIGRATIONS)          # 循环前捕获完整列表
    assert len(FULL) >= 10  # 9 基础表 + style 追加
    for k in range(1, len(FULL)):
        db = Database(tmp_path / f"s{k}.db")
        monkeypatch.setattr(dbmod, "MIGRATIONS", FULL[:k])
        db.migrate()                       # 模拟历史版本创建的库
        monkeypatch.setattr(dbmod, "MIGRATIONS", FULL)
        db.migrate()                       # 升级到完整列表 → 不得抛错
