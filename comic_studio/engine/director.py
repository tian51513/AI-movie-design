# comic_studio/engine/director.py
"""P7-D 整段批量快车道（设计 §16）：shots → MiniMaxH3Director timeline v5。

真机 ground truth（2026-08-28，用户以保健室4 分镜实测 MiniMaxH3_Director_视频展示
工作流）：timeline.version=5、prompt_batch 模式；refs 编号规则 = index N → <Picture N+1>，
global.refs 先占连续 index，段 refs 续接；段 taskType 可空（继承节点级 r2v）；
段间连贯 continuityFromPrev + output.continuityEnabled。

v1 语义：每段 refs = 该镜绑定角色的主图（index 从 0 起 → <Picture 1..k>），
与现有逐镜链路的每镜槽位表同构，prompt 原样进段（token 不重排）；
fl2v 衔接语义由 continuityFromPrev（latent 运动上下文）承担。
"""
import json
import random

from .logbus import emit as emit_log
from .queue.worker import register


def _align_frames(n: int) -> int:
    """H3 patchify 对齐（导演台 frame_align：n % 17 == 5）。"""
    while n % 17 != 5:
        n += 1
    return n


def _canvas(aspect: str) -> tuple[int, int]:
    """×32 对齐画布（用户实测模板值：9:16 → 608×1056）。"""
    return (608, 1056) if aspect == "9:16" else (1056, 608)


def build_timeline(db, data_dir, project_id: int, fps: int = 24):
    """生效镜（disabled 过滤）→ (timeline_dict, uploads)。
    uploads = [{path, name}]：角色主图待 ComfyUI /upload/image（确定性命名），
    refs[].imageFile 即上传名。"""
    from .paths import data_to_abs
    from .projects import get_project
    from .shots import list_shots
    from .assets import get_asset

    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    shots = [s for s in list_shots(db, project_id) if not s["disabled"]]
    if not shots:
        raise ValueError("无生效分镜")

    width, height = _canvas(proj["aspect_ratio"])
    upload_by_path: dict[str, str] = {}

    def _ref_entry(index: int, asset_id: int) -> dict:
        a = get_asset(db, asset_id)
        if a is None:
            raise ValueError(f"资产不存在: {asset_id}")
        main = data_to_abs(data_dir, a["library_dir"]) / "main.png"
        if not main.exists():
            raise ValueError(f"角色「{a['name']}」缺主图（先过门1 生成参考图）: {main}")
        if str(main) not in upload_by_path:
            upload_by_path[str(main)] = f"cs__{proj['slug']}__a{asset_id}__main.png"
        return {"index": index, "imageFile": upload_by_path[str(main)],
                "fileName": "", "type": "input", "subfolder": ""}

    segments, start = [], 0
    for shot in shots:
        ledger = json.loads(shot["ledger_json"] or "{}")
        char_ids = list(((ledger.get("assets") or {}).get("characters")) or [])
        frames = _align_frames(max(5, round(float(shot["duration"]) * fps)))
        refs = [_ref_entry(i, cid) for i, cid in enumerate(dict.fromkeys(char_ids))]
        segments.append({
            "id": f"cs-shot-{shot['seq']}",
            "start": start, "length": frames, "frameCount": frames,
            "durationSec": float(shot["duration"]),
            "prompt": (shot["prompt"] or "").strip() or (shot["description"] or ""),
            "negativePrompt": "", "taskType": "",
            "refs": refs, "refAudios": [], "refVideos": [],
            "genImage": {"imageFile": ""},
            "continuityFromPrev": shot["depends_on"] is not None,
        })
        start += frames

    timeline = {
        "version": 5, "editMode": "segment",
        "totalFrames": start, "frameRate": fps,
        "width": width, "height": height, "refMaxSize": max(width, height),
        "timelineMode": "prompt_batch",
        "video": {"fileName": "", "videoFile": "", "subfolder": "", "type": "input",
                  "frames": [], "frameMap": []},
        "videoClips": [],
        "global": {"taskType": "r2v", "prompt": "", "refs": [],
                   "referenceVideo": {}, "continuousReference": False,
                   "genImage": {"imageFile": ""}, "refAudios": [], "refVideos": [],
                   "commonEnabled": False, "commonCollapsed": True},
        "output": {"mode": "fixed", "aspectRatio": proj["aspect_ratio"],
                   "width": width, "height": height,
                   "maxExportFrames": 0, "exportMode": "all", "audioMode": "generate",
                   "continuityEnabled": True, "continuityOverlapFrames": 22},
        "runSelectEnabled": False, "runSelection": [],
        "segments": segments,
        "keyframes": [], "shots": [], "videoClips": [],
    }
    uploads = [{"path": p, "name": n} for p, n in upload_by_path.items()]
    return timeline, uploads


@register("gen_director")
def handle_gen_director(db, data_dir, job, comfy):
    """整段快车道 worker：shots → timeline → 一次提交 → 整片落盘 output/epNNN.mp4。
    v1 限制（设计 §16.3）：整片不混 TTS/字幕（逐镜链路保留该能力）；所有生效镜
    video_path 指向同一整片、直达 merged。stall 放宽到 2 小时（整部连跑）。"""
    from .jobs import attach_snapshot
    from .paths import data_to_abs
    from .projects import get_project, set_stage
    from .shots import list_shots, update_shot
    from .workflows.registry import resolve_template

    payload = json.loads(job["payload_json"] or "{}")
    pid = payload.get("project_id", job["project_id"])
    proj = get_project(db, pid)
    timeline, uploads = build_timeline(db, data_dir, pid)
    tmpl = resolve_template(db, "director")
    wf = tmpl.api_json()
    for key, value in {"timeline_data": json.dumps(timeline, ensure_ascii=False),
                       "seed": random.randint(0, 2**31 - 1)}.items():
        ip = tmpl.inject_params.get(key)
        if ip:
            wf[ip.node]["inputs"][ip.field] = value
    attach_snapshot(db, job["id"],
                    prompt=f"(director：{len(timeline['segments'])} 段整片，"
                           f"共 {timeline['totalFrames']} 帧)",
                    workflow=wf, template_id=tmpl.id)
    for up in uploads:
        comfy.upload_image(up["path"], up["name"])
    emit_log(db, "comfy", "info",
             f"导演台整段提交（{len(timeline['segments'])} 段，模板 {tmpl.id}）",
             project_id=pid, job_id=job["id"])
    prompt_id = comfy.submit(wf, client_id=f"cs-dir-{job['id']}")
    videos = comfy.wait_and_collect(prompt_id, stall_seconds=7200)
    video = next(v for v in videos
                 if str(v.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov")))
    out_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob("ep*.mp4"))) + 1
    dest = out_dir / f"ep{n:03d}.mp4"
    comfy.download(video["filename"], video.get("subfolder", ""),
                   video.get("type", "output"), dest)
    rel = f"projects/{proj['slug']}/output/ep{n:03d}.mp4"
    for s in list_shots(db, pid):
        if not s["disabled"]:
            update_shot(db, s["id"], {"status": "rendered", "video_path": rel})
    set_stage(db, pid, "merged")
    emit_log(db, "comfy", "info", f"导演台整片已落盘 {rel}（直达 merged）",
             project_id=pid, job_id=job["id"])
    return dest
