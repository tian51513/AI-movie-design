"""P10 有声书链路（2026-09-05 计划）：ASR 转写、音频段存取、原声切片。

重依赖 faster-whisper/pyannote 一律函数内惰性导入——基础安装不带 asr
extra 时不影响服务（入口路由显式 422 给安装指引）。"""
import json
from pathlib import Path

SEGMENTS_NAME = "audio/segments.json"

_INSTALL_HINT = ("faster-whisper 未安装：WSL `.venv/bin/pip install -e .[asr]` / "
                 "Windows `.venv-win/Scripts/pip.exe install -e .[asr]`"
                 "（首次运行需下载模型）")


class TranscribeUnavailable(Exception):
    pass


def _default_backend(audio_path: Path, model_size: str):
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as e:
        raise TranscribeUnavailable(_INSTALL_HINT) from e
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segs, _info = model.transcribe(str(audio_path), language="zh",
                                   vad_filter=True, beam_size=5)
    return [(s.start, s.end, s.text) for s in segs]


def transcribe(audio_path: Path, model_size: str = "large-v3",
               _backend=None) -> list[dict]:
    """转写 → 归一化段列表（升序、<0.2s 间隙合并、文本首尾剥离）。"""
    backend = _backend or _default_backend
    try:
        raw = backend(Path(audio_path), model_size)
    except ModuleNotFoundError as e:
        # 注入后端/次级依赖缺失也归一为 TranscribeUnavailable（路由 422 指引）
        raise TranscribeUnavailable(f"{_INSTALL_HINT}（{e}）") from e
    segs = []
    for start, end, text in sorted(raw):
        t = str(text).strip()
        if not t:
            continue
        if segs and start - segs[-1]["end"] < 0.2:
            segs[-1]["end"] = end
            segs[-1]["text"] = (segs[-1]["text"] + " " + t).strip()
        else:
            segs.append({"start": float(start), "end": float(end), "text": t})
    return segs


def audio_rel(slug: str) -> str:
    return f"projects/{slug}/audio"


def save_segments(data_dir, slug: str, segs: list) -> None:
    d = Path(data_dir) / audio_rel(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "segments.json").write_text(
        json.dumps(segs, ensure_ascii=False), encoding="utf-8")


def load_segments(data_dir, slug: str) -> list | None:
    f = Path(data_dir) / audio_rel(slug) / "segments.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _norm(t: str) -> str:
    return "".join(ch for ch in t if ch.isalnum())


def span_duration_for(segments: list, text: str):
    """镜文本 → 音频段时长并集（P10A：转写文本与 text_span 同源，规范化后
    子串匹配可靠）。返回 None=未命中（调用方回落估时公式）。"""
    key = _norm(text)
    if not key:
        return None
    hits = [s for s in segments if _norm(s["text"]) and (
        key in _norm(s["text"]) or _norm(s["text"]) in key)]
    if not hits:
        return None
    return max(s["end"] for s in hits) - min(s["start"] for s in hits)
