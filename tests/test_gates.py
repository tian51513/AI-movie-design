# tests/test_gates.py
"""门禁提取到 engine（计划5B 任务2）：gate_pass 与手动端点逻辑/语义一致（409/422 不变）。"""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.paths import data_to_abs
from comic_studio.engine.pipeline_gates import GateStageError, gate_pass
from comic_studio.engine.projects import create_project, get_project, set_stage
from comic_studio.engine.shots import persist_shots, update_shot
from comic_studio.web.app import create_app


def _proj(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "门禁剧", "16:9", "正文")["id"]
    return db, pid


def _add_asset_with_sheet(tmp_path, db, pid):
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                      scenes=[], props=[]))
    for a in list_project_assets(db, pid):
        views = data_to_abs(tmp_path / "data", a["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")


def test_gate1_missing_then_pass(tmp_path):
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "analyzed")
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                      scenes=[], props=[]))
    with pytest.raises(ValueError, match="还没有参考图"):
        gate_pass(db, tmp_path / "data", pid, 1)
    _add_asset_with_sheet(tmp_path, db, pid)
    gate_pass(db, tmp_path / "data", pid, 1, source="自动通过")
    assert get_project(db, pid)["stage"] == "assets_ready"


def test_gate_stage_mismatch_distinct_error(tmp_path):
    db, pid = _proj(tmp_path)
    assert isinstance(
        pytest.raises(GateStageError, gate_pass, db, tmp_path / "data", pid, 1).value,
        ValueError)  # GateStageError 也是 ValueError（autopilot 统一吞）


def test_gate2_requires_prompts(tmp_path):
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "assets_ready")
    with pytest.raises(ValueError, match="尚无分镜"):
        gate_pass(db, tmp_path / "data", pid, 2)
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="t2v", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="")])[0]
    with pytest.raises(ValueError, match="缺提示词"):
        gate_pass(db, tmp_path / "data", pid, 2)
    update_shot(db, sid, {"prompt": "提示词"})
    gate_pass(db, tmp_path / "data", pid, 2)
    assert get_project(db, pid)["stage"] == "storyboard_ready"


def test_gate3_requires_videos(tmp_path):
    db, pid = _proj(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="t2v", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="p")])[0]
    with pytest.raises(ValueError, match="缺视频"):
        gate_pass(db, tmp_path / "data", pid, 3)
    update_shot(db, sid, {"video_path": "projects/门禁剧/shots/1/video_v1.mp4"})
    gate_pass(db, tmp_path / "data", pid, 3)
    assert get_project(db, pid)["stage"] == "rendered"


def test_routes_keep_http_semantics(tmp_path):
    """routes 转调后：409（阶段不符）/422（缺件）语义不变。"""
    db = Database(tmp_path / "s2.db"); db.migrate()
    pid = create_project(db, tmp_path / "data2", "接口门禁", "16:9", "正文")["id"]
    with TestClient(create_app(tmp_path / "s2.db", tmp_path / "data2",
                               start_workers=False)) as c:
        # created 阶段过门2 → 409
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 409
        # assets_ready 无分镜过门2 → 422
        set_stage(db, pid, "assets_ready")
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 422
