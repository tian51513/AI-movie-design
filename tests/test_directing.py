# tests/test_directing.py
"""定向执导技能（2026-10-01 T8 借鉴）：项目类型关键词自动匹配 + 手动覆写。
方法论自写中文系统词（T8 本体 ARR 不逐字搬，注释注明出处）。"""


def _proj(db, tmp_path, text, style="", era=""):
    from comic_studio.engine.projects import create_project
    p = create_project(db, tmp_path / "data", "执导测试", "9:16", text, style=style)
    if era:
        import sqlite3
        db.connect().execute("UPDATE projects SET era=? WHERE id=?", (era, p["id"]))
        db.connect().commit()
    from comic_studio.engine.projects import get_project
    return get_project(db, p["id"])


def _db(tmp_path):
    from comic_studio.engine.db import Database
    return Database(tmp_path / "s.db")


def test_match_wuxu_by_novel_keywords(tmp_path):
    """武侠正文命中武术打斗（发力/支撑/跨镜接力方法论注入分镜与提示词）。"""
    from comic_studio.engine.directing import match_directing_skills
    db = _db(tmp_path); db.migrate()
    proj = _proj(db, tmp_path, "少年踏入江湖，门派大比武中与师兄过招三百回合。")
    assert match_directing_skills(db, proj, tmp_path / "data") == ["wushu"]


def test_match_drama_and_pov(tmp_path):
    from comic_studio.engine.directing import match_directing_skills
    db = _db(tmp_path); db.migrate()
    proj = _proj(db, tmp_path, "都市情感悬疑：她察觉丈夫的谎言。", style="都市悬疑剧")
    ids = match_directing_skills(db, proj, tmp_path / "data")
    assert "drama" in ids
    proj2 = _proj(db, tmp_path, "第一人称视角，我推开房门。")
    assert "pov" in match_directing_skills(db, proj2, tmp_path / "data")


def test_no_match_returns_empty(tmp_path):
    from comic_studio.engine.directing import match_directing_skills
    db = _db(tmp_path); db.migrate()
    proj = _proj(db, tmp_path, "今天天气很好，我们去公园野餐。")
    assert match_directing_skills(db, proj, tmp_path / "data") == []


def test_override_beats_auto(tmp_path):
    """projects.directing_override：'off'=全关 / 'a,b'=强制指定 / 空=自动。"""
    from comic_studio.engine.directing import (match_directing_skills,
                                               effective_directing)
    db = _db(tmp_path); db.migrate()
    proj = _proj(db, tmp_path, "少年踏入江湖比武。")
    conn = db.connect()
    conn.execute("UPDATE projects SET directing_override='off' WHERE id=?",
                 (proj["id"],))
    conn.commit()
    from comic_studio.engine.projects import get_project
    assert effective_directing(db, get_project(db, proj["id"]),
                               tmp_path / "data") == []
    conn.execute("UPDATE projects SET directing_override='drama,pov' WHERE id=?",
                 (proj["id"],))
    conn.commit()
    assert effective_directing(db, get_project(db, proj["id"]),
                               tmp_path / "data") == ["drama", "pov"]
    # 覆写值里的未知 id 忽略（引擎宽容，API 层校验）
    conn.execute("UPDATE projects SET directing_override='wushu,bogus' WHERE id=?",
                 (proj["id"],))
    conn.commit()
    assert effective_directing(db, get_project(db, proj["id"]), tmp_path / "data") == ["wushu"]
    # 自动匹配不受影响（覆写为空）
    assert match_directing_skills(db, proj, tmp_path / "data") == ["wushu"]


def test_directing_block_content(tmp_path):
    """directing_block：命中技能的中文方法论块（带标题头）；未命中空串。"""
    from comic_studio.engine.directing import directing_block
    db = _db(tmp_path); db.migrate()
    proj = _proj(db, tmp_path, "少年踏入江湖比武。")
    blk = directing_block(db, proj, tmp_path / "data")
    assert blk.startswith("【定向执导")
    assert "支撑" in blk and "接力" in blk      # 武术方法论关键词
    proj2 = _proj(db, tmp_path, "野餐日记。")
    assert directing_block(db, proj2, tmp_path / "data") == ""
