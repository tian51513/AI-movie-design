# tests/test_workflow_manifest_params.py
from pathlib import Path

from comic_studio.engine.workflows.registry import scan_templates


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
