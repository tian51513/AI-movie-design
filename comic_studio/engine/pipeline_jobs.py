# comic_studio/engine/pipeline_jobs.py
"""LLM 流水线任务 handler：分镜拆解与视频提示词生成（经 worker 队列，spec §8.1 资源路由）。"""
import json

from .jobs import enqueue_job
from .logbus import emit as emit_log
from .queue.worker import register
from .settings import get_setting


_ROUTE_KEY = {"gen_prompt": "gen_video_prompt", "analyze": "extract_assets",
              "describe_shots": "describe_shot"}  # job 类型 → llm_routing 键


def enqueue_llm_job(db, jtype, project_id, shot_id=None, payload=None):
    routing = get_setting(db, "llm_routing").get(_ROUTE_KEY.get(jtype, jtype))
    resource = "gpu_llm_local" if routing == "local" else None
    return enqueue_job(db, jtype, project_id=project_id, shot_id=shot_id,
                       resource=resource, payload=payload)


@register("analyze")
def handle_analyze(db, data_dir, job, comfy):
    """资产分析（created→analyzed）：autopilot 经队列跑，手动路径仍是 BackgroundTask。"""
    from .llm.analyze import analyze_project
    payload = json.loads(job["payload_json"] or "{}")
    analyze_project(db, data_dir, payload.get("project_id", job["project_id"]))
    emit_log(db, "analyze", "info", "资产分析完成",
             project_id=job["project_id"], job_id=job["id"])


@register("split_storyboards")
def handle_split(db, data_dir, job, comfy):
    from .llm.storyboard import split_storyboards
    payload = json.loads(job["payload_json"] or "{}")
    ids = split_storyboards(db, data_dir, payload.get("project_id", job["project_id"]),
                            target_count=payload.get("target_count"),
                            chapter_range=payload.get("chapter_range"))
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
    if (shot["workflow_type"] or "") == "comic":
        # 漫画镜短路（2026-09-13）：H3 视频提示词链不适用——prompt=description
        # 机械填充（拆解时已填；此处兜住 stale 联动/手动重生路径），不烧 LLM
        update_shot(db, shot["id"], {
            "prompt": (shot["description"] or shot["text_span"] or "").strip(),
            "status": "ready"})
        emit_log(db, "llm", "info",
                 f"漫画页 {shot['seq']} 提示词机械填充（不烧 LLM）",
                 project_id=job["project_id"], job_id=job["id"])
        return
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


@register("describe_shots")
def handle_describe_shots(db, data_dir, job, comfy):
    """P8 VLM 读图（队列任务化 2026-08-29）：单镜（payload.shot_id）或批量。"""
    import json as _json
    payload = _json.loads(job["payload_json"] or "{}")
    from .comic import describe_shots
    from .llm.provider import client_for_task
    try:
        client = client_for_task(db, "describe_shot")
    except Exception:
        client = client_for_task(db, "gen_video_prompt")
    n = describe_shots(db, data_dir, payload.get("project_id", job["project_id"]),
                       client, shot_id=payload.get("shot_id"),
                       force=bool(payload.get("force")))
    emit_log(db, "llm", "info", f"VLM 读图任务完成：{n} 镜",
             project_id=job["project_id"], job_id=job["id"])


@register("extract_comic_characters")
def handle_extract_comic_characters(db, data_dir, job, comfy):
    """P8-B 漫改模式角色提取（VLM 读前几页 → 建资产）。"""
    import json as _json
    payload = _json.loads(job["payload_json"] or "{}")
    from .comic import extract_comic_characters
    from .llm.provider import client_for_task
    try:
        client = client_for_task(db, "describe_shot")
    except Exception:
        client = client_for_task(db, "gen_video_prompt")
    n = extract_comic_characters(db, data_dir,
                                 payload.get("project_id", job["project_id"]), client,
                                 characters_only=bool(payload.get("characters_only")),
                                 bind_shots=bool(payload.get("bind_shots")))
    emit_log(db, "llm", "info", f"角色提取任务完成：{n} 个角色",
             project_id=job["project_id"], job_id=job["id"])


@register("asr_cleanup")
def handle_asr_cleanup(db, data_dir, job, comfy):
    """P10 转写校对（2026-09-06 队列化）：占 gpu_llm_local 槽与渲染互斥
    （同步 HTTP 时代绕过资源组，与 ComfyUI 抢显存）。payload 带 theme/mode。"""
    from . import asr as asr_mod
    from .llm.provider import client_for_task
    from .paths import data_to_abs
    from .projects import get_project
    payload = json.loads(job["payload_json"] or "{}")
    pid = payload.get("project_id", job["project_id"])
    proj = get_project(db, pid)
    if proj is None:
        raise ValueError(f"项目不存在: {pid}")
    theme = str(payload.get("theme") or "")
    if not theme.strip():
        tf = data_to_abs(data_dir, f"{asr_mod.audio_rel(proj['slug'])}/theme.txt")
        if tf.exists():
            theme = tf.read_text(encoding="utf-8").strip()
    emit_log(db, "asr", "info", "转写校对任务开始（队列模式，与渲染自动错峰）",
             project_id=pid, job_id=job["id"])
    res = asr_mod.cleanup_transcription(
        db, data_dir, pid, client_for_task(db, "asr_cleanup"),
        theme=theme, mode=str(payload.get("mode") or "conservative"))
    # 结果摘要落 snapshot_json（2026-09-06 真机：202 化后前端读不到旧同步字段，
    # 弹「保留 undefined 段」）——GET /api/jobs/{id}/snapshot 轮询取真数
    conn = db.connect()
    conn.execute(
        "UPDATE jobs SET snapshot_json=? WHERE id=?",
        (json.dumps({"result": {k: res.get(k) for k in ("mode", "segments", "removed")}},
                    ensure_ascii=False), job["id"]))
    conn.commit()
    emit_log(db, "asr", "info",
             f"转写校对完成：{res['segments']} 段 / 丢弃 {res['removed']}"
             f"（{res['mode']}）", project_id=pid, job_id=job["id"])
    # 2026-09-06 有声2 真机：校正写回正文后分镜没重拆，后半段拆的是旧文本
    # （引号形态变化 → 对白提取全空）——已有分镜必须明示重拆，不再静默脱节
    n_shots = db.connect().execute(
        "SELECT COUNT(*) FROM shots WHERE project_id=?", (pid,)).fetchone()[0]
    if n_shots:
        emit_log(db, "asr", "warn",
                 f"正文已校正写回，但已有 {n_shots} 镜分镜基于旧文本——"
                 "对白/时长可能与新正文失配，请重拆分镜后重生成提示词",
                 project_id=pid)


@register("transcribe")
def handle_transcribe(db, data_dir, job, comfy):
    """P10 有声书转写（2026-09-05 计划）：源音频 → 段落盘 + novel.txt 回填
    + 章节重算。失败由 retry_or_fail 兜底（autopilot/手动重发解除同 analyze）。"""
    from . import asr as asr_mod
    from .chapters import parse_chapters
    from .paths import data_to_abs
    from .projects import get_project
    payload = json.loads(job["payload_json"] or "{}")
    pid = payload.get("project_id", job["project_id"])
    proj = get_project(db, pid)
    if proj is None:
        raise ValueError(f"项目不存在: {pid}")
    src = data_to_abs(data_dir, f"{asr_mod.audio_rel(proj['slug'])}"
                                f"/source.{payload.get('ext', 'mp3')}")
    # 开始日志（2026-09-05 真机：模型下载/加载期完全黑盒，「连开始日志都没有」）
    emit_log(db, "asr", "info",
             "转写任务开始（首次需下载/加载 large-v3 模型——下载期数分钟无进度提示）",
             project_id=pid, job_id=job["id"])
    if not src.exists():
        raise ValueError(f"源音频缺失: {src}")

    def _heartbeat(n):
        emit_log(db, "asr", "info", f"转写进行中…已 {n} 段",
                 project_id=pid, job_id=job["id"])

    from ..engine.settings import get_setting as _gs
    _engine = (( _gs(db, "asr") or {}).get("engine")) or "faster_whisper"
    if _engine == "comfy_qwen3":
        from ..engine.settings import ensure_comfy_configured
        ensure_comfy_configured(db)
        from ..engine.comfy.client import ComfyClient
        from ..engine.settings import get_setting
        _base = (get_setting(db, "comfy") or {}).get("base_url")
        _theme = ""
        _tf = data_to_abs(data_dir, f"{asr_mod.audio_rel(proj['slug'])}/theme.txt")
        if _tf.exists():
            _theme = _tf.read_text(encoding="utf-8").strip()
        segs = asr_mod.transcribe_comfy(db, src, ComfyClient(_base),
                                        progress=_heartbeat, theme=_theme)
    else:
        segs = asr_mod.transcribe(src, progress=_heartbeat)
    asr_mod.save_segments(data_dir, proj["slug"], segs)
    full = "\n\n".join(x["text"] for x in segs)
    data_to_abs(data_dir, proj["novel_path"]).write_text(full, encoding="utf-8")
    conn = db.connect()
    conn.execute("UPDATE projects SET chapters_json=? WHERE id=?",
                 (json.dumps(parse_chapters(full), ensure_ascii=False), pid))
    conn.commit()
    emit_log(db, "asr", "info",
             f"转写完成：{len(segs)} 段 / {len(full)} 字（正文已回填，"
             "可继续 分析→一键出片）", project_id=pid, job_id=job["id"])
