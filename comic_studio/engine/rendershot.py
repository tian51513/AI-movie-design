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

# 项目画幅 → ResolutionSelector 节点枚举（2026-08-30 五档，本机 ComfyUI /object_info 实测值）
ASPECT_ENUM = {
    "16:9": "16:9 (Widescreen)", "9:16": "9:16 (Portrait Widescreen)",
    "3:4": "3:4 (Portrait Standard)", "4:3": "4:3 (Standard)", "1:1": "1:1 (Square)",
}


def pick_template_id(shot_row, db=None) -> str:
    """分镜视频模板路由（2026-08-26 需求）：优先读 template_map 按类型映射，
    用户可在设置页为 ref2va/fl2v/t2v 各自切换实际模板；无映射时走内置默认。"""
    wt = (shot_row["workflow_type"] or "").strip() or "ref2va"
    # i2v 默认（2026-09-18）：fl2v 缺尾帧的降级目标此前硬编码在 render_shot，
    # type 路由重构后走统一入口——template_map.i2v 也可自选降级模板
    defaults = {"ref2va": "h3_ref2va", "fl2v": "h3_fl2v", "t2v": "h3_t2v",
                "i2v": "h3_i2v"}
    if db is not None:
        try:
            tmpl_id = get_setting(db, "template_map").get(wt)
            if tmpl_id:
                return tmpl_id
        except Exception:
            pass
    return defaults.get(wt, "h3_ref2va")


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
        if len(refs) >= 2:  # TODO 动态槽数（需 db 查 template inject_images 数）
            break
        asset = get_asset(db, asset_id)
        if asset and asset["library_dir"]:
            refs.append({"slot": f"ref{len(refs)}",
                         "path": f"{asset['library_dir']}/views/sheet.png"})
    if len(refs) == 1:
        refs.append({"slot": "ref1", "path": refs[0]["path"]})
    return refs


def _voice_slots_for_shot(db, data_dir, proj, shot, audio_slots: list):
    """Phase 2 音色（2026-08-30）：dialogue 说话人（去重保序）→ 角色资产 voice 字段
    （音色名或样本相对路径）→ 样本文件。返回 (audio_images 条目, <Audio N> 声明)。
    voice 缺失或样本不存在 → 该说话人跳过（渲染照旧走 TTS）。"""
    from .voicelib import resolve_sample
    from .assets import list_project_assets
    ledger = json.loads(shot["ledger_json"] or "{}")
    speakers = [d.get("speaker") for d in (ledger.get("dialogue") or [])
                if d.get("speaker")]
    if not speakers or not audio_slots:
        return [], []
    chars = {a["name"]: a for a in list_project_assets(db, shot["project_id"])
             if a["kind"] == "character"}
    entries, decls = [], []
    for sp in dict.fromkeys(speakers):
        if len(entries) >= len(audio_slots):
            break  # 模板实际接线槽数（AUTOGROW 理论无限，按 manifest 为准）
        a = chars.get(sp)
        voice = ((a["voice"] if a is not None and "voice" in a.keys() else "") or "").strip()
        if not voice:
            continue
        from pathlib import PurePosixPath
        path = None
        if PurePosixPath(voice).suffix:  # 相对路径（角色自己的上传音频）
            cand = data_to_abs(data_dir, voice)
            path = cand if cand.exists() else None
        if path is None:                 # 音色名（项目级 > 全局 > 预设）
            path = resolve_sample(data_dir, voice, proj["slug"])
        if path:
            entries.append({"slot": audio_slots[len(entries)], "path": str(path)})
            decls.append(f"<Audio {len(entries)}> is a reference audio for {sp}'s "
                         "voice timbre, vocal identity and speaking tone.")
    return entries, decls


def _clear_native_voice(db, shot) -> None:
    """M11（2026-09-05 审计）：无音频槽模板（t2v/fl2v/i2v）渲染成功即清
    h3_native_voice——此前只设不清，换模板重渲后该镜永远无声。"""
    ledger = json.loads(shot["ledger_json"] or "{}")
    if ledger.pop("h3_native_voice", None) is not None:
        update_shot(db, shot["id"],
                    {"ledger_json": json.dumps(ledger, ensure_ascii=False)})


def _mark_native_voice(db, shot) -> None:
    ledger = json.loads(shot["ledger_json"] or "{}")
    ledger["h3_native_voice"] = True
    update_shot(db, shot["id"],
                {"ledger_json": json.dumps(ledger, ensure_ascii=False)})


# fl2v 镜内禁切约束（2026-08-30 英化：与模式 E 英文控制式统一，用户实测风格）
KF_NO_CUT = ("This shot is generated by first-last frame interpolation: a single continuous "
             "shot. Camera position, framing, lighting and scene must stay constant throughout "
             "— no cuts, no scene changes, no camera jumps. Character motion transitions "
             "smoothly from the first-frame pose to the last-frame pose.")
KF_PAIR_CONSTRAINT = ("两帧必须严格同机位、同景别、同构图、同光线与背景，"
                      "仅人物的肢体动作与表情不同；禁止任何镜头切换")


def build_keyframe_prompt(db, shot, proj, phase: str) -> str:
    """首/尾关键帧提示词：分镜描述 + 角色外貌文字 + 画风 + 时代 + ZImage 尾缀。"""
    from .era import ERA_SUFFIX
    from .genref import PHOTO_BOOST, ZIMAGE_TAIL, condense_appearance, is_photo_style
    detail = (shot["description"] or "").strip().rstrip("。；;，,") or "按分镜描述"
    prompt = f"漫剧分镜关键帧（{phase}瞬间）：{detail}"
    ledger = json.loads(shot["ledger_json"] or "{}")
    # 场景描述优先（2026-08-26 真机：白底 → 场景放前面提高权重）
    for sid in (ledger.get("assets", {}) or {}).get("scenes", []):
        sc = get_asset(db, sid)
        if sc:
            scene_desc = json.loads(sc["appearance_json"]).get("detail", "")
            if scene_desc:
                prompt += f"。场景环境：{sc['name']}——{scene_desc[:120]}"
            break
    # 角色外貌
    for aid in (ledger.get("assets", {}) or {}).get("characters", []):
        a = get_asset(db, aid)
        if a:
            appearance = json.loads(a["appearance_json"]).get("detail", "")
            appearance = condense_appearance(appearance)
            if appearance:
                prompt += f"。角色「{a['name']}」：{appearance[:150]}"
            break
    # 画风拆层（方案A）：关键帧是图像——用视觉子集，叙事词不进
    style = (((proj["style_vis"] or proj["style"]) or "")
             if proj is not None else "").strip().rstrip("。；;，,")
    if style:
        prompt += "。" + style
    if is_photo_style(style):
        prompt += "。" + PHOTO_BOOST  # 写实增强（与主图同款，2026-08-27 真机二次元泄漏）
    era = proj["era"] if proj is not None and "era" in proj.keys() else ""
    if era:
        prompt += "。" + ERA_SUFFIX.format(era=era)
    # 注意：不放 KF_PAIR_CONSTRAINT——'两帧'字样会让图像模型在单张图里画两个画面；
    # 配对一致性由同 seed + 相似提示词保证
    return prompt + ZIMAGE_TAIL["scene"]


def _anchor_refs(db, shot, data_dir):
    """取角色的主图+多视图参考（关键帧人设锚定）。返回绝对路径列表。"""
    ledger = json.loads(shot["ledger_json"] or "{}")
    refs = []
    for aid in (ledger.get("assets", {}) or {}).get("characters", []):
        a = get_asset(db, aid)
        if not a or not a["library_dir"]:
            continue
        lib = data_to_abs(data_dir, a["library_dir"])
        main = lib / "main.png"
        if main.exists():
            refs.append(main)
        break
    return refs


def _scene_sheet(db, shot, data_dir):
    """取绑定场景资产的参考图（关键帧场景锚定，双槽模板用）。"""
    ledger = json.loads(shot["ledger_json"] or "{}")
    for sid in (ledger.get("assets", {}) or {}).get("scenes", []):
        a = get_asset(db, sid)
        if a and a["library_dir"]:
            sheet = data_to_abs(data_dir, a["library_dir"]) / "views" / "sheet.png"
            if sheet.exists():
                return sheet
        break
    return None


def ensure_keyframes(db, data_dir, shot_id, comfy, job_id=None):
    """fl2v 关键帧生成（方案A）：缺 kf_*.png 时经 t2i 模板生成首尾对。
    首帧参考图（按优先级，2026-08-26 用户需求——必须有一张参考图）：
      1. 上镜尾帧 kf_end.png（画面/人物接力——帧链传播人设）
      2. 角色 main.png（首镜人设锚定）
    尾帧参考首帧（img2img，构图/服装继承，仅动作变化）。
    denoise 差异化：首帧 0.7 自由创作场景 / 尾帧 0.45 紧贴首帧。"""
    shot = get_shot(db, shot_id)
    proj = get_project(db, shot["project_id"])
    shot_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}")
    shot_dir.mkdir(parents=True, exist_ok=True)
    kf_start, kf_end = shot_dir / "kf_start.png", shot_dir / "kf_end.png"
    if kf_start.exists() and kf_end.exists():
        return kf_start, kf_end
    from .workflows.registry import resolve_template, scan_templates, TEMPLATE_ROOT
    tmpl = resolve_template(db, "keyframe")  # 独立关键帧图生图模板（2026-08-26 用户建议）
    n_slots = len(tmpl.inject_images) if tmpl.inject_images else 0

    # 首帧参考图：上镜尾帧 > 角色主图（帧链接力）
    prev_kf_end = None
    if shot["depends_on"]:
        prev = get_shot(db, shot["depends_on"])
        if prev:
            prev_dir = data_to_abs(data_dir,
                                   f"projects/{proj['slug']}/shots/{prev['seq']}")
            _pkf = prev_dir / "kf_end.png"
            if _pkf.exists():
                prev_kf_end = _pkf
    char_refs = _anchor_refs(db, shot, data_dir)  # [main.png]（不含三视图）

    # 合并参考：上镜尾帧优先，无则角色主图
    # 参考图策略：IP-Adapter 模板角色图是身份参考（不是底图），始终传入；
    # img2img 模板才需要区分底图来源（上镜尾帧 > 首镜无底图退纯文）
    # 2026-08-27：模板有图槽就尽量填——具体怎么用由模板决定
    start_ref = prev_kf_end or (char_refs[0] if char_refs else None)

    def _start_images():
        if not n_slots or not start_ref:
            return None
        if n_slots >= 2:
            # 双槽模板（如 zimage_dual_ref）：槽0=角色/尾帧，槽1=场景参考图
            imgs = [{"slot": tmpl.inject_images[0]["slot"], "path": str(start_ref)}]
            # 场景参考图：从绑定的场景资产取 sheet.png
            _sc = _scene_sheet(db, shot, data_dir)
            if _sc:
                imgs.append({"slot": tmpl.inject_images[1]["slot"], "path": str(_sc)})
            return imgs
        return [{"slot": tmpl.inject_images[0]["slot"], "path": str(start_ref)}]

    images = _start_images()
    anchor_line = ""
    if images:
        anchor_line = ("。人物的外貌、五官、发型与服装与参考图保持完全一致，"
                       "但背景场景按本提示词的文字描述生成（参考图仅为人物立绘，"
                       "其白色背景不是画面背景，必须替换为描述中的具体场景）")
    if not images and not start_ref and n_slots > 0:
        # 无参考图 + 模板需要图输入 → 引导纯文
        boot = scan_templates(TEMPLATE_ROOT).get("zimage_t2i")
        if boot is None or boot.inject_images:
            raise ValueError("关键帧模板需要图片输入且无参考图可传")
        tmpl = boot
        n_slots = 0
        emit_log(db, "comfy", "info",
                 f"分镜 {shot['seq']} 关键帧无参考图，引导纯文生图 {boot.id}",
                 project_id=proj["id"], job_id=job_id)

    overrides = (get_setting(db, "model_overrides") or {}).get(tmpl.id)
    seed = random.randint(0, 2**31 - 1)
    for phase, dest in (("起始", kf_start), ("结尾", kf_end)):
        if dest.exists():
            continue
        if phase == "结尾" and kf_start.exists():
            # 尾帧：首帧 img2img（构图/人物/服装/场景继承，仅动作变化）
            if n_slots >= 1:
                images = [{"slot": tmpl.inject_images[0]["slot"], "path": str(kf_start)}]
            else:
                images = None
            anchor_line = ("。画面构图、场景、光线与人物外貌服装与参考图完全一致，"
                           "仅人物动作与表情变化为本镜结尾瞬间" if images else
                           "。与起始帧同构图同场景同人物同服装，仅动作与表情变化为结尾瞬间")
        wf, uploads = fill_workflow(
            tmpl, prompt=build_keyframe_prompt(db, shot, proj, phase) + anchor_line,
            params={"seed": seed}, images=images,
            output_ctx={"project": proj["slug"],
                        "asset": f"shot-{shot['seq']}-kf-{phase}"},
            model_overrides=overrides)
        for up in uploads:
            comfy.upload_image(Path(up["path"]), up["name"])
        if job_id:
            from .jobs import attach_snapshot
            attach_snapshot(db, job_id,
                            prompt=build_keyframe_prompt(db, shot, proj, phase) + anchor_line,
                            workflow=wf, template_id=tmpl.id)
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


def _video_seed(shot) -> int:
    """B5 组 seed（2026-09-01）：延续组内继承 +3/镜防画风漂移——拆解时算好
    存 shots.seed；无值（老项目/未重拆）随机兜底。"""
    try:
        s = shot["seed"] if shot is not None and "seed" in shot.keys() else None
    except TypeError:
        s = getattr(shot, "seed", None) if shot is not None else None
    return int(s) if s else random.randint(0, 2 ** 31 - 1)


def h3_sla_params(db) -> dict:
    """H3 SLA 注意力（2026-09-10 用户自定义节点 H3SLAAttention）：settings 三键 →
    filler params 键。模板 manifest 声明了注入点才真生效（五个 h3_* 模板已接，
    其余模板 filler 忽略多余键）。部分覆盖/旧库缺键走 .get 缺省。
    2026-09-18 新链：block_size 转 INT（新接口，旧 COMBO 字符串会过不了校验）；
    sage_attention 联动退役——PathchSageAttentionKJ 节点已随 ComfyUI 更新
    淘汰，新链稠密后端由模板内 ModelAttentionBackend 决定。"""
    cfg = get_setting(db, "comfy") or {}
    enabled = bool(cfg.get("h3_sla_enabled", True))
    return {"h3_sla_enabled": enabled,
            "h3_sla_sparsity": cfg.get("h3_sla_sparsity", 0.9),
            "h3_sla_block_size": int(cfg.get("h3_sla_block_size", "64"))}


# 加速 LoRA 双版（2026-09-10 联动）：普通 turbo 按稠密注意力蒸馏、SLA 版按
# 块稀疏蒸馏——开关各配各的才是配对正确；模板 api.json 默认=普通版（无参=历史基线）
SLA_TURBO_LORA = "minimax_h3\\minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors"
PLAIN_TURBO_LORA = "minimax_h3\\minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors"


def h3_lora_link(db, tmpl_id: str) -> dict:
    """加速 LoRA 随 SLA 开关联动：开→SLA 蒸馏版 / 关→普通 turbo（历史基线）。
    settings model_overrides 手动选过该模板的 lora_turbo 槽 → 返回 {} 不注入
    （filler 里参数后于槽注入会覆盖手动选择，手动优先就得缺席参数）。"""
    mo = (get_setting(db, "model_overrides") or {}).get(tmpl_id) or {}
    if "lora_turbo" in mo:
        return {}
    cfg = get_setting(db, "comfy") or {}
    return {"lora_turbo_name": SLA_TURBO_LORA
            if bool(cfg.get("h3_sla_enabled", True)) else PLAIN_TURBO_LORA}


def render_shot(db, data_dir, shot_id, comfy, job_id=None,
                first_frame_png: Path | None = None) -> Path:
    shot = get_shot(db, shot_id)
    proj = get_project(db, shot["project_id"])

    tmpl_id = pick_template_id(shot, db=db)
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    # 类型路由（2026-09-18 重构）：按 manifest type 判断 fl2v/i2v 语义——
    # 此前硬编码 tmpl_id in ("h3_fl2v","h3_i2v")，设置页把 fl2v 映射换成
    # 自定义模板（如 Spectrum 加速道）时关键帧接线/图片槽全走错分支
    tmpl_type = reg[tmpl_id].type if tmpl_id in reg else ""
    # 关键帧接线（方案A 一期）：fl2v 走首尾帧插值；
    # kf_end 缺失时降级 i2v（仅首帧），二期关键帧任务补齐 kf_* 后自动升回
    shot_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}")
    kf_start, kf_end = shot_dir / "kf_start.png", shot_dir / "kf_end.png"
    if tmpl_type == "fl2v" and not (kf_start.exists() and kf_end.exists()):
        try:
            ensure_keyframes(db, data_dir, shot_id, comfy, job_id=job_id)  # 二期：自动补对
        except Exception as exc:
            emit_log(db, "comfy", "warn",
                     f"分镜 {shot['seq']} 关键帧生成失败，降级首帧模式：{exc}",
                     project_id=proj["id"], job_id=job_id)
    if tmpl_type == "fl2v" and not kf_end.exists():
        tmpl_id = pick_template_id({"workflow_type": "i2v"}, db=db)
        tmpl_type = reg[tmpl_id].type if tmpl_id in reg else "i2v"
    template = reg[tmpl_id]

    prompt = shot["prompt"]
    if not prompt:
        raise ValueError("shot prompt 为空")
    if tmpl_type == "fl2v":
        # 官方 FL2VA 对齐头（2026-09-11 借鉴 MiniMax-H3-skills base 指南 §2.1
        # 原句——训练分布内的句式；此前自拟 EXACT 句为猜测近似）+ 镜内禁切约束
        dur = max(4, int(shot["duration"]))
        prompt = ("How the reference pictures align with the target video — "
                  "Picture 1 (from Shot 1) aligns with the 0.00-second mark of "
                  "the target video; Picture 2 (from Shot 1) aligns with the "
                  f"{dur:.2f}-second mark of the target video.\n\n"
                  + prompt + "\n" + KF_NO_CUT)
    elif tmpl_type == "i2v" and (first_frame_png is not None or kf_start.exists()):
        # 官方 I2VA 对齐头（首帧参考注入时）
        prompt = ("For the target video, at 0.00 seconds into the target video, "
                  "<Picture 1> (from [Shot 1]) is fully referenced.\n\n" + prompt)

    params = {
        "seed": _video_seed(shot),
        "megapixels": proj["video_megapixels"],
        "multiple": proj["video_multiple"],
        "duration": max(4, int(shot["duration"])),
        "lora_strength": proj["lora_realism"],
        **h3_sla_params(db),   # SLA 注意力三键（模板声明才注入，2026-09-10）
        **h3_lora_link(db, template.id),   # 加速 LoRA 随开关切换（手动槽优先）
        # RTX Video Super Resolution（2026-09-18）：Spectrum 加速道模板的
        # switch_links 旁路开关；其余模板未声明，filler 自然忽略
        "rtx_vsr": bool((get_setting(db, "comfy") or {}).get("rtx_vsr_enabled", False)),
    }
    # 远景规避：远景/大全景自动升一档兆像素（上限 1.2）
    camera = json.loads(shot["camera_json"] or "{}")
    if camera.get("景别") in ("远景", "大全景"):
        params["megapixels"] = min(1.2, float(proj["video_megapixels"]) + 0.4)
    # 工作流不支持的画幅回落默认 16:9（2026-08-30 用户决策）
    params["aspect"] = ASPECT_ENUM.get(proj["aspect_ratio"], ASPECT_ENUM["16:9"])

    # Images
    # P8-B 漫改模式参考图优先级（2026-08-29 用户需求）：
    # ① 有绑定角色资产+参考图 → 用角色参考图（用户确认过，优先）
    # ② 无角色资产 → 漫画原页直接作参考（兜底，画风+角色=原作）
    comic_mode = proj["comic_mode"] if "comic_mode" in proj.keys() else ""
    images: list | None = None  # 漫改原页兜底可先行赋值——通用链末端不再覆盖
    if comic_mode == "film_adaptation":
        asset_refs = collect_ref_images(db, shot)
        if asset_refs:
            # 用户已提取角色+生成参考图 → 正常 ref2va 流程（走下方通用代码）
            pass
        elif kf_start.exists():
            images = [{"slot": "ref0", "path": str(kf_start)}]
            if kf_end.exists():
                images.append({"slot": "ref1", "path": str(kf_end)})
            emit_log(db, "comfy", "info",
                     f"分镜 {shot['seq']} 漫改模式：无角色资产，漫画原页作参考",
                     project_id=proj["id"], job_id=job_id)
    if first_frame_png is None and tmpl_type in ("fl2v", "i2v") and kf_start.exists():
        first_frame_png = kf_start  # 无上镜衔接时，本镜关键帧首图兜底
    if first_frame_png and tmpl_type in ("fl2v", "i2v"):
        images = [{"slot": "first", "path": str(first_frame_png)}]
    elif shot["workflow_type"] == "t2v" or tmpl_type in ("fl2v", "i2v"):
        images = []  # i2v/fl2v 无首帧——交由 I1 快失败给出明确报错
    elif first_frame_png is not None:
        # 接力（连贯性①配套）：上镜尾帧优先占 ref0（画面/姿态延续），
        # 角色参考占 ref1（锁人设）——全链后 ref2va 的默认形态（优先于原页兜底）
        raw_refs = collect_ref_images(db, shot)
        ref1_path = (data_to_abs(data_dir, raw_refs[0]["path"])
                     if raw_refs else first_frame_png)
        images = [{"slot": "ref0", "path": str(first_frame_png)},
                  {"slot": "ref1", "path": str(ref1_path)}]
    elif images is None:
        raw_refs = collect_ref_images(db, shot)
        if not raw_refs:
            emit_log(db, "comfy", "warn",
                     f"分镜 {shot['seq']} 无参考图，LoadImage 可能失败",
                     project_id=proj["id"], job_id=job_id)
        images = [{"slot": r["slot"],
                   "path": str(data_to_abs(data_dir, r["path"]))}
                  for r in raw_refs]
    if tmpl_type == "fl2v":
        images.append({"slot": "last", "path": str(kf_end)})

    output_ctx = {"project": proj["slug"], "asset": f"shot-{shot['seq']}"}
    model_overrides = (get_setting(db, "model_overrides") or {}).get(template.id)
    # Phase 2 音色（2026-08-30）：说话人音色样本 → ref_audios 槽 + <Audio N> 声明；
    # 注入成功即标 ledger.h3_native_voice（合成时不再 TTS 替换，保 H3 原声口型）
    audio_slots = [i["slot"] for i in (template.inject_images or [])
                   if str(i.get("slot", "")).startswith("audio")]
    voice_entries, voice_decls = _voice_slots_for_shot(db, data_dir, proj, shot,
                                                       audio_slots)
    if voice_entries:
        images = list(images or []) + voice_entries
        prompt = "\n".join(voice_decls) + "\n\n" + prompt
        _mark_native_voice(db, shot)

    wf, uploads = fill_workflow(template, prompt=prompt, params=params,
                                images=images, output_ctx=output_ctx,
                                model_overrides=model_overrides)
    # 2026-09-06 有声2 真机：dialogue 空 → 音色槽不注入 → 模板默认
    # cs_voice_0.mp3 不在 ComfyUI input → 400 全灭。未注入的音频槽补静音
    # 占位（data 缓存生成一次），模板校验必过；有对白时正常注入覆盖
    _injected_names = {u["name"] for u in uploads}
    for spec in (template.inject_images or []):
        if not str(spec.get("slot", "")).startswith("audio"):
            continue
        _node = str(spec["node"])
        _cur = wf.get(_node, {}).get("inputs", {}).get(spec["field"])
        if _cur and _cur not in _injected_names:
            _sil = _silent_placeholder(data_dir)
            uploads.append({"path": str(_sil), "name": _cur})
    if job_id:
        from .jobs import attach_snapshot
        attach_snapshot(db, job_id, prompt=prompt, workflow=wf, template_id=template.id)

    # I1: 若模板声明图片槽但上传清单为空，快失败（只看图片类条目——
    # 2026-09-06 静音占位会补音频条目，不能因此绕过空图检查）
    _img_names = {u["name"] for u in uploads} - {
        wf.get(str(sp["node"]), {}).get("inputs", {}).get(sp["field"])
        for sp in (template.inject_images or [])
        if str(sp.get("slot", "")).startswith("audio")}
    if template.inject_images and not _img_names:
        raise ValueError(
            f"模板 {template.id} 需要图片输入但未提供"
            f"（镜头 {shot['seq']} 的资产无参考图或衔接首帧缺失）")

    for up in uploads:
        comfy.upload_media(Path(up["path"]), up["name"])

    prompt_id = comfy.submit(wf, client_id=f"cs-shot-{shot_id}")
    update_shot(db, shot_id, {"status": "渲染中"})  # 阶段状态
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
    # M11：本镜模板无音频槽（t2v/fl2v/i2v）→ 清 h3_native_voice（换模板重渲
    # 后不再残留「保原声」标记导致该镜永远无声）
    try:
        from .workflows.registry import TEMPLATE_ROOT, scan_templates
        _has_audio = any(str(i.get("slot", "")).startswith("audio")
                         for i in (scan_templates(TEMPLATE_ROOT)
                                   [pick_template_id(shot)].inject_images or []))
    except Exception:
        _has_audio = True  # 探测失败不清（保守）
    if not _has_audio:
        _clear_native_voice(db, shot)
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
        # 接力门控（真机 2026-08-26）：上镜无本镜角色时跳过尾帧衔接——
        # 否则新角色被无关节奏帧锚定而漂移，且接力链会传播漂移画风
        _cur = json.loads(shot["ledger_json"] or "{}").get("assets", {}) or {}
        _prev = json.loads(prev_shot["ledger_json"] or "{}").get("assets", {}) or {} \
            if prev_shot else {}
        _cur_chars = set(_cur.get("characters") or [])
        _prev_chars = set(_prev.get("characters") or [])
        if _cur_chars and not (_cur_chars & _prev_chars):
            emit_log(db, "comfy", "info",
                     f"分镜 {shot['seq']} 含上镜未出场角色，跳过尾帧衔接（防漂移）",
                     project_id=proj["id"], job_id=job["id"])
            prev_shot = None
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

    # 阶段状态（2026-08-26 用户需求：分镜 pill 需区分"生成首尾帧"与"渲染中"）
    if shot["workflow_type"] == "fl2v":
        update_shot(db, shot_id, {"status": "生成首尾帧"})

    dest = render_shot(db, data_dir, shot_id, comfy, job_id=job["id"],
                       first_frame_png=first_frame_png)
    emit_log(db, "comfy", "info", f"分镜 {shot['seq']} gen_shot 完成",
             project_id=proj["id"], job_id=job["id"],
             data={"video_path": str(dest)})
    return dest


def _silent_placeholder(data_dir) -> Path:
    """音频槽静音占位（0.5s 无声 mp3，data/_cache 缓存生成一次）——
    只喂模板校验：无对白镜的 ref2va 音色槽不注入时，模板默认文件名
    必须存在于 input 否则 ComfyUI 400（2026-09-06 有声2 全灭事故）。"""
    import subprocess as _sp
    from .merge import ffmpeg_bin
    f = Path(data_dir) / "_cache" / "silence_500ms.mp3"
    if f.exists():
        return f
    f.parent.mkdir(parents=True, exist_ok=True)
    _sp.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
             "anullsrc=r=24000:cl=mono", "-t", "0.5", "-b:a", "32k", str(f)],
            check=True, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    return f
