# tests/test_pipeline_jobs.py
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.pipeline_jobs import enqueue_llm_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_enqueue_resource_follows_routing(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    j1 = enqueue_llm_job(db, "split_storyboards", project_id=pid, payload={"project_id": pid})
    assert get_job(db, j1)["resource"] is None  # 默认路由 online
    set_setting(db, "llm_routing", {"split_storyboards": "local"})
    j2 = enqueue_llm_job(db, "split_storyboards", project_id=pid, payload={"project_id": pid})
    assert get_job(db, j2)["resource"] == "gpu_llm_local"


def test_enqueue_shot_id_persisted(tmp_path):
    from comic_studio.engine.shots import persist_shots
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    draft = NS(text_span="原文", description="镜头描述", shot_type="对话",
              camera={}, duration=5.0, workflow_type="ref2va",
              ledger={}, character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    sid = persist_shots(db, pid, [draft])[0]
    jid = enqueue_llm_job(db, "gen_prompt", project_id=pid, shot_id=sid,
                           payload={"shot_id": sid})
    assert get_job(db, jid)["shot_id"] == sid


def test_enqueue_gen_prompt_uses_routed_key(tmp_path):
    """I1: gen_prompt 路由键应映射到 gen_video_prompt 设置键。"""
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    # 设置 gen_video_prompt 路由为 local
    set_setting(db, "llm_routing", {"gen_video_prompt": "local"})
    jid = enqueue_llm_job(db, "gen_prompt", project_id=pid)
    assert get_job(db, jid)["resource"] == "gpu_llm_local"
    # 默认（online/无路由）→ resource=None
    set_setting(db, "llm_routing", {})
    jid2 = enqueue_llm_job(db, "gen_prompt", project_id=pid)
    assert get_job(db, jid2)["resource"] is None


def test_handlers_registered():
    from comic_studio.engine.queue.worker import HANDLERS
    import comic_studio.engine.pipeline_jobs  # noqa: F401 注册触发
    assert "split_storyboards" in HANDLERS and "gen_prompt" in HANDLERS
