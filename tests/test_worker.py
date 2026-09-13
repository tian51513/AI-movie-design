# tests/test_worker.py
import threading
import time

from comic_studio.engine.db import Database
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.comfy.client import ComfyClient, ComfyUnreachable
from comic_studio.engine.queue.worker import Worker, register


def test_worker_executes_handler_and_finishes(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    done = []
    frees = []

    @register("test_job")
    def handle(db, data_dir, job, comfy):
        done.append(job["payload_json"])
        if comfy is not None:
            frees.append(comfy)   # comfy=None 时不触发

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("test_job",), comfy_factory=None)
    w.start()
    jid = enqueue_job(db, "test_job", project_id=pid, payload={"x": 1})
    for _ in range(100):
        if get_job(db, jid)["status"] == "done":
            break
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    assert get_job(db, jid)["status"] == "done"
    assert done == ['{"x": 1}']


def test_worker_retries_then_fails(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    calls = []

    @register("boom_job")
    def handle(db, data_dir, job, comfy):
        calls.append(1)
        raise RuntimeError("always bad")

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("boom_job",), comfy_factory=None)
    w.start()
    jid = enqueue_job(db, "boom_job", project_id=pid)
    for _ in range(300):
        if get_job(db, jid)["status"] == "failed":
            break
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    assert get_job(db, jid)["status"] == "failed"
    assert len(calls) == 3 and "always bad" in get_job(db, jid)["error"]


def test_comfy_from_settings_passes_comfyclient(tmp_path):
    """comfy_from_settings=True 时 handler 收到 ComfyClient 实例（从 settings 读取）。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    received = []

    @register("cf_test")
    def handle(db, data_dir, job, comfy):
        received.append(comfy)

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("cf_test",), comfy_from_settings=True)
    w.start()
    jid = enqueue_job(db, "cf_test", project_id=pid)
    for _ in range(100):
        if get_job(db, jid)["status"] == "done":
            break
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    assert get_job(db, jid)["status"] == "done"
    assert len(received) == 1
    assert isinstance(received[0], ComfyClient)


def test_comfy_unreachable_waits_without_consuming(tmp_path):
    """ComfyUnreachable → 任务保持 pending 等待 ComfyUI 恢复，不消耗尝试次数（spec §7）。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    calls = []

    @register("unreach_job")
    def handle(db, data_dir, job, comfy):
        calls.append(1)
        raise ComfyUnreachable("down")

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("unreach_job",), comfy_factory=None, backoff_base=0)
    w.start()
    jid = enqueue_job(db, "unreach_job", project_id=pid)
    for _ in range(100):  # 跑足够多轮（远超 3 次尝试预算）
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    job = get_job(db, jid)
    assert job["status"] == "pending"      # 一直等待，不失败
    assert job["attempts"] <= 1           # claim +1 被回退，尝试预算未消耗（0 或 1 取决于停止时机）
    assert len(calls) > 3                  # 反复重试
    assert "ComfyUnreachable" in job["error"]


def test_worker_comfy_job_without_config_fails_clearly(tmp_path):
    """事故复盘（2026-09-01）：base_url 为空 → worker 拿 None client，
    曾以 AttributeError 裸崩且 handler 已执行一半——必须进 handler 前给可读报错。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    called = []

    @register("cfg_job")
    def handle(db, data_dir, job, comfy):
        called.append(1)

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("cfg_job",), comfy_factory=lambda: None)
    w.start()
    jid = enqueue_job(db, "cfg_job", project_id=pid, resource="gpu_comfy")
    for _ in range(300):
        if get_job(db, jid)["status"] == "failed":
            break
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    job = get_job(db, jid)
    assert job["status"] == "failed"
    assert "ComfyUI 未配置" in job["error"]
    assert called == []                    # 没带 None client 进 handler


def test_llm_job_frees_comfy_vram_first(tmp_path):
    """让位双向化（2026-09-13 真机判例：拆分切 27B 重度秒 504——ComfyUI 渲染
    模型跑完驻留显存，重 LLM 装不下；旧让位只有单向 LLM→Comfy）。gpu_llm_local
    任务启动前 comfy.free() 释放驻留模型。队列 gpu 组互斥保证 free 时无渲染在跑。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    freed = []
    # 起手=半卡被渲染模型占（有驻留场景）
    stats = {"vram_total": 12 * 2**30, "vram_free": 6 * 2**30}

    class FakeComfy:
        def health(self):
            return {"devices": [dict(stats)]}
        def free(self, unload_models=True):
            freed.append(unload_models)

    @register("llm_test_job")
    def handle(db, data_dir, job, comfy):
        pass  # free 断言在 handler 外

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("llm_test_job",), comfy_factory=lambda: FakeComfy())
    w.start()
    jid = enqueue_job(db, "llm_test_job", project_id=pid, resource="gpu_llm_local")
    for _ in range(100):
        if get_job(db, jid)["status"] == "done":
            break
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    assert get_job(db, jid)["status"] == "done"
    assert freed, "ComfyUI 有驻留时未先清显存"

    # ComfyUI 空闲（无驻留）→ 不调用清理（用户要求检查式，非无脑 free）
    freed.clear()
    stats["vram_free"] = 12 * 2**30   # 恢复空闲

    @register("llm_idle_test")
    def handle3(db, data_dir, job, comfy):
        pass
    stop3 = threading.Event()
    w3 = Worker(db.path, tmp_path / "data", None, stop3, poll_interval=0.05,
                handler_types=("llm_idle_test",), comfy_factory=lambda: FakeComfy())
    w3.start()
    jid3 = enqueue_job(db, "llm_idle_test", project_id=pid, resource="gpu_llm_local")
    for _ in range(100):
        if get_job(db, jid3)["status"] == "done":
            break
        time.sleep(0.05)
    stop3.set(); w3.join(timeout=2)
    assert not freed, "ComfyUI 空闲时不应调用清理"

    # 非 gpu_llm_local 任务不触发（普通任务不打扰 ComfyUI 缓存）
    freed.clear()

    @register("plain_test_job")
    def handle2(db, data_dir, job, comfy):
        pass
    stop2 = threading.Event()
    w2 = Worker(db.path, tmp_path / "data", None, stop2, poll_interval=0.05,
                handler_types=("plain_test_job",), comfy_factory=lambda: FakeComfy())
    w2.start()
    jid2 = enqueue_job(db, "plain_test_job", project_id=pid)
    for _ in range(100):
        if get_job(db, jid2)["status"] == "done":
            break
        time.sleep(0.05)
    stop2.set(); w2.join(timeout=2)
    assert not freed, "普通任务不应清 ComfyUI 显存"
