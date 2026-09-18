# tests/test_render_routing.py
"""分镜渲染 type 路由（2026-09-18 重构）：fl2v/i2v 语义按 manifest type 判断，
不再硬编码 tmpl_id——设置页把 fl2v 映射切到自定义模板（如 Spectrum 加速道）
时，关键帧接线/FL2VA 提示词头/图片槽/降级全链路保持正确。"""
import json
import shutil
from pathlib import Path

import pytest

from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.rendershot import render_shot
from comic_studio.engine.settings import set_setting
from comic_studio.engine.shots import persist_shots, update_shot
from comic_studio.engine.workflows import registry
from tests.comfy_mock import comfy_server
from tests.test_rendershot import _setup, _shot_draft


@pytest.fixture
def custom_fl2v(tmp_path, monkeypatch):
    """临时模板目录：h3_fl2v 复制改名 my_fl2v（同 type=fl2v）。"""
    tdir = tmp_path / "tmpls"
    tdir.mkdir()
    src = Path("templates/workflows")
    for ext in ("yaml", "api.json"):
        text = (src / f"h3_fl2v.{ext}").read_text(encoding="utf-8")
        (tdir / f"my_fl2v.{ext}").write_text(
            text.replace("id: h3_fl2v", "id: my_fl2v")
                .replace("file: h3_fl2v.api.json", "file: my_fl2v.api.json"),
            encoding="utf-8")
    # 降级目标也要在注册表里
    for ext in ("yaml", "api.json"):
        shutil.copy(src / f"h3_i2v.{ext}", tdir / f"h3_i2v.{ext}")
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", tdir)
    return tdir


def _kf(db, tmp_path, proj_slug, seq, with_end=True):
    from comic_studio.engine.paths import data_to_abs
    shot_dir = data_to_abs(tmp_path / "data",
                           f"projects/{proj_slug}/shots/{seq}")
    shot_dir.mkdir(parents=True, exist_ok=True)
    (shot_dir / "kf_start.png").write_bytes(b"\x89PNG start")
    if with_end:
        (shot_dir / "kf_end.png").write_bytes(b"\x89PNG end")


def _slug(db, pid):
    from comic_studio.engine.projects import get_project
    return get_project(db, pid)["slug"]


def test_custom_fl2v_template_routes_by_type(tmp_path, custom_fl2v):
    """template_map.fl2v → 自定义 id：首尾帧槽 + FL2VA 对齐头照常注入。"""
    db, pid, assets = _setup(tmp_path)
    set_setting(db, "template_map", {"fl2v": "my_fl2v"})
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]], workflow_type="fl2v")])[0]
    update_shot(db, sid, {"prompt": "林晨推门。"})
    _kf(db, tmp_path, _slug(db, pid), 1)
    with comfy_server("ok", video=True) as m:
        render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        # 首尾帧注入到 my_fl2v 的 LoadImage 槽位（47/48）
        assert wf["47"]["inputs"]["image"].startswith("cs__")
        assert wf["48"]["inputs"]["image"].startswith("cs__")
        # FL2VA 对齐头（type 语义，不认模板 id）
        assert wf["64"]["inputs"]["prompt"].startswith(
            "How the reference pictures align with the target video")
        assert m.prompts[0]["prompt"]["68"]["inputs"]["filename_prefix"]


def test_custom_fl2v_missing_kf_end_degrades_to_i2v(tmp_path, custom_fl2v):
    """kf_end 缺失 → 降级 i2v 模板（不因自定义 fl2v id 而绕过降级）。"""
    db, pid, assets = _setup(tmp_path)
    set_setting(db, "template_map", {"fl2v": "my_fl2v"})
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]], workflow_type="fl2v")])[0]
    update_shot(db, sid, {"prompt": "林晨推门。"})
    _kf(db, tmp_path, _slug(db, pid), 1, with_end=False)
    with comfy_server("ok", video=True) as m:
        render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        # h3_i2v 的 SaveVideo 节点 63 出现 = 降级生效
        assert "63" in wf and "68" not in wf
        assert wf["53"]["inputs"]["image"].startswith("cs__")  # 仅首帧槽


def test_rtx_vsr_param_flows_to_switch_links(tmp_path, custom_fl2v):
    """rtx_vsr_enabled=True → params.rtx_vsr=True；模板无 switch_links 声明时
    提交载荷不受影响（filler 忽略）。my_fl2v 未声明 → 断言不炸即可。"""
    db, pid, assets = _setup(tmp_path)
    set_setting(db, "template_map", {"fl2v": "my_fl2v"})
    set_setting(db, "comfy", {"rtx_vsr_enabled": True})
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]], workflow_type="fl2v")])[0]
    update_shot(db, sid, {"prompt": "林晨推门。"})
    _kf(db, tmp_path, _slug(db, pid), 1)
    with comfy_server("ok", video=True) as m:
        render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert m.prompts  # 开关开但模板无声明 → 照常提交
