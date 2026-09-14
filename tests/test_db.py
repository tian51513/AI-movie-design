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



def test_migration_35_redraw_columns(tmp_path):
    """迁移 35（2026-09-09）：动态漫角色重绘双列——redraw_characters 开关 +
    redraw_done 已批量重绘标记（autopilot 停等）。"""
    db = Database(tmp_path / "s.db")
    db.migrate()
    conn = db.connect()
    row = conn.execute("SELECT redraw_characters, redraw_done FROM projects").description
    names = {c[0] for c in row}
    assert {"redraw_characters", "redraw_done"} <= names


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


def test_busy_timeout_and_logs_index(tmp_path):
    """写锁竞争防线（2026-09-14 删项目 database is locked）：连接挂 30s
    busy_timeout（撞锁等待而非报错）+ logs(project_id, id) 索引（删项目
    的 logs 清理不再整表扫删）。"""
    import threading
    db = Database(tmp_path / "s.db")
    db.migrate()
    # ① busy_timeout=30s（sqlite3 timeout 参数与 PRAGMA 双确认）
    assert db.connect().execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    # ② logs 项目索引存在
    idx = db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='logs'"
    ).fetchall()
    assert any(r["name"] == "idx_logs_project" for r in idx)
    # ③ 竞争实测：线程 A 持写锁 1s 后提交，主线程写入应等待成功而非报错
    def _hold():
        c = db.connect()
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO logs (project_id, source, level, message, data_json)"
                  " VALUES (NULL,'t','info','hold','{}')")
        import time as _t; _t.sleep(1.0)
        c.commit()
    t = threading.Thread(target=_hold); t.start()
    import time as _t; _t.sleep(0.15)   # 等 A 拿到写锁
    c2 = db.connect()
    c2.execute("INSERT INTO logs (project_id, source, level, message, data_json)"
               " VALUES (NULL,'t','info','contender','{}')")
    c2.commit()
    t.join()
    msgs = [r["message"] for r in c2.execute("SELECT message FROM logs")]
    assert "hold" in msgs and "contender" in msgs


def test_orphan_running_jobs_failed_on_startup(tmp_path):
    """非白名单类型重启后孤儿 running → failed（2026-09-14）：否则
    _has_active_job 永真，autopilot 卡「漫画页生成中」且手动补页被在飞
    守卫拦死。白名单类型由 requeue_on_restart 处理不在此路径。"""
    from fastapi.testclient import TestClient
    from comic_studio.engine.jobs import enqueue_job
    from comic_studio.engine.projects import create_project
    from comic_studio.web.app import create_app
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "孤儿剧", "16:9", "t")["id"]
    from types import SimpleNamespace as NS
    from comic_studio.engine.shots import persist_shots
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="comic", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    # describe_shots 不在 REQUEUE_ON_RESTART_TYPES（gen_comic_page 在——
    # 09-12 已加，孤儿会被重排回 pending 自动续跑，无需此路径）
    jid = enqueue_job(db, "describe_shots", project_id=pid, shot_id=sid,
                      resource="gpu_comfy", payload={"shot_id": sid})
    db.connect().execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    db.connect().commit()
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        assert c.get("/api/settings").status_code == 200  # lifespan 已跑
    row = db.connect().execute("SELECT status, error FROM jobs WHERE id=?",
                               (jid,)).fetchone()
    assert row["status"] == "failed"
    assert "interrupted by restart" in row["error"]  # requeue_on_restart 既有收尾
