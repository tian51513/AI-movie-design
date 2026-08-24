# tests/test_queue_primitives.py
from comic_studio.engine.db import Database
from comic_studio.engine.jobs import (claim_next_job, enqueue_job, finish_job,
                                      get_job, retry_or_fail, requeue_on_restart)
from comic_studio.engine.projects import create_project


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def _pid(db, tmp_path):
    return create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]


def test_enqueue_claim_retry_cycle(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    jid = enqueue_job(db, "gen_ref", project_id=pid, resource="gpu_comfy", payload={"asset_id": 1})
    assert get_job(db, jid)["status"] == "pending"
    job = claim_next_job(db, ("gen_ref",))
    assert job["id"] == jid and job["status"] == "running" and job["attempts"] == 1
    assert claim_next_job(db, ("gen_ref",)) is None  # 无 pending
    assert retry_or_fail(db, jid, "boom") == "pending"
    job2 = claim_next_job(db, ("gen_ref",))
    assert job2["attempts"] == 2
    assert retry_or_fail(db, jid, "boom") == "pending"   # attempts=2 <3
    job3 = claim_next_job(db, ("gen_ref",))
    assert job3["attempts"] == 3
    assert retry_or_fail(db, jid, "boom") == "failed"    # 第3次失败


def test_resource_mutex(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    a = enqueue_job(db, "gen_ref", project_id=pid, resource="gpu_comfy")
    b = enqueue_job(db, "gen_ref", project_id=pid, resource="gpu_comfy")
    c = enqueue_job(db, "other", project_id=pid, resource=None)
    assert claim_next_job(db, ("gen_ref", "other"))["id"] == a
    # a 在跑：同资源 b 不能认领，但无资源 c 可以
    assert claim_next_job(db, ("gen_ref", "other"))["id"] == c
    finish_job(db, a, None)
    assert claim_next_job(db, ("gen_ref", "other"))["id"] == b


def test_resource_mutex_cross_group(tmp_path):
    """I5: gpu_comfy 与 gpu_llm_local 共享 GPU 组，互斥；None 资源不受影响。"""
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    a = enqueue_job(db, "gen_ref", project_id=pid, resource="gpu_comfy")
    b = enqueue_job(db, "gen_prompt", project_id=pid, resource="gpu_llm_local")
    c = enqueue_job(db, "other", project_id=pid, resource=None)
    # claim a (gpu_comfy)
    assert claim_next_job(db, ("gen_ref", "gen_prompt", "other"))["id"] == a
    # b (gpu_llm_local) 与 a (gpu_comfy) 同组 GPU → 不可认领
    # c (None) 不受互斥影响，先返回 c
    claimed = claim_next_job(db, ("gen_prompt", "other"))
    assert claimed["id"] == c
    # b 仍不可认领
    assert claim_next_job(db, ("gen_prompt",)) is None
    finish_job(db, a, None)
    # a 完成后 b 可认领
    assert claim_next_job(db, ("gen_prompt",))["id"] == b


def test_claim_ignores_unhandled_types(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    enqueue_job(db, "analyze", project_id=pid)
    assert claim_next_job(db, ("gen_ref",)) is None


def test_requeue_on_restart(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    from comic_studio.engine.jobs import create_job
    j = create_job(db, project_id=pid, jtype="gen_ref")          # running, attempts=0
    a = create_job(db, project_id=pid, jtype="analyze")           # running
    n = requeue_on_restart(db, ("gen_ref",))
    assert n == 1
    assert get_job(db, j)["status"] == "pending"
    assert get_job(db, a)["status"] == "failed"  # 非重排队列 → 失败（原语义）
