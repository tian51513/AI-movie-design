# tests/test_shots.py
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import (get_shot, list_shots, mark_stale_for_asset,
                                       persist_shots, update_shot)


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def _draft(seq_deps=None, **kw):
    base = dict(text_span="原文", description="镜头描述", shot_type="对话",
                camera={"景别": "中景", "机位": "平视", "运镜": "固定", "转场": "切"},
                duration=5.0, workflow_type="ref2va",
                ledger={"must_appear": ["萧炎"], "assets": {"characters": [1], "scenes": [], "props": []}},
                character_ids=[1], scene_ids=[], prop_ids=[],
                depends_on=seq_deps)
    base.update(kw)
    return NS(**base)


def test_persist_replace_semantics_and_listing(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    ids = persist_shots(db, pid, [_draft(), _draft(depends_on=1, description="第二镜")])
    assert [r["seq"] for r in list_shots(db, pid)] == [1, 2]
    assert list_shots(db, pid)[1]["depends_on"] == ids[0]
    # 重拆替换
    persist_shots(db, pid, [_draft(description="重拆后唯一镜")])
    rows = list_shots(db, pid)
    assert len(rows) == 1 and rows[0]["description"] == "重拆后唯一镜"


def test_update_shot_whitelist(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [_draft()])[0]
    update_shot(db, sid, {"prompt": "新提示词", "workflow_type": "fl2v", "status": "ready"})
    shot = get_shot(db, sid)
    assert shot["prompt"] == "新提示词" and shot["workflow_type"] == "fl2v"
    import pytest
    with pytest.raises(ValueError):
        update_shot(db, sid, {"id": 999})  # 非白名单字段拒绝


def test_mark_stale_for_asset(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    persist_shots(db, pid, [_draft(), _draft(character_ids=[2], scene_ids=[7])])
    n = mark_stale_for_asset(db, 1)
    assert n == 1
    statuses = [r["status"] for r in list_shots(db, pid)]
    assert statuses == ["stale", "pending"]
