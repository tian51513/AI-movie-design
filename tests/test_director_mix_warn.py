# tests/test_director_mix_warn.py
"""M8（2026-09-05 审计）：快车道 spans 未适配音频收口——TTS 长于段会被
apad+atrim 硬截断；最小可见化：超段即 warn（完整收口需帧数轴重切，留待决策）。"""
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project


def test_warn_long_tts(tmp_path, monkeypatch):
    from comic_studio.engine import director_mix as DM
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "快车道音", "16:9", "t")["id"]
    (tmp_path / "a.mp3").write_bytes(b"mp3")
    monkeypatch.setattr(DM, "probe", lambda p: {"duration": 4.2})
    spans = [(1, 0.0, 2.0, tmp_path / "a.mp3"), (2, 2.0, 5.0, None)]
    DM.warn_long_tts(db, pid, spans)
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%将被截断%'").fetchone()["c"]
    assert n == 1


def test_warn_long_tts_quiet_when_fits(tmp_path, monkeypatch):
    from comic_studio.engine import director_mix as DM
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "快车道音2", "16:9", "t")["id"]
    (tmp_path / "a.mp3").write_bytes(b"mp3")
    monkeypatch.setattr(DM, "probe", lambda p: {"duration": 1.5})
    DM.warn_long_tts(db, pid, [(1, 0.0, 2.0, tmp_path / "a.mp3")])
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%将被截断%'").fetchone()["c"]
    assert n == 0
