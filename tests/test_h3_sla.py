# tests/test_h3_sla.py
"""H3 SLA 注意力接入（2026-09-10 用户需求）：五模板接线 + settings 三键 + 注入链路。

节点 H3SLAAttention（用户 ComfyUI 自定义，类目 SparseAttention）：MODEL→MODEL，
作者定位「LoRA 之后、采样器之前的最后一环」——插在 MiniMaxLowVRAMAttention 之后、
BasicGuider/MiniMaxH3Director 之前。settings 三键：h3_sla_enabled（默认 true）/
h3_sla_sparsity（0.9 本机验证值）/h3_sla_block_size（"64" 音频安全值，COMBO 字符串）。"""
import json
from pathlib import Path

import pytest

TEMPLATES = ("h3_fl2v", "h3_i2v", "h3_ref2va", "h3_t2v", "h3_director")
SLA_LORA = "minimax_h3\\minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors"


def _tmpl(t):
    from comic_studio.engine.workflows import registry
    return registry.scan_templates(Path("templates/workflows"))[t]


def _wf(t):
    return json.loads(
        Path(f"templates/workflows/{t}.api.json").read_text(encoding="utf-8"))


def _sla_node(wf):
    return next(((nid, n) for nid, n in wf.items()
                 if n["class_type"] == "H3SLAAttention"), (None, None))


def test_settings_defaults(tmp_path):
    from comic_studio.engine.db import Database
    from comic_studio.engine.settings import get_setting
    db = Database(tmp_path / "s.db"); db.migrate()
    comfy = get_setting(db, "comfy")
    assert comfy["h3_sla_enabled"] is True
    assert comfy["h3_sla_sparsity"] == 0.9
    assert comfy["h3_sla_block_size"] == "64"


@pytest.mark.parametrize("t", TEMPLATES)
def test_template_wiring(t):
    """结构：SLA=LowVRAM 后最后一环，Guider/Director 消费之；manifest 声明
    三参数 + lora_turbo 模型槽；加速 LoRA 默认换 SLA 蒸馏版（设置页可切回）。"""
    wf = _wf(t)
    sla_id, sla = _sla_node(wf)
    assert sla_id, f"{t} 缺 H3SLAAttention 节点"
    low = next(nid for nid, n in wf.items()
               if n["class_type"] == "MiniMaxLowVRAMAttention")
    assert sla["inputs"]["model"] == [low, 0]   # 紧跟 LowVRAM（作者：最后一环）
    consumer = next((nid for nid, n in wf.items()
                     if n["class_type"] in ("BasicGuider", "MiniMaxH3Director")
                     and n["inputs"].get("model") == [sla_id, 0]), None)
    assert consumer, f"{t} Guider/Director 未接 SLA 节点"
    assert any(n["inputs"].get("lora_name") == SLA_LORA
               for n in wf.values()
               if n["class_type"] == "LoraLoaderModelOnly"), \
        f"{t} 加速 LoRA 未换 SLA 蒸馏版"
    tmpl = _tmpl(t)
    for key, field in (("h3_sla_enabled", "enabled"),
                       ("h3_sla_sparsity", "sparsity_ratio"),
                       ("h3_sla_block_size", "block_size")):
        ip = tmpl.inject_params[key]
        assert ip.node == sla_id and ip.field == field, f"{t}.{key}"
    # Sage 联动参数：SLA 开→disabled（T8 禁令：注意力双实现叠加不可预期）
    sage = next((nid for nid, n in wf.items()
                 if n["class_type"] == "PathchSageAttentionKJ"), None)
    sip = tmpl.inject_params["sage_attention"]
    assert sip.node == sage and sip.field == "sage_attention", f"{t}.sage_attention"
    assert any(s.label == "lora_turbo" for s in tmpl.models), f"{t} 缺 lora_turbo 槽"


def test_fill_workflow_injects_sla():
    from comic_studio.engine.workflows.filler import fill_workflow
    tmpl = _tmpl("h3_fl2v")
    wf, _ = fill_workflow(tmpl, prompt="x", images=None,
                          output_ctx={"project": "t", "asset": "a"},
                          params={"seed": 1, "h3_sla_enabled": False,
                                  "h3_sla_sparsity": 0.85,
                                  "h3_sla_block_size": "128"})
    sla_id, _ = _sla_node(wf)
    ins = wf[sla_id]["inputs"]
    assert ins["enabled"] is False and ins["sparsity_ratio"] == 0.85 \
        and ins["block_size"] == "128"


def test_h3_sla_params_helper(tmp_path):
    """helper 读 settings 带缺省——部分覆盖（旧库/整键替换）不炸；
    sage_attention 与 SLA 开关联动（开=disabled 关=auto）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.rendershot import h3_sla_params
    db = Database(tmp_path / "s.db"); db.migrate()
    assert h3_sla_params(db) == {"h3_sla_enabled": True, "h3_sla_sparsity": 0.9,
                                 "h3_sla_block_size": "64",
                                 "sage_attention": "disabled"}
    set_setting(db, "comfy", {"h3_sla_enabled": False, "h3_sla_sparsity": 0.85})
    out = h3_sla_params(db)
    assert out["h3_sla_enabled"] is False and out["h3_sla_sparsity"] == 0.85
    assert out["h3_sla_block_size"] == "64"      # 缺省回落
    assert out["sage_attention"] == "auto"       # SLA 关→Sage 回稠密基线


def test_render_shot_carries_sla_settings(tmp_path, monkeypatch):
    """端到端：settings 关 SLA → 提交的 prompt 里 enabled=False（ref2va 主链）。"""
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.rendershot import render_shot
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.shots import persist_shots, update_shot
    from comic_studio.engine.workflows import registry
    from tests.comfy_mock import comfy_server
    from tests.test_rendershot import _setup, _shot_draft
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]])])[0]
    update_shot(db, sid, {"prompt": "林晨在庭院推门。"})
    set_setting(db, "comfy", {"h3_sla_enabled": False})
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        sla = next(n for n in wf.values() if n["class_type"] == "H3SLAAttention")
        assert sla["inputs"]["enabled"] is False
        sage = next(n for n in wf.values()
                    if n["class_type"] == "PathchSageAttentionKJ")
        assert sage["inputs"]["sage_attention"] == "auto"   # SLA 关→Sage 基线
