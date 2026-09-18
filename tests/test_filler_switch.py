# tests/test_filler_switch.py
"""switch_links 开关接线（2026-09-18 RTX VSR 需求）：manifest 声明式分支——
默认（关）直连 off 链；params 键为真时注入 add_nodes 节点并改接 on 链。
动机：ComfyUI API 格式无节点禁用，孤立节点照样执行——旁路分支必须
"默认不存在、开时注入"，否则关着的 RTX 超分也会白烧 GPU。"""
import json
import textwrap

from comic_studio.engine.workflows.filler import fill_workflow
from comic_studio.engine.workflows.registry import load_manifest

API = {"10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0]}},
       "91": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": 24}},
       "93": {"class_type": "SaveVideo", "inputs": {"video": ["91", 0],
                                                    "filename_prefix": "video/Mini"}}}

MANIFEST = textwrap.dedent("""
    id: t_switch
    type: fl2v
    name: 开关接线测试
    file: t.api.json
    prompt_format: "{prompt}"
    inject:
      prompt: {node: "91", field: "fps"}   # 借个字段占位（测试不关心）
    switch_links:
      rtx_vsr:
        node: "91"
        field: images
        # "on"/"off" 必须带引号——YAML 1.1 裸 on/off 会被解析成布尔键（PyYAML 判例）
        "on": ["138", 0]
        "off": ["10", 0]
        add_nodes:
          "138":
            class_type: RTXVideoSuperResolution
            inputs:
              images: ["10", 0]
              resize_type: "scale by multiplier"
              "resize_type.scale": 2
              quality: "ULTRA"
    outputs:
      - {node: "93", filename_prefix: "cs/{project}/{asset}"}
    requires: []
""")


def _setup(tmp_path):
    (tmp_path / "t.api.json").write_text(json.dumps(API))
    (tmp_path / "m.yaml").write_text(MANIFEST)
    return load_manifest(tmp_path / "m.yaml")


def test_switch_off_keeps_direct_link_and_no_extra_node(tmp_path):
    t = _setup(tmp_path)
    wf, _ = fill_workflow(t, prompt=None, params={}, images=None,
                          output_ctx={"project": "p", "asset": "1"})
    assert wf["91"]["inputs"]["images"] == ["10", 0]
    assert "138" not in wf, "关态不得注入分支节点（孤立节点 ComfyUI 也会执行）"


def test_switch_off_explicit_false_same(tmp_path):
    t = _setup(tmp_path)
    wf, _ = fill_workflow(t, prompt=None, params={"rtx_vsr": False}, images=None,
                          output_ctx={"project": "p", "asset": "1"})
    assert wf["91"]["inputs"]["images"] == ["10", 0] and "138" not in wf


def test_switch_on_injects_nodes_and_relinks(tmp_path):
    t = _setup(tmp_path)
    wf, _ = fill_workflow(t, prompt=None, params={"rtx_vsr": True}, images=None,
                          output_ctx={"project": "p", "asset": "1"})
    assert wf["91"]["inputs"]["images"] == ["138", 0]
    assert wf["138"]["class_type"] == "RTXVideoSuperResolution"
    assert wf["138"]["inputs"]["images"] == ["10", 0]
    assert wf["138"]["inputs"]["quality"] == "ULTRA"


def test_template_without_switch_links_unaffected(tmp_path):
    manifest = textwrap.dedent("""
        id: t_plain
        type: fl2v
        name: 无开关测试
        file: t.api.json
        prompt_format: "{prompt}"
        inject:
          prompt: {node: "91", field: "fps"}
        outputs:
          - {node: "93", filename_prefix: "cs/{project}/{asset}"}
        requires: []
    """)
    (tmp_path / "t.api.json").write_text(json.dumps(API))
    (tmp_path / "m.yaml").write_text(manifest)
    t = load_manifest(tmp_path / "m.yaml")
    wf, _ = fill_workflow(t, prompt=None, params={"rtx_vsr": True}, images=None,
                          output_ctx={"project": "p", "asset": "1"})
    assert wf["91"]["inputs"]["images"] == ["10", 0] and "138" not in wf


def test_unknown_param_no_switch(tmp_path):
    """模板声明了开关但 params 没带该键 → 走 off 默认（幂等安全）。"""
    t = _setup(tmp_path)
    wf, _ = fill_workflow(t, prompt=None, params={"unrelated": 1}, images=None,
                          output_ctx={"project": "p", "asset": "1"})
    assert wf["91"]["inputs"]["images"] == ["10", 0]
