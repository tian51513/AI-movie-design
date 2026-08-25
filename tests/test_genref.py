# tests/test_genref.py
import json
import time

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.genref import handle_gen_ref, build_gen_prompt
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting
from comic_studio.engine.workflows import registry
from comfy_mock import comfy_server

API = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
       "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
       "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
MANIFEST = """
id: t_t2i_test
type: t2i
name: 测试
file: t.api.json
prompt_format: "{kind_label}：{name}。{detail}"
inject:
  prompt: {node: "6", field: "text"}
  params:
    seed: {node: "3", field: "seed"}
outputs:
  - {node: "9", filename_prefix: "cs/{project}/{asset}"}
requires: []
"""


def _setup(tmp_path, monkeypatch):
    (tmp_path / "t.api.json").write_text(json.dumps(API))
    (tmp_path / "m.yaml").write_text(MANIFEST)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", tmp_path)  # 防跨测试污染
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "t_t2i_test"})
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    return db, pid


def test_build_gen_prompt_by_kind(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[NS(name="庭院", description="古宅院子", tags=[])],
                      props=[]))
    from comic_studio.engine.assets import list_project_assets
    rows = {r["kind"]: r for r in list_project_assets(db, pid)}
    p_char, _ = build_gen_prompt(rows["character"])
    assert "萧炎" in p_char and "三视图" in p_char and "禁止视角重复" in p_char
    p_scene, _ = build_gen_prompt(rows["scene"])
    assert "场景概念" in p_scene and "无人物" in p_scene
    # 项目级风格段注入（公共参数）
    p_styled, _ = build_gen_prompt(rows["character"], style="日系动漫风格，赛璐璐上色")
    assert "日系动漫风格" in p_styled and p_styled.index("日系动漫") > p_styled.index("白色干净背景")  # 风格段收尾主导画风


def test_handle_gen_ref_end_to_end_with_mock(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets, list_project_assets, get_asset
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        # 提交的工作流里 prompt 已注入
        wf = m.prompts[0]["prompt"]
        assert "萧炎" in wf["6"]["inputs"]["text"]
        assert wf["9"]["inputs"]["filename_prefix"].startswith("cs/")
        # 产物落盘
        lib = get_asset(db, asset["id"])["library_dir"]
        sheet = (tmp_path / "data" / lib / "views" / "sheet.png")
        assert sheet.exists() and sheet.stat().st_size == 2
    # 日志埋点
    from comic_studio.engine.logbus import fetch_logs
    msgs = " | ".join(r["message"] for r in fetch_logs(db, pid))
    assert "提交" in msgs and "参考图" in msgs


def test_regen_marks_stale(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.shots import persist_shots, list_shots
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset_id = list_project_assets(db, pid)[0]["id"]
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[asset_id], scene_ids=[], prop_ids=[], depends_on=None)])
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset_id,
                      resource="gpu_comfy", payload={"asset_id": asset_id})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
    assert list_shots(db, pid)[0]["status"] == "stale"
    # 验证 warn 日志
    from comic_studio.engine.logbus import fetch_logs
    msgs = [r["message"] for r in fetch_logs(db, pid)]
    assert any("stale" in m and "萧炎" in m for m in msgs)


def test_style_goes_after_suffix_and_dedup(tmp_path):
    from comic_studio.engine.genref import build_gen_prompt
    row = {"kind": "character", "name": "直葉", "appearance_json": '{"detail": "黑发少女。"}',
           "source_project": 1, "id": 2}
    p, _ = build_gen_prompt(row, style="真人电影，电影质感。")
    assert "。。" not in p
    assert p.index("白色干净背景") < p.index("真人电影")  # 风格段在设定图套话之后
    # 顺序：风格段 → Turbo 尾缀 → 结构再强调
    assert p.index("真人电影") < p.index("ultra-detailed") < p.index("严格三视图布局")


def test_gen_prompt_zimage_turbo_tail():
    """ZImage-Turbo 规范（data/ZImage-Turbo 技能模板）：无负向词、纠错正向写入、
    中英混编、质量尾缀适配 8 步推理。"""
    row = {"kind": "character", "name": "林晨", "source_project": 1, "id": 1,
           "appearance_json": '{"detail":"黑发少年"}'}
    p, _ = build_gen_prompt(row)
    assert "ultra-detailed" in p and "8k" in p
    assert "避免多余手指" in p and "避免五官扭曲" in p and "无蜡像塑料感" in p
    assert "无文字水印" in p
    p_scene, _ = build_gen_prompt(dict(row, kind="scene",
                                       appearance_json='{"detail":"古城"}'))
    assert "cinematic color grading" in p_scene and "画面完整" in p_scene
    p_prop, _ = build_gen_prompt(dict(row, kind="prop",
                                      appearance_json='{"detail":"剑"}'))
    assert "材质纹理" in p_prop
