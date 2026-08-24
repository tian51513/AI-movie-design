# tests/test_projects.py
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.projects import (
    create_project, get_project, list_projects, set_stage, slugify, STAGES, update_video_params)


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


def test_novel_path_stored_relative_for_portability(tmp_path):
    """WSL ↔ Windows 共享 data/：DB 必须存相对路径（spec 跨环境约定）。"""
    from comic_studio.engine.paths import data_to_abs
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "跨端剧", "9:16", "正文")
    assert row["novel_path"] == "projects/跨端剧/novel.txt"          # 相对、POSIX 分隔
    assert data_to_abs(tmp_path / "data", row["novel_path"]).read_text(encoding="utf-8") == "正文"


def test_create_with_video_params(tmp_path):
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "视频参数剧", "16:9", "t",
                         video_megapixels=0.9, video_multiple=32,
                         video_speed="高质量", default_shot_duration=6.0)
    assert row["video_megapixels"] == 0.9 and row["video_speed"] == "高质量"
    assert row["default_shot_duration"] == 6.0


def test_update_video_params_validation(tmp_path):
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "p", "9:16", "t")
    upd = update_video_params(db, row["id"], video_megapixels=1.2, video_speed="快速")
    assert upd["video_megapixels"] == 1.2 and upd["video_speed"] == "快速"
    assert upd["video_multiple"] == 32  # 未传不动
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], video_speed="极速")
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], video_megapixels=9.9)
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], video_multiple=24)
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], default_shot_duration=0)
