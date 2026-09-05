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


def load_env_file(path) -> int:
    """项目根 .env 加载（2026-09-05 用户需求）：缺失键注入进程环境，已存在键
    不覆盖（系统级变量/setx 优先于文件）。只做 KEY=VALUE 与 # 注释，够用即止
    ——不引 python-dotenv 依赖。返回注入数。"""
    import os
    f = Path(path)
    if not f.exists():
        return 0
    n = 0
    for ln in f.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ and v:
            os.environ[k] = v
            n += 1
    return n


def _add_nvidia_dll_dirs() -> int:
    """把 pip 装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 的 bin 目录加进
    DLL 搜索路径（2026-09-05 真机：cublas64_12.dll 缺失——ctranslate2 GPU
    模式必需，装在 site-packages 里 Windows 不会自动找到）。返回命中目录数。"""
    import os
    import sysconfig
    sp = Path(sysconfig.get_paths()["purelib"])
    n = 0
    for pkg in ("cublas", "cudnn"):
        base = sp / "nvidia" / pkg / "bin"
        if base.is_dir():
            try:
                os.add_dll_directory(str(base))
                n += 1
            except OSError:
                pass
    return n


def _gpu_ready() -> bool:
    """GPU 预检（2026-09-05 真机 18:47）：构造器可能惰性加载 CUDA 库——
    推理期才炸 cublas/cudnn 且原始 RuntimeError 穿透。预检=注入 DLL 目录后
    ctypes 试载 cublas64_12 + 任一 cudnn64_*；失败直接走 CPU int8。"""
    _add_nvidia_dll_dirs()
    import ctypes
    import sysconfig
    sp = Path(sysconfig.get_paths()["purelib"])
    cublas = sorted((sp / "nvidia" / "cublas" / "bin").glob("cublas64_*.dll"))
    cudnn = sorted((sp / "nvidia" / "cudnn" / "bin").glob("cudnn64_*.dll"))
    if not cublas or not cudnn:
        return False
    try:
        ctypes.CDLL(str(cublas[0]))
        ctypes.CDLL(str(cudnn[0]))
        return True
    except OSError:
        return False


def _run_model(model, audio_path: Path, progress):
    segs, _info = model.transcribe(str(audio_path), language="zh",
                                   vad_filter=True, beam_size=5)
    out = []
    for s in segs:   # 生成器逐段产出——每 25 段回调（长转写心跳日志）
        out.append((s.start, s.end, s.text))
        if progress and len(out) % 25 == 0:
            progress(len(out))
    return out


def _default_backend(audio_path: Path, model_size: str, progress=None):
    # 先吃项目根 .env（HF_TOKEN 等）——在 huggingface_hub 读环境之前
    load_env_file(Path(__file__).resolve().parents[2] / ".env")
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as e:
        raise TranscribeUnavailable(_INSTALL_HINT) from e
    if _gpu_ready():
        try:
            return _run_model(WhisperModel(model_size, device="auto",
                                           compute_type="auto"),
                              audio_path, progress)
        except RuntimeError as e:
            # 推理期才炸的缺库（构造器惰性加载）→ 回退 CPU int8 重跑
            if not any(k in str(e).lower() for k in ("cublas", "cudnn", "cudart")):
                raise
    return _run_model(WhisperModel(model_size, device="cpu",
                                   compute_type="int8"), audio_path, progress)


def transcribe(audio_path: Path, model_size: str = "large-v3",
               _backend=None, progress=None) -> list[dict]:
    """转写 → 归一化段列表（升序、<0.2s 间隙合并、文本首尾剥离）。
    progress(n)：每 25 段心跳回调（2026-09-05 真机：长转写无反馈）。"""
    backend = _backend or _default_backend
    try:
        raw = backend(Path(audio_path), model_size, progress=progress)
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


_CLEANUP_SYSTEM = """你在清洗 ASR（语音转文字）结果。给你带序号的转写段，逐段处理：
1. 修正同音/近音错字（依上下文猜正确词，如「陈薄了」→「承不住了」）
2. 纯语气词段（嗯/啊/哦/呃……无实义）返回空串
3. 保留原意与口语风格，不扩写不润色不合并段
只输出一个 JSON 对象：{"段序号": "清洗后文本或空串"}，覆盖所有给出的序号。"""


def cleanup_transcription(db, data_dir, project_id, client) -> dict:
    """P10C 转写校对遍（2026-09-05 用户需求：语气词+同音错字）：按段 LLM 清洗，
    时间轴原样保留，空串段丢弃；重写 segments.json + novel.txt + 章节重算。"""
    import re as _re
    from .chapters import parse_chapters
    from .logbus import emit as emit_log
    from .projects import get_project

    def _parse_json(text):
        m = _re.search(r"\{.*\}", text or "", _re.S)
        if not m:
            raise ValueError(f"校对输出非 JSON：{(text or '')[:80]}")
        return json.loads(m.group(0))

    proj = get_project(db, project_id)
    segs = load_segments(data_dir, proj["slug"])
    if not segs:
        return {"removed": 0, "segments": 0}
    out, removed = [], 0
    BATCH = 40
    for i in range(0, len(segs), BATCH):
        chunk = segs[i:i + BATCH]
        lines = "\n".join(f"{j + 1}: {s['text']}" for j, s in enumerate(chunk))
        text, _u = client.raw_chat(
            [{"role": "system", "content": _CLEANUP_SYSTEM},
             {"role": "user", "content": lines}], temperature=0.2)
        fixed = _parse_json(text)
        for j, s in enumerate(chunk):
            t = str(fixed.get(str(j + 1), s["text"])).strip()
            if not t:
                removed += 1
                continue
            out.append({**s, "text": t})
    save_segments(data_dir, proj["slug"], out)
    full = "\n\n".join(s["text"] for s in out)
    from .paths import data_to_abs
    data_to_abs(data_dir, proj["novel_path"]).write_text(full, encoding="utf-8")
    conn = db.connect()
    conn.execute("UPDATE projects SET chapters_json=? WHERE id=?",
                 (json.dumps(parse_chapters(full), ensure_ascii=False), project_id))
    conn.commit()
    emit_log(db, "asr", "info",
             f"转写校对完成：{len(out)} 段保留 / {removed} 段语气词丢弃",
             project_id=project_id)
    return {"removed": removed, "segments": len(out)}
