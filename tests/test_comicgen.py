# tests/test_comicgen.py
"""小说转漫画（2026-09-12）：第五种项目类型。"""
from comic_studio.engine.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _comic_project(tmp_path, **kw):
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    defaults = dict(comic_mode="comic_output", dialogue_mode="bubble",
                    target_pages=0, image_size="1024x1536",
                    quality_tier="standard")
    defaults.update(kw)
    return db, create_project(db, tmp_path / "data", "漫画剧", "16:9",
                               "正文" * 100, **defaults)["id"]


def test_migration_36_comic_columns(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    conn = db.connect()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(projects)")}
    assert {"dialogue_mode", "target_pages", "image_size", "quality_tier"} <= cols


def test_comic_project_created(tmp_path):
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.projects import get_project
    p = get_project(db, pid)
    assert p["comic_mode"] == "comic_output"
    assert p["dialogue_mode"] == "bubble"
    assert p["target_pages"] == 0
    assert p["image_size"] == "1024x1536"
    assert p["quality_tier"] == "standard"
