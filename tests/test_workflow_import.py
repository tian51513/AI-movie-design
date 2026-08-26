# tests/test_workflow_import.py
"""工作流导入（2026-08-26 需求）：ComfyUI API JSON 上传 → 自动识别类型/注入点 →
生成 manifest 入库，用户无需手写 yaml。"""
import json

import pytest
from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                                  start_workers=False))


def _t2i_json():
    """最简 t2i 工作流（CLIPTextEncode + KSampler + SaveImage）。"""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "test.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "test.safetensors"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["2", 0]}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 123, "steps": 8,
              "cfg": 1, "positive": ["5", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "test"}},
    }


def _video_json():
    """最简视频工作流（MiniMaxH3ReferenceToVideo + LoadImage + SaveVideo）。"""
    return {
        "90": {"class_type": "UNETLoader", "inputs": {"unet_name": "h3.safetensors"}},
        "92": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen.safetensors"}},
        "94": {"class_type": "VAELoader", "inputs": {"vae_name": "vae.safetensors"}},
        "96": {"class_type": "LoadImage", "inputs": {"image": "ref.jpg"}},
        "97": {"class_type": "LoadImage", "inputs": {"image": "ref2.jpg"}},
        "101": {"class_type": "KSampler", "inputs": {"seed": 123, "steps": 4}},
        "110": {"class_type": "MiniMaxH3ReferenceToVideo",
                "inputs": {"prompt": "", "width": 832, "height": 1248,
                           "length": 124, "clip": ["92", 0], "vae": ["94", 0]}},
        "114": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "test"}},
    }


def test_analyze_workflow_t2i(tmp_path):
    from comic_studio.engine.workflows.importer import analyze_workflow
    result = analyze_workflow(_t2i_json(), "my_t2i")
    assert result["type"] == "t2i"
    assert result["inject"]["prompt"]["node"] == "5"
    assert result["inject"]["prompt"]["field"] == "text"
    assert result["inject"]["params"]["seed"]["node"] == "4"
    assert result["outputs"][0]["node"] == "10"
    assert result["models"][0]["label"] == "unet"


def test_analyze_workflow_video(tmp_path):
    from comic_studio.engine.workflows.importer import analyze_workflow
    result = analyze_workflow(_video_json(), "my_ref2va")
    assert result["type"] == "ref2va"  # ReferenceToVideo → ref2va
    assert result["inject"]["prompt"]["node"] == "110"
    assert result["inject"]["prompt"]["field"] == "prompt"
    assert len(result["inject"]["images"]) == 2  # 两个 LoadImage
    assert result["inject"]["images"][0]["slot"] == "ref0"
    assert result["outputs"][0]["node"] == "114"


def test_import_endpoint(tmp_path):
    c = _client(tmp_path)
    with c:
        r = c.post("/api/workflows/import",
                   files={"file": ("my_t2i.json",
                                   json.dumps(_t2i_json()).encode(),
                                   "application/json")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "my_t2i"
        assert body["type"] == "t2i"
        # 注册表可找到（生成的 yaml+api.json 落盘）
        from comic_studio.engine.workflows import registry
        reg = registry.scan_templates(registry.TEMPLATE_ROOT)
        # tmp_path 不是 TEMPLATE_ROOT，这里只验证响应；真实验证靠端到端
        assert body["inject"]["prompt"]["node"] == "5"


def test_import_rejects_invalid_json(tmp_path):
    c = _client(tmp_path)
    with c:
        r = c.post("/api/workflows/import",
                   files={"file": ("bad.json", b"not json", "application/json")})
        assert r.status_code == 422
