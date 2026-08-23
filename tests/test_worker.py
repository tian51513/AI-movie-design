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
