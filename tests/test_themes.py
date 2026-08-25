# tests/test_themes.py
"""主题模板（2026-08-25 需求）：templates/tpl/*.md 解析入库；成人向一节按约定跳过。"""
from pathlib import Path

from comic_studio.engine.db import Database
from comic_studio.engine.themes import THEME_ROOT, parse_theme_file, sync_themes


def test_parse_real_theme_file():
    """真实模板文件：常规项整理为 {name, category, description}；成人向节跳过。"""
    items = parse_theme_file(THEME_ROOT / "default_theme.md")
    names = [i["name"] for i in items]
    assert len(items) == 10  # 常规项十个
    assert names[0] == "心跳节拍：校园纯爱物语"
    assert all(i["description"] and len(i["description"]) > 20 for i in items)
    assert not any("NTR" in n or "禁忌" in n or "催眠" in n for n in names)


def test_sync_idempotent(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    n1 = sync_themes(db)
    assert n1 >= 10
    n2 = sync_themes(db)  # 重复同步按 name 去重
    assert n2 == n1
    row = db.connect().execute(
        "SELECT COUNT(*) c FROM theme_templates").fetchone()["c"]
    assert row == n1


def test_parse_empty_file(tmp_path):
    p = tmp_path / "t.md"
    p.write_text("# 无主题条目", encoding="utf-8")
    assert parse_theme_file(p) == []
