# tests/test_h3_sla.py
"""H3 SLA 注意力接入（2026-09-10；2026-09-18 ComfyUI 更新后五模板换新链）：
`UNET → ModelAttentionBackend → (SigmaShift) → LazyH3LoraStack →
MiniMaxChunkFeedForward → H3SLAAttention → BasicGuider/MiniMaxH3Director`。
旧链 MiniMaxLowVRAMAttention/TESpeedMiniMaxH3/PathchSageAttentionKJ 全部退役
（Sage 联动注入随之消失——新链用 ModelAttentionBackend 选稠密后端）；
LoRA 换 LazyH3LoraStack 多槽开关栈（filler switch 语义，lora1..lora8）。"""
import json
from pathlib import Path

import pytest

SHOT_TEMPLATES = ("h3_fl2v", "h3_i2v", "h3_ref2va", "h3_t2v")
ALL_TEMPLATES = SHOT_TEMPLATES + ("h3_director",)
SLA_LORA = "minimax_h3\\minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors"
PLAIN_LORA = "minimax_h3\\minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors"
DEAD_NODES = ("MiniMaxLowVRAMAttention", "TESpeedMiniMaxH3", "PathchSageAttentionKJ")


def _tmpl(t):
    from comic_studio.engine.workflows import registry
    return registry.scan_templates(Path("templates/workflows"))[t]


def _wf(t):
    return json.loads(
        Path(f"templates/workflows/{t}.api.json").read_text(encoding="utf-8"))


def _node(wf, cls):
    return next(((nid, n) for nid, n in wf.items()
                 if n["class_type"] == cls), (None, None))


@pytest.mark.parametrize("t", SHOT_TEMPLATES)
def test_shot_chain_structure(t):
    """四分镜模板：UNet→MAB→SigmaShift→LoRA栈→ChunkFF→SLA→Guider 一条链。"""
    wf = _wf(t)
    for dead in DEAD_NODES:
        assert _node(wf, dead)[0] is None, f"{t} 仍有退役节点 {dead}"
    sla_id, sla = _node(wf, "H3SLAAttention")
    assert sla_id, f"{t} 缺 H3SLAAttention"
    cff_id, cff = _node(wf, "MiniMaxChunkFeedForward")
    assert cff_id and sla["inputs"]["model"] == [cff_id, 0], f"{t} SLA 未接 ChunkFF"
    lora_id, lora = _node(wf, "LazyH3LoraStack")
    assert lora_id and cff["inputs"]["model"] == [lora_id, 0], f"{t} ChunkFF 未接 LoRA 栈"
    sig_id, sig = _node(wf, "MiniMaxH3SigmaShift")
    assert sig_id and lora["inputs"]["model_fl"] == [sig_id, 0], f"{t} LoRA 栈未接 SigmaShift"
    mab_id, mab = _node(wf, "ModelAttentionBackend")
    unet_id, unet = _node(wf, "UNETLoader")
    assert mab_id and unet_id and mab["inputs"]["model"] == [unet_id, 0]
    assert sig["inputs"]["model"] == [mab_id, 0]
    consumer = next((nid for nid, n in wf.items()
                     if n["class_type"] == "BasicGuider"
                     and n["inputs"].get("model") == [sla_id, 0]), None)
    assert consumer, f"{t} BasicGuider 未接 SLA"
    assert sla["inputs"].get("block_size") in ("64", "128"), f"{t} block_size 应为 COMBO 字符串"


def test_director_chain_structure():
    """导演台（2026-09-18 手动移植）：UNet→MAB→LoRA×2→ChunkFF→SLA→Director。
    SigmaShift 不需要——MiniMaxH3Director 自带 shift_video/shift_audio 输入。"""
    wf = _wf("h3_director")
    for dead in DEAD_NODES:
        assert _node(wf, dead)[0] is None, f"h3_director 仍有退役节点 {dead}"
    sla_id, sla = _node(wf, "H3SLAAttention")
    cff_id, cff = _node(wf, "MiniMaxChunkFeedForward")
    assert sla_id and cff_id and sla["inputs"]["model"] == [cff_id, 0]
    lora_ids = sorted(nid for nid, n in wf.items()
                      if n["class_type"] == "LoraLoaderModelOnly")
    assert len(lora_ids) == 2 and cff["inputs"]["model"] == [lora_ids[1], 0]
    mab_id, mab = _node(wf, "ModelAttentionBackend")
    unet_id, _ = _node(wf, "UNETLoader")
    assert mab_id and unet_id and mab["inputs"]["model"] == [unet_id, 0]
    assert wf[lora_ids[0]]["inputs"]["model"] == [mab_id, 0]
    dir_id, dirn = _node(wf, "MiniMaxH3Director")
    assert dir_id and dirn["inputs"]["model"] == [sla_id, 0]
    assert sla["inputs"].get("block_size") in ("64", "128")


@pytest.mark.parametrize("t", ALL_TEMPLATES)
def test_sla_manifest_params(t):
    """manifest 三键注入点指向 SLA 节点；sage_attention 注入已退役。"""
    tmpl = _tmpl(t)
    wf = _wf(t)
    sla_id, _ = _node(wf, "H3SLAAttention")
    for key, field in (("h3_sla_enabled", "enabled"),
                       ("h3_sla_sparsity", "sparsity_ratio"),
                       ("h3_sla_block_size", "block_size")):
        ip = tmpl.inject_params[key]
        assert ip.node == sla_id and ip.field == field, f"{t}.{key}"
    assert "sage_attention" not in tmpl.inject_params, f"{t} sage 注入应退役"


@pytest.mark.parametrize("t", SHOT_TEMPLATES)
def test_lora_stack_slots(t):
    """四分镜模板 LoRA 槽 = LazyH3LoraStack lora1..lora8（switch=启用_N）。"""
    tmpl = _tmpl(t)
    wf = _wf(t)
    lora_id, _ = _node(wf, "LazyH3LoraStack")
    labels = {s.label: s for s in tmpl.models}
    for i in range(1, 9):
        slot = labels[f"lora{i}"]
        assert slot.node == lora_id and slot.field == f"LoRA_{i}"
        assert slot.switch_field == f"启用_{i}", f"{t}.lora{i} 缺 switch"
    assert not any(s.label == "lora_turbo" for s in tmpl.models), \
        f"{t} 旧 lora_turbo 槽应退役"


def test_director_keeps_lora_slots():
    """导演台保留 lora_realism/lora_turbo 双槽与 lora_turbo_name 联动注入。"""
    tmpl = _tmpl("h3_director")
    labels = {s.label for s in tmpl.models}
    assert {"lora_realism", "lora_turbo"} <= labels
    assert "lora_turbo_name" in tmpl.inject_params


def test_settings_defaults(tmp_path):
    from comic_studio.engine.db import Database
    from comic_studio.engine.settings import get_setting
    db = Database(tmp_path / "s.db"); db.migrate()
    comfy = get_setting(db, "comfy")
    assert comfy["h3_sla_enabled"] is True
    assert comfy["h3_sla_sparsity"] == 0.9
    assert comfy["h3_sla_block_size"] == "64"


def test_fill_workflow_injects_sla():
    from comic_studio.engine.workflows.filler import fill_workflow
    tmpl = _tmpl("h3_fl2v")
    wf, _ = fill_workflow(tmpl, prompt="x", images=None,
                          output_ctx={"project": "t", "asset": "a"},
                          params={"seed": 1, "h3_sla_enabled": False,
                                  "h3_sla_sparsity": 0.85,
                                  "h3_sla_block_size": "128"})
    sla_id, _ = _node(wf, "H3SLAAttention")
    ins = wf[sla_id]["inputs"]
    assert ins["enabled"] is False and ins["sparsity_ratio"] == 0.85 \
        and ins["block_size"] == "128"  # COMBO 串（int 归一在 h3_sla_params）


def test_fill_workflow_lora_switch_semantics():
    """LazyH3LoraStack 槽位开关语义（2026-09-14 判例延续）：非空=设名+开、
    空串=只关、未覆盖=模板默认。"""
    from comic_studio.engine.workflows.filler import fill_workflow
    tmpl = _tmpl("h3_fl2v")
    wf, _ = fill_workflow(tmpl, prompt="x", images=None,
                          output_ctx={"project": "t", "asset": "a"},
                          params={}, model_overrides={"lora4": "my.safetensors",
                                                      "lora1": ""})
    lora_id, lora = _node(wf, "LazyH3LoraStack")
    ins = lora["inputs"]
    assert ins["LoRA_4"] == "my.safetensors" and ins["启用_4"] is True
    assert ins["启用_1"] is False and "LoRA_1" in ins  # 空串只关不抹名
    assert ins["启用_2"] is True                        # 未覆盖=模板默认


def test_lora_link_follows_sla_toggle(tmp_path):
    """加速 LoRA 联动（现仅导演台消费）：SLA 开→SLA 蒸馏版；关→普通 turbo；
    手动槽优先（缺席参数）。四分镜模板无 lora_turbo 槽——联动参数自然被
    filler 忽略（inject_params 未声明）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.rendershot import (PLAIN_TURBO_LORA, SLA_TURBO_LORA,
                                                 h3_lora_link)
    from comic_studio.engine.settings import set_setting
    db = Database(tmp_path / "s.db"); db.migrate()
    assert h3_lora_link(db, "h3_director") == {"lora_turbo_name": SLA_TURBO_LORA}
    set_setting(db, "comfy", {"h3_sla_enabled": False})
    assert h3_lora_link(db, "h3_director") == {"lora_turbo_name": PLAIN_TURBO_LORA}
    set_setting(db, "model_overrides",
                {"h3_director": {"lora_turbo": "my_manual.safetensors"}})
    assert h3_lora_link(db, "h3_director") == {}   # 手动选过→参数缺席，槽注入生效


def test_h3_sla_params_helper(tmp_path):
    """helper 读 settings 带缺省；block_size 转 INT（新 H3SLAAttention 接口，
    2026-09-18 前是 COMBO 字符串）；sage_attention 键已退役。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.rendershot import h3_sla_params
    db = Database(tmp_path / "s.db"); db.migrate()
    assert h3_sla_params(db) == {"h3_sla_enabled": True, "h3_sla_sparsity": 0.9,
                                 "h3_sla_block_size": "64"}
    set_setting(db, "comfy", {"h3_sla_enabled": False, "h3_sla_sparsity": 0.85,
                              "h3_sla_block_size": "128"})
    out = h3_sla_params(db)
    assert out["h3_sla_enabled"] is False and out["h3_sla_sparsity"] == 0.85
    assert out["h3_sla_block_size"] == "128"
    assert "sage_attention" not in out


def test_render_shot_carries_sla_settings(tmp_path, monkeypatch):
    """端到端：settings 关 SLA → 提交的 prompt 里 enabled=False（ref2va 主链），
    且新链节点齐全、退役节点不出现在提交载荷里。"""
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
        classes = {n["class_type"] for n in wf.values()}
        sla = next(n for n in wf.values() if n["class_type"] == "H3SLAAttention")
        assert sla["inputs"]["enabled"] is False
        assert sla["inputs"]["block_size"] == "64"
        assert {"ModelAttentionBackend", "MiniMaxChunkFeedForward",
                "LazyH3LoraStack"} <= classes
        for dead in DEAD_NODES:
            assert dead not in classes, f"提交载荷不应含退役节点 {dead}"
