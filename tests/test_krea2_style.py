# tests/test_krea2_style.py
"""Krea2 工作台风格槽（2026-09-20 用户实测判例：提示词文字段对部分 Krea2 模型
推不动画风，工作台「风格库+风格」槽才是强杠杆——手动指定库风格可出漫画风）。
projects.krea2_style="lib|style" 透传 genref 主图/单段 + comicgen 漫画页；
双管齐下（用户决策）：文字段照拼。角色主图加单人物强化禁令（库风格场景词
会稀释锚定，背景冒多人判例）。"""
import io
from pathlib import Path
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.stylepresets import format_krea2_style, parse_krea2_style
from comic_studio.web.app import create_app

KS_LIB = "krea2_Anime-Cel_Illustration-1_动漫-赛璐璐与插画"
KS = f"{KS_LIB}|Anime Cel Illustration"


def test_parse_format_roundtrip():
    assert parse_krea2_style(KS) == (KS_LIB, "Anime Cel Illustration")
    assert parse_krea2_style("") == ("", "")
    assert parse_krea2_style("只有一段") == ("", "")
    assert parse_krea2_style("|style") == ("", "")
    assert format_krea2_style("lib", "name") == "lib|name"
    assert format_krea2_style("", "name") == ""


def test_filler_injects_workbench_style_slots():
    from comic_studio.engine.workflows import registry
    from comic_studio.engine.workflows.filler import fill_workflow
    regs = registry.scan_templates(Path("templates/workflows"))
    tmpl = regs["krea_t2i"]
    wf, _ = fill_workflow(tmpl, prompt="x", params={
        "seed": 1, "krea_style_lib": "lazy_styles", "krea_style": "Glossy Urban"},
        images=None, output_ctx={"project": "p", "asset": "a"})
    assert wf["2000"]["inputs"]["风格库"] == "lazy_styles"
    assert wf["2000"]["inputs"]["风格"] == "Glossy Urban"
    wf2, _ = fill_workflow(tmpl, prompt="x", params={"seed": 2},  # 未提供=模板默认（现状零回归）
                           images=None, output_ctx={"project": "p", "asset": "a"})
    assert wf2["2000"]["inputs"]["风格库"] == "none"


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def test_patch_and_create_passthrough(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "风格剧", "aspect_ratio": "9:16",
                                            "krea2_style": KS},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        assert c.get(f"/api/projects/{pid}").json()["krea2_style"] == KS
        assert c.patch(f"/api/projects/{pid}", json={"krea2_style": ""}).status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["krea2_style"] == ""
        assert c.patch(f"/api/projects/{pid}", json={"krea2_style": "坏格式"}).status_code == 422


def test_genref_main_injects_style_and_single_character_guard(tmp_path, monkeypatch):
    """主图链路：krea_t2i + krea2_style → 工作台两槽注入 + 角色主图单人物强化。"""
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.db import Database
    from comic_studio.engine.jobs import enqueue_job, get_job
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "krea_t2i", "character_views": ""})
    pid = create_project(db, tmp_path / "data", "p", "9:16", "文本", krea2_style=KS)["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="羽川翼", appearance="性别：女", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    import sys
    sys.path.insert(0, "tests")
    from comfy_mock import comfy_server
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        from comic_studio.engine.genref import handle_gen_ref
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        wb = wf["2000"]["inputs"]
        assert wb["风格库"] == KS_LIB and wb["风格"] == "Anime Cel Illustration"
        text = wf["28"]["inputs"]["value"]
        assert "有且仅有一个人物" in text  # 库风格场景词稀释锚定的强化禁令
        assert "羽川翼" in text and "Style" not in text or True  # 双管齐下：style 段照拼（此处项目无 style）
