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


def _align32(v: float) -> int:
    import math
    return max(32, math.ceil(v / 32) * 32)  # ceil：不低于目标像素档


def _canvas(aspect: str, megapixels: float = 0.4) -> tuple[int, int]:
    """按项目兆像素档计算 ×32 对齐画布（2026-08-28 需求：跟随项目预设；
    此前固定 608×1056=0.64MP，比逐镜 0.4MP 档多耗 ~25% 采样）。"""
    mp = max(0.1, float(megapixels or 0.4)) * 1e6
    r = 9 / 16 if aspect == "9:16" else 16 / 9  # w/h
    w = (mp * r) ** 0.5
    h = w / r
    return _align32(w), _align32(h)


def _segments_for_shots(db, data_dir, proj, shots, upload_by_path, fps=24):
    from .paths import data_to_abs
    from .assets import get_asset

    def _ref_entry(index, asset_id):
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
    return segments


def _timeline_shell(proj, segments, fps=24):
    width, height = _canvas(proj["aspect_ratio"],
                            float(proj["video_megapixels"] or 0.4)
                            if "video_megapixels" in proj.keys() else 0.4)
    total = sum(s["frameCount"] for s in segments)
    return {
        "version": 5, "editMode": "segment",
        "totalFrames": total, "frameRate": fps,
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


def build_timeline(db, data_dir, project_id: int, fps: int = 24):
    """生效镜（disabled 过滤）→ (timeline_dict, uploads)。
    uploads = [{path, name}]：角色主图待 ComfyUI /upload/image（确定性命名），
    refs[].imageFile 即上传名。"""
    from .projects import get_project
    from .shots import list_shots

    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    shots = [s for s in list_shots(db, project_id) if not s["disabled"]]
    if not shots:
        raise ValueError("无生效分镜")
    upload_by_path: dict[str, str] = {}
    segments = _segments_for_shots(db, data_dir, proj, shots, upload_by_path, fps)
    timeline = _timeline_shell(proj, segments, fps)
    uploads = [{"path": p, "name": n} for p, n in upload_by_path.items()]
    return timeline, uploads


def _batch_shots(shots, frame_budget: int, fps: int = 24) -> list[list]:
    """按帧预算切块（真机 2026-08-28 job 721：41 镜 5084 帧的灰占位画布在 CPU
    一次性物化 39GB → DefaultCPUAllocator 爆。Director prompt_batch 的画布 =
    帧数×W×H×3ch×4B，512 帧 ≈ 3.9GB 上限内）。每批至少 1 镜。"""
    batches, cur, cur_frames = [], [], 0
    for s in shots:
        frames = _align_frames(max(5, round(float(s["duration"]) * fps)))
        if cur and cur_frames + frames > frame_budget:
            batches.append(cur)
            cur, cur_frames = [], 0
        cur.append(s)
        cur_frames += frames
    if cur:
        batches.append(cur)
    return batches


@register("gen_director")
def handle_gen_director(db, data_dir, job, comfy):
    """整段快车道 worker：shots → 按帧预算分批 → 每批一次提交（批内 latent 连贯）
    → 批间 ffmpeg 拼接 → 整片落盘 output/epNNN.mp4。
    真机 2026-08-28 job 721 教训：整部一次提交时 Director 会在 CPU 物化全时间轴
    灰画布（5084 帧 ≈ 39GB）→ 分批（默认 512 帧/批，批间断开是 v1 已知限制）。
    v1 限制（设计 §16.3）：整片不混 TTS/字幕；所有生效镜 video_path 指向整片、
    直达 merged。"""
    from .jobs import attach_snapshot
    from .paths import data_to_abs
    from .projects import get_project, set_stage
    from .settings import get_setting
    from .shots import list_shots, update_shot
    from .workflows.registry import resolve_template

    payload = json.loads(job["payload_json"] or "{}")
    pid = payload.get("project_id", job["project_id"])
    proj = get_project(db, pid)
    shots = [s for s in list_shots(db, pid) if not s["disabled"]]
    if not shots:
        raise ValueError("无生效分镜")
    tmpl = resolve_template(db, "director")
    comfy_cfg = get_setting(db, "comfy") or {}
    budget = int(comfy_cfg.get("director_batch_frames") or 512)
    batches = _batch_shots(shots, budget)
    upload_by_path: dict[str, str] = {}
    parts_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/director_parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, batch in enumerate(batches, 1):
        segments = _segments_for_shots(db, data_dir, proj, batch, upload_by_path)
        segments[0]["continuityFromPrev"] = False  # 批间断开（无跨提交 latent 接力）
        # 批内 start 重排
        _start = 0
        for seg in segments:
            seg["start"] = _start
            _start += seg["frameCount"]
        timeline = _timeline_shell(proj, segments)
        wf = tmpl.api_json()
        # 性能开关（设置页可调，引擎注入覆盖模板值）：段间清显存/源帧导出
        dir_node = tmpl.inject_params["timeline_data"].node
        wf[dir_node]["inputs"]["clear_vram_between_segments"] = bool(
            comfy_cfg.get("director_clear_vram"))
        wf[dir_node]["inputs"]["export_source_images"] = bool(
            comfy_cfg.get("director_export_source"))
        for key, value in {"timeline_data": json.dumps(timeline, ensure_ascii=False),
                           "seed": random.randint(0, 2**31 - 1)}.items():
            ip = tmpl.inject_params.get(key)
            if ip:
                wf[ip.node]["inputs"][ip.field] = value
        attach_snapshot(db, job["id"],
                        prompt=f"(director 批次 {i}/{len(batches)}：{len(segments)} 段 "
                               f"{timeline['totalFrames']} 帧，budget={budget})",
                        workflow=wf, template_id=tmpl.id)
        for up in ({"path": p, "name": n} for p, n in upload_by_path.items()):
            comfy.upload_image(up["path"], up["name"])
        emit_log(db, "comfy", "info",
                 f"导演台批次 {i}/{len(batches)} 提交（{len(segments)} 段 "
                 f"{timeline['totalFrames']} 帧，模板 {tmpl.id}）",
                 project_id=pid, job_id=job["id"])
        prompt_id = comfy.submit(wf, client_id=f"cs-dir-{job['id']}-{i}")
        videos = comfy.wait_and_collect(prompt_id, stall_seconds=7200)
        video = next(v for v in videos
                     if str(v.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov")))
        part = parts_dir / f"part{i:03d}.mp4"
        comfy.download(video["filename"], video.get("subfolder", ""),
                       video.get("type", "output"), part)
        parts.append(part)
        emit_log(db, "comfy", "info", f"导演台批次 {i}/{len(batches)} 完成",
                 project_id=pid, job_id=job["id"])
    out_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob("ep*.mp4"))) + 1
    dest = out_dir / f"ep{n:03d}.mp4"
    if len(parts) == 1:
        import shutil
        shutil.copyfile(parts[0], dest)
    else:
        from . import merge
        merge.concat(parts, dest)
    rel = f"projects/{proj['slug']}/output/ep{n:03d}.mp4"
    for s in list_shots(db, pid):
        if not s["disabled"]:
            update_shot(db, s["id"], {"status": "rendered", "video_path": rel})
    set_stage(db, pid, "merged")
    emit_log(db, "comfy", "info",
             f"导演台整片已落盘 {rel}（{len(batches)} 批拼接，直达 merged）",
             project_id=pid, job_id=job["id"])
    return dest
