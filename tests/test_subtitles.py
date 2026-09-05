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


def test_generate_srt_accepts_frame_spans(tmp_path):
    """快车道混音（2026-08-29）：导演台实际时长=帧数/24（对齐后 5s→5.167s），
    与 duration 漂移累计——SRT 必须能用外部 spans（(seq,start,dur) 列表）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.shots import persist_shots
    from comic_studio.engine.subtitles import generate_srt
    from types import SimpleNamespace as NS
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "跨度剧", "9:16", "t")["id"]
    persist_shots(db, pid, [
        NS(text_span="", description="a", shot_type="", camera={}, duration=5.0,
           workflow_type="ref2va", ledger={"dialogue": [{"speaker": "甲", "line": "你好"}]},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None),
        NS(text_span="", description="b", shot_type="", camera={}, duration=5.0,
           workflow_type="ref2va", ledger={}, character_ids=[], scene_ids=[],
           prop_ids=[], depends_on=None)])
    spans = [(1, 0.0, 124 / 24), (2, 124 / 24, 107 / 24)]  # 帧数轴
    srt = generate_srt(db, tmp_path / "data", pid, spans=spans)
    text = srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:05,166" in text  # 镜1 帧数时长（124/24）


def test_generate_srt_proportional_by_length(tmp_path):
    """C7（2026-09-01 台词组拆镜配套）：镜内多句按字数比例分时长——
    台词组打包后一镜 3~8 句，均分会让短句占长、长句赶读。"""
    import json as _json
    db, pid = _proj(tmp_path)
    conn = db.connect()
    conn.execute(
        "UPDATE shots SET duration=8, ledger_json=? WHERE seq=1",
        (_json.dumps({"dialogue": [
            {"speaker": "A", "line": "好"},                     # 1 字
            {"speaker": "B", "line": "这一段台词明显要长得多"}]}),))  # 10 字
    conn.commit()
    from comic_studio.engine.subtitles import generate_srt
    srt = generate_srt(db, tmp_path / "data", pid).read_text(encoding="utf-8")
    stamps = [l for l in srt.splitlines() if "-->" in l]
    assert len(stamps) >= 2
    def _sec(ts):
        h, m, rest = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest.replace(",", "."))
    d1 = _sec(stamps[0].split("-->")[1]) - _sec(stamps[0].split("-->")[0])
    d2 = _sec(stamps[1].split("-->")[1]) - _sec(stamps[1].split("-->")[0])
    assert abs((d1 + d2) - 8.0) < 0.01          # 总时长守恒
    assert d2 / d1 > 8                          # 10:1 字数比 → 时长比接近 10


def test_srt_uses_audio_duration_when_longer(tmp_path):
    """设计B3：镜 1 配音 8s > duration 5s → 字幕轴按 8s（与合成端末帧补长一致，
    后续镜起点不漂移）。"""
    import subprocess
    from comic_studio.engine.merge import ffmpeg_bin
    db, pid = _proj(tmp_path)
    shot_dir = tmp_path / "data" / "projects" / "字幕剧" / "shots" / "1"
    shot_dir.mkdir(parents=True)
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=24000:cl=mono", "-t", "8", "-q:a", "9",
                    str(shot_dir / "dialogue.mp3")],
                   check=True, capture_output=True, timeout=60)
    from comic_studio.engine.subtitles import generate_srt
    content = generate_srt(db, tmp_path / "data", pid).read_text(encoding="utf-8")
    assert "00:00:08" in content          # 镜2 台词起点 8s（旧逻辑漂移到 5s）
    assert "00:00:05 -->" not in content


def test_srt_timeline_follows_real_media_durations(tmp_path):
    """2026-09-05 真机（ep007 配音/分镜错位）：轴长不能再信 duration 字段——
    17k+5 帧对齐让 4.0→4.5/5.0→5.2，46 镜累计 ~9s 漂移。改按真实视频时长排轴；
    有配音且更长的镜按 音频+0.1（与 merge 补长段长一致）。"""
    import subprocess
    from comic_studio.engine.merge import ffmpeg_bin, probe
    from comic_studio.engine.shots import update_shot
    db, pid = _proj(tmp_path)
    ids = list_shots_ids = db.connect().execute(
        "SELECT id, seq FROM shots WHERE project_id=? ORDER BY seq", (pid,)).fetchall()
    shot1_dir = tmp_path / "data" / "projects" / "字幕剧" / "shots" / "1"
    shot1_dir.mkdir(parents=True)
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=6:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(shot1_dir / "video_v1.mp4")],
                   check=True, capture_output=True, timeout=60)
    update_shot(db, ids[0]["id"], {"video_path": "projects/字幕剧/shots/1/video_v1.mp4"})
    from comic_studio.engine.subtitles import generate_srt
    content = generate_srt(db, tmp_path / "data", pid).read_text(encoding="utf-8")
    # 镜1 真实 6s（duration 字段 5.0）→ 镜2 台词起点 6s，不是 5s
    assert "00:00:06" in content, content
    assert "00:00:05 -->" not in content
