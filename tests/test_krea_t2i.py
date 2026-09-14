# tests/test_krea_t2i.py
"""Krea2 文生图模板（Lazy_Krea2_文生图，2026-09-14 用户导入）：
LoRA 槽位开关语义（覆盖=开/空串=关/未覆盖=模板默认）+ t2i 步数设置。"""
import json

from fastapi.testclient import TestClient

from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.db import Database
from comic_studio.engine.genref import handle_gen_ref
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting
from comic_studio.engine.workflows import registry
from comic_studio.engine.workflows.filler import fill_workflow
from comic_studio.web.app import create_app
from comfy_mock import comfy_server


def _reg():
    return registry.scan_templates(registry.TEMPLATE_ROOT)


def test_krea_t2i_template_registered():
    t = _reg()["krea_t2i"]
    assert t.type == "t2i"
    # t2i 尺寸由宽高决定——百万像素不注入（2026-09-14 用户：MP 仅图生图缩放）
    assert set(t.inject_params) == {"seed", "steps", "width", "height"}
    slots = {s.label: s for s in t.models}
    assert slots["unet"].cls == "UNETLoader"
    assert slots["clip"].cls == "CLIPLoader"
    assert slots["vae"].cls == "VAELoader"
    # 8 个 LoRA 槽全带开关字段
    assert {s.label for s in t.models if s.switch_field} == \
        {f"lora{i}" for i in range(1, 9)}
    assert slots["lora3"].switch_field == "启用_3"
    assert slots["lora3"].field == "LoRA_3"
    assert slots["unet"].switch_field == ""


def test_filler_lora_switch_semantics():
    t = _reg()["krea_t2i"]
    wf, _ = fill_workflow(
        t, prompt="p", params={"seed": 7, "width": 768, "height": 1152},
        images=None, output_ctx={"project": "x", "asset": "y"},
        model_overrides={"lora1": "Krea2\\new.safetensors", "lora2": "",
                         "unet": "Krea2\\other.safetensors"})
    n = wf["4"]["inputs"]
    # 覆盖非空 → 文件名 + 开
    assert n["LoRA_1"] == "Krea2\\new.safetensors" and n["启用_1"] is True
    # 空串 → 只关开关（文件名保留）
    assert n["启用_2"] is False and n["LoRA_2"].endswith(".safetensors")
    # 未覆盖 → 模板默认原样（lora3 内置关）
    assert n["启用_3"] is False and n["LoRA_3"].endswith(".safetensors")
    assert wf["90"]["inputs"]["unet_name"] == "Krea2\\other.safetensors"
    # 尺寸/种子注入
    g = wf["2001"]["inputs"]
    assert g["宽度"] == 768 and g["高度"] == 1152 and g["种子"] == 7


def test_model_choices_switchable_current(tmp_path):
    """枚举端点：LoRA 槽 current 反映开关态（内置关 → 空=「（关闭）」）。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    create_project(db, tmp_path / "data", "枚举剧", "16:9", "t")
    with comfy_server("ok") as mock:
        set_setting(db, "comfy", {"base_url": mock.base_url})
        with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                                   start_workers=False)) as c:
            r = c.get("/api/settings/models/choices", params={"template": "krea_t2i"})
            assert r.status_code == 200, r.text
            slots = {s["label"]: s for s in r.json()}
            assert slots["lora1"]["switchable"] is True
            assert slots["lora1"]["current"].endswith(".safetensors")  # 内置开
            assert slots["lora3"]["switchable"] is True
            assert slots["lora3"]["current"] == ""                     # 内置关→空
            assert slots["unet"]["switchable"] is False
            assert "a.safetensors" in slots["lora1"]["choices"]        # mock 枚举


def _steps_setup(tmp_path, steps=None):
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "krea_t2i"})
    if steps is not None:
        comfy = {"t2i_steps": steps}
        base = db.connect().execute(
            "SELECT value_json FROM settings WHERE key='comfy'").fetchone()
        comfy_full = {**json.loads(base["value_json"]), **comfy} if base else comfy
        set_setting(db, "comfy", comfy_full)
    pid = create_project(db, tmp_path / "data", "步数剧", "9:16", "t")["id"]
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[], scenes=[NS(name="卧室", description="x", tags=[])],
                      props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    return db, jid


def test_t2i_steps_setting_injected(tmp_path):
    """comfy.t2i_steps>0 → 注入；0/缺省 → 走模板内置步数。"""
    db, jid = _steps_setup(tmp_path, steps=20)
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        assert m.prompts[0]["prompt"]["2001"]["inputs"]["步数"] == 20


def test_t2i_steps_zero_keeps_template_default(tmp_path):
    db, jid = _steps_setup(tmp_path, steps=0)
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        assert m.prompts[0]["prompt"]["2001"]["inputs"]["步数"] == 10  # 模板内置


def test_t2i_steps_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    create_project(db, tmp_path / "data", "设置剧", "16:9", "t")
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.put("/api/settings", json={"comfy": {"t2i_steps": 15}})
        assert r.status_code == 200
        assert c.get("/api/settings").json()["comfy"]["t2i_steps"] == 15
