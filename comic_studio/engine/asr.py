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


_ENRICH_SYSTEM = """你在把 ASR 对白转写扩写成小说正文。给你带序号的转写段与
【内容主题】，逐段扩写：
1. **原句逐字保留**——可加引号变对白、可调语序外的标点，但字不得增删改
   （后续要与音频时间轴对齐，改字=锚点失效）
2. 句间自由加旁白：动作/环境/神态/心理/衔接，围绕【内容主题】让剧情连贯丰富
3. 纯语气词段（嗯/啊）可融入邻近段的旁白或返回空串
4. 每段扩写 50~150 字，输出仍按段对应
只输出一个 JSON 对象：{"段序号": "扩写文本或空串"}，覆盖所有给出的序号。"""


_CLEANUP_SYSTEM = """你在清洗 ASR（语音转文字）结果。给你带序号的转写段，逐段处理：
1. 修正同音/近音错字（依上下文猜正确词，如「陈薄了」→「承不住了」）
2. 纯语气词段（嗯/啊/哦/呃……无实义）返回空串
3. 保留原意与口语风格，不扩写不润色不合并段
只输出一个 JSON 对象：{"段序号": "清洗后文本或空串"}，覆盖所有给出的序号。"""


def cleanup_transcription(db, data_dir, project_id, client, theme: str = "",
                          mode: str = "conservative") -> dict:
    """P10C 转写校对遍（2026-09-05 用户需求：语气词+同音错字）：按段 LLM 清洗，
    时间轴原样保留，空串段丢弃；重写 segments.json + novel.txt + 章节重算。
    theme（2026-09-05 用户需求：内容主题锚）：注入提示词供纠错围绕核心；
    持久化 audio/theme.txt——下次不带参自动复用。
    mode：conservative（默认，保守修错+重写段落盘）｜enrich（丰富扩写：
    原句逐字保留+围绕主题加旁白；**segments.json 不动**——音频段是时长/
    原声锚点，扩写文本靠原句子串双向包含仍能命中 span_duration_for）。"""
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
    tfile = Path(data_dir) / audio_rel(proj["slug"]) / "theme.txt"
    if not theme.strip() and tfile.exists():
        theme = tfile.read_text(encoding="utf-8").strip()   # 复用持久化主题
    theme_line = (f"\n【内容主题（校对围绕此核心纠错）】{theme.strip()}\n"
                  if theme.strip() else "")
    out, removed = [], 0
    BATCH = 40
    n_batches = (len(segs) + BATCH - 1) // BATCH
    from .logbus import emit as _el
    _el(db, "asr", "info",
        f"转写校对开始：{len(segs)} 段 / {n_batches} 批（模型思考+输出需数分钟，"
        "本地慢模型更久——耐心等完成日志）", project_id=project_id)
    for bi, i in enumerate(range(0, len(segs), BATCH), 1):
        chunk = segs[i:i + BATCH]
        lines = "\n".join(f"{j + 1}: {s['text']}" for j, s in enumerate(chunk))
        sys_prompt = (_ENRICH_SYSTEM if mode == "enrich" else _CLEANUP_SYSTEM) + theme_line
        _el(db, "asr", "info", f"校对批 {bi}/{n_batches} 调用中（{len(chunk)} 段）…",
            project_id=project_id)
        text, _u = client.raw_chat(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": lines}], temperature=0.2)
        fixed = _parse_json(text)
        for j, s in enumerate(chunk):
            t = str(fixed.get(str(j + 1), s["text"])).strip()
            if not t:
                removed += 1
                continue
            out.append({**s, "text": t})
    if theme.strip():
        tfile.parent.mkdir(parents=True, exist_ok=True)
        tfile.write_text(theme.strip(), encoding="utf-8")
    if mode != "enrich":
        # 保守模式才重写段落盘（丰富模式 segments=音频锚点，原样保留）
        save_segments(data_dir, proj["slug"], out)
    removed = removed if mode != "enrich" else sum(
        1 for _ in range(0)) or removed  # enrich 的空串=融入，不计丢弃
    full = "\n\n".join(s["text"] for s in out)
    from .paths import data_to_abs
    data_to_abs(data_dir, proj["novel_path"]).write_text(full, encoding="utf-8")
    conn = db.connect()
    conn.execute("UPDATE projects SET chapters_json=? WHERE id=?",
                 (json.dumps(parse_chapters(full), ensure_ascii=False), project_id))
    conn.commit()
    emit_log(db, "asr", "info",
             (f"转写扩写完成（丰富模式）：{len(out)} 段，原句逐字保留、"
              "段落盘（音频锚点）未动" if mode == "enrich" else
              f"转写校对完成：{len(out)} 段保留 / {removed} 段语气词丢弃"),
             project_id=project_id)
    return {"removed": removed, "segments": len(out), "mode": mode}


# ═══ P10-D：ComfyUI Qwen3-ASR 后端（2026-09-05 用户接入，输出不全防御）═══

import re as _re2


def parse_qwen_output(text: str, chunk_start: float, chunk_dur: float = 0.0) -> list:
    """Qwen3ASR 时间戳输出解析（格式未实测——多格式容忍+整块兜底）：
    A JSON {"segments":[{start,end,text}]}；B 行式 [s -> e] txt；
    C SRT 块；兜底=纯文本整块单段（start=chunk_start, end=+chunk_dur）。
    所有时间偏移 chunk_start（分块拼接）。"""
    t = (text or "").strip()
    if not t:
        return []
    # A: JSON
    m = _re2.search(r"\{.*\"segments\".*\}", t, _re2.S)
    if m:
        try:
            segs = json.loads(m.group(0)).get("segments") or []
            out = [{"start": chunk_start + float(x["start"]),
                    "end": chunk_start + float(x["end"]),
                    "text": str(x.get("text", "")).strip()}
                   for x in segs if str(x.get("text", "")).strip()]
            if out:
                return out
        except (ValueError, KeyError, TypeError):
            pass
    # C: SRT（先于行式——SRT 行含 ,mmm）
    blocks = _re2.split(r"\n\s*\n", t)
    srt = []
    for blk in blocks:
        mm = _re2.search(
            r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-+>\s*"
            r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*\n(.+)", blk, _re2.S)
        if mm:
            g = [int(x) for x in mm.groups()[:8]]
            st = g[0]*3600 + g[1]*60 + g[2] + g[3]/1000
            en = g[4]*3600 + g[5]*60 + g[6] + g[7]/1000
            srt.append({"start": chunk_start + st, "end": chunk_start + en,
                        "text": mm.group(9).strip().replace("\n", " ")})
    if srt:
        return srt
    # B: 行式 [00:00.500 -> 00:02.000] txt（或 [0.5->2.0]）
    line_re = _re2.compile(
        r"\[?\s*(\d{1,2}):(\d{2})[.:](\d{1,3})\s*-+>\s*"
        r"(\d{1,2}):(\d{2})[.:](\d{1,3})\s*\]?\s*(.+)")
    out = []
    for ln in t.splitlines():
        mm = line_re.match(ln.strip())
        if mm:
            g = [int(x) for x in mm.groups()[:6]]
            st = g[0]*60 + g[1] + g[2]/1000
            en = g[3]*60 + g[4] + g[5]/1000
            txt = mm.group(7).strip()
            if txt:
                out.append({"start": chunk_start + st, "end": chunk_start + en,
                            "text": txt})
    if out:
        return out
    # 兜底：纯文本整块（保时长轴可用；chunk_dur 未知则 0=end=start）
    return [{"start": chunk_start, "end": chunk_start + chunk_dur, "text": t}]


def text_density_ok(text: str, audio_dur: float, min_cps: float = 0.5) -> bool:
    """完整度校验（用户 flagged 输出不全）：中文语速约 3~4 字/s，低于
    min_cps 判可疑（大量留白音频会误报——warn 不拦截，提示换引擎复查）。"""
    if audio_dur <= 0:
        return True
    return len((text or "").strip()) >= min_cps * audio_dur


def transcribe_comfy(db, audio_path, comfy, progress=None, theme: str = "",
                     chunk_seconds: float = 300.0) -> list:
    """ComfyUI Qwen3-ASR 后端：长音频 ffmpeg 分块（防单次输出不全）→
    每块上传提交 asr_qwen3 工作流（context=主题热词、时间戳开）→ 轮询取
    SaveText 产物 → parse_qwen_output 偏移拼接 → 完整度校验 warn。
    依赖：ComfyUI 可达 + custom node（Qwen3ASR Loader/Transcribe/SaveText）。"""
    import subprocess as _sp
    import tempfile as _tf
    from .logbus import emit as emit_log
    from .merge import ffmpeg_bin, probe
    from .settings import get_setting
    from .workflows import filler, registry

    audio_path = Path(audio_path)
    total = probe(audio_path).get("duration") or 0.0
    cfg = get_setting(db, "asr") or {}
    chunk_seconds = float(cfg.get("chunk_seconds", chunk_seconds))
    tmpl = registry.resolve_template(db, "asr")

    tmpdir = Path(_tf.mkdtemp(prefix="asr_chunks_"))
    chunks = []
    if total > chunk_seconds > 0:
        _sp.run([ffmpeg_bin(), "-y", "-i", str(audio_path), "-f", "segment",
                 "-segment_time", str(int(chunk_seconds)), "-c", "copy",
                 str(tmpdir / "c%04d.mp3")],
                check=True, capture_output=True, timeout=600)
        chunks = sorted(tmpdir.glob("c*.mp3"))
    else:
        chunks = [audio_path]

    segs_all, full_text = [], []
    try:
        for i, ch in enumerate(chunks, 1):
            ch_start = (i - 1) * chunk_seconds if len(chunks) > 1 else 0.0
            ch_dur = probe(ch).get("duration") or 0.0
            comfy.upload_media(ch, ch.name)
            wf = json.loads((Path(registry.TEMPLATE_ROOT) /
                             "audio_to_text.api.json").read_text(encoding="utf-8"))
            wf["1"]["inputs"]["audio"] = ch.name
            wf["10"]["inputs"]["context"] = theme or ""
            wf["10"]["inputs"]["return_timestamps"] = True
            pid = comfy.submit(wf, f"asr-{i}")
            items = comfy.wait_and_collect(pid, stall_seconds=1800,
                                           on_interrupt=None)
            texts = [x for x in items if str(x.get("filename", "")).endswith(".txt")]
            raw = ""
            for x in texts:
                try:
                    raw += comfy.download_text(x) or ""
                except Exception:
                    pass
            if not raw:   # SaveText 无产物→异常上抛（走 job 失败守卫）
                raise RuntimeError(f"ASR 块 {i}：ComfyUI 未返回文本产物")
            part = parse_qwen_output(raw, ch_start, ch_dur)
            segs_all.extend(part)
            full_text.append("\n".join(p["text"] for p in part))
            if progress:
                progress(len(segs_all))
            emit_log(db, "asr", "info",
                     f"ASR 块 {i}/{len(chunks)} 完成（{len(part)} 段）",
                     project_id=None)
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
    if not text_density_ok("\n".join(full_text), total):
        emit_log(db, "asr", "warn",
                 f"转写文本密度偏低（{len(''.join(full_text))} 字 / "
                 f"{total:.0f}s）——可能输出不全或音频大量留白，"
                 "可复查或换引擎重试", project_id=None)
    return segs_all
