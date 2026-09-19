# tests/test_musiclib.py
"""音乐库（2026-09-19 spec）：music3 模板 / 库 CRUD / LLM 建议 / gen_music /
API / 合成混入。本文件随任务逐段追加。"""
import json
from pathlib import Path

from comic_studio.engine.workflows.registry import scan_templates


def test_music3_template():
    reg = scan_templates(Path("templates/workflows"))
    t = reg["music3"]
    assert t.type == "music"
    wf = t.api_json()
    assert wf["37:13"]["class_type"] == "MiniMaxMusic3TextEncode"
    assert wf["35"]["class_type"] == "SaveAudioAdvanced"
    assert t.inject_prompt.node == "37:13" and t.inject_prompt.field == "caption"
    for key, node, field in (("seed", "37:38", "seed"),
                             ("max_duration", "37:13", "max_duration"),
                             ("lyrics", "37:13", "lyrics")):
        ip = t.inject_params[key]
        assert (ip.node, ip.field) == (node, field), key
    assert {s.label for s in t.models} == {"unet", "clip", "vae"}
