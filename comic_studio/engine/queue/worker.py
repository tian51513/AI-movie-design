# comic_studio/engine/queue/worker.py
"""worker 线程：认领-执行-完结循环；资源互斥与重试由队列原语保证（spec §8）。"""
import json
import threading
import time
from pathlib import Path

from ..comfy.client import ComfyUnreachable
from ..db import Database
from ..jobs import claim_next_job, finish_job, retry_or_fail
from ..logbus import emit as emit_log

HANDLERS: dict = {}


def register(jtype: str):
    def deco(fn):
        HANDLERS[jtype] = fn
        return fn
    return deco


class Worker(threading.Thread):
    def __init__(self, db_path, data_dir, comfy_base_url, stop_event,
                 poll_interval=0.5, handler_types=None, comfy_factory=None,
                 comfy_from_settings: bool = False, backoff_base: float = 2.0):
        super().__init__(daemon=True, name="cs-worker")
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        self.comfy_base_url = comfy_base_url
        self.stop_event = stop_event
        self.poll_interval = poll_interval
        self.handler_types = tuple(handler_types) if handler_types else tuple(HANDLERS)
        self.comfy_factory = comfy_factory
        self.comfy_from_settings = comfy_from_settings
        self.backoff_base = backoff_base
        self.last_template = None
        self._db = None

    def _comfy(self):
        if self.comfy_factory:
            return self.comfy_factory()
        if self.comfy_from_settings and self._db is not None:
            from ..comfy.client import ComfyClient
            from ..settings import get_setting
            base_url = get_setting(self._db, "comfy")["base_url"]
            if base_url:
                return ComfyClient(base_url)
            return None
        if self.comfy_base_url:
            from ..comfy.client import ComfyClient
            return ComfyClient(self.comfy_base_url)
        return None

    def run(self):
        db = Database(self.db_path)
        db.migrate()
        self._db = db
        while not self.stop_event.is_set():
            try:
                job = claim_next_job(db, self.handler_types)
            except Exception:
                # 锁竞争等瞬时异常不得杀死 worker 线程（真机 2026-08-25：
                # 删除项目持锁时 BEGIN IMMEDIATE 超时，线程阵亡队列停摆）
                time.sleep(1)
                continue
            if job is None:
                time.sleep(self.poll_interval)
                continue
            payload = json.loads(job["payload_json"] or "{}")
            template_id = payload.get("template")
            comfy = None
            try:
                comfy = self._comfy()
                if comfy is not None and self.last_template and template_id != self.last_template:
                    comfy.free()  # 模型切换释放（spec §8.3）
                HANDLERS[job["type"]](db, self.data_dir, job, comfy)
                finish_job(db, job["id"], None)
            except ComfyUnreachable as e:
                emit_log(db, "comfy", "error",
                         f"job {job['id']}（{job['type']}）ComfyUI 不可达：{e}",
                         project_id=job["project_id"], job_id=job["id"])
                retry_or_fail(db, job["id"], f"ComfyUnreachable: {e}", consume_attempt=False)
                time.sleep(min(10, self.backoff_base ** min(job["attempts"], 3)))
            except Exception as e:
                emit_log(db, "comfy", "error",
                         f"job {job['id']}（{job['type']}）失败：{type(e).__name__}: {e}",
                         project_id=job["project_id"], job_id=job["id"])
                retry_or_fail(db, job["id"], f"{type(e).__name__}: {e}")
            finally:
                if template_id:
                    self.last_template = template_id


def start_workers(db_path, data_dir, comfy_base_url, n, **worker_kwargs):
    stop = threading.Event()
    workers = [Worker(db_path, data_dir, comfy_base_url, stop, **worker_kwargs) for _ in range(max(1, n))]
    for w in workers:
        w.start()
    return workers, stop


def stop_workers(workers, stop):
    stop.set()
    for w in workers:
        w.join(timeout=3)
