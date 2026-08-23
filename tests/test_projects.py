# tests/test_projects.py
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.projects import (
    create_project, get_project, list_projects, set_stage, slugify, STAGES)


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_slugify_strips_path_chars():
    assert slugify('斗破/苍穹:第一部?') == '斗破_苍穹_第一部_'


def test_create_project_writes_novel_and_row(tmp_path):
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "测试剧", "9:16", "第一章 正文")
    assert row["slug"] == "测试剧"
    assert row["stage"] == "created"
    novel = (tmp_path / "data" / "projects" / "测试剧" / "novel.txt").read_text(encoding="utf-8")
    assert novel == "第一章 正文"
    assert get_project(db, row["id"])["name"] == "测试剧"


def test_duplicate_name_gets_suffix(tmp_path):
    db = _db(tmp_path)
    create_project(db, tmp_path / "data", "同名的剧", "16:9", "a")
    second = create_project(db, tmp_path / "data", "同名的剧", "16:9", "b")
    assert second["slug"] == "同名的剧-2"


def test_set_stage_validates(tmp_path):
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "x", "9:16", "t")
    set_stage(db, row["id"], "analyzed")
    assert get_project(db, row["id"])["stage"] == "analyzed"
    with pytest.raises(ValueError):
        set_stage(db, row["id"], "nonexistent_stage")
    assert len(list_projects(db)) == 1
