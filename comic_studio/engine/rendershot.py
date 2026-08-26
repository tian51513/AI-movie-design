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
from .settings import get_setting
from .shots import get_shot, update_shot
from .video import extract_last_frame
from .workflows import registry
from .workflows.filler import fill_workflow

ASPECT_ENUM = {"16:9": "16:9 (Widescreen)", "9:16": "9:16 (Portrait Widescreen)"}


def pick_template_id(shot_row) -> str:
    wt = shot_row["workflow_type"] or ""
    if wt == "fl2v":
        return "h3_fl2v"
    if wt == "t2v":
        return "h3_t2v"
    return "h3_ref2va"


def _shot_versions_in(shot_dir) -> list:
    """列出镜头目录全部视频版本文件名（video*.mp4，含历史 video.mp4 / video_*.mp4）。
    排序：video.mp4 最前；v{N} 按数字大小（防 v10 < v2 字符串序 bug）；其余按文件名。"""
    import re as _re
    d = Path(shot_dir)
    if not d.is_dir():
        return []

    def sort_key(n: str):
        if n == "video.mp4":
            return (0, 0, "")
        m = _re.fullmatch(r"video_v(\d+)\.mp4", n)
        if m:
            return (1, int(m.group(1)), "")
        return (2, 0, n)

    return sorted(
        (f.name for f in d.iterdir()
         if f.is_file() and _re.fullmatch(r"video[\w.\-]*\.mp4", f.name)),
        key=sort_key)


def _max_version_number(versions: list) -> int:
    import re as _re
    best = 0
    for name in versions:
        m = _re.search(r"video_v(\d+)\.mp4", name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def shot_versions(data_dir, slug: str, seq: int) -> list:
    """对外辅助：项目 slug + 镜头序号 → 版本文件名列表（video.mp4 最前，其余自然排序）。"""
    shot_dir = Path(data_dir) / "projects" / slug / "shots" / str(seq)
    return _shot_versions_in(shot_dir)


def collect_ref_images(db, shot_row) -> list[dict]:
    """角色优先占满参考槽（人物一致性 > 场景还原），场景/道具仅在有空槽时补位。
    C 版实验教训：第二角色无参考图 + 同款服装 → 模型身份融合（2026-08-24 实测）。"""
    if shot_row["workflow_type"] == "t2v":
        return []
    ledger = json.loads(shot_row["ledger_json"] or "{}")
    assets_map = ledger.get("assets", {})
    ordered_ids = (assets_map.get("characters", [])
                   + assets_map.get("scenes", [])
                   + assets_map.get("props", []))
    refs = []
    for asset_id in ordered_ids:
        if len(refs) >= 2:
            break
        asset = get_asset(db, asset_id)
        if asset and asset["library_dir"]:
            refs.append({"slot": f"ref{len(refs)}",
                         "path": f"{asset['library_dir']}/views/sheet.png"})
    if len(refs) == 1:
        refs.append({"slot": "ref1", "path": refs[0]["path"]})
    return refs


KF_NO_CUT = ("本镜头为首尾帧插值生成：单一连续镜头，机位、景别、光线全程保持不变，"
             "禁止镜内切换与跳切；人物动作从首帧状态平滑过渡至尾帧状态。")
KF_PAIR_CONSTRAINT = ("两帧必须严格同机位、同景别、同构图、同光线与背景，"
                      "仅人物的肢体动作与表情不同；禁止任何镜头切换")


def build_keyframe_prompt(shot, proj, phase: str) -> str:
    """首/尾关键帧提示词：分镜描述 + 画风 + 时代 + 成对约束 + ZImage 尾缀。"""
    from .era import ERA_SUFFIX
    from .genref import ZIMAGE_TAIL
    detail = (shot["description"] or "").strip().rstrip("。；;，,") or "按分镜描述"
    prompt = f"漫剧分镜关键帧（{phase}瞬间）：{detail}"
    style = ((proj["style"] or "") if proj is not None else "").strip().rstrip("。；;，,")
    if style:
        prompt += "。" + style
    era = proj["era"] if proj is not None and "era" in proj.keys() else ""
    if era:
        prompt += "。" + ERA_SUFFIX.format(era=era)
    return prompt + "。" + KF_PAIR_CONSTRAINT + ZIMAGE_TAIL["scene"]


def _anchor_main_png(db, shot):
    """取本镜首位有主图的角色 main.png（关键帧人设锚定，连贯性②）。"""
    ledger = json.loads(shot["ledger_json"] or "{}")
    for aid in (ledger.get("assets", {}) or {}).get("characters", []):
        a = get_asset(db, aid)
        if a and a["library_dir"]:
            main = Path(a["library_dir"]) / "main.png"
            return main
    return None


def ensure_keyframes(db, data_dir, shot_id, comfy, job_id=None):
    """fl2v 关键帧生成（方案A 二期）：缺 kf_*.png 时经 t2i 模板生成首尾对。
    两帧共用同一 seed（保构图一致），成对约束写入提示词（方案 A 避坑守则）。
    连贯性②：主图模板有图槽且角色有 main.png → 作参考锚定脸/服装；
    无主图时引导纯文生图（zimage_t2i）。"""
    shot = get_shot(db, shot_id)
    proj = get_project(db, shot["project_id"])
    shot_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}")
    shot_dir.mkdir(parents=True, exist_ok=True)
    kf_start, kf_end = shot_dir / "kf_start.png", shot_dir / "kf_end.png"
    if kf_start.exists() and kf_end.exists():
        return kf_start, kf_end
    from .workflows.registry import resolve_template, scan_templates, TEMPLATE_ROOT
    tmpl = resolve_template(db, "t2i")
    images = None
    anchor_line = ""
    main_rel = _anchor_main_png(db, shot)
    if tmpl.inject_images:
        if main_rel is not None:
            main_abs = data_to_abs(data_dir, str(main_rel))
            if main_abs.exists():
                images = [{"slot": tmpl.inject_images[0]["slot"], "path": str(main_abs)}]
                anchor_line = "。人物外貌与服装与参考图保持完全一致"
        if images is None:
            boot = scan_templates(TEMPLATE_ROOT).get("zimage_t2i")
            if boot is None or boot.inject_images:
                raise ValueError("关键帧模板需要图片输入且无可锚定的角色主图")
            tmpl = boot
            emit_log(db, "comfy", "info",
                     f"分镜 {shot['seq']} 关键帧无主图可锚定，引导纯文生图 {boot.id}",
                     project_id=proj["id"], job_id=job_id)
    overrides = (get_setting(db, "model_overrides") or {}).get(tmpl.id)
    seed = random.randint(0, 2**31 - 1)  # 两帧同 seed：构图一致，仅动作不同
    for phase, dest in (("起始", kf_start), ("结尾", kf_end)):
        if dest.exists():
            continue
        wf, uploads = fill_workflow(
            tmpl, prompt=build_keyframe_prompt(shot, proj, phase) + anchor_line,
            params={"seed": seed}, images=images,
            output_ctx={"project": proj["slug"],
                        "asset": f"shot-{shot['seq']}-kf-{phase}"},
            model_overrides=overrides)
        for up in uploads:
            comfy.upload_image(Path(up["path"]), up["name"])
        emit_log(db, "comfy", "info",
                 f"分镜 {shot['seq']} 关键帧（{phase}）提交（模板 {tmpl.id}）",
                 project_id=proj["id"], job_id=job_id)
        results = comfy.wait_and_collect(
            comfy.submit(wf, client_id=f"cs-kf-{shot_id}-{phase}"), stall_seconds=600)
        img = next((r for r in results if r.get("_kind") == "image"), None)
        if img is None:
            raise RuntimeError(f"分镜 {shot['seq']} 关键帧（{phase}）未返回图片")
        comfy.download(img["filename"], img.get("subfolder", ""),
                       img.get("type", "output"), dest)
        emit_log(db, "comfy", "info",
                 f"分镜 {shot['seq']} 关键帧（{phase}）已生成落盘",
                 project_id=proj["id"], job_id=job_id)
    return kf_start, kf_end


def render_shot(db, data_dir, shot_id, comfy, job_id=None,
                first_frame_png: Path | None = None) -> Path:
    shot = get_shot(db, shot_id)
    proj = get_project(db, shot["project_id"])

    tmpl_id = pick_template_id(shot)
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    # 关键帧接线（方案A 一期）：fl2v 走 h3_fl2v 首尾帧插值；
    # kf_end 缺失时降级 h3_i2v（仅首帧），二期关键帧任务补齐 kf_* 后自动升回
    shot_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}")
    kf_start, kf_end = shot_dir / "kf_start.png", shot_dir / "kf_end.png"
    if tmpl_id == "h3_fl2v" and not (kf_start.exists() and kf_end.exists()):
        try:
            ensure_keyframes(db, data_dir, shot_id, comfy, job_id=job_id)  # 二期：自动补对
        except Exception as exc:
            emit_log(db, "comfy", "warn",
                     f"分镜 {shot['seq']} 关键帧生成失败，降级首帧模式：{exc}",
                     project_id=proj["id"], job_id=job_id)
    if tmpl_id == "h3_fl2v" and not kf_end.exists():
        tmpl_id = "h3_i2v"
    template = reg[tmpl_id]

    prompt = shot["prompt"]
    if not prompt:
        raise ValueError("shot prompt 为空")
    if tmpl_id == "h3_fl2v":
        prompt += "\n" + KF_NO_CUT  # 插值约束：和解 D 模式提示词的镜内切换

    params = {
        "seed": random.randint(0, 2**31 - 1),
        "megapixels": proj["video_megapixels"],
        "multiple": proj["video_multiple"],
        "duration": max(4, int(shot["duration"])),
        "lora_strength": proj["lora_realism"],
    }
    # 远景规避：远景/大全景自动升一档兆像素（上限 1.2）
    camera = json.loads(shot["camera_json"] or "{}")
    if camera.get("景别") in ("远景", "大全景"):
        params["megapixels"] = min(1.2, float(proj["video_megapixels"]) + 0.4)
    aspect_val = ASPECT_ENUM.get(proj["aspect_ratio"])
    if aspect_val is not None:
        params["aspect"] = aspect_val

    # Images
    if first_frame_png is None and tmpl_id in ("h3_fl2v", "h3_i2v") and kf_start.exists():
        first_frame_png = kf_start  # 无上镜衔接时，本镜关键帧首图兜底
    if first_frame_png and tmpl_id in ("h3_fl2v", "h3_i2v"):
        images = [{"slot": "first", "path": str(first_frame_png)}]
    elif shot["workflow_type"] == "t2v" or tmpl_id in ("h3_fl2v", "h3_i2v"):
        images = []  # i2v/fl2v 无首帧——交由 I1 快失败给出明确报错
    elif first_frame_png is not None:
        # 接力（连贯性①配套）：上镜尾帧优先占 ref0（画面/姿态延续），
        # 角色参考占 ref1（锁人设）——全链后 ref2va 的默认形态
        raw_refs = collect_ref_images(db, shot)
        ref1_path = (data_to_abs(data_dir, raw_refs[0]["path"])
                     if raw_refs else first_frame_png)
        images = [{"slot": "ref0", "path": str(first_frame_png)},
                  {"slot": "ref1", "path": str(ref1_path)}]
    else:
        raw_refs = collect_ref_images(db, shot)
        if not raw_refs:
            emit_log(db, "comfy", "warn",
                     f"分镜 {shot['seq']} 无参考图，LoadImage 可能失败",
                     project_id=proj["id"], job_id=job_id)
        images = [{"slot": r["slot"],
                   "path": str(data_to_abs(data_dir, r["path"]))}
                  for r in raw_refs]
    if tmpl_id == "h3_fl2v":
        images.append({"slot": "last", "path": str(kf_end)})

    output_ctx = {"project": proj["slug"], "asset": f"shot-{shot['seq']}"}
    model_overrides = (get_setting(db, "model_overrides") or {}).get(template.id)
    wf, uploads = fill_workflow(template, prompt=prompt, params=params,
                                images=images, output_ctx=output_ctx,
                                model_overrides=model_overrides)

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

    return _download_video_result(db, data_dir, comfy, shot, proj, video,
                                  job_id=job_id)


def _download_video_result(db, data_dir, comfy, shot, proj, video,
                           job_id=None) -> Path:
    """产物下载落盘段（render_shot 与 reattach 共用）：版本递增落盘 + 状态回写。"""
    shot_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}")
    versions = _shot_versions_in(shot_dir)
    next_n = _max_version_number(versions) + 1
    rel_path = f"projects/{proj['slug']}/shots/{shot['seq']}/video_v{next_n}.mp4"
    dest = data_to_abs(data_dir, rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    comfy.download(video["filename"], video.get("subfolder", ""),
                   video.get("type", "output"), dest)

    update_shot(db, shot["id"], {"status": "rendered", "video_path": rel_path})
    emit_log(db, "comfy", "info", f"分镜 {shot['seq']} 视频已落盘",
             project_id=proj["id"], job_id=job_id,
             data={"path": rel_path})
    return dest


def reattach(db, data_dir, job_row, comfy) -> Path | None:
    """断点对账（spec §5）：running job 按 comfy_prompt_id 查 /history，
    ComfyUI 已完成 → 直接下载落盘不重渲；未完成/无视频产物 → None（交回 requeue）。"""
    if job_row["shot_id"] is None:
        return None
    shot = get_shot(db, job_row["shot_id"])
    if shot is None:
        return None
    proj = get_project(db, shot["project_id"])
    results = comfy.history_result(job_row["comfy_prompt_id"])
    if results is None:
        return None
    video = next((r for r in results if r.get("_kind") == "video"), None)
    if video is None:
        return None
    dest = _download_video_result(db, data_dir, comfy, shot, proj, video,
                                  job_id=job_row["id"])
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 断点对账：ComfyUI 已完成，未重渲",
             project_id=proj["id"], job_id=job_row["id"],
             data={"prompt_id": job_row["comfy_prompt_id"]})
    return dest


def reattach_wait(db, data_dir, job_row, comfy,
                  stall_seconds: float = 900) -> Path | None:
    """等待式接回：prompt 仍在 ComfyUI 队列/执行中 → 等它跑完直接下载（不重提交）。
    失速/失败 → None（交回上层标 failed，由下一轮真正重渲）。"""
    if job_row["shot_id"] is None:
        return None
    shot = get_shot(db, job_row["shot_id"])
    if shot is None:
        return None
    proj = get_project(db, shot["project_id"])
    results = comfy.wait_and_collect(job_row["comfy_prompt_id"],
                                     stall_seconds=stall_seconds)
    video = next((r for r in results if r.get("_kind") == "video"), None)
    if video is None:
        return None
    dest = _download_video_result(db, data_dir, comfy, shot, proj, video,
                                  job_id=job_row["id"])
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 断点对账：等待 ComfyUI 跑完落盘，未重渲",
             project_id=proj["id"], job_id=job_row["id"],
             data={"prompt_id": job_row["comfy_prompt_id"]})
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
