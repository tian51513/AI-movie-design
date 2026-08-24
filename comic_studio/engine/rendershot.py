# comic_studio/engine/rendershot.py
"""gen_shot 渲染编排：模板选择/参考图槽位绑定/项目参数注入/提交-等待-落盘（spec §9）。"""
import json
import random
from pathlib import Path

from .assets import get_asset
from .logbus import emit as emit_log
from .paths import data_to_abs
from .projects import get_project
from .queue.worker import register
from .shots import get_shot, update_shot
from .video import extract_last_frame
from .workflows import registry
from .workflows.filler import fill_workflow

ASPECT_ENUM = {"16:9": "16:9 (Widescreen)", "9:16": "9:16 (Portrait Widescreen)"}
SPEED_STEPS = {"快速": 8, "标准": 16, "高质量": 25}


def pick_template_id(shot_row) -> str:
    wt = shot_row["workflow_type"] or ""
    if wt == "fl2v":
        return "h3_i2v"
    if wt == "t2v":
        return "h3_t2v"
    return "h3_ref2va"


def collect_ref_images(db, shot_row) -> list[dict]:
    if shot_row["workflow_type"] == "t2v":
        return []
    ledger = json.loads(shot_row["ledger_json"] or "{}")
    assets_map = ledger.get("assets", {})
    char_ids = assets_map.get("characters", [])
    scene_ids = assets_map.get("scenes", [])
    refs = []
    if char_ids:
        asset = get_asset(db, char_ids[0])
        if asset and asset["library_dir"]:
            refs.append({"slot": "ref0",
                         "path": f"{asset['library_dir']}/views/sheet.png"})
    if scene_ids:
        asset = get_asset(db, scene_ids[0])
        if asset and asset["library_dir"]:
            refs.append({"slot": "ref1",
                         "path": f"{asset['library_dir']}/views/sheet.png"})
    if len(refs) == 1:
        other = "ref1" if refs[0]["slot"] == "ref0" else "ref0"
        refs.append({"slot": other, "path": refs[0]["path"]})
    return refs


def render_shot(db, data_dir, shot_id, comfy, job_id=None,
                first_frame_png: Path | None = None) -> Path:
    shot = get_shot(db, shot_id)
    proj = get_project(db, shot["project_id"])

    tmpl_id = pick_template_id(shot)
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    template = reg[tmpl_id]

    prompt = shot["prompt"]
    if not prompt:
        raise ValueError("shot prompt 为空")

    params = {
        "seed": random.randint(0, 2**31 - 1),
        "megapixels": proj["video_megapixels"],
        "multiple": proj["video_multiple"],
        "steps": SPEED_STEPS[proj["video_speed"]],
        "duration": max(4, int(shot["duration"])),
    }
    aspect_val = ASPECT_ENUM.get(proj["aspect_ratio"])
    if aspect_val is not None:
        params["aspect"] = aspect_val

    # Images
    if first_frame_png:
        images = [{"slot": "first", "path": str(first_frame_png)}]
    elif shot["workflow_type"] == "t2v":
        images = []
    else:
        raw_refs = collect_ref_images(db, shot)
        if not raw_refs:
            emit_log(db, "comfy", "warn",
                     f"分镜 {shot['seq']} 无参考图，LoadImage 可能失败",
                     project_id=proj["id"], job_id=job_id)
        images = [{"slot": r["slot"],
                   "path": str(data_to_abs(data_dir, r["path"]))}
                  for r in raw_refs]

    output_ctx = {"project": proj["slug"], "asset": f"shot-{shot['seq']}"}
    wf, uploads = fill_workflow(template, prompt=prompt, params=params,
                                images=images, output_ctx=output_ctx)

    # I1: 若模板声明图片槽但上传清单为空，快失败
    if template.inject_images and not uploads:
        raise ValueError(
            f"模板 {template.id} 需要图片输入但未提供"
            f"（镜头 {shot['seq']} 的资产无参考图或衔接首帧缺失）")

    for up in uploads:
        comfy.upload_image(Path(up["path"]), up["name"])

    prompt_id = comfy.submit(wf, client_id=f"cs-shot-{shot_id}")
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 提交渲染（模板 {template.id}）",
             project_id=proj["id"], job_id=job_id,
             data={"prompt_id": prompt_id})

    if job_id is not None:
        conn = db.connect()
        conn.execute("UPDATE jobs SET comfy_prompt_id=? WHERE id=?",
                     (prompt_id, job_id))
        conn.commit()

    results = comfy.wait_and_collect(
        prompt_id, stall_seconds=900,
        on_interrupt=lambda: emit_log(
            db, "comfy", "warn",
            f"分镜 {shot['seq']} 渲染失速，已 interrupt",
            project_id=proj["id"], job_id=job_id))

    video = next((r for r in results if r.get("_kind") == "video"), None)
    if video is None:
        raise RuntimeError("ComfyUI 未返回视频输出")

    rel_path = f"projects/{proj['slug']}/shots/{shot['seq']}/video.mp4"
    dest = data_to_abs(data_dir, rel_path)
    comfy.download(video["filename"], video.get("subfolder", ""),
                   video.get("type", "output"), dest)

    update_shot(db, shot_id, {"status": "rendered", "video_path": rel_path})
    emit_log(db, "comfy", "info", f"分镜 {shot['seq']} 视频已落盘",
             project_id=proj["id"], job_id=job_id,
             data={"path": rel_path})

    return dest


@register("gen_shot")
def handle_gen_shot(db, data_dir, job, comfy):
    """gen_shot worker handler：首帧链 + 渲染编排。"""
    import json

    payload = json.loads(job["payload_json"] or "{}")
    shot_id = payload["shot_id"]
    shot = get_shot(db, shot_id)

    if shot is None:
        raise ValueError("分镜已删除（gen_shot 任务）")

    proj = get_project(db, shot["project_id"])
    first_frame_png = None

    # 首帧链：depends_on 非空时尝试提取前一镜最后一帧
    if shot["depends_on"]:
        prev_shot = get_shot(db, shot["depends_on"])
        if prev_shot and prev_shot["video_path"]:
            prev_video = data_to_abs(data_dir, prev_shot["video_path"])
            if prev_video.exists():
                first_png_path = data_to_abs(
                    data_dir,
                    f"projects/{proj['slug']}/shots/{shot['seq']}/first.png"
                )
                first_png_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    extract_last_frame(prev_video, first_png_path)
                    first_frame_png = first_png_path
                    emit_log(db, "comfy", "info",
                             f"分镜 {shot['seq']} 使用首帧（来自镜 {prev_shot['seq']}）",
                             project_id=proj["id"], job_id=job["id"])
                except Exception:
                    emit_log(db, "comfy", "warn",
                             f"分镜 {shot['seq']} 提取首帧失败，降级常规路径",
                             project_id=proj["id"], job_id=job["id"])
            else:
                emit_log(db, "comfy", "warn",
                         f"分镜 {shot['seq']} 前镜视频不存在，降级常规路径",
                         project_id=proj["id"], job_id=job["id"])
        else:
            emit_log(db, "comfy", "warn",
                     f"分镜 {shot['seq']} 前镜无视频，降级常规路径",
                     project_id=proj["id"], job_id=job["id"])

    dest = render_shot(db, data_dir, shot_id, comfy, job_id=job["id"],
                       first_frame_png=first_frame_png)
    emit_log(db, "comfy", "info", f"分镜 {shot['seq']} gen_shot 完成",
             project_id=proj["id"], job_id=job["id"],
             data={"video_path": str(dest)})
    return dest
