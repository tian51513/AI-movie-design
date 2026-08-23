# tests/test_workflow_filler.py
import copy
import json
import textwrap
from pathlib import Path

from comic_studio.engine.workflows.filler import fill_workflow
from comic_studio.engine.workflows.registry import load_manifest

API = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "旧值"}},
       "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
       "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}},
       "17": {"class_type": "LoadImage", "inputs": {"image": ""}}}

MANIFEST = textwrap.dedent("""
    id: t_fill
    type: t2i
    name: 填充测试
    file: t.api.json
    prompt_format: "{kind_label}：{name}"
    inject:
      prompt: {node: "6", field: "text"}
      params:
        seed: {node: "3", field: "seed"}
      images:
        - {node: "17", field: "image", slot: front}
    outputs:
      - {node: "9", filename_prefix: "cs/{project}/{asset}"}
    requires: []
""")


def _setup(tmp_path):
    (tmp_path / "t.api.json").write_text(json.dumps(API))
    (tmp_path / "m.yaml").write_text(MANIFEST)
    return load_manifest(tmp_path / "m.yaml")


def test_fill_injects_all_points(tmp_path):
    t = _setup(tmp_path)
    seed_path = tmp_path / "front.png"; seed_path.write_bytes(b"png")
    wf, uploads = fill_workflow(
        t, prompt="角色设定图：萧炎", params={"seed": 42},
        images=[{"slot": "front", "path": seed_path}],
        output_ctx={"project": "doupo", "asset": "7"})
    assert wf["6"]["inputs"]["text"] == "角色设定图：萧炎"
    assert wf["3"]["inputs"]["seed"] == 42 and wf["3"]["inputs"]["steps"] == 20  # 未声明不动
    assert wf["9"]["inputs"]["filename_prefix"] == "cs/doupo/7"
    assert wf["17"]["inputs"]["image"] == "cs__doupo__7__front.png"
    assert uploads == [{"path": seed_path, "name": "cs__doupo__7__front.png"}]


def test_fill_does_not_mutate_api_file(tmp_path):
    t = _setup(tmp_path)
    fill_workflow(t, prompt="x", params={}, images=None, output_ctx={"project": "p", "asset": "1"})
    assert json.loads((tmp_path / "t.api.json").read_text())["6"]["inputs"]["text"] == "旧值"


def test_fill_without_images_ok(tmp_path):
    t = _setup(tmp_path)
    wf, uploads = fill_workflow(t, prompt="x", params={}, images=None,
                                output_ctx={"project": "p", "asset": "1"})
    assert uploads == [] and "17" not in wf or wf.get("17", {"inputs": {}})["inputs"].get("image") != "cs__p__1__front.png"
