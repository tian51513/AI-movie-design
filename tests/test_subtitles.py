# tests/test_subtitles.py
"""P6 Task 2：SRT 字幕生成——从 dialogue + 分镜时长计算时间戳。"""
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import persist_shots


def _proj(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "字幕剧", "16:9", "正文")["id"]
    persist_shots(db, pid, [
        NS(text_span="", description="镜1", shot_type="", camera={},
           duration=5.0, workflow_type="t2v",
           ledger={"dialogue": [{"speaker": "A", "line": "第一句"},
                                 {"speaker": "B", "line": "第二句"}]},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None),
        NS(text_span="", description="镜2", shot_type="", camera={},
           duration=3.0, workflow_type="t2v",
           ledger={"dialogue": [{"speaker": "A", "line": "第三句"}]},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None),
    ])
    return db, pid


def test_generate_srt(tmp_path):
    db, pid = _proj(tmp_path)
    from comic_studio.engine.subtitles import generate_srt
    srt_path = generate_srt(db, tmp_path / "data", pid)
    content = srt_path.read_text(encoding="utf-8")
    assert content.count("-->") == 3
    assert "00:00:00,000 -->" in content
    assert "00:00:05" in content
    assert "第一句" in content and "第二句" in content and "第三句" in content


def test_no_dialogue_empty_srt(tmp_path):
    db, pid = _proj(tmp_path)
    conn = db.connect()
    conn.execute("UPDATE shots SET ledger_json='{}' WHERE project_id=?", (pid,))
    conn.commit()
    from comic_studio.engine.subtitles import generate_srt
    srt_path = generate_srt(db, tmp_path / "data", pid)
    assert srt_path.read_text(encoding="utf-8").strip() == ""
