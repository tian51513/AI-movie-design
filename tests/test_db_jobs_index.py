# tests/test_db_jobs_index.py


def test_jobs_indexes_after_migrate(tmp_path):
    """事故复盘（2026-09-01）：jobs 表零索引 + 3.6 万行（含肥大 snapshot_json）→
    GET /shots 每镜 2 次 _last_job 全表扫描，564 镜项目 /shots 要 95s——
    慢响应乱序覆盖让用户看到「分镜内容错乱」。"""
    from comic_studio.engine.db import Database
    db = Database(tmp_path / "i.db")
    db.migrate()
    names = {r["name"] for r in db.connect().execute("PRAGMA index_list(jobs)")}
    assert {"idx_jobs_shot", "idx_jobs_asset", "idx_jobs_proj",
            "idx_jobs_status"} <= names
