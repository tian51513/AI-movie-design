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
    segs = transcribe(tmp_path / "a.mp3", _backend=lambda p, m, progress=None: fake)
    assert segs == [{"start": 0.0, "end": 4.5, "text": "你好 世界"},
                    {"start": 10.0, "end": 12.0, "text": "下一段"}]  # <0.2s 间隙合并


def test_transcribe_unavailable_message(tmp_path, monkeypatch):
    def boom(path, model):
        raise ModuleNotFoundError("No module named 'faster_whisper'")
    with pytest.raises(TranscribeUnavailable, match="pip install -e .\\[asr\\]"):
        transcribe(tmp_path / "a.mp3", _backend=lambda p, m, progress=None: boom(p, m))


def test_segments_roundtrip(tmp_path):
    save_segments(tmp_path, "剧", [{"start": 0, "end": 1, "text": "x"}])
    assert load_segments(tmp_path, "剧") == [{"start": 0, "end": 1, "text": "x"}]
    assert load_segments(tmp_path, "无此剧") is None


# ---- P10 Task 2: from-audio 导入入口（建项目+存源音频+入队 transcribe）----

def _stub_faster_whisper(monkeypatch):
    """Ruling-1：测试环境无 asr 包，用桩模块通过路由依赖探测
    （缺包 → 422 的生产语义由真机验证，不在无包环境覆盖）。
    探测是 `from faster_whisper import WhisperModel`——桩需带该名才能
    走通到建项目分支（空模块的 ImportError→422 语义见上面的破装测试）。"""
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


# ---- P10 Task 3: transcribe 队列任务（转写→回填正文+段落盘+章节重算）----

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
                        lambda p, model_size="large-v3", _backend=None, progress=None: [
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
    # 终审顺手项：完成日志带 job_id（与同文件兄弟 handler 一致，可溯源）
    lrow = db.connect().execute(
        "SELECT job_id FROM logs WHERE message LIKE '%转写完成%'").fetchone()
    assert lrow["job_id"] == jid


# ---- P10 Task 4: 分镜时长音频驱动（segments 对齐 text_span 覆盖估时）----

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


def test_split_audio_beats_target_average(tmp_path):
    """终审 I-2：音频实测时长是权威——项目设了预设总时长也不得均摊覆盖
    （旧代码均摊是全表 UPDATE，把逐镜音频覆盖整批抹平回均摊值）。"""
    import json as _json
    from tests.test_duration_control import _proj, _split_shot
    from tests.test_storyboard_split import FakeLLM
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine import asr as asr_mod
    db, pid = _proj(tmp_path, target_duration=10)  # 若均摊：2 镜 → 每镜 5s
    asr_mod.save_segments(tmp_path / "data", "时长剧",
                          [{"start": 0.0, "end": 7.0, "text": "推门"}])
    two = _json.dumps({"shots": [_split_shot("甲镜"), _split_shot("乙镜")]},
                      ensure_ascii=False)
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: FakeLLM([two]))
    assert [s["duration"] for s in list_shots(db, pid)] == [7.0, 7.0]  # 音频 7s 存活


def test_from_audio_broken_install_returns_422(tmp_path, monkeypatch):
    """终审顺手项：破损安装（模块在、缺 WhisperModel → 普通 ImportError，
    不是 ModuleNotFoundError）也走 422 安装指引，不 500。"""
    stub = types.ModuleType("faster_whisper")  # 故意不带 WhisperModel 属性
    monkeypatch.setitem(sys.modules, "faster_whisper", stub)
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    with TestClient(create_app(tmp_path / "t.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-audio",
                   data={"name": "破装剧", "aspect_ratio": "16:9"},
                   files={"audio": ("b.mp3", io.BytesIO(b"x"), "audio/mpeg")})
        assert r.status_code == 422
        assert "ASR 依赖未安装" in r.text


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


def test_dotenv_loader_sets_missing_and_respects_existing(tmp_path, monkeypatch):
    """P10 配套：.env 加载——缺失键注入、已存在键不覆盖（系统 env 优先）。"""
    import os
    from comic_studio.engine.asr import load_env_file
    envf = tmp_path / ".env"
    envf.write_text("HF_TOKEN=hf_test123\n"
                    "# 注释行忽略\n"
                    "HF_HUB_DISABLE_SYMLINKS_WARNING=1\n"
                    "\n"
                    "BAD_LINE_NO_EQ\n", encoding="utf-8")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS_WARNING", "0")  # 已存在→不覆盖
    n = load_env_file(envf)
    assert os.environ.get("HF_TOKEN") == "hf_test123"
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "0"
    assert n == 1  # 只注入了缺失的一个


def test_dotenv_loader_missing_file_noop(tmp_path):
    from comic_studio.engine.asr import load_env_file
    assert load_env_file(tmp_path / "不存在.env") == 0


def test_transcribe_progress_callback(tmp_path):
    """转写心跳（真机 2026-09-05 空打事故：asr 层从未接 progress——护栏硬化：
    transcribe 必须把 progress 转发给 backend 且透传回调）。"""
    seen = []
    def fake(path, model, progress=None):
        assert progress is not None, "transcribe 未转发 progress"
        for i in (25, 50):
            progress(i)
        return [(0.0, 1.0, "a"), (1.5, 2.5, "b")]
    segs = transcribe(tmp_path / "a.mp3", _backend=fake,
                      progress=lambda n: seen.append(n))
    assert len(segs) == 2
    assert seen == [25, 50]   # 回调逐次透传


def test_transcribe_job_emits_start_and_heartbeat(tmp_path, monkeypatch):
    """handler 级护栏（2026-09-05 空打事故：asr 层心跳全绿但 handler 没挂上）：
    开始日志必打；transcribe 收到 progress 回调且回调写日志。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.queue.worker import HANDLERS
    import comic_studio.engine.pipeline_jobs as PJ  # noqa: F401 注册
    from comic_studio.engine import asr as asr_mod
    from comic_studio.engine.jobs import enqueue_job
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "心剧", "16:9", "占位")["id"]
    adir = tmp_path / "data" / "projects" / "心剧" / "audio"
    adir.mkdir(parents=True)
    (adir / "source.mp3").write_bytes(b"f")
    calls = {}

    def fake_transcribe(path, model_size="large-v3", _backend=None, progress=None):
        calls["progress"] = progress
        if progress:
            progress(25)
        return [{"start": 0.0, "end": 1.0, "text": "甲"}]
    monkeypatch.setattr(asr_mod, "transcribe", fake_transcribe)
    jid = enqueue_job(db, "transcribe", project_id=pid,
                      payload={"project_id": pid, "ext": "mp3"})
    job = dict(db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    HANDLERS["transcribe"](db, tmp_path / "data", job, None)
    assert callable(calls.get("progress"))
    msgs = [r[0] for r in db.connect().execute(
        "SELECT message FROM logs WHERE source='asr'").fetchall()]
    assert any("转写任务开始" in m for m in msgs)
    assert any("转写进行中…已 25 段" in m for m in msgs)


def test_transcribe_requeued_on_restart(tmp_path):
    """重启重排白名单补 transcribe（2026-09-05 真机：卡死 job 被
    'interrupted by restart' 落 failed 且无人再触发）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.jobs import (REQUEUE_ON_RESTART_TYPES,
                                          enqueue_job, requeue_on_restart)
    assert "transcribe" in REQUEUE_ON_RESTART_TYPES
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project_stub = None
    from comic_studio.engine.projects import create_project
    pid = create_project(db, tmp_path / "d", "重排剧", "16:9", "t")["id"]
    jid = enqueue_job(db, "transcribe", project_id=pid, payload={})
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    conn.commit()
    n = requeue_on_restart(db, REQUEUE_ON_RESTART_TYPES)
    assert n >= 1
    assert db.connect().execute(
        "SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == "pending"


def test_retry_transcribe_endpoint(tmp_path):
    """手动重发入口（真机：failed transcribe 此前只能删项目重传）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.jobs import enqueue_job, finish_job
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "重发剧", "16:9", "占位")["id"]
    adir = tmp_path / "d" / "projects" / "重发剧" / "audio"
    adir.mkdir(parents=True)
    (adir / "source.mp3").write_bytes(b"f")
    jid = enqueue_job(db, "transcribe", project_id=pid,
                      payload={"project_id": pid, "ext": "mp3"})
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    conn.commit()
    finish_job(db, jid, "interrupted by restart")
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    with TestClient(create_app(db_path=tmp_path / "s.db", data_dir=tmp_path / "d",
                               start_workers=False)) as c:
        r = c.post(f"/api/projects/{pid}/retry-transcribe")
        assert r.status_code == 202, r.text
        row = db.connect().execute(
            "SELECT type, status FROM jobs WHERE project_id=? "
            "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        assert row["type"] == "transcribe" and row["status"] == "pending"
        # 源音频缺失 → 422
        pid2 = create_project(db, tmp_path / "d", "无源剧", "16:9", "t")["id"]
        assert c.post(f"/api/projects/{pid2}/retry-transcribe").status_code == 422


def test_gpu_dll_dirs_discovery_no_crash():
    from comic_studio.engine.asr import _add_nvidia_dll_dirs
    assert isinstance(_add_nvidia_dll_dirs(), int)


def test_gpu_dll_fallback_to_cpu(tmp_path, monkeypatch):
    """缺 cuBLAS/cuDNN 时回退 CPU int8（真机 2026-09-05 cublas64_12.dll），
    而非直接失败——装齐 nvidia-cublas-cu12/cudnn-cu12 后自动走 GPU。"""
    import sys, types
    calls = []
    stub = types.ModuleType("faster_whisper")

    class WM:
        def __init__(self, model_size, device="auto", compute_type="auto"):
            calls.append((device, compute_type))
            if device == "auto":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        def transcribe(self, *a, **k):
            return iter([]), {}

    stub.WhisperModel = WM
    monkeypatch.setitem(sys.modules, "faster_whisper", stub)
    # 预检失败路径：直接构造 CPU（构造器不再承担回退探测）
    monkeypatch.setattr("comic_studio.engine.asr._gpu_ready", lambda: False)
    segs = transcribe(tmp_path / "a.mp3")
    assert segs == []
    assert calls == [("cpu", "int8")]


def test_gpu_ready_returns_bool():
    from comic_studio.engine.asr import _gpu_ready
    assert isinstance(_gpu_ready(), bool)


def test_inference_stage_dll_fallback(tmp_path, monkeypatch):
    """推理期才炸的缺库（构造器惰性加载）也要能回退 CPU 重跑。"""
    import sys, types
    calls = []
    stub = types.ModuleType("faster_whisper")

    class WM:
        def __init__(self, model_size, device="auto", compute_type="auto"):
            calls.append((device, compute_type))
        def transcribe(self, *a, **k):
            if len(calls) == 1:   # 首个（auto）模型在推理期炸
                raise RuntimeError("Library cudnn64_9.dll is not found")
            return iter([types.SimpleNamespace(start=0.0, end=1.0, text="甲")]), {}

    stub.WhisperModel = WM
    monkeypatch.setitem(sys.modules, "faster_whisper", stub)
    monkeypatch.setattr("comic_studio.engine.asr._gpu_ready", lambda: True)
    segs = transcribe(tmp_path / "a.mp3")
    assert segs and segs[0]["text"] == "甲"
    assert ("cpu", "int8") in calls          # 推理期回退重建了 CPU 模型




def test_cleanup_transcription_by_llm(tmp_path):
    """P10C 校对遍（2026-09-05 用户决策：机械规则不如发 LLM）：按段清洗——
    修同音错字、纯语气词段返回空串丢弃；时间轴保留；正文/段落盘/章节重写。"""
    from comic_studio.engine.asr import (cleanup_transcription, load_segments,
                                         save_segments)
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project, get_project
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "校剧", "16:9", "（占位）")["id"]
    save_segments(tmp_path / "d", "校剧", [
        {"start": 0.0, "end": 1.0, "text": "嗯"},
        {"start": 1.2, "end": 3.0, "text": "儿子 你这是怎么了"},
        {"start": 3.5, "end": 5.0, "text": "陈薄了 你想妈帮你吗"},
    ])
    # 正文预填为原始转写（模拟转写完成态）
    from comic_studio.engine.paths import data_to_abs
    data_to_abs(tmp_path / "d", get_project(db, pid)["novel_path"]).write_text(
        "嗯\n\n儿子 你这是怎么了\n\n陈薄了 你想妈帮你吗", encoding="utf-8")

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.2):
            assert "陈薄了" in messages[1]["content"]   # 原文进上下文
            return ('{"1": "", "2": "儿子 你这是怎么了", '
                    '"3": "承不住了 你想妈帮你吗"}', {})
    res = cleanup_transcription(db, tmp_path / "d", pid, FakeLLM())
    assert res == {"removed": 1, "segments": 2, "mode": "conservative"}
    segs = load_segments(tmp_path / "d", "校剧")
    assert [s["text"] for s in segs] == ["儿子 你这是怎么了", "承不住了 你想妈帮你吗"]
    assert segs[0]["start"] == 1.2 and segs[0]["end"] == 3.0   # 时间轴保留
    novel = data_to_abs(tmp_path / "d", get_project(db, pid)["novel_path"]).read_text(
        encoding="utf-8")
    assert "承不住了" in novel and "陈薄了" not in novel and "嗯" not in novel


def test_asr_cleanup_endpoint(tmp_path, monkeypatch):
    """POST /{id}/asr-cleanup：路由走 asr_cleanup 任务键（默认 local），
    同步返回清洗统计；无转写 409；在飞 409。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project, get_project
    from comic_studio.engine.asr import save_segments
    from comic_studio.engine.paths import data_to_abs

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.2):
            return '{"1": ""}', {}   # 桩：语气词段返回空串=丢弃
    monkeypatch.setattr("comic_studio.engine.llm.provider.client_for_task",
                        lambda db, task: FakeLLM())
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "清剧", "16:9", "占位")["id"]
    save_segments(tmp_path / "d", "清剧", [{"start": 0.0, "end": 1.0, "text": "嗯"}])
    data_to_abs(tmp_path / "d", get_project(db, pid)["novel_path"]).write_text(
        "嗯", encoding="utf-8")
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    with TestClient(create_app(db_path=tmp_path / "s.db", data_dir=tmp_path / "d",
                               start_workers=False)) as c:
        r = c.post(f"/api/projects/{pid}/asr-cleanup")
        assert r.status_code == 200, r.text
        assert r.json() == {"removed": 1, "segments": 0, "mode": "conservative"}
        # 无转写项目 → 409
        pid2 = create_project(db, tmp_path / "d", "无段剧", "16:9", "t")["id"]
        assert c.post(f"/api/projects/{pid2}/asr-cleanup").status_code == 409


def test_cleanup_with_theme_anchor(tmp_path):
    """2026-09-05 用户需求：校对加内容主题锚——主题进提示词（纠错围绕核心），
    持久化 audio/theme.txt（下次不带参自动复用）。"""
    from comic_studio.engine.asr import cleanup_transcription, load_segments, save_segments
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "题剧", "16:9", "占位")["id"]
    save_segments(tmp_path / "d", "题剧", [{"start": 0.0, "end": 2.0, "text": "陈薄了"}])
    captured = {}

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.2):
            captured["prompt"] = messages[1]["content"] + messages[0]["content"]
            return '{"1": "承不住了"}', {}
    cleanup_transcription(db, tmp_path / "d", pid, FakeLLM(),
                          theme="母子清晨日常，妈妈准备上班")
    assert "母子清晨日常" in captured["prompt"]          # 主题进词
    assert (tmp_path / "d" / "projects" / "题剧" / "audio" / "theme.txt"
            ).read_text(encoding="utf-8") == "母子清晨日常，妈妈准备上班"
    # 第二次不带 theme → 自动复用持久化主题
    captured.clear()
    save_segments(tmp_path / "d", "题剧", [{"start": 0.0, "end": 2.0, "text": "陈薄了"}])
    cleanup_transcription(db, tmp_path / "d", pid, FakeLLM())
    assert "母子清晨日常" in captured["prompt"]


def test_cleanup_enrich_mode_preserves_sentences_and_segments(tmp_path):
    """丰富模式（2026-09-05 用户决策 2）：围绕主题扩写旁白/衔接，原句逐字
    保留（时长锚不破坏）；segments.json 不动（音频段=时长/原声锚点），
    只重写 novel.txt。"""
    from comic_studio.engine.asr import (cleanup_transcription, load_segments,
                                         save_segments)
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project, get_project
    from comic_studio.engine.paths import data_to_abs
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "丰剧", "16:9", "占位")["id"]
    save_segments(tmp_path / "d", "丰剧", [
        {"start": 0.0, "end": 3.0, "text": "儿子 你这是怎么了"},
        {"start": 3.5, "end": 5.0, "text": "嗯"}])
    data_to_abs(tmp_path / "d", get_project(db, pid)["novel_path"]).write_text(
        "儿子 你这是怎么了\n\n嗯", encoding="utf-8")
    captured = {}

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.2):
            captured["system"] = messages[0]["content"]
            return ('{"1": "清晨的阳光斜照进卧室，母亲一边整理着装一边回头——'
                    '儿子 你这是怎么了——她伸手探了探儿子的额头，语气里满是关切。", '
                    '"2": ""}', {})
    res = cleanup_transcription(db, tmp_path / "d", pid, FakeLLM(),
                                theme="母子清晨日常", mode="enrich")
    assert "扩写" in captured["system"] and "逐字保留" in captured["system"]
    assert "母子清晨日常" in captured["system"]
    novel = data_to_abs(tmp_path / "d", get_project(db, pid)["novel_path"]).read_text(
        encoding="utf-8")
    assert "儿子 你这是怎么了" in novel or "儿子，你这是怎么了" in novel  # 原句在
    assert "探了探儿子的额头" in novel          # 扩写旁白在
    # segments.json 原样（含语气词段——时长/原声锚不破坏）
    segs = load_segments(tmp_path / "d", "丰剧")
    assert len(segs) == 2 and segs[0]["text"] == "儿子 你这是怎么了"
    assert res["mode"] == "enrich"
    # 保守模式照旧重写 segments
    save_segments(tmp_path / "d", "丰剧", [{"start": 0.0, "end": 3.0, "text": "嗯"}])
    class F2:
        model = "f"
        def raw_chat(self, m, temperature=0.2):
            return '{"1": ""}', {}
    cleanup_transcription(db, tmp_path / "d", pid, F2())
    assert load_segments(tmp_path / "d", "丰剧") == []


def test_split_audio_project_gets_dialogue_rules(tmp_path):
    """有声书分镜对白规则（2026-09-05 真机：转写无引号 → LLM 不识台词 →
    dialogue 空 → 提示词无对白）：音频项目（segments 在）拆解系统词注入
    「全篇皆对白」规则+说话人推断指引+主题（若有）。"""
    from comic_studio.engine.llm.provider import Usage
    from comic_studio.engine.asr import save_segments
    from tests.test_duration_control import CHUNK, _proj
    from tests.test_storyboard_split import FakeLLM
    from comic_studio.engine.llm.storyboard import split_storyboards
    captured = {}

    class CaptureLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured["system"] = messages[0]["content"]
            return CHUNK.format(desc="甲", dur=5), Usage(1, 2)
    db, pid = _proj(tmp_path)
    save_segments(tmp_path / "data", "时长剧", [{"start": 0.0, "end": 4.0, "text": "儿子 你这是怎么了"}])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: CaptureLLM())
    assert "全篇皆对白" in captured["system"]
    assert "说话人" in captured["system"]
    # 非音频项目不注入
    captured.clear()
    db3, pid3 = _proj(tmp_path / "x")
    split_storyboards(db3, tmp_path / "x" / "data", pid3, client_factory=lambda t: CaptureLLM())
    assert "全篇皆对白" not in captured.get("system", "")


# ===== P10-D：ComfyUI Qwen3-ASR 后端（2026-09-05 用户接入）=====

def test_parse_qwen_output_formats():
    """时间戳格式未实测——解析器多格式容忍 + 整块兜底（保时长可用）。"""
    from comic_studio.engine.asr import parse_qwen_output
    # 格式A：JSON segments
    a = parse_qwen_output('{"segments":[{"start":0.5,"end":2.0,"text":"甲"},{"start":2.1,"end":4.0,"text":"乙"}]}', 10.0)
    assert a == [{"start": 10.5, "end": 12.0, "text": "甲"}, {"start": 12.1, "end": 14.0, "text": "乙"}]
    # 格式B：行式 [00:00.500 -> 00:02.000] 甲
    b = parse_qwen_output("[00:00.500 -> 00:02.000] 甲\n[00:02.100 -> 00:04.000] 乙", 0.0)
    assert b[0]["text"] == "甲" and abs(b[0]["start"] - 0.5) < 0.01 and abs(b[1]["end"] - 4.0) < 0.01
    # 格式C：SRT 块
    c = parse_qwen_output("1\n00:00:00,500 --> 00:00:02,000\n甲\n\n2\n00:00:02,100 --> 00:00:04,000\n乙", 0.0)
    assert len(c) == 2 and c[1]["text"] == "乙"
    # 兜底：纯文本 → 整块单段
    d = parse_qwen_output("只是一段纯文本没有时间戳", 5.0, chunk_dur=8.0)
    assert d == [{"start": 5.0, "end": 13.0, "text": "只是一段纯文本没有时间戳"}]


def test_completeness_heuristic():
    """完整度校验（用户 flagged 输出不全长）：字符密度过低 → warn 提示。"""
    from comic_studio.engine.asr import text_density_ok
    assert text_density_ok("字" * 600, 300.0) is True    # 2 字/s 正常
    assert text_density_ok("字" * 30, 300.0) is False    # 0.1 字/s 异常
    assert text_density_ok("", 300.0) is False
    assert text_density_ok("字" * 600, 0.0) is True      # 无时长不判


def test_transcribe_engine_setting_branch(tmp_path, monkeypatch):
    """handler 按设置选引擎：comfy_qwen3 → transcribe_comfy（不碰 faster_whisper）。
    """
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.queue.worker import HANDLERS
    from comic_studio.engine.jobs import enqueue_job
    from comic_studio.engine.settings import set_setting
    import comic_studio.engine.pipeline_jobs as PJ
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "擎剧", "16:9", "占位")["id"]
    adir = tmp_path / "d" / "projects" / "擎剧" / "audio"
    adir.mkdir(parents=True)
    (adir / "source.mp3").write_bytes(b"f")
    set_setting(db, "asr", {"engine": "comfy_qwen3"})
    called = {}

    def fake_comfy(db, audio_path, comfy, progress=None, theme=""):
        called["engine"] = "comfy"
        return [{"start": 0.0, "end": 2.0, "text": "甲"}]
    monkeypatch.setattr("comic_studio.engine.asr.transcribe_comfy", fake_comfy)

    def bomb(path, model_size="large-v3", _backend=None, progress=None):
        raise AssertionError("comfy 引擎不得走 faster_whisper")
    monkeypatch.setattr("comic_studio.engine.asr.transcribe", bomb)
    jid = enqueue_job(db, "transcribe", project_id=pid,
                      payload={"project_id": pid, "ext": "mp3"})
    job = dict(db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    HANDLERS["transcribe"](db, tmp_path / "d", job, None)
    assert called["engine"] == "comfy"
    from comic_studio.engine.asr import load_segments
    assert load_segments(tmp_path / "d", "擎剧")[0]["text"] == "甲"
