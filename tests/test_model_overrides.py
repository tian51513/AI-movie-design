# tests/test_model_overrides.py
"""工作流模型切换（计划5B 任务6）：manifest 槽位 + filler 注入 + choices 枚举。"""
from fastapi.testclient import TestClient

from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.workflows import registry
from comic_studio.engine.workflows.filler import fill_workflow
from comic_studio.web.app import create_app
from comfy_mock import comfy_server


def test_manifest_model_slots():
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    t = reg["h3_ref2va"]
    slots = {s.label: s for s in t.models}
    assert set(slots) == {"unet", "clip", "vae_audio", "vae_video",
                          "lora_realism", "lora_turbo"}
    assert slots["unet"].cls == "UNETLoader"
    assert slots["clip"].node == "92"
    assert slots["lora_realism"].label_cn == "真实感 LoRA"
    assert reg["t2i_ref"].models[0].label == "ckpt"
    # 七模板全覆盖：无槽位模板=枚举空、用户误读为失败（2026-08-25 真机）
    for tid in ("h3_ref2va", "h3_i2v", "h3_t2v", "h3_fl2v", "t2i_ref",
                "zimage_t2i", "character_views"):
        assert reg[tid].models, f"{tid} 缺模型槽位"


def test_filler_injects_overrides_only():
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    t = reg["h3_ref2va"]
    wf, _ = fill_workflow(
        t, prompt="p", params={"seed": 1}, images=None,
        output_ctx={"project": "x", "asset": "y"},
        model_overrides={"unet": "h3_q4.safetensors"})
    assert wf["90"]["inputs"]["unet_name"] == "h3_q4.safetensors"
    # 未覆盖槽位保持模板原值
    assert wf["92"]["inputs"]["clip_name"].endswith(".safetensors")
    before = reg["h3_ref2va"].api_json()["92"]["inputs"]["clip_name"]
    assert wf["92"]["inputs"]["clip_name"] == before


def test_choices_endpoint_and_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    create_project(db, tmp_path / "data", "模型剧", "16:9", "t")
    with comfy_server(mode="ok") as mock:
        from comic_studio.engine.settings import set_setting
        set_setting(db, "comfy", {"base_url": mock.base_url})
        with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                                   start_workers=False)) as c:
            r = c.get("/api/settings/models/choices", params={"template": "h3_ref2va"})
            assert r.status_code == 200
            slots = {s["label"]: s for s in r.json()}
            assert slots["unet"]["cls"] == "UNETLoader"
            assert slots["unet"]["label_cn"] == "主模型 UNet"
            assert slots["unet"]["current"].endswith(".safetensors")  # 模板内置当前值
            assert "a.safetensors" in slots["unet"]["choices"]  # 来自 /object_info
            # PUT 覆盖 → 读回 → filler 端到端生效
            r = c.put("/api/settings", json={"model_overrides": {
                "h3_ref2va": {"unet": "h3_q4.safetensors"}}})
            assert r.status_code == 200
            got = c.get("/api/settings").json()["model_overrides"]
            assert got["h3_ref2va"]["unet"] == "h3_q4.safetensors"
            # 非法标签 → 422
            r = c.put("/api/settings", json={"model_overrides": {
                "h3_ref2va": {"nope": "x.safetensors"}}})
            assert r.status_code == 422


def test_empty_override_dict_clears_template(tmp_path):
    """恢复默认：PUT 空字典 → 清除该模板覆盖（merge 语义不适用于清空）。"""
    db = Database(tmp_path / "s2.db"); db.migrate()
    create_project(db, tmp_path / "data2", "清覆剧", "16:9", "t")
    with TestClient(create_app(tmp_path / "s2.db", tmp_path / "data2",
                               start_workers=False)) as c:
        c.put("/api/settings", json={"model_overrides": {
            "h3_ref2va": {"unet": "x.safetensors"}}})
        c.put("/api/settings", json={"model_overrides": {"h3_ref2va": {}}})
        assert c.get("/api/settings").json()["model_overrides"].get("h3_ref2va") in (None, {})
