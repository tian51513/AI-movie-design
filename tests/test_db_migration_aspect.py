# tests/test_db_migration_aspect.py
"""迁移 28（列表末位）：projects 画幅 CHECK 放宽五档——SQLite 不能改 CHECK，需重建表。"""
import sqlite3

from comic_studio.engine.db import Database, MIGRATIONS


def test_migration_28_widens_aspect_check(tmp_path):
    # 手工搭一个停在倒数第二位的旧库（两档 CHECK），再跑迁移
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for sql in MIGRATIONS[:-1]:
        conn.executescript(sql)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (len(MIGRATIONS) - 1,))
    conn.execute("INSERT INTO schema_version (version) VALUES (26)")
    conn.execute(
        "INSERT INTO projects (slug, name, aspect_ratio, novel_path) "
        "VALUES ('legacy', '旧库', '9:16', 'projects/legacy/novel.txt')")
    conn.commit()
    conn.close()

    db = Database(path)
    db.migrate()

    c = db.connect()
    row = c.execute("SELECT id, name, aspect_ratio FROM projects WHERE slug='legacy'").fetchone()
    assert row["name"] == "旧库" and row["aspect_ratio"] == "9:16"  # 重建表不丢数据、id 不变
    pid = row["id"]
    c.execute("UPDATE projects SET aspect_ratio='3:4' WHERE slug='legacy'")  # 旧 CHECK 下必炸
    c.commit()
    assert c.execute("SELECT aspect_ratio FROM projects WHERE slug='legacy'").fetchone()[0] == "3:4"

    # 外键引用（jobs/assets 等）仍指向同一 id
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("SELECT COUNT(*) n FROM projects WHERE id=?", (pid,)).fetchone()["n"] == 1
