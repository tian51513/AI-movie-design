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
