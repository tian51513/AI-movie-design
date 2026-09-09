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


def test_build_page_redraw_prompt_style_and_clean_text(tmp_path):
    db, pid = _motion_project(tmp_path)
    from comic_studio.engine.projects import update_video_params
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine.pageredraw import build_page_redraw_prompt
    from comic_studio.engine.projects import get_project
    update_video_params(db, pid)  # no-op 占位，保证 proj 新鲜
    proj = get_project(db, pid)
    shot = list_shots(db, pid)[0]
    p = build_page_redraw_prompt(db, proj, shot)
    assert "清除对白气泡内的" in p              # 决策 9：清文字（文本为「…全部文字」）
    assert "按原页画风" in p                    # 画风空=原画风高清化
    # 转风格：改画风后出现画风段与禁令
    import sqlite3
    conn = db.connect()
    conn.execute("UPDATE projects SET style=?, style_vis=? WHERE id=?",
                 ("宫崎骏水彩画风", "水彩手绘质感", pid))
    conn.commit()
    p2 = build_page_redraw_prompt(db, get_project(db, pid), shot)
    assert "水彩手绘质感" in p2


def test_redraw_page_produces_v2_and_clears_video(tmp_path):
    """整页重绘：comfy_mock 出图 → start v2 + 前镜（若有）尾帧联动 + 视频置空。"""
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.shots import list_shots, update_shot
    from comic_studio.engine.pageredraw import redraw_page, kf_versions
    from comic_studio.engine.paths import data_to_abs
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    shots = list_shots(db, pid)
    s2 = shots[1]
    update_shot(db, s2["id"], {"status": "rendered",
                               "video_path": "projects/重绘剧/shots/2/video.mp4"})
    with comfy_server("ok") as m:
        out = redraw_page(db, tmp_path / "data", s2["id"], ComfyClient(m.base_url))
    assert out.name == "kf_start_v2.png"
    d2 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/2")
    assert kf_versions(d2, "start") == ["v1", "v2"]
    after = {s["id"]: s for s in list_shots(db, pid)}
    assert after[s2["id"]]["video_path"] is None            # 决策 12
    assert after[shots[0]["id"]]["video_path"] is None      # 前镜尾帧联动连带
    d1 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/1")
    assert (d1 / "kf_end_v2.png").exists()
    # 底图=原页（不是活动 kf）：上传清单里应有 page_002.png
    # （comfy_mock 记录上传名，见 _make_handler；此处以产物存在为弱断言）


def test_redraw_page_bootstraps_missing_pages(tmp_path):
    """旧项目（PATCH 后开重绘，无 pages/）：从活动 kf_start 拷贝建源页再重绘。"""
    import shutil
    db, pid = _motion_project(tmp_path)
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    shutil.rmtree(data_to_abs(tmp_path / "data", f"projects/{slug}/pages"))
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine.pageredraw import redraw_page
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok") as m:
        redraw_page(db, tmp_path / "data", list_shots(db, pid)[0]["id"],
                    ComfyClient(m.base_url))
    assert (data_to_abs(tmp_path / "data", f"projects/{slug}/pages/page_001.png")).exists()
