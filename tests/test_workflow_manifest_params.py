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
    reg = scan_templates(Path("templates/workflows"))
    assert "lora_strength" in reg["h3_ref2va"].inject_params
    assert reg["h3_ref2va"].inject_params["lora_strength"].node == "117"


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
