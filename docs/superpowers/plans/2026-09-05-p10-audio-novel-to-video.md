# P10 有声小说转视频 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上传一段有声小说音频 → ASR 转写 → 走现有小说生产线（资产/分镜/提示词/渲染/合成），最终成片使用**原声音轨切片**作为配音（免 TTS），分镜时长由**音频实测时长**驱动。

**Architecture:** 新增 `engine/asr.py`（转写/切片/时长对齐，重依赖全部惰性导入）+ `from-audio` 导入入口 + `transcribe` 队列任务；音频段元数据按约定落 `projects/<slug>/audio/segments.json`（零迁移）；B 期在 `tts.generate_dialogue_audio` 分流：镜 ledger 带 `audio_span` 且源音频在 → ffmpeg 切原声替代 TTS；C 期增强（diarization/人名校正/ref2va 原声口型）。现有门禁/autopilot/提示词/渲染/合成链路**零改动**复用。

**Tech Stack:** faster-whisper（A/B，词级时间戳，CPU/GPU）、pyannote.audio（C，可选）、ffmpeg（切片，复用 engine/merge.ffmpeg_bin）、现有 FastAPI+SQLite+Vue3。

**Spec:** 2026-09-05 会话内可行性评估（对话记录）+ `docs/2026-09-05-feature-audit.md` §2 功能全景。评估结论：音频自带精确时长与成品配音两大优势；风险=转写人名错误/单人多角色/重依赖。

## Global Constraints

- TDD：先失败测试后实现；`pytest -q` 全绿才提交；小步提交
- `comic_studio/engine/` 禁止 import fastapi/starlette/uvicorn
- LLM 测试一律 FakeClient；ffmpeg 测试用真二进制（`engine/merge.ffmpeg_bin`，模式同 tests/test_merge.py）
- 重依赖（faster-whisper/pyannote）**惰性导入** + `[project.optional-dependencies] asr`（装法：两侧 venv `pip install -e ".[dev,asr]"`；缺包时 API 显式 422 含安装指引，不炸服务）
- 新文件遵循仓库惯例：中文 docstring 带日期与缘由；数据路径存 data 相对 POSIX
- 每期完成同步 CLAUDE.md 模块地图 + README + 本计划勾选
- 音频源文件上限 200MB（Form 校验）；格式 mp3/wav/m4a/flac/ogg

---

## Phase A：最小闭环（转写→现有链，音频时长驱动分镜）

### Task 1: engine/asr.py 骨架——转写函数（可注入后端）

**Files:**
- Create: `comic_studio/engine/asr.py`
- Modify: `pyproject.toml`（optional-dependencies 加 asr 组）
- Test: `tests/test_asr.py`

**Interfaces:**
- Produces: `transcribe(audio_path: Path, model_size: str = "large-v3", _backend=None) -> list[dict]`——返回 `[{"start": float, "end": float, "text": str}]`（升序、合并 <0.2s 间隙、剥首尾空白）；`TranscribeUnavailable(Exception)`；`SEGMENTS_NAME = "audio/segments.json"`、`audio_dir(project_slug)` 约定助手；`load_segments(data_dir, slug) -> list | None`；`save_segments(data_dir, slug, segs)`。`_backend` 参数供测试注入（真实现签名 `(audio_path, model_size) -> list[tuple[float,float,str]]`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_asr.py
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
```

- [ ] **Step 2: 跑测试确认失败** — `pytest tests/test_asr.py -q` → ModuleNotFoundError: asr
- [ ] **Step 3: 实现**

```python
# comic_studio/engine/asr.py
"""P10 有声书链路（2026-09-05 计划）：ASR 转写、音频段存取、原声切片。

重依赖 faster-whisper/pyannote 一律函数内惰性导入——基础安装不带 asr
extra 时不影响服务（入口路由显式 422 给安装指引）。"""
import json
from pathlib import Path

SEGMENTS_NAME = "audio/segments.json"


class TranscribeUnavailable(Exception):
    pass


def _default_backend(audio_path: Path, model_size: str):
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as e:
        raise TranscribeUnavailable(
            f"faster-whisper 未安装：WSL `.venv/bin/pip install -e '.[asr]'` / "
            f"Windows `.venv-win/Scripts/pip.exe install -e '.[asr]'`（首次运行需下载模型）") from e
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segs, _info = model.transcribe(str(audio_path), language="zh",
                                   vad_filter=True, beam_size=5)
    return [(s.start, s.end, s.text) for s in segs]


def transcribe(audio_path: Path, model_size: str = "large-v3", _backend=None):
    """转写 → 归一化段列表（升序、<0.2s 间隙合并、文本首尾剥离）。"""
    backend = _backend or _default_backend
    raw = backend(Path(audio_path), model_size)
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


def load_segments(data_dir, slug: str):
    f = Path(data_dir) / audio_rel(slug) / "segments.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None
```

pyproject：`[project.optional-dependencies]` 追加 `asr = ["faster-whisper>=1.0"]`（保留 dev 行）。

- [ ] **Step 4: 跑测试确认通过** — `pytest tests/test_asr.py -q` → 3 passed
- [ ] **Step 5: 提交** — `git add comic_studio/engine/asr.py tests/test_asr.py pyproject.toml && git commit -m "feat(P10A): engine/asr 转写骨架——可注入后端+段归一化+存取"`

### Task 2: from-audio 导入入口（建项目+存源音频+入队转写）

**Files:**
- Modify: `comic_studio/web/routes_projects.py`（追加端点，置于 from-comic 之后）
- Test: `tests/test_asr.py`（追加）

**Interfaces:**
- Produces: `POST /api/projects/from-audio`（Form: name/aspect_ratio/default_shot_duration/target_duration + File: audio）→ 201 项目 JSON；产物约定 `projects/<slug>/audio/source.<原扩展名>`；队列 job `type="transcribe"`、`payload={"project_id": pid}`、`resource=None`（CPU 可跑，不占 gpu 组）。缺 asr 依赖时入队前探测 → 422（不建半截项目）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_asr.py 追加
import io
from fastapi.testclient import TestClient
from comic_studio.web.app import create_app


def test_from_audio_creates_project_and_enqueues(tmp_path, monkeypatch):
    from comic_studio.engine import asr as asr_mod
    monkeypatch.setattr(asr_mod, "_default_backend",
                        lambda p, m: [(0.0, 1.0, "你好")])  # 入队前探测用
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


def test_from_audio_rejects_oversize(tmp_path):
    with TestClient(create_app(tmp_path / "t.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-audio",
                   data={"name": "大剧", "aspect_ratio": "16:9"},
                   files={"audio": ("big.mp3", io.BytesIO(b"x" * (200 * 1024 * 1024 + 1)),
                                    "audio/mpeg")})
        assert r.status_code == 422
```

- [ ] **Step 2: 确认失败**（404 not found / 413）
- [ ] **Step 3: 实现端点**

```python
# routes_projects.py 追加（from-comic 之后）
@router.post("/from-audio", status_code=201)
def create_from_audio(request: Request, name: str = Form(...),
                      aspect_ratio: str = Form("9:16"),
                      default_shot_duration: float = Form(0.0),
                      target_duration: float = Form(0.0),
                      audio: UploadFile = File(...)):
    """P10 有声书导入（2026-09-05 计划）：存源音频 → 建项目（占位正文）→
    入队 transcribe；转写完成后回填 novel.txt，之后走现有小说链。"""
    data = audio.file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(422, "音频超过 200MB 上限（请先切分）")
    ext = (Path(audio.filename or "a.mp3").suffix.lstrip(".") or "mp3").lower()
    if ext not in ("mp3", "wav", "m4a", "flac", "ogg"):
        raise HTTPException(422, f"不支持的音频格式 .{ext}")
    from ..engine.asr import TranscribeUnavailable, _default_backend  # noqa: 探测依赖
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix="." + ext) as tf:  # 探测可导入
            pass
        from ..engine import asr as _asr  # 惰性依赖在 handler 里才真加载
        hasattr(_asr, "transcribe")
        from faster_whisper import WhisperModel  # noqa: F401 —— 缺包即 422
    except (ModuleNotFoundError, TranscribeUnavailable) as e:
        raise HTTPException(422, f"ASR 依赖未安装：{e}；"
                                 "WSL `.venv/bin/pip install -e '.[asr]'` / "
                                 "Windows `.venv-win/Scripts/pip.exe install -e '.[asr]'`")
    proj = create_project(request.app.state.db, request.app.state.data_dir,
                          name, aspect_ratio, "（有声书转写中，转写完成后自动回填正文）",
                          default_shot_duration=default_shot_duration,
                          target_duration=target_duration)
    adir = Path(request.app.state.data_dir) / f"projects/{proj['slug']}/audio"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"source.{ext}").write_bytes(data)
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(request.app.state.db, "transcribe", project_id=proj["id"],
                      payload={"project_id": proj["id"], "ext": ext})
    return _public(get_project(request.app.state.db, proj["id"])) if "_public" in dir() else proj
```

> 注：`_public` 是本文件既有序列化助手（from-comic 用它）——实现时直接 `return _public(proj)`，上面末行写成防御式仅为提醒；落地时清理为 `return _public(proj)`。

- [ ] **Step 4: 跑测试通过**（第二条测试走 422 路径不需 asr 依赖——读取 body 大小在依赖探测之前）
- [ ] **Step 5: 提交** — `git commit -m "feat(P10A): from-audio 导入入口——存源音频+建项目+入队转写"`

### Task 3: transcribe 队列任务（转写→回填正文+段落盘+章节重算）

**Files:**
- Modify: `comic_studio/engine/pipeline_jobs.py`（追加 handler）
- Test: `tests/test_asr.py`（追加）

**Interfaces:**
- Consumes: Task 1 全部；`comic_studio/engine/chapters.parse_chapters`
- Produces: `@register("transcribe")` handler——完成态：`novel.txt` 为转写全文（段间空行）、`audio/segments.json` 落盘、`projects.chapters_json` 重算、日志「转写完成：N 段 / M 字」。

- [ ] **Step 1: 失败测试**

```python
def test_transcribe_job_fills_novel_and_segments(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project, get_project
    from comic_studio.engine.queue.worker import HANDLERS
    from comic_studio.engine.jobs import enqueue_job
    import comic_studio.engine.pipeline_jobs as PJ  # 触发注册
    from comic_studio.engine import asr as asr_mod
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "转写剧", "16:9",
                         "（有声书转写中）")["id"]
    adir = tmp_path / "data" / "projects" / "转写剧" / "audio"
    adir.mkdir(parents=True)
    (adir / "source.mp3").write_bytes(b"f")
    monkeypatch.setattr(asr_mod, "transcribe",
                        lambda p, model_size="large-v3", _backend=None: [
                            {"start": 0.0, "end": 2.0, "text": "林凡推门。"},
                            {"start": 2.2, "end": 5.0, "text": "雨夜。"}])
    jid = enqueue_job(db, "transcribe", project_id=pid,
                      payload={"project_id": pid, "ext": "mp3"})
    job = db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    HANDLERS["transcribe"](db, tmp_path / "data", dict(job), None)
    from comic_studio.engine.paths import data_to_abs
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    assert novel.read_text(encoding="utf-8") == "林凡推门。\n\n雨夜。"
    assert asr_mod.load_segments(tmp_path / "data", "转写剧")[0]["text"] == "林凡推门。"
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%转写完成%'"
    ).fetchone()["c"]
    assert n == 1
```

- [ ] **Step 2: 确认失败**（HANDLAMES 无 transcribe）
- [ ] **Step 3: 实现 handler（pipeline_jobs.py 追加）**

```python
@register("transcribe")
def handle_transcribe(db, data_dir, job, comfy):
    """P10 有声书转写（2026-09-05 计划）：源音频 → 段落盘 + novel.txt 回填
    + 章节重算。失败由 retry_or_fail 兜底（autopilot/手动重发解除同 analyze）。"""
    from ..engine import asr as asr_mod  # noqa: 相对导入按本文件既有惯例
    from .projects import get_project  # engine 内既有延迟导入风格
```

```python
# pipeline_jobs.py 追加（相对导入风格随文件头既有惯例调整为 ..engine）
@register("transcribe")
def handle_transcribe(db, data_dir, job, comfy):
    from ..engine import asr as asr_mod
    from ..engine.logbus import emit as emit_log
    from ..engine.paths import data_to_abs
    from ..engine.projects import get_project
    payload = json.loads(job["payload_json"] or "{}")
    pid = payload.get("project_id", job["project_id"])
    proj = get_project(db, pid)
    if proj is None:
        raise ValueError(f"项目不存在: {pid}")
    src = data_to_abs(data_dir, f"projects/{proj['slug']}/audio/source.{payload.get('ext', 'mp3')}")
    if not src.exists():
        raise ValueError(f"源音频缺失: {src}")
    segs = asr_mod.transcribe(src)
    asr_mod.save_segments(data_dir, proj["slug"], segs)
    full = "\n\n".join(x["text"] for x in segs)
    data_to_abs(data_dir, proj["novel_path"]).write_text(full, encoding="utf-8")
    from ..engine.chapters import parse_chapters
    conn = db.connect()
    conn.execute("UPDATE projects SET chapters_json=? WHERE id=?",
                 (json.dumps(parse_chapters(full), ensure_ascii=False), pid))
    conn.commit()
    emit_log(db, "asr", "info",
             f"转写完成：{len(segs)} 段 / {len(full)} 字（正文已回填，"
             "可继续 分析→一键出片）", project_id=pid)
```

- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: 提交** — `git commit -m "feat(P10A): transcribe 任务——转写回填正文+段落盘+章节重算"`

### Task 4: 拆解时长音频驱动（segments 对齐 text_span）

**Files:**
- Modify: `comic_studio/engine/asr.py`（追加 `span_duration_for`）、`comic_studio/engine/llm/storyboard.py`（split 尾部挂钩）
- Test: `tests/test_asr.py`（追加）

**Interfaces:**
- Produces: `span_duration_for(segments: list, text: str) -> float | None`——把镜 text_span（normalize：去空白/标点）在段文本里找包含或最大重叠，返回对应段时长并集；找不到返回 None。split_storyboards 在 reestimate 之后：段文件存在时逐镜 `d = span_duration_for(...)`，命中则 `s.duration = min(15.0, max(4.0, d))`（音频实测值为最终权威，覆盖一切估时）。

- [ ] **Step 1: 失败测试**

```python
def test_span_duration_matching(tmp_path):
    from comic_studio.engine.asr import span_duration_for
    segs = [{"start": 0.0, "end": 6.4, "text": "林凡推门而入，雨水顺着发梢滴落"},
            {"start": 6.5, "end": 9.0, "text": "他愣住了"}]
    assert span_duration_for(segs, "林凡推门而入，雨水顺着发梢滴落") == 6.4
    assert span_duration_for(segs, "林凡推门而入 雨水顺着发梢滴落 他愣住了") == 9.0  # 跨段并集
    assert span_duration_for(segs, "完全无关的句子") is None


def test_split_uses_audio_durations(tmp_path):
    from tests.test_duration_control import CHUNK, _proj  # 复用 FakeLLM 模板
    from tests.test_storyboard_split import FakeLLM
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine import asr as asr_mod
    db, pid = _proj(tmp_path)
    asr_mod.save_segments(tmp_path / "data", "时长剧",
                          [{"start": 0.0, "end": 8.0, "text": "推门"}])
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: FakeLLM([CHUNK.format(desc="推门", dur=5)]))
    assert [s["duration"] for s in list_shots(db, pid)] == [8.0]  # 音频覆盖 LLM 估时
```

- [ ] **Step 2: 确认失败** — ImportError span_duration_for / duration==5.0
- [ ] **Step 3: 实现**（asr.py 追加 + storyboard.py 挂钩）

```python
# asr.py 追加
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
```

storyboard.py：`reestimate_durations(staged)` 之后、`persist_shots` 之前插入：

```python
    # P10A（2026-09-05）：音频实测时长为最终权威——段文件存在时覆盖估时
    from .asr import load_segments, span_duration_for  # 惰性，避免无 P10 项目时的开销
    _segs = load_segments(data_dir, proj["slug"])
    if _segs:
        for s in staged:
            d = span_duration_for(_segs, s.text_span)
            if d:
                s.duration = min(15.0, max(4.0, d))
```

（注意 `proj` 在本函数开头已取。）

- [ ] **Step 4: 跑测试通过**（并确认既有拆解测试不受影响——无段文件时零行为变化）
- [ ] **Step 5: 提交** — `git commit -m "feat(P10A): 分镜时长音频驱动——segments 对齐 text_span 覆盖估时"`

### Task 5: 前端「🎧 有声书」tab

**Files:**
- Modify: `frontend/index.html`（创建弹窗第四 tab）、`frontend/app.js`（audioFiles/fromAudio）
- Test: 手工验收（无 JS 测试基建；Playwright 冒烟可选）

**Interfaces:**
- Produces: tab 内 name/ratio/段时长/总时长复用公共字段 + 音频选择 + 上传进度（fetch from-audio）；成功关弹窗进详情（stage=created，等 transcribe job）。

- [ ] **Step 1: index.html tab 按钮与面板**（照 🖼 漫画 tab 结构复制改：`audioFiles` 多选→单选 `<input type="file" accept="audio/*">`，提交按钮调 `fromAudio()`）
- [ ] **Step 2: app.js**（照 createFromComic 写 `async fromAudio()`：FormData name/aspect_ratio/default_shot_duration(Number||0)/target_duration(Number||0)/audio → POST /api/projects/from-audio → 失败 alert 保留表单（L19 模式）→ 成功清表关弹窗 refresh）
- [ ] **Step 3: 手工验收**——启动服务 → 上传一段 mp3 → 队列出现 transcribe → 完成后正文回填 → 点分析走既有链
- [ ] **Step 4: 提交** — `git commit -m "feat(P10A): 创建弹窗🎧有声书 tab——from-audio 上传"`

---

## Phase B：原声直用（audio_span 切片替代 TTS）

### Task 6: 原声切片 + tts 分流

**Files:**
- Modify: `comic_studio/engine/asr.py`（`cut_span`）、`comic_studio/engine/tts.py`（`generate_dialogue_audio` 分流）、`comic_studio/engine/llm/storyboard.py`（拆解时把命中段的起止写进 ledger）
- Test: `tests/test_asr.py`（追加）

**Interfaces:**
- Produces: `cut_span(src: Path, start: float, end: float, out: Path, db=None) -> Path`（ffmpeg `-ss/-to` 重编码 aac 44100 立体声——与合成段参数统一，避免 QC 时代码参数混拼复发）。ledger 新键 `audio_span={"start","end"}`；`generate_dialogue_audio` 遇 `audio_span` + 源音频存在 → 切片为 dialogue.mp3 并**跳过 TTS/克隆**（ledger 标 `origin_audio: true` 供审计），异常回退常规 TTS 链。

- [ ] **Step 1: 失败测试**

```python
def test_cut_span_and_tts_origin_flow(tmp_path, monkeypatch):
    import subprocess
    from comic_studio.engine.merge import ffmpeg_bin, probe
    from comic_studio.engine.asr import cut_span
    wav = tmp_path / "src.wav"
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=10", str(wav)],
                   check=True, capture_output=True, timeout=60)
    out = cut_span(wav, 2.0, 5.5, tmp_path / "cut.m4a")
    p = probe(out)
    assert abs(p["duration"] - 3.5) < 0.3 and p["sample_rate"] == 44100


def test_tts_uses_origin_audio_span(tmp_path, monkeypatch):
    """ledger.audio_span + 源音频 → 切片直用，不调任何 TTS 后端。"""
    import subprocess
    from comic_studio.engine.merge import ffmpeg_bin
    from comic_studio.engine.tts import generate_dialogue_audio
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.shots import list_shots, persist_shots
    from types import SimpleNamespace as NS
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "原声剧", "16:9", "t")["id"]
    persist_shots(db, pid, [NS(text_span="推门", description="d", shot_type="",
        camera={}, duration=4.0, workflow_type="t2v",
        ledger={"dialogue": [{"speaker": "旁白", "line": "推门"}],
                "audio_span": {"start": 1.0, "end": 4.2}},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])
    adir = tmp_path / "data" / "projects" / "原声剧" / "audio"
    adir.mkdir(parents=True)
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=6", str(adir / "source.mp3")],
                   check=True, capture_output=True, timeout=60)

    def bomb(*a, **kw):
        raise AssertionError("原声镜不得走 TTS")
    monkeypatch.setattr("comic_studio.engine.tts._tts_sync", bomb)
    monkeypatch.setattr("comic_studio.engine.tts._try_voiced_tts", bomb)
    res = generate_dialogue_audio(db, tmp_path / "data", pid)
    assert res and res[0]["seq"] == 1
    mp3 = tmp_path / "data" / "projects" / "原声剧" / "shots" / "1" / "dialogue.mp3"
    assert mp3.exists()
    led = list_shots(db, pid)[0]["ledger_json"]
    assert "origin_audio" in led
```

- [ ] **Step 2: 确认失败**（cut_span 不存在 / bomb 被触发）
- [ ] **Step 3: 实现**

```python
# asr.py 追加
def cut_span(src: Path, start: float, end: float, out: Path) -> Path:
    """原声切片（P10B）：重编码 aac 44100 立体声——段参数与合成链统一。"""
    from .merge import ffmpeg_bin
    out.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(src),
                    "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    str(out)], check=True, capture_output=True, timeout=120)
    return out
```

tts.py `generate_dialogue_audio` 循环体开头（清 stale 之后、克隆/Edge 之前）插入分流：

```python
        # P10B：原声镜——ledger.audio_span + 源音频在 → 切片直用免 TTS
        span = (ledger := json.loads(shot["ledger_json"] or "{}")).get("audio_span")
        src = None
        for cand in (data_dir_abs / f"projects/{proj['slug']}/audio").glob("source.*"):
            src = cand
            break
        if span and src:
            try:
                from .asr import cut_span
                cut_span(src, float(span["start"]), float(span["end"]),
                         shot_dir / "dialogue.mp3")
                ledger["origin_audio"] = True
                conn.execute("UPDATE shots SET ledger_json=? WHERE id=?",
                             (json.dumps(ledger, ensure_ascii=False), shot["id"]))
                conn.commit()
                results.append({"seq": shot["seq"], "origin": True})
                continue
            except Exception as exc:
                emit_log(db, "tts", "warn",
                         f"镜 {shot['seq']} 原声切片失败（{exc}），回退 TTS",
                         project_id=project_id)
```

storyboard.py（Task 4 的音频覆盖块内同步写 ledger）：

```python
            if d:
                s.duration = min(15.0, max(4.0, d))
                hits = hits_of(_segs, s.text_span)   # 与 span_duration_for 同一命中集
                s.ledger = dict(s.ledger or {})
                s.ledger["audio_span"] = {"start": min(x["start"] for x in hits),
                                          "end": max(x["end"] for x in hits)}
```

配套：asr.py 把命中判断抽公开函数并让 `span_duration_for` 复用——

```python
def hits_of(segments: list, text: str) -> list:
    """规范化子串双向包含的命中段（P10A/B 共用命中集）。"""
    key = _norm(text)
    if not key:
        return []
    return [s for s in segments if _norm(s["text"]) and (
        key in _norm(s["text"]) or _norm(s["text"]) in key)]


def span_duration_for(segments: list, text: str):
    hits = hits_of(segments, text)
    if not hits:
        return None
    return max(s["end"] for s in hits) - min(s["start"] for s in hits)
```

- [ ] **Step 4: 跑测试通过 + 全套件**（既有 TTS 测试不受影响——无 audio_span 键时零变化）
- [ ] **Step 5: 提交** — `git commit -m "feat(P10B): 原声直用——audio_span 切片替代 TTS，参数与合成链统一"`

### Task 7: 合成/字幕链确认 + 文档

**Files:**
- Test: `tests/test_asr.py`（追加端到端冒烟）
- Modify: `CLAUDE.md`、`README.md`、`.claude/skills/comic-studio/REFERENCE.md`

**Interfaces:**
- Consumes: Task 6；现有 merge 音频收口与 SRT mp3 探测（应零改动生效——本任务验证之）。

- [ ] **Step 1: 端到端冒烟测试**——原声镜（mp3 3.2s）+ 无声视频 4s → `merge_project` → 成片段长 ≈3.7（3.2+呼吸）、音轨 44100 立体声、`subtitles.generate_srt` 镜内条目落在 [0,3.7)。断言三件套照 tests/test_merge.py 既有模式写。
- [ ] **Step 2: 若冒烟失败**——按失败点修（预期无需改 merge/subtitles：mp3 存在即替换即收口；若 origin_audio 镜被 h3_native_voice 分支误跳则补判定），并把差异记进本计划附注
- [ ] **Step 3: 文档同步**——CLAUDE.md 模块地图新增「P10 有声书链路」节（入口/约定路径/asr extra 装法/origin_audio 语义）；README 功能列表+创建入口一句；REFERENCE.md §1 生命周期加音频路径（created 起与小说同构）
- [ ] **Step 4: 提交** — `git commit -m "feat(P10B): 原声链端到端冒烟+文档同步"`

---

## Phase C：增强（diarization / 人名校正 / ref2va 原声口型）

### Task 8: diarization 说话人段（可选依赖）

**Files:**
- Modify: `pyproject.toml`（asr 组加 `pyannote.audio>=3.3`）、`comic_studio/engine/asr.py`（`diarize` + `merge_speakers`）、`pipeline_jobs.py`（transcribe handler 调用开关）
- Test: `tests/test_asr.py`（追加）

**Interfaces:**
- Produces: `diarize(audio_path, _backend=None) -> list[{"start","end","speaker"}]`（pyannote 惰性导入，缺包 `TranscribeUnavailable` 变体文案）；`merge_speakers(segs, turns) -> segs'`——段内多数覆盖说话人写 `seg["speaker"]`；handler 读 payload `diarize=True`（from-audio Form 复选，默认关）时调用。HF token 约定：环境变量 `HF_TOKEN`（缺时 warn 跳过不炸）。

- [ ] **Step 1: 失败测试**——`merge_speakers` 纯函数：段 [0,4] 与 turn speaker A [0,3.9] → speaker="A"；无覆盖 → 无 speaker 键；`diarize` 缺包报文案含 `pyannote`
- [ ] **Step 2: 确认失败 → 实现 → 通过**

```python
# asr.py 追加
def diarize(audio_path: Path, _backend=None) -> list:
    if _backend is None:
        def _backend(path, ):
            try:
                from pyannote.audio import Pipeline
            except ModuleNotFoundError as e:
                raise TranscribeUnavailable(
                    "pyannote 未安装：pip install -e '.[asr]'（另需 HF_TOKEN 环境变量）") from e
            import os
            if not os.environ.get("HF_TOKEN"):
                raise TranscribeUnavailable("缺少 HF_TOKEN 环境变量（pyannote 模型下载需要）")
            pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                                            use_auth_token=os.environ["HF_TOKEN"])
            return [(t.start, t.end, t.speaker) for t in pipe(str(path)).itertracks(yield_label=True)]
    return [{"start": float(a), "end": float(b), "speaker": str(c)}
            for a, b, c in sorted(_backend(audio_path))]


def merge_speakers(segs: list, turns: list) -> list:
    """段内多数覆盖说话人写入 seg['speaker']（无覆盖不加键）。"""
    out = []
    for sg in segs:
        mid = (sg["start"] + sg["end"]) / 2
        cover = {}
        for t in turns:
            ov = min(sg["end"], t["end"]) - max(sg["start"], t["start"])
            if ov > 0:
                cover[t["speaker"]] = cover.get(t["speaker"], 0.0) + ov
        if cover:
            sg = {**sg, "speaker": max(cover, key=cover.get)}
        out.append(sg)
    return out
```

handler 接线：`payload.get("diarize")` 为真 → `segs = merge_speakers(segs, diarize(src))`（diarize 失败 warn 不炸，降级无 speaker 继续落盘）。
- [ ] **Step 3: from-audio 表单加「区分说话人」复选（默认关）透传 payload**
- [ ] **Step 4: 提交** — `git commit -m "feat(P10C): diarization 说话人段标注（可选依赖，默认关）"`

### Task 9: 人名校正遍（转写名 ↔ 资产名册对齐）

**Files:**
- Modify: `comic_studio/engine/asr.py`（`correct_names`）、`routes_projects.py`（POST /{id}/asr-correct-names 手动入口）
- Test: `tests/test_asr.py`（追加，FakeClient）

**Interfaces:**
- Produces: `correct_names(db, data_dir, project_id, client) -> int`——读 novel.txt+segments+资产名册 → LLM 单轮产出 `{"mapping": {"转写名": "正确名"}}`（FakeClient 注入）→ 全文/段/已存 shots 的 text/prompt 内替换 → 重存。返回替换处数；LLM 失败 raise（手动入口 502）。定位：**analyze 之后、split 之前**手动跑（资产名册已就绪）；skill 手册记录操作顺序。

- [ ] **Step 1: 失败测试**——FakeLLM 返回 mapping {"林烦": "林凡"}；novel/segments 含「林烦」→ 调用后全部替换、返回 2
- [ ] **Step 2: 确认失败 → 实现 → 通过 → 提交**

```python
# asr.py 追加（LLM 单轮，FakeClient 可注入）
def correct_names(db, data_dir, project_id, client) -> int:
    from .assets import list_project_assets
    from .projects import get_project
    proj = get_project(db, project_id)
    names = [a["name"] for a in list_project_assets(db, project_id)]
    novel = data_to_abs(data_dir, proj["novel_path"]).read_text(encoding="utf-8")
    segs = load_segments(data_dir, proj["slug"]) or []
    text, _u = client.raw_chat([
        {"role": "system", "content":
            "你是转写校对。对照角色名册，从转写文本中找出被转错的人名变体，"
            '只输出 JSON：{"mapping": {"错名": "名册正确名"}}；没有则输出空 mapping。'},
        {"role": "user", "content":
            f"名册：{json.dumps(names, ensure_ascii=False)}\n\n转写节选（前 4000 字）：\n{novel[:4000]}"}],
        temperature=0.0)
    import re as _re
    m = _re.search(r"\{.*\}", text or "", _re.S)
    mapping = (json.loads(m.group(0)).get("mapping") if m else None) or {}
    mapping = {k: v for k, v in mapping.items() if k != v and v in names}
    if not mapping:
        return 0
    full = novel
    for k, v in mapping.items():
        full = full.replace(k, v)
    data_to_abs(data_dir, proj["novel_path"]).write_text(full, encoding="utf-8")
    for sg in segs:
        for k, v in mapping.items():
            sg["text"] = sg["text"].replace(k, v)
    if segs:
        save_segments(data_dir, proj["slug"], segs)
    return len(mapping)
```

路由：`POST /api/projects/{id}/asr-correct-names` → `client_for_task(db, "fix_appearance")` 复用既有路由 → 200 `{"replaced": n}` / LLM 异常 502。

### Task 10: ref2va 原声口型（音色槽注入原声段）

**Files:**
- Modify: `comic_studio/engine/rendershot.py`（`_voice_slots_for_shot` 扩展）
- Test: `tests/test_rendershot.py`（追加，comfy_mock）

**Interfaces:**
- Produces: ledger 带 `audio_span`+`origin_audio` 的镜，ref2va 模板音频空槽优先注入**本镜 dialogue.mp3**（原声段）而非音色库样本——H3 按实际台词口型生成；注入后仍标 `h3_native_voice`（合成跳 TTS 既有语义）。开关：项目参数无新增，仅当 origin_audio 存在时优先（音色库样本退居其后）。

- [ ] **Step 1: 失败测试**——原声镜（dialogue.mp3 已切）+ ref2va 模板 + 绑定音色角色 → mock 收到 upload 的 audio 槽文件名 = dialogue.mp3、prompt 含 `<Audio 1>` 声明
- [ ] **Step 2: 确认失败 → 实现 → 通过**

```python
# rendershot.py _voice_slots_for_shot 开头插入（照既有音色样本注入结构）
    ledger = json.loads(shot["ledger_json"] or "{}")
    if ledger.get("origin_audio"):
        mp3 = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}/dialogue.mp3")
        if mp3.exists() and audio_slots:
            # P10C：原声段优先占第一个音频槽——H3 按实际台词口型生成；
            # 音色库样本退居其后（本镜原声即「音色」）
            return ([{"slot": audio_slots[0], "path": str(mp3)}],
                    [f"<Audio 1> carries this character's original spoken line "
                     f"in Mandarin, lip-synced exactly."])
    # ……既有音色库逻辑不动，紧随其后
```
- [ ] **Step 3: 全套件 + CLAUDE.md/REFERENCE.md 收尾 + 提交** — `git commit -m "feat(P10C): ref2va 原声口型——audio 槽优先注入原声段"`

---

## 验收清单（整体）

- [ ] A：上传 3 分钟 mp3 → transcribe job → 正文回填 → 一键出片 → 成片（TTS 配音、时长贴近音频段）
- [ ] B：同上重跑（重拆）→ 成片配音为**原声切片**（听感=原演播）、对白镜段长=原声+0.5
- [ ] C：双人广播剧 → diarize 勾选 → 说话人标注进段；校正遍修转写人名；ref2va 镜口型对原声
- [ ] 三期各自 pytest 全绿 + 文档同步 + skill REFERENCE 更新

## 已知风险与对策（执行时对照）

- 转写人名错 → Task 9 校正遍；首次验收重点看 split 后角色绑定率
- faster-whisper 首跑下载模型（~3GB large-v3）→ README 注明可 `model_size="medium"` 权衡
- 单人多角色 → diarization 不承担角色归属（LLM 从文本判说话人，既有三则兜底）
- 200MB 上限 → 长书先切分（README 说明 ffmpeg 命令一行）
