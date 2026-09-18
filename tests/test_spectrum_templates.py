# tests/test_spectrum_templates.py
"""Spectrum 加速道模板四件套（2026-09-18 用户 BulletTime/SpectrumSpeed 工作流
统一骨架接入）：DiffusionModelLoaderKJ → LazyH3LoraStack → EasyCache →
BlockSparseAttention → SpectrumApplyMiniMaxH3 → 4 步采样；RTX VSR 走
switch_links 旁路（默认关=节点不存在）。"""
import json
from pathlib import Path

import pytest

from comic_studio.engine.workflows.filler import fill_workflow
from comic_studio.engine.workflows.registry import scan_templates

IDS = ("h3_spectrum_fl2v", "h3_spectrum_i2v", "h3_spectrum_t2v",
       "h3_spectrum_ref2va")


def _wf(t):
    return json.loads(
        Path(f"templates/workflows/{t}.api.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("t", IDS)
def test_chain_structure(t):
    """模型链完整且顺序正确；无旧链死节点。"""
    wf = _wf(t)
    assert wf['105:128']['class_type'] == 'DiffusionModelLoaderKJ'
    assert wf['105:156']['inputs']['model_fl'] == ['105:128', 0]
    assert wf['105:133']['class_type'] == 'EasyCache'
    assert wf['105:133']['inputs']['model'] == ['105:156', 0]
    assert wf['105:157']['class_type'] == 'BlockSparseAttention'
    assert wf['105:157']['inputs']['model'] == ['105:133', 0]
    assert wf['105:153']['class_type'] == 'SpectrumApplyMiniMaxH3'
    assert wf['105:153']['inputs']['model'] == ['105:157', 0]
    assert wf['105:16']['inputs']['model'] == ['105:153', 0]  # BasicGuider
    for dead in ('MiniMaxLowVRAMAttention', 'TESpeedMiniMaxH3',
                 'PathchSageAttentionKJ', 'VHS_VideoCombine'):
        assert not any(n['class_type'] == dead for n in wf.values()), \
            f"{t} 不应含 {dead}"


@pytest.mark.parametrize("t", IDS)
def test_manifest_inject_points(t):
    """manifest 声明的注入点在 api.json 里真实存在（node + field）。"""
    reg = scan_templates(Path("templates/workflows"))
    tmpl = reg[t]
    wf = _wf(t)
    for key, ip in tmpl.inject_params.items():
        node = wf.get(ip.node)
        assert node is not None, f"{t}.{key} 节点 {ip.node} 不存在"
        if ip.field != 'steps':  # steps 等标量字段直接 set_input
            assert ip.field in node['inputs'] or key == 'seed', \
                f"{t}.{key} 字段 {ip.field} 不在节点 {ip.node}"
    for spec in tmpl.inject_images:
        assert spec['node'] in wf, f"{t} 图片槽节点 {spec['node']} 不存在"
    out = tmpl.outputs[0]
    assert wf[out.node]['class_type'] == 'SaveVideo'
    assert '140' == out.node


@pytest.mark.parametrize("t", IDS)
def test_rtx_default_off_and_switchable(t):
    """RTX 默认直连（无 141 节点——API 格式孤立节点也会执行）；开→注入+改接。"""
    reg = scan_templates(Path("templates/workflows"))
    tmpl = reg[t]
    wf, _ = fill_workflow(tmpl, prompt="p", params={"seed": 7},
                          images=None, output_ctx={"project": "x", "asset": "y"})
    assert wf['105:91']['inputs']['images'] == ['105:10', 0]
    assert '141' not in wf
    assert wf['105:130']['inputs']['noise_seed'] == 7  # RandomNoise 槽
    wf2, _ = fill_workflow(tmpl, prompt="p",
                           params={"seed": 7, "rtx_vsr": True},
                           images=None, output_ctx={"project": "x", "asset": "y"})
    assert wf2['105:91']['inputs']['images'] == ['141', 0]
    assert wf2['141']['class_type'] == 'RTXVideoSuperResolution'
    assert wf2['141']['inputs']['images'] == ['105:10', 0]


def test_fl2v_first_last_and_ref2va_audio_slots():
    reg = scan_templates(Path("templates/workflows"))
    fl2v = reg['h3_spectrum_fl2v']
    assert [i['slot'] for i in fl2v.inject_images] == ['first', 'last']
    r2v = reg['h3_spectrum_ref2va']
    assert [i['slot'] for i in r2v.inject_images] == \
        ['ref0', 'ref1', 'audio0', 'audio1']
    # ref2va 核心节点是 ReferenceToVideo，音频接线 key 从 0 起（引擎约定）
    wf = _wf('h3_spectrum_ref2va')
    core = wf['105:104']
    assert core['class_type'] == 'MiniMaxH3ReferenceToVideo'
    assert core['inputs']['ref_audios.ref_audio_0'] == ['250', 0]
    assert core['inputs']['ref_audios.ref_audio_1'] == ['251', 0]


def test_unet_slot_is_kj_loader():
    """unet 槽指向 DiffusionModelLoaderKJ.model_name（不是 UNETLoader——
    模板里无 UNETLoader，误配会让设置页覆盖静默失效）。"""
    reg = scan_templates(Path("templates/workflows"))
    for t in IDS:
        unet = next(s for s in reg[t].models if s.label == 'unet')
        assert unet.cls == 'DiffusionModelLoaderKJ' and unet.node == '105:128'
        wf = _wf(t)
        assert wf['105:128']['inputs']['model_name'].endswith('.safetensors')
