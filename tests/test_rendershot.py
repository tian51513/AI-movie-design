# tests/test_rendershot.py
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.assets import persist_assets
from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.rendershot import (
    SPEED_STEPS, collect_ref_images, pick_template_id, render_shot)
from comic_studio.engine.shots import get_shot, list_shots, persist_shots, update_shot
from comfy_mock import comfy_server


def _setup(tmp_path, **proj_kw):
    db = Database(tmp_path / "s.db"); db.migrate()
    kw = dict(style="真人电影", video_megapixels=0.6, video_speed="高质量",
              default_shot_duration=5.0)
    kw.update(proj_kw)
    pid = create_project(db, tmp_path / "data", "渲染剧", "16:9", "林晨推门。", **kw)["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                      scenes=[NS(name="庭院", description="古宅", tags=[])], props=[]))
    assets = {r["name"]: r for r in
              __import__("comic_studio.engine.assets", fromlist=["list_project_assets"])
              .list_project_assets(db, pid)}
    # 给两个资产放 sheet.png
    from comic_studio.engine.paths import data_to_abs
    for a in assets.values():
        views = data_to_abs(tmp_path / "data", a["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
    return db, pid, assets


def _shot_draft(**kw):
    base = dict(text_span="", description="推门", shot_type="", camera={},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_pick_template_mapping():
    assert pick_template_id({"workflow_type": "ref2va"}) == "h3_ref2va"
    assert pick_template_id({"workflow_type": "fl2v"}) == "h3_i2v"
    assert pick_template_id({"workflow_type": "t2v"}) == "h3_t2v"
    assert pick_template_id({"workflow_type": None}) == "h3_ref2va"


def test_collect_ref_images_slots(tmp_path):
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]],
        scene_ids=[assets["庭院"]["id"]])])[0]
    refs = collect_ref_images(db, get_shot(db, sid))
    assert [r["slot"] for r in refs] == ["ref0", "ref1"]
    # 单资产：复制补第二槽
    sid2 = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]])])[0]
    refs2 = collect_ref_images(db, get_shot(db, sid2))
    assert len(refs2) == 2 and refs2[0]["path"] == refs2[1]["path"]


def test_render_shot_end_to_end(tmp_path, monkeypatch):
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]])])[0]
    update_shot(db, sid, {"prompt": "林晨在庭院推门，真人电影质感。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert out.exists() and out.stat().st_size == 2
        wf = m.prompts[0]["prompt"]
        assert wf["110"]["inputs"]["prompt"].startswith("林晨在庭院推门")
        assert wf["116"]["inputs"]["megapixels"] == 0.6
    shot = get_shot(db, sid)
    assert shot["status"] == "rendered"
    assert shot["video_path"] == f"projects/渲染剧/shots/1/video.mp4"


def test_handle_gen_shot_registered_and_guard():
    """Step 1: 验证 handler 注册与守卫（非空 shot 校验）"""
    from comic_studio.engine.queue.worker import HANDLERS
    import comic_studio.engine.rendershot  # noqa: F401
    assert "gen_shot" in HANDLERS

    import tempfile
    import pathlib
    import json
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "g.db")
    db.migrate()
    pid = create_project(db, tmp / "d", "g", "9:16", "t")["id"]
    # 手动插入 job（shot_id=999 不存在）以触发守卫
    # 禁用外键约束以插入无效 job
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO jobs (project_id, shot_id, type, resource, payload_json, status) "
        "VALUES (?,?,?,?,?, 'pending')",
        (pid, 999, "gen_shot", "gpu_comfy", json.dumps({"shot_id": 999}, ensure_ascii=False))
    )
    jid = cur.lastrowid
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    from comic_studio.engine.jobs import get_job
    from comic_studio.engine.rendershot import handle_gen_shot
    with pytest.raises(ValueError, match="分镜"):
        handle_gen_shot(db, tmp / "d", get_job(db, jid), None)
