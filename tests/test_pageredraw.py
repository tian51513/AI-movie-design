# tests/test_pageredraw.py
"""动态漫角色重绘（2026-09-09）：版本留档/整页重绘/批量编排。"""

from comic_studio.engine.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _motion_project(tmp_path, n=3, redraw=1):
    from comic_studio.engine.comic import import_comic
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "重绘剧", "9:16",
                       [(f"p{i}.png", PNG * i) for i in range(1, n + 1)],
                       redraw_characters=redraw)["id"]
    return db, pid


def test_kf_versions_natural_order(tmp_path):
    from comic_studio.engine.pageredraw import kf_versions, save_kf_version
    d = tmp_path / "shots/1"; d.mkdir(parents=True)
    (d / "kf_start_v1.png").write_bytes(PNG)
    assert kf_versions(d, "start") == ["v1"]
    (d / "kf_start_v2.png").write_bytes(PNG * 2)
    (d / "kf_start_v10.png").write_bytes(PNG * 3)   # 数字序防 v10<v2
    assert kf_versions(d, "start") == ["v1", "v2", "v10"]
    assert kf_versions(d, "end") == []


def test_activate_start_syncs_prev_end_and_clears_video(tmp_path):
    """切 start 版本 → 前镜尾帧联动同名版本 + 双镜 video_path 置空 + ledger 记录。"""
    import json as _json
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.shots import list_shots, update_shot
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.pageredraw import save_kf_version, activate_kf_version
    shots = list_shots(db, pid)
    s2 = shots[1]
    d2 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/2")
    src = tmp_path / "new.png"; src.write_bytes(PNG * 9)
    save_kf_version(d2, "start", src)               # → v2 + 活动拷贝已刷新
    # 预置两镜已渲染（模拟旧视频）
    for s in (shots[0], s2):
        update_shot(db, s["id"], {"status": "rendered",
                                  "video_path": f"projects/重绘剧/shots/{s['seq']}/video.mp4"})
    out = activate_kf_version(db, tmp_path / "data", s2["id"], "start", "v2")
    assert out["start"] == "v2"
    assert set(out["video_cleared"]) == {s2["id"], shots[0]["id"]}
    d1 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/1")
    assert (d1 / "kf_end_v2.png").read_bytes() == PNG * 9   # 前镜尾帧联动
    assert (d1 / "kf_end.png").read_bytes() == PNG * 9
    after = {s["id"]: s for s in list_shots(db, pid)}
    assert after[shots[0]["id"]]["video_path"] is None
    assert after[s2["id"]]["video_path"] is None
    led = _json.loads(after[s2["id"]]["ledger_json"])
    assert led["kf_active"]["start"] == "v2"


def test_activate_end_only_last_shot(tmp_path):
    """尾帧独立切版仅最后一镜（其余 422 由路由层拦，这里测引擎层 ValueError）。"""
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine.pageredraw import activate_kf_version
    import pytest
    with pytest.raises(ValueError):
        activate_kf_version(db, tmp_path / "data", list_shots(db, pid)[0]["id"],
                            "end", "v1")
