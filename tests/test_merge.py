# tests/test_merge.py
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.merge import concat, ffmpeg_bin, merge_project, normalize, probe
from comic_studio.engine.projects import create_project, get_project, set_stage
from comic_studio.engine.shots import persist_shots, update_shot


def _make(src: Path, seconds=1, size="320x240") -> Path:
    subprocess.run(
        [ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size={size}:rate=10",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True, timeout=60)
    return src


def test_probe_and_normalize_roundtrip(tmp_path):
    a = _make(tmp_path / "a.mp4", 1, "320x240")
    p = probe(a)
    assert p["width"] == 320 and p["fps"] > 0 and p["duration"] >= 0.9
    dst = normalize(a, tmp_path / "a_norm.mp4", 640, 360, 10)
    assert dst.exists() and dst.stat().st_size > 0
    p2 = probe(dst)
    assert p2["width"] == 640 and p2["height"] == 360


def test_concat_two_clips(tmp_path):
    a = normalize(_make(tmp_path / "a.mp4", 1, "320x240"), tmp_path / "a2.mp4", 640, 360, 10)
    b = normalize(_make(tmp_path / "b.mp4", 1, "320x240"), tmp_path / "b2.mp4", 640, 360, 10)
    out = concat([a, b], tmp_path / "ep.mp4")
    assert out.exists()
    p = probe(out)
    assert p["duration"] >= 1.8 and p["width"] == 640


def _proj_with_shots(tmp_path, name):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", name, "16:9", "t")["id"]
    set_stage(db, pid, "rendered")
    drafts = [NS(text_span="", description="x", shot_type="", camera={},
                 duration=5.0, workflow_type="ref2va", ledger={},
                 character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
                 prompt=f"提示{i}") for i in (1, 2)]
    sids = persist_shots(db, pid, drafts)
    for i, sid in zip((1, 2), sids):
        dest = tmp_path / "data" / "projects" / name / "shots" / str(i)
        dest.mkdir(parents=True, exist_ok=True)
        _make(dest / "video_v1.mp4", 1, "320x240")
        update_shot(db, sid, {"video_path": f"projects/{name}/shots/{i}/video_v1.mp4",
                             "status": "rendered"})
    return db, pid


def test_merge_project_end_to_end(tmp_path):
    db, pid = _proj_with_shots(tmp_path, "合成剧")
    out = merge_project(db, tmp_path / "data", pid)
    assert out.exists()
    assert probe(out)["duration"] >= 1.8
    assert get_project(db, pid)["stage"] == "merged"
    ep2 = merge_project(db, tmp_path / "data", pid)  # 幂等重跑 → ep002
    assert "ep002" in str(ep2)


def test_merge_missing_video_raises(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "缺片剧", "16:9", "t")["id"]
    set_stage(db, pid, "rendered")
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
        prompt="a")])
    with pytest.raises(ValueError, match="无视频"):
        merge_project(db, tmp_path / "data", pid)
