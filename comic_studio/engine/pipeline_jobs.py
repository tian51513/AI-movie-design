# comic_studio/engine/pipeline_jobs.py
"""LLM 流水线任务 handler：分镜拆解与视频提示词生成（经 worker 队列，spec §8.1 资源路由）。"""
import json

from .jobs import enqueue_job
from .logbus import emit as emit_log
from .queue.worker import register
from .settings import get_setting


_ROUTE_KEY = {"gen_prompt": "gen_video_prompt"}  # job 类型 → llm_routing 键


def enqueue_llm_job(db, jtype, project_id, shot_id=None, payload=None):
    routing = get_setting(db, "llm_routing").get(_ROUTE_KEY.get(jtype, jtype))
    resource = "gpu_llm_local" if routing == "local" else None
    return enqueue_job(db, jtype, project_id=project_id, shot_id=shot_id,
                       resource=resource, payload=payload)


@register("split_storyboards")
def handle_split(db, data_dir, job, comfy):
    from .llm.storyboard import split_storyboards
    payload = json.loads(job["payload_json"] or "{}")
    ids = split_storyboards(db, data_dir, payload.get("project_id", job["project_id"]))
    emit_log(db, "storyboard", "info", f"分镜拆解完成：{len(ids)} 镜",
             project_id=job["project_id"], job_id=job["id"])


@register("gen_prompt")
def handle_gen_prompt(db, data_dir, job, comfy):
    import time
    from .llm.provider import client_for_task
    from .prompts.gen import generate_video_prompt
    from .shots import get_shot, update_shot
    payload = json.loads(job["payload_json"] or "{}")
    shot = get_shot(db, payload["shot_id"])
    if shot is None:
        raise ValueError("分镜已删除（重拆后旧任务）")
    backend = "ltx" if "ltx" in (shot["workflow_type"] or "") else "h3"
    from .projects import get_project
    proj = get_project(db, shot["project_id"])
    mode = (proj["prompt_mode"] if proj is not None and "prompt_mode" in proj.keys()
            else None)
    client = client_for_task(db, "gen_video_prompt")
    t0 = time.monotonic()
    text = generate_video_prompt(db, payload["shot_id"], client, backend=backend,
                                 mode=mode)
    update_shot(db, payload["shot_id"], {"prompt": text, "status": "ready"})
    emit_log(db, "llm", "info",
             f"镜头 {shot['seq']} 提示词就绪（{backend}，{len(text)} 字，"
             f"{time.monotonic()-t0:.1f}s）", project_id=job["project_id"], job_id=job["id"])
