# tests/test_voicelib.py
"""Phase 2 音色库：预设生成（VoiceDesign）/ 上传处理（VoiceClone）/ 库列表。"""
import json
from pathlib import Path

import pytest

from comic_studio.engine.voices import voice_instruct


def _registry(tmp_path, monkeypatch):
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    return registry.scan_templates(registry.TEMPLATE_ROOT)


def test_upload_media_routes_by_suffix(tmp_path):
    """client.upload_media：图片走 /upload/image，音频走 /upload/audio。"""
    from comic_studio.engine.comfy.client import ComfyClient
    from comfy_mock import comfy_server
    (tmp_path / "a.mp3").write_bytes(b"ID3xxx")
    (tmp_path / "b.png").write_bytes(b"\x89PNG")
    with comfy_server("ok") as m:
        c = ComfyClient(m.base_url)
        c.upload_media(tmp_path / "a.mp3", "cs__voice.mp3")
        c.upload_media(tmp_path / "b.png", "cs__ref.png")
        assert m.audio_uploads == ["cs__voice.mp3"]
        assert m.uploads == ["cs__ref.png"]


def test_history_result_audio_kind(tmp_path):
    """SaveAudio 产物在 outputs 的 audio 键 → _kind=audio（下载走同一 /view）。"""
    from comic_studio.engine.comfy.client import ComfyClient
    from comfy_mock import comfy_server
    with comfy_server("ok", audio=True) as m:
        c = ComfyClient(m.base_url)
        out = c.wait_and_collect("p1")
        assert any(o["_kind"] == "audio" for o in out)


def test_voicelib_generate_preset(tmp_path, monkeypatch):
    """预设生成：design 模板注入 instruct/seed → 产物落 data/voices/presets/<名>.flac。"""
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine import voicelib
    from comfy_mock import comfy_server
    _registry(tmp_path, monkeypatch)
    with comfy_server("ok", audio=True) as m:
        c = ComfyClient(m.base_url)
        out = voicelib.generate_preset(c, tmp_path, "高冷御姐")
        assert out.exists() and out.parent == tmp_path / "voices" / "presets"
        wf = m.prompts[0]["prompt"]
        assert wf["1"]["inputs"]["voice_instruction"] == voice_instruct("高冷御姐")
        assert wf["3"]["inputs"]["filename_prefix"] == "cs/voices/高冷御姐"


def test_voicelib_process_upload(tmp_path, monkeypatch):
    """上传处理：clone 模板注入裁剪起止 + 音频文件名（后缀保留）→ staging。"""
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine import voicelib
    from comfy_mock import comfy_server
    _registry(tmp_path, monkeypatch)
    src = tmp_path / "raw.mp3"; src.write_bytes(b"ID3")
    with comfy_server("ok", audio=True) as m:
        c = ComfyClient(m.base_url)
        out = voicelib.process_upload(c, tmp_path, src, name="试音",
                                      start=45, dur=60)
        assert out.exists() and "试音" in out.name
        assert out.parent == tmp_path / "voices" / "_staging"   # 先试听再确认入库
        final = voicelib.confirm_staged(tmp_path, f"voices/_staging/试音{out.suffix}",
                                        "试音", scope="global")
        assert final.parent == tmp_path / "voices" / "custom" and final.exists()
        assert not out.exists()
        wf = m.prompts[0]["prompt"]
        assert wf["79"]["inputs"]["start_index"] == 45
        assert wf["79"]["inputs"]["duration"] == 60
        assert wf["74"]["inputs"]["audio"].endswith(".mp3")   # 后缀保留
        assert m.audio_uploads and m.audio_uploads[0].endswith(".mp3")


def test_voicelib_confirm_project_scope(tmp_path, monkeypatch):
    """项目级作用域：staging 确认后样本落项目 voices 目录；非法路径被拦。"""
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine import voicelib
    from comfy_mock import comfy_server
    _registry(tmp_path, monkeypatch)
    src = tmp_path / "raw2.wav"; src.write_bytes(b"RIFF")
    with comfy_server("ok", audio=True) as m:
        c = ComfyClient(m.base_url)
        out = voicelib.process_upload(c, tmp_path, src, name="专属", start=0, dur=10)
        final = voicelib.confirm_staged(tmp_path, f"voices/_staging/专属{out.suffix}",
                                        "专属", scope="project", project="myproj")
        assert final.parent == tmp_path / "projects" / "myproj" / "voices"
        # 防穿越：staging 外的路径拒绝
        import pytest as _pytest
        with _pytest.raises(ValueError):
            voicelib.confirm_staged(tmp_path, "projects/myproj/novel.txt", "x")


def test_list_voices_origins(tmp_path):
    """列表：presets(已生成的) + global custom + project custom，各标 origin。"""
    from comic_studio.engine import voicelib
    (tmp_path / "voices" / "presets").mkdir(parents=True)
    (tmp_path / "voices" / "presets" / "萝莉.flac").write_bytes(b"x")
    (tmp_path / "voices" / "custom").mkdir(parents=True)
    (tmp_path / "voices" / "custom" / "我的音色.flac").write_bytes(b"x")
    (tmp_path / "projects" / "p1" / "voices").mkdir(parents=True)
    (tmp_path / "voices" / "custom" / "项目同名.flac").write_bytes(b"x")  # 同名：项目级优先
    (tmp_path / "projects" / "p1" / "voices" / "项目同名.flac").write_bytes(b"x")
    rows = voicelib.list_voices(tmp_path, project="p1")
    by = {r["name"]: r["origin"] for r in rows}
    assert by["萝莉"] == "preset" and by["我的音色"] == "global"
    assert by["项目同名"] == "project"   # 同名覆盖
    from comic_studio.engine.voices import VOICE_PRESETS
    assert any(r["name"] == "高冷御姐" and r.get("missing") for r in rows)  # 未生成的预设也列出
