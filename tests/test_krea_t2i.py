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


def _steps_setup(tmp_path, template_params=None):
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "krea_t2i"})
    if template_params is not None:
        set_setting(db, "template_params", template_params)
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


def test_template_params_steps_injected(tmp_path):
    """template_params 按模板配步数（2026-09-14 用户：模型切换区各模板各自设）：
    >0 → 注入；0/缺省 → 走模板内置步数。"""
    db, jid = _steps_setup(tmp_path, {"krea_t2i": {"steps": 20}})
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        assert m.prompts[0]["prompt"]["2001"]["inputs"]["步数"] == 20


def test_template_params_zero_keeps_template_default(tmp_path):
    db, jid = _steps_setup(tmp_path, {"krea_t2i": {"steps": 0}})
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        assert m.prompts[0]["prompt"]["2001"]["inputs"]["步数"] == 10  # 模板内置


def test_template_params_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    create_project(db, tmp_path / "data", "设置剧", "16:9", "t")
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.put("/api/settings", json={"template_params": {
            "krea_t2i": {"steps": 15}}})
        assert r.status_code == 200
        got = c.get("/api/settings").json()["template_params"]
        assert got["krea_t2i"]["steps"] == 15
        # 未知模板 / 未知参数键 → 422；空字典=清除该模板参数
        assert c.put("/api/settings", json={"template_params": {
            "nope_tmpl": {"steps": 1}}}).status_code == 422
        assert c.put("/api/settings", json={"template_params": {
            "krea_t2i": {"bogus": 1}}}).status_code == 422
        assert c.put("/api/settings", json={"template_params": {
            "krea_t2i": {}}}).status_code == 200
        assert not c.get("/api/settings").json()["template_params"].get("krea_t2i")


def test_comic_page_krea2_full_lora_slots():
    """全模式工作台（2026-09-14 用户：漫画页主力道也要设置页可配）：
    vae + lora1~8 开关槽与 krea_t2i 同权。"""
    t = _reg()["comic_page_krea2"]
    slots = {s.label: s for s in t.models}
    assert slots["vae"].cls == "VAELoader" and slots["vae"].node == "93"
    assert {s.label for s in t.models if s.switch_field} == \
        {f"lora{i}" for i in range(1, 9)}
    assert slots["lora1"].field == "LoRA_1" and slots["lora1"].node == "4"


def test_templates_payload_carries_params_and_slots(tmp_path):
    """GET /api/settings 的 model_templates 带 params/image_slots——
    前端按此决定「步数」输入框与快道槽位过滤。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    create_project(db, tmp_path / "data", "载荷剧", "16:9", "t")
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        tmpls = {t["id"]: t for t in c.get("/api/settings").json()["model_templates"]}
        assert "steps" in tmpls["krea_t2i"]["params"]
        assert set(tmpls["comic_page_krea2"]["image_slots"]) == {"scene", "char"}
        assert tmpls["comic_page"]["image_slots"] == []


def _krea_main_setup(tmp_path):
    """comic_page_krea2 当主图模板 + 已有 main.png 的重生场景
    （2026-09-14 真机 400：char 槽留 char1.png 默认引用）。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "comic_page_krea2"})
    pid = create_project(db, tmp_path / "data", "主图剧", "9:16", "t")["id"]
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from types import SimpleNamespace as NS
    from pathlib import Path
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="约翰", appearance="金发男子", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    main_png = Path(tmp_path / "data" / asset["library_dir"] / "main.png")
    main_png.parent.mkdir(parents=True, exist_ok=True)
    main_png.write_bytes(b"\x89PNG fake main")
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy",
                      payload={"asset_id": asset["id"], "stage": "main"})
    return db, jid


def test_gen_ref_krea_workbench_placeholder_fill(tmp_path):
    """主图重生经 Krea2 快道：main.png 进人物槽（char），未提供的 scene 槽
    上传中性灰占位——两个 LoadImage 都指向已上传文件，无 char1.png/page.png
    残留默认引用（真机 400 根因）。"""
    db, jid = _krea_main_setup(tmp_path)
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        # 人物槽=已上传的旧主图（cs__ 命名），场景槽=灰占位（cs__ 命名）
        assert wf["100"]["inputs"]["image"].startswith("cs__")
        assert wf["100"]["inputs"]["image"] != "char1.png"
        assert wf["23"]["inputs"]["image"].startswith("cs__")
        assert wf["23"]["inputs"]["image"] != "page.png"
        # 占位图确实上传过（两次图片上传：旧主图 + 灰）
        assert len(m.uploads) >= 2


def test_gen_ref_krea_workbench_scene_asset_all_placeholder(tmp_path):
    """场景资产（单段路径，无 main 注入）：双槽全灰占位，不留默认引用。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "comic_page_krea2"})
    pid = create_project(db, tmp_path / "data", "场景剧", "9:16", "t")["id"]
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[], scenes=[NS(name="卧室", description="x", tags=[])],
                      props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        assert wf["23"]["inputs"]["image"].startswith("cs__")
        assert wf["100"]["inputs"]["image"].startswith("cs__")
        assert len(m.uploads) == 2
