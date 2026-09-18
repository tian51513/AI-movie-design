# tests/test_workflow_manifest_params.py
from pathlib import Path

from comic_studio.engine.workflows.registry import scan_templates


def test_h3_i2v_has_seed_param():
    """I2: h3_i2v yaml params 应含 seed（节点 51 rgthree Seed）。"""
    reg = scan_templates(Path("templates/workflows"))
    assert "seed" in reg["h3_i2v"].inject_params, "h3_i2v 缺 seed 参数"
    sp = reg["h3_i2v"].inject_params["seed"]
    assert sp.node == "51" and sp.field == "seed"


def test_all_video_templates_have_multiple():
    """I2+T4: 三模板均须含 multiple 参数。"""
    reg = scan_templates(Path("templates/workflows"))
    for tid in ("h3_ref2va", "h3_i2v", "h3_t2v"):
        assert "multiple" in reg[tid].inject_params, f"{tid} 缺 multiple"


def test_video_templates_have_render_params():
    reg = scan_templates(Path("templates/workflows"))
    for tid in ("h3_ref2va", "h3_i2v", "h3_t2v"):
        params = reg[tid].inject_params
        assert "steps" in params, f"{tid} 缺 steps"
        for k in ("megapixels",):
            assert k in params, f"{tid} 缺 {k}"
        # aspect 枚举串必须与 api json 里现有值完全一致（防拼写错）
        api = reg[tid].api_json()
        ar_node = params.get("aspect")
        if ar_node:
            assert isinstance(ar_node.node, str) and ar_node.node in api


def test_ref2va_lora_strength_point():
    """2026-09-18 新链：独立 realism LoRA 节点退役，lora_strength 注入随之退役
    （项目级 lora_realism=0.75 若注入会覆盖用户验证的栈槽强度 1.0——强度改由
    LazyH3LoraStack 槽位在设置页控制）。"""
    reg = scan_templates(Path("templates/workflows"))
    assert "lora_strength" not in reg["h3_ref2va"].inject_params


def test_zimage_t2i_steps_default_10():
    """用户指定（2026-08-25）：zimage_t2i steps 默认 10，且作为可注入参数。"""
    from comic_studio.engine.workflows import registry
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    t = reg["zimage_t2i"]
    assert "steps" in t.inject_params
    assert t.api_json()["57:3"]["inputs"]["steps"] == 10


def test_manifest_prompt_optional():
    """inject.prompt 可选（四视图等用内置触发词的工作流）。"""
    from comic_studio.engine.workflows import registry
    from comic_studio.engine.workflows.filler import fill_workflow
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    t = reg["character_views"]
    assert t.inject_prompt is None
    wf, uploads = fill_workflow(
        t, prompt=None, params={"seed": 7},
        images=[{"slot": "body", "path": "x.png"}],
        output_ctx={"project": "p", "asset": "a"})
    assert "Character Sheet" in wf["24"]["inputs"]["prompt"]  # 内置词未被覆盖
    assert wf["17"]["inputs"]["image"] == "cs__p__a__body.png"  # 图槽换成上传名
    assert uploads and uploads[0]["path"] == "x.png"


def test_character_views_multi_lora_stack():
    """2026-09-16 刷新（_raw ▶▷MiniMaxH3辅助四视图生成流）：单 LoRA 节点退役，
    LazyKreaLoraStack 8 开关槽；模型链 UNet(25)→LoRA栈(36)→Edit patch(23)。"""
    from comic_studio.engine.workflows import registry
    from comic_studio.engine.workflows.filler import fill_workflow
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    t = reg["character_views"]
    slots = {s.label: s for s in t.models}
    assert "lora_quadview" not in slots          # 旧单 LoRA 槽退役
    for i in range(1, 9):
        s = slots[f"lora{i}"]
        assert (s.cls, s.node, s.field, s.switch_field) == (
            "LazyKreaLoraStack", "36", f"LoRA_{i}", f"启用_{i}")
    api = t.api_json()
    assert "16" not in api                        # 旧 LoraLoaderModelOnly 孤儿已删
    assert api["23"]["inputs"]["model"] == ["36", 0]
    assert api["36"]["inputs"]["模型"] == ["25", 0]
    assert api["14"]["inputs"]["steps"] == 4      # turbo 4 步 LoRA 随栈启用
    # 开关槽覆盖语义：换 lora2 文件 → 设文件名+开开关（设置页「当前选中即生效值」）
    wf, _ = fill_workflow(t, prompt=None, params={"seed": 7},
                          images=[{"slot": "body", "path": "x.png"}],
                          output_ctx={"project": "p", "asset": "a"},
                          model_overrides={"lora2": "Krea2\\new.safetensors"})
    n = wf["36"]["inputs"]
    assert n["LoRA_2"] == "Krea2\\new.safetensors" and n["启用_2"] is True
