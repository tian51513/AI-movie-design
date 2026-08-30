# comic_studio/engine/voicelib.py
"""Phase 2 音色库（2026-08-30）：预设生成 / 上传处理 / 库列表。

生成器 = ComfyUI 两张 TTS 模板（用户实测工作流落库）：
- qwen_tts_design（VoiceDesign）：instruct → 音色样本，用于 15 预设批量生成
- qwen_tts_clone（VoiceClone）：上传音频 + 裁剪起止 → 克隆样本（默认句），
  用户上传的音色经此处理为统一格式样本（兼做试听）

存储布局（data 相对）：
- voices/presets/<预设名>.flac   15 预设（VoiceDesign 生成）
- voices/custom/<名>.flac        全局自定义（设置页上传）
- projects/<slug>/voices/<名>.flac 项目级自定义（项目详情上传）
同名时项目级优先。
"""
import random
from pathlib import Path

from .voices import VOICE_PRESETS, voice_instruct

# 预设样本的固定文本（与用户工作流默认句一致，保证全库样本口径统一）
PRESET_TEXT = "你好，这是我的声音。风从东边来，故事从今晚开始。"

_AUDIO_EXTS = (".flac", ".mp3", ".wav", ".ogg", ".m4a")


def _scan(dir_: Path) -> dict[str, Path]:
    if not dir_.exists():
        return {}
    return {p.stem: p for p in dir_.iterdir()
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS}


def list_voices(data_dir, project: str | None = None) -> list[dict]:
    """音色库列表：15 预设（未生成也列出，标 missing）+ 全局自定义 + 项目自定义。
    同名时项目级优先（origin=project）。"""
    data_dir = Path(data_dir)
    presets = _scan(data_dir / "voices" / "presets")
    glob = _scan(data_dir / "voices" / "custom")
    proj = _scan(data_dir / "projects" / project / "voices") if project else {}
    rows: dict[str, dict] = {}
    for p in VOICE_PRESETS:  # 预设打底
        path = presets.get(p["name"])
        rows[p["name"]] = {"name": p["name"], "origin": "preset", "gender": p["gender"],
                           "timbre": p["timbre"], "instruct": voice_instruct(p["name"]),
                           **({"path": str(path)} if path else {"missing": True})}
    for name, path in glob.items():
        rows[name] = {"name": name, "origin": "global", "path": str(path)}
    for name, path in proj.items():  # 项目级最后写入 = 同名优先
        rows[name] = {"name": name, "origin": "project", "path": str(path)}
    return sorted(rows.values(), key=lambda r: (r["origin"] != "preset", r["name"]))


def resolve_sample(data_dir, name: str, project: str | None = None) -> Path | None:
    """音色名 → 样本文件（项目级 > 全局自定义 > 预设）；无样本返回 None。"""
    data_dir = Path(data_dir)
    if project:
        hit = _scan(data_dir / "projects" / project / "voices").get(name)
        if hit:
            return hit
    return (_scan(data_dir / "voices" / "custom").get(name)
            or _scan(data_dir / "voices" / "presets").get(name))


def _run_template(comfy, template_id: str, params: dict, images: list | None,
                  dest_dir: Path, name: str) -> Path:
    """提交 TTS 模板 → 轮询 → 下载首个 audio 产物到 dest_dir/<name><原后缀>。"""
    from .workflows import registry
    from .workflows.filler import fill_workflow
    tmpl = registry.scan_templates(registry.TEMPLATE_ROOT)[template_id]
    wf, uploads = fill_workflow(tmpl, prompt=None, params=params, images=images,
                                output_ctx={"project": "voices", "asset": name},
                                model_overrides=None)
    for up in uploads:
        comfy.upload_media(Path(up["path"]), up["name"])
    pid = comfy.submit(wf, client_id="comic-studio-voicelib")
    results = comfy.wait_and_collect(pid)
    audios = [r for r in results if r.get("_kind") == "audio"]
    if not audios:
        raise RuntimeError(f"{template_id} 未产出音频（结果: {results}）")
    a = audios[0]
    dest = dest_dir / f"{name}{Path(a['filename']).suffix}"
    comfy.download(a["filename"], a.get("subfolder", ""), a.get("type", "output"), dest)
    return dest


def generate_preset(comfy, data_dir, name: str) -> Path:
    """生成预设音色样本（VoiceDesign 模板）→ data/voices/presets/<名>.flac。"""
    return _run_template(comfy, "qwen_tts_design",
                         params={"voice_instruction": voice_instruct(name),
                                 "text": PRESET_TEXT,
                                 "seed": random.randint(0, 2**31 - 1)},
                         images=None,
                         dest_dir=Path(data_dir) / "voices" / "presets", name=name)


def process_upload(comfy, data_dir, audio_path: Path, *, name: str,
                   start: float, dur: float, scope: str = "global",
                   project: str | None = None) -> Path:
    """上传音色处理（VoiceClone 模板）：裁剪起止 + 默认句克隆 → 样本落库。"""
    if scope == "project":
        assert project, "项目级音色必须给 project slug"
    dest_dir = (Path(data_dir) / "projects" / project / "voices" if scope == "project"
                else Path(data_dir) / "voices" / "custom")
    return _run_template(comfy, "qwen_tts_clone",
                         params={"start_index": start, "duration": dur},
                         images=[{"slot": "audio", "path": str(audio_path)}],
                         dest_dir=dest_dir, name=name)
