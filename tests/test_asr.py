# tests/test_asr.py
import io
import sys
import types

import pytest
from pathlib import Path
from comic_studio.engine.asr import (TranscribeUnavailable, transcribe,
                                     save_segments, load_segments)


def test_transcribe_normalizes_and_merges(tmp_path):
    fake = [(0.0, 2.0, "  你好 "), (2.1, 4.5, "世界"), (10.0, 12.0, "下一段")]
    segs = transcribe(tmp_path / "a.mp3", _backend=lambda p, m: fake)
    assert segs == [{"start": 0.0, "end": 4.5, "text": "你好 世界"},
                    {"start": 10.0, "end": 12.0, "text": "下一段"}]  # <0.2s 间隙合并


def test_transcribe_unavailable_message(tmp_path, monkeypatch):
    def boom(path, model):
        raise ModuleNotFoundError("No module named 'faster_whisper'")
    with pytest.raises(TranscribeUnavailable, match="pip install -e .\\[asr\\]"):
        transcribe(tmp_path / "a.mp3", _backend=boom)


def test_segments_roundtrip(tmp_path):
    save_segments(tmp_path, "剧", [{"start": 0, "end": 1, "text": "x"}])
    assert load_segments(tmp_path, "剧") == [{"start": 0, "end": 1, "text": "x"}]
    assert load_segments(tmp_path, "无此剧") is None


# ---- P10 Task 2: from-audio 导入入口（建项目+存源音频+入队 transcribe）----

def _stub_faster_whisper(monkeypatch):
    """Ruling-1：测试环境无 asr 包，用桩模块通过路由依赖探测
    （缺包 → 422 的生产语义由真机验证，不在无包环境覆盖）。
    探测是 `from faster_whisper import WhisperModel`——空模块会抛普通
    ImportError（不被 except ModuleNotFoundError 捕获→500），桩需带该名。"""
    stub = types.ModuleType("faster_whisper")
    stub.WhisperModel = object  # 探测仅验证可导入
    monkeypatch.setitem(sys.modules, "faster_whisper", stub)


def test_from_audio_creates_project_and_enqueues(tmp_path, monkeypatch):
    _stub_faster_whisper(monkeypatch)
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    with TestClient(create_app(tmp_path / "t.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-audio",
                   data={"name": "有声剧", "aspect_ratio": "16:9"},
                   files={"audio": ("book.mp3", io.BytesIO(b"ID3fake"),
                                    "audio/mpeg")})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["stage"] == "created"
        pid = body["id"]
        src = tmp_path / "data" / "projects" / "有声剧" / "audio" / "source.mp3"
        assert src.exists() and src.read_bytes() == b"ID3fake"
        row = c.app.state.db.connect().execute(
            "SELECT type, status FROM jobs WHERE project_id=? "
            "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        assert row["type"] == "transcribe" and row["status"] == "pending"


def test_from_audio_rejects_oversize(tmp_path, monkeypatch):
    _stub_faster_whisper(monkeypatch)
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    with TestClient(create_app(tmp_path / "t.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-audio",
                   data={"name": "大剧", "aspect_ratio": "16:9"},
                   files={"audio": ("big.mp3", io.BytesIO(b"x" * (200 * 1024 * 1024 + 1)),
                                    "audio/mpeg")})
        assert r.status_code == 422
