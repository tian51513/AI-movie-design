# comic_studio/engine/pageredraw.py
"""动态漫·角色重绘（2026-09-09 设计共识）：首尾帧版本留档/整页重绘/批量编排。

版本机制（镜像 video_v{N} 惯例）：磁盘存 kf_{role}_v{N}.png，活动名
kf_{role}.png 是当前版的拷贝——渲染/fl2v/合成/查看器零改动。
尾帧派生联动：镜 i 尾帧≡镜 i+1 首帧（同一页），切 start 版本自动同步
前镜尾帧并连带置空两镜 video_path（决策 12：kf 变更→待重渲）。"""
import json
import re
from pathlib import Path

from .logbus import emit as emit_log
from .queue.worker import register
from .shots import get_shot, list_shots, update_shot


def kf_versions(shot_dir, role: str) -> list:
    """列 kf_{role}_v{N}.png → ["v1","v2",…]（数字序，防 v10 < v2 字符串序）。"""
    d = Path(shot_dir)
    if not d.is_dir():
        return []
    out = []
    for f in d.iterdir():
        m = re.fullmatch(r"kf_" + role + r"_v(\d+)\.png", f.name)
        if f.is_file() and m:
            out.append((int(m.group(1)), f.name))
    return [f"v{n}" for n, _ in sorted(out)]


def active_kf_version(shot_row, role: str) -> str:
    try:
        led = json.loads(shot_row["ledger_json"] or "{}")
        v = (led.get("kf_active") or {}).get(role)
        if v:
            return str(v)
    except (ValueError, TypeError):
        pass
    return "v1"


def _set_active(db, shot, role: str, version: str) -> None:
    led = json.loads(shot["ledger_json"] or "{}")
    led.setdefault("kf_active", {})[role] = version
    update_shot(db, shot["id"], {"ledger_json": json.dumps(led, ensure_ascii=False)})


def refresh_kf_thumb(shot_dir, role: str) -> Path | None:
    """活动帧缩略图（2026-09-12 性能优化）：胶片条 <img> 此前加载全尺寸
    kf（1-2MB PNG × 729 镜全量入 DOM）——切换版本=重拉+解码大图=卡顿。
    ffmpeg 缩到 360px 宽 jpg（条上秒切；查看器仍用全图）。失败容错返 None
    （调用方回落全图 URL）。每次活动帧被重写后调用（mtime 即缓存戳）。"""
    import subprocess as _sp
    from .merge import ffmpeg_bin
    d = Path(shot_dir)
    src = d / f"kf_{role}.png"
    if not src.is_file():
        return None
    out = d / f"kf_{role}_thumb.jpg"
    try:
        _sp.run([ffmpeg_bin(), "-y", "-i", str(src), "-vf", "scale=360:-2",
                 "-frames:v", "1", "-q:v", "4", str(out)],
                check=True, capture_output=True, timeout=60,
                encoding="utf-8", errors="replace")
        return out if out.is_file() else None
    except Exception:
        return None


def save_kf_version(shot_dir, role: str, src: Path) -> str:
    """新版本落盘（v{max+1}）并刷新活动拷贝（+活动帧缩略图）。返回 "vN"。"""
    d = Path(shot_dir); d.mkdir(parents=True, exist_ok=True)
    n = 0
    for v in kf_versions(d, role):
        n = max(n, int(v[1:]))
    data = Path(src).read_bytes()
    (d / f"kf_{role}_v{n + 1}.png").write_bytes(data)
    (d / f"kf_{role}.png").write_bytes(data)
    refresh_kf_thumb(d, role)
    return f"v{n + 1}"


def _clear_video(db, shot_ids) -> list:
    """决策 12：kf 变更的镜 video_path 置空（文件留盘），status 回待重绘。"""
    out = []
    for sid in shot_ids:
        s = get_shot(db, sid)
        if s is not None and s["video_path"]:
            update_shot(db, sid, {"video_path": None, "status": "ready"})
            out.append(sid)
    return out


def _sync_prev_end(db, data_dir, shot, data: bytes, version: str) -> int | None:
    """尾帧派生联动：镜 i 首帧变更 → 前镜（depends_on 指向者）尾帧同名版本同步。
    返回前镜 id（无前镜/非连续链返回 None）。"""
    if not shot["depends_on"]:
        return None
    prev = get_shot(db, shot["depends_on"])
    if prev is None or prev["project_id"] != shot["project_id"]:
        return None
    from .projects import get_project
    proj = get_project(db, shot["project_id"])
    if proj is None or (proj["comic_mode"] if "comic_mode" in proj.keys() else "") \
            != "motion_comic":
        return None   # 漫改/小说链无翻页联动
    d = Path(data_dir) / "projects" / proj["slug"] / "shots" / str(prev["seq"])
    (d / f"kf_end_{version}.png").write_bytes(data)
    (d / "kf_end.png").write_bytes(data)
    _set_active(db, prev, "end", version)
    return prev["id"]


def activate_kf_version(db, data_dir, shot_id: int, role: str, version: str) -> dict:
    """切活动版本：拷回活动名 + start 联动前镜尾帧 + 双镜视频置空 + ledger 记录。
    尾帧独立切版仅最后一镜（其余镜尾帧=下镜首帧派生，抛 ValueError）。"""
    if role not in ("start", "end"):
        raise ValueError("role 只能是 start/end")
    shot = get_shot(db, shot_id)
    if shot is None:
        raise ValueError(f"分镜不存在: {shot_id}")
    from .projects import get_project
    proj = get_project(db, shot["project_id"])
    shot_dir = Path(data_dir) / "projects" / proj["slug"] / "shots" / str(shot["seq"])
    src = shot_dir / f"kf_{role}_{version}.png"
    if not src.exists():
        raise ValueError(f"版本不存在: {role} {version}")
    if role == "end":
        sibs = [s for s in list_shots(db, shot["project_id"]) if s["seq"] > shot["seq"]]
        if sibs and (proj["comic_mode"] if "comic_mode" in proj.keys() else "") \
                == "motion_comic":
            raise ValueError("动态漫尾帧=下镜首帧派生，请切下镜首帧版本")
    data = src.read_bytes()
    (shot_dir / f"kf_{role}.png").write_bytes(data)
    refresh_kf_thumb(shot_dir, role)
    _set_active(db, shot, role, version)
    cleared = [shot_id]
    if role == "start":
        prev_id = _sync_prev_end(db, data_dir, shot, data, version)
        if prev_id is not None:
            cleared.append(prev_id)
    ids = _clear_video(db, cleared)
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 首尾帧切至 {role} {version}（视频待重渲：{len(ids)} 镜）",
             project_id=shot["project_id"])
    return {"start": active_kf_version(get_shot(db, shot_id), "start"),
            "end": active_kf_version(get_shot(db, shot_id), "end"),
            "video_cleared": ids}


# 页级质量尾缀：借 character 尾缀肢体纠错 + 无乱码文字（决策 9），
# 严禁用 genref.ZIMAGE_TAIL["scene"]——它带「画面中无人物」禁令
PAGE_TAIL = ("，cinematic color grading，sharp focus，ultra-detailed，8k，"
             "避免畸形肢体，避免多余手指，避免五官扭曲，无蜡像塑料感，"
             "无乱码文字，无文字水印，画面完整")

CLEAN_TEXT_LINE = ("。重绘要求：清除对白气泡内的全部文字（保留气泡形状与位置，"
                   "文字区域留白），保留画面构图、人物位置、表情与分格布局不变")


def _ensure_source_page(data_dir, proj, shot) -> Path:
    """重绘底图=原页（决策 2/16）：pages/ 单一事实源；旧项目（PATCH 后开重绘）
    无 pages → 从活动 kf_start 拷贝建源页（bootstrap）。"""
    canonical = Path(data_dir) / "projects" / proj["slug"] / "pages" / \
        f"page_{shot['seq']:03d}.png"
    if not canonical.exists():
        kf = Path(data_dir) / "projects" / proj["slug"] / "shots" / \
            str(shot["seq"]) / "kf_start.png"
        if not kf.exists():
            raise ValueError(f"镜 {shot['seq']} 无原页可作重绘底图")
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(kf.read_bytes())
    return canonical


def build_page_redraw_prompt(db, proj, shot) -> str:
    """整页重绘提示词（F1 2026-09-10 真机四修）：i2i 的画面内容在底图 latent 里
    ——文本只给重绘指令/角色外貌锚/清文字/画风段，**弃用 shot.description**
    （旧版把视频提示词六段脚手架截断塞给图像模型=真机根因③：模型不知保什么、
    不知往哪美化，summary 还是运镜描述）。画风空=按原画风高清化（决策 8）。"""
    from .genref import PHOTO_BOOST, condense_appearance, is_photo_style
    from .assets import get_asset
    led = json.loads(shot["ledger_json"] or "{}")
    bound = list((led.get("assets", {}) or {}).get("characters", []))
    if not bound:
        # 纯场景镜（2026-09-12 真机三修）：零人像词——人物姿态/肢体/五官
        # 词都在暗示模型造人（桌子镜真机判例），尾帧有人物不影响首帧重绘
        prompt = ("高质量重绘这张漫画页：保持原有分格构图、场景布局、物体位置"
                  "与透视关系完全一致，仅提升画质清晰度、光影与细节。"
                  "严禁添加任何人物、角色或人脸——本画面中没有人物")
    else:
        prompt = ("高质量重绘这张漫画页：保持原有分格构图、人物姿态、位置与表情"
                  "与参考图完全一致，仅提升画质清晰度、光影与细节")
        # 角色外貌文字锚（首个绑定角色；IP-Adapter 图像锚走工作流 char_ref 槽）
        for aid in bound:
            a = get_asset(db, aid)
            if a:
                app = condense_appearance(json.loads(a["appearance_json"]).get("detail", ""))
                if app:
                    prompt += f"。角色「{a['name']}」：{app[:150]}"
                break
    prompt += CLEAN_TEXT_LINE
    style = ((proj["style_vis"] or proj["style"]) or "").strip().rstrip("。；;，,")
    if style:
        prompt += f"。画风：{style}（全页统一转为该画风）"
    else:
        prompt += "。按原页画风高清化重绘（不改变画风，提升清晰度与质感统一）"
    if is_photo_style(style):
        prompt += "。" + PHOTO_BOOST
    if not bound:
        # 场景尾缀：剥离人像纠错词（避免畸形肢体/五官扭曲暗示画面应有肢体五官）
        return prompt + ("，cinematic color grading，sharp focus，ultra-detailed，"
                         "8k，无乱码文字，无文字水印，画面完整")
    return prompt + PAGE_TAIL


def _bound_char_refs(db, data_dir, shot, limit: int) -> list:
    """该镜绑定角色 → [(name, 明细, main.png 绝对路径)]，取有主图的前 limit 个。
    v6/v7 共用（v6.1 真机：大众特征须带明细锚定）。"""
    from .assets import get_asset
    from .paths import data_to_abs

    def _ref_trait(detail: str) -> str:
        """参考锚明细：直取外貌行的值（跳过性别/无值行）——condense_
        appearance 会把发型/服装压掉只剩性别锚，锚不住大众特征。"""
        parts = []
        for ln in (detail or "").splitlines():
            if "：" not in ln:
                continue
            k, v = ln.split("：", 1)
            k, v = k.strip(), v.strip()
            if not v or v == "无" or k == "性别":
                continue
            parts.append(v)
        return "、".join(parts)[:60]

    out = []
    led = json.loads(shot["ledger_json"] or "{}")
    for aid in (led.get("assets", {}) or {}).get("characters", []):
        a = get_asset(db, aid)
        if a and a["library_dir"]:
            main = data_to_abs(data_dir, a["library_dir"]) / "main.png"
            if main.exists():
                out.append((a["name"],
                            _ref_trait(json.loads(a["appearance_json"])
                                       .get("detail", "")),
                            str(main)))
        if len(out) >= limit:
            break
    return out


def _extract_first_frame(video: Path, out_png: Path) -> None:
    """ffmpeg 抽视频首帧（v7 重绘抽帧用）——select 滤镜内逗号须反斜杠转义
    （Windows 滤镜判例同款）；capture 统一 utf-8（GBK 判例）。"""
    import subprocess as _sp
    from .merge import ffmpeg_bin
    _sp.run([ffmpeg_bin(), "-y", "-i", str(video),
             "-vf", "select=eq(n\\,0)", "-frames:v", "1", str(out_png)],
            check=True, capture_output=True, timeout=120,
            encoding="utf-8", errors="replace")


def build_page_redraw_h3_prompt(db, proj, shot, char_refs: list) -> str:
    """v7（2026-09-12 用户提案）：H3 一致性重绘提示词——漫改 ref2va 协议
    （<Picture N> 锚 + subject_definitions，项目已验证格式）+ 静态镜头约束
    （抽帧用途：无运镜无切镜、人物仅微动）。"""
    style = ((proj["style_vis"] or proj["style"]) or "").strip().rstrip("。；;，,")
    style_line = f"转为{style}画风与质感" if style else "画质高清化重制（保持原画风）"
    lines = ["subject_definitions:"]
    lines.append("<Picture 1> 是原漫画页，本镜画面内容与构图的参考")
    for k, (name, trait, _p) in enumerate(char_refs, start=2):
        detail = f"（{trait}）" if trait else ""
        lines.append(f"<Subject {k - 1}>（{name}）是来自 <Picture {k}> 的人物，"
                     f"其外观由该图提供{detail}")
    lines.append("summary:")
    lines.append(f"[reference generation] 静态重绘：按 <Picture 1> 的构图与人物"
                 f"位置重绘画面，{style_line}，人物使用参考图外观")
    lines.append("retention_analysis:")
    lines.append("<Picture 1>（构图与分格）：weak_reference - 保持画面布局、"
                 "人物站位、姿态与表情，画风与质感转换")
    for k, (name, _t, _p) in enumerate(char_refs, start=2):
        lines.append(f"<Subject {k - 1}>（{name}）：fully_preserved - "
                     "保持参考图的发型、五官与服装")
    lines.append("detailed_description:")
    lines.append(f"画面为静态重绘结果。[Shot 1] 画面内容与构图严格按 <Picture 1>："
                 "相同的分格布局、人物位置、姿态与表情；所有人物使用对应 "
                 "<Subject N> 参考图的外貌；清除对白气泡内的全部文字"
                 "（保留气泡形状与位置）。The camera is completely static — "
                 "镜头完全静止，人物仅保持姿态与微小呼吸浮动，无运镜无切镜。")
    lines.append("overall_soundscape:")
    lines.append("安静的环境氛围，无对白，无哼唱。")
    lines.append("non_diegetic_music: N/A")
    return "\n".join(lines)


def _redraw_page_h3(db, data_dir, shot, proj, comfy, tmpl, base,
                    job=None, seed=None) -> Path:
    """v7 重绘执行：H3 ref2va 三参考（原页+角色主图）→ 4s 静态短视频 →
    抽首帧 → kf 版本落盘（尾帧联动/视频置空与图像道共用语义）。"""
    from .rendershot import (ASPECT_ENUM, _silent_placeholder, _video_seed,
                             h3_lora_link, h3_sla_params)
    from .settings import get_setting
    from .workflows.filler import fill_workflow
    from .jobs import attach_snapshot

    shot_id = shot["id"]
    seed = seed if seed is not None else _video_seed(shot)
    char_slots = [i["slot"] for i in (tmpl.inject_images or [])[1:]
                  if str(i.get("slot", "")).startswith("char")]
    char_refs = _bound_char_refs(db, data_dir, shot, len(char_slots))
    prompt = build_page_redraw_h3_prompt(db, proj, shot, char_refs)
    images = [{"slot": tmpl.inject_images[0]["slot"], "path": str(base)}]
    for k, slot in enumerate(char_slots):
        # 未绑定槽回填原页（重复参考无语义污染，比白图安全——Edit 道判例）
        images.append({"slot": slot,
                       "path": char_refs[k][2] if k < len(char_refs) else str(base)})
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": seed,
                # 抽帧用途：短视频即可（2026-09-12 用户需求，settings 可调 1~4）
                "duration": int((get_setting(db, "comfy") or {})
                                .get("page_redraw_h3_duration", 2)),
                "aspect": ASPECT_ENUM.get(proj["aspect_ratio"], ASPECT_ENUM["16:9"]),
                "megapixels": proj["video_megapixels"],
                "multiple": proj["video_multiple"],
                "lora_strength": proj["lora_realism"],
                **h3_sla_params(db), **h3_lora_link(db, tmpl.id)},
        images=images,
        output_ctx={"project": proj["slug"], "asset": f"shot-{shot['seq']}-redraw"},
        model_overrides=(get_setting(db, "model_overrides") or {}).get(tmpl.id))
    # 音频槽静音占位（cs_voice 判例：抽帧不用声音，未注入槽补静音过校验）
    _injected = {u["name"] for u in uploads}
    for spec in (tmpl.inject_images or []):
        if not str(spec.get("slot", "")).startswith("audio"):
            continue
        _cur = wf.get(str(spec["node"]), {}).get("inputs", {}).get(spec["field"])
        if _cur and _cur not in _injected:
            uploads.append({"path": str(_silent_placeholder(data_dir)), "name": _cur})
    for up in uploads:
        comfy.upload_media(Path(up["path"]), up["name"])
    if job is not None:
        attach_snapshot(db, job["id"], prompt=prompt, workflow=wf, template_id=tmpl.id)
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 整页重绘提交（模板 {tmpl.id}，H3 抽帧）",
             project_id=proj["id"], job_id=job["id"] if job else None)
    results = comfy.wait_and_collect(
        comfy.submit(wf, client_id=f"cs-redraw-{shot_id}"), stall_seconds=900)
    video = next((r for r in results if r.get("_kind") == "video"), None)
    if video is None:
        raise RuntimeError(f"分镜 {shot['seq']} H3 重绘未返回视频")
    shot_dir = Path(data_dir) / "projects" / proj["slug"] / "shots" / str(shot["seq"])
    shot_dir.mkdir(parents=True, exist_ok=True)
    tmp_mp4 = shot_dir / f"_redraw_tmp_{shot_id}.mp4"
    tmp_png = shot_dir / f"_redraw_tmp_{shot_id}.png"
    comfy.download(video["filename"], video.get("subfolder", ""),
                   video.get("type", "output"), tmp_mp4)
    try:
        _extract_first_frame(tmp_mp4, tmp_png)
        version = save_kf_version(shot_dir, "start", tmp_png)
    finally:
        tmp_mp4.unlink(missing_ok=True)
        tmp_png.unlink(missing_ok=True)
    _set_active(db, shot, "start", version)
    data = (shot_dir / "kf_start.png").read_bytes()
    cleared = [shot_id]
    prev_id = _sync_prev_end(db, data_dir, shot, data, version)
    if prev_id is not None:
        cleared.append(prev_id)
    _clear_video(db, cleared)
    return shot_dir / f"kf_start_{version}.png"


def redraw_page(db, data_dir, shot_id, comfy, job=None, seed=None) -> Path:
    """整页重绘一镜首帧：base=原页 → 新版本 → 激活 → 前镜尾帧联动 → 视频置空。
    模板=template_map.page_redraw——按类型分道：ref2va=H3 抽帧道（v7），
    其余=图像编辑道（v4/v5/v6）。"""
    from .projects import get_project
    from .rendershot import _video_seed
    from .settings import get_setting
    from .workflows.filler import fill_workflow
    from .workflows.registry import resolve_template
    from .jobs import attach_snapshot

    shot = get_shot(db, shot_id)
    if shot is None:
        raise ValueError(f"分镜不存在: {shot_id}")
    proj = get_project(db, shot["project_id"])
    base = _ensure_source_page(data_dir, proj, shot)
    tmpl = resolve_template(db, "page_redraw")
    if tmpl.type == "ref2va":
        return _redraw_page_h3(db, data_dir, shot, proj, comfy, tmpl, base,
                               job=job, seed=seed)
    images = [{"slot": tmpl.inject_images[0]["slot"], "path": str(base)}]
    # v3（2026-09-11）：线稿 ControlNet 版——结构由 CN 条件锁（提线稿在模板链内），
    # 提示词专注风格化/清文字/画质；v1 的「布局一致」句让位给结构条件
    seed = seed if seed is not None else _video_seed(shot)
    prompt = build_page_redraw_prompt(db, proj, shot)
    # v6 多角色槽（2026-09-12）：模板声明 char1..N 槽时注入该镜绑定角色主图
    # （Edit-2511 多图协议——参考到人的对应是模型原生能力，RC5 的 IP-Adapter
    # 全局盖章教训的正解）；空槽 1x1 白图占位（PlusPro image2-5 全必填）；
    # **v4/v5 模板无 char 槽——此块零执行，其他路线零影响**
    char_slots = [i["slot"] for i in (tmpl.inject_images or [])[1:]
                  if str(i.get("slot", "")).startswith("char")]
    if char_slots:
        from .assets import get_asset
        from .paths import data_to_abs

        def _ref_trait(detail: str) -> str:
            """参考锚明细：直取外貌行的值（跳过性别/无值行）——condense_
            appearance 会把发型/服装压掉只剩性别锚，锚不住大众特征。"""
            parts = []
            for ln in (detail or "").splitlines():
                if "：" not in ln:
                    continue
                k, v = ln.split("：", 1)
                k, v = k.strip(), v.strip()
                if not v or v == "无" or k == "性别":
                    continue
                parts.append(v)
            return "、".join(parts)[:60]

        refs, names, traits = [], [], []
        led = json.loads(shot["ledger_json"] or "{}")
        all_bound = list((led.get("assets", {}) or {}).get("characters", []))
        # 多人镜不给参考图（2026-09-12 真机三修）：Krea2 双图=单人合成
        # （把图2的人放进图1场景）——多人镜给了单人参考 → 其他人被丢。
        # 仅当模板 char 槽装不下全部绑定角色时跳过（Krea2=1 槽 vs 2+ 角色）；
        # 多槽模板（MR 的 char1+char2）正常逐槽注入
        multi = len(all_bound) > len(char_slots)
        if not multi:
            for aid in all_bound:
                a = get_asset(db, aid)
                if a and a["library_dir"]:
                    main = data_to_abs(data_dir, a["library_dir"]) / "main.png"
                    if main.exists():
                        refs.append(str(main))
                        names.append(a["name"])
                        traits.append(_ref_trait(
                            json.loads(a["appearance_json"]).get("detail", "")))
                if len(refs) >= len(char_slots):
                    break
        for k, slot in enumerate(char_slots):
            if k < len(refs):
                images.append({"slot": slot, "path": refs[k]})
                # 图N 从 2 起数（图1=原页）；指令式措辞（真机对照实验：
                # 指令式参考参与度明显高于陈述式）
                detail = f"（{traits[k]}）" if traits[k] else ""
                # 条件式措辞（2026-09-12 真机二连修）：①画面无人→不添加
                # （桌子镜凭空造人）②多人→只改对应角色其余保持（两人镜丢
                # 男性）③部分露出的人物也不得删（男主只露下半身真机判例）
                prompt += (f"。若图1中存在「{names[k]}」人物，"
                           f"则仅将其面部特征与发型{detail}调整为与图{k + 2}参考图一致。"
                           "图1中的所有其他人物——包括仅部分露出的人物（如只显示下半身、"
                           "手部、背影或侧面的人物）——必须完整保留在原位置，"
                           "不得删除、隐藏、合并或补全；背景与构图保持完全不变；"
                           "若图1中无任何人物则不添加人物")
            else:
                # 多人镜/无绑定：纯白图（2026-09-12 真机四修：原页回填=双图
                # 模式在 image2 里看到人脸 → 复制成 3 个头像；白图无人脸
                # → identity LoRA 无东西可注入 → 纯场景风格转换）
                images.append({"slot": slot, "path": str(_blank_ref_png(data_dir))})
    # v3 线稿 ControlNet 版画幅链：ResolutionSelector 三必填（2026-09-11 真机
    # 400——manifest 声明了注入点但 params 没带键=required_input_missing）
    from .rendershot import ASPECT_ENUM
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": seed,
                "denoise": (get_setting(db, "comfy") or {}).get("page_redraw_denoise", 1.0),
                "aspect": ASPECT_ENUM.get(proj["aspect_ratio"], ASPECT_ENUM["16:9"]),
                "megapixels": proj["video_megapixels"],
                "multiple": proj["video_multiple"]},
        images=images,
        output_ctx={"project": proj["slug"], "asset": f"shot-{shot['seq']}-redraw"},
        model_overrides=(get_setting(db, "model_overrides") or {}).get(tmpl.id))
    for up in uploads:
        comfy.upload_image(Path(up["path"]), up["name"])
    if job is not None:
        attach_snapshot(db, job["id"], prompt=prompt, workflow=wf, template_id=tmpl.id)
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 整页重绘提交（模板 {tmpl.id}）",
             project_id=proj["id"], job_id=job["id"] if job else None)
    results = comfy.wait_and_collect(
        comfy.submit(wf, client_id=f"cs-redraw-{shot_id}"), stall_seconds=600)
    img = next((r for r in results if r.get("_kind") == "image"), None)
    if img is None:
        raise RuntimeError(f"分镜 {shot['seq']} 整页重绘未返回图片")
    shot_dir = Path(data_dir) / "projects" / proj["slug"] / "shots" / str(shot["seq"])
    tmp = shot_dir / f"_redraw_tmp_{shot_id}.png"
    comfy.download(img["filename"], img.get("subfolder", ""), img.get("type", "output"),
                   tmp)
    try:
        version = save_kf_version(shot_dir, "start", tmp)
    finally:
        tmp.unlink(missing_ok=True)
    _set_active(db, shot, "start", version)
    data = (shot_dir / "kf_start.png").read_bytes()
    cleared = [shot_id]
    prev_id = _sync_prev_end(db, data_dir, shot, data, version)
    if prev_id is not None:
        cleared.append(prev_id)
    _clear_video(db, cleared)
    return shot_dir / f"kf_start_{version}.png"


def require_redraw_project(db, project_id):
    """批量重绘门禁：动态漫 + 已开角色重绘，否则 ValueError（路由转 422）。
    通过则返回项目行。"""
    from .projects import get_project
    proj = get_project(db, project_id)
    if proj is None \
            or (proj["comic_mode"] if "comic_mode" in proj.keys() else "") \
            != "motion_comic" \
            or not ("redraw_characters" in proj.keys() and proj["redraw_characters"]):
        raise ValueError("仅动态漫+开启重绘的项目可批量重绘分镜")
    return proj


# 发色近义归一（2026-09-10 用户确认）：黑白/低饱和页的浅金发常被 VLM 读成
# 白发（寝取美人妻 729 镜实测 白发女子 24 处与金发同人）；银发同源
_HAIR_ALIAS = {"白发": "金发", "银发": "金发"}


def _blank_ref_png(data_dir) -> Path:
    """1x1 白图占位（v6）：PlusPro image2-5 全必填，未绑定角色的槽注白图
    （data/_cache 缓存生成一次；纯 stdlib 手craft PNG，不引 PIL）。"""
    import struct as _struct
    import zlib as _zlib
    f = Path(data_dir) / "_cache" / "blank_ref.png"
    if f.exists():
        return f
    f.parent.mkdir(parents=True, exist_ok=True)

    def _chunk(tag, data):
        c = tag + data
        return (_struct.pack(">I", len(data)) + c
                + _struct.pack(">I", _zlib.crc32(c) & 0xffffffff))

    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", _struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", _zlib.compress(b"\x00\xff\xff\xff"))
           + _chunk(b"IEND", b""))
    f.write_bytes(png)
    return f


def bind_redraw_characters(db, project_id) -> int:
    """重绘链专用补绑（F2b+ 2026-09-10 真机）：资产名**变体归一**匹配——
    ① 全名精确；② 去末字前缀（资产名 ≥3 字：黑发女性↔黑发女子/黑发女——
    VLM 的 性/子 尾字摇摆）；③ 发色近义归一（白发/银发→金发）。
    2 字资产名只走精确（防 小红→小* 误绑）。只在批量重绘入口调用，
    通用 auto_bind_characters（小说/漫改）语义不动。"""
    from .assets import list_project_assets
    from .shots import list_shots

    def _norm_asset(name: str) -> str:
        for a, b in _HAIR_ALIAS.items():
            name = name.replace(a, b)
        return name[:-1] if len(name) >= 3 else name   # 黑发女性→黑发女

    chars = [(a["id"], _norm_asset(a["name"]))
             for a in list_project_assets(db, project_id)
             if a["kind"] == "character"]
    if not chars:
        return 0
    conn = db.connect()
    bound = 0
    for shot in list_shots(db, project_id):
        desc = shot["description"] or ""
        for a, b in _HAIR_ALIAS.items():
            desc = desc.replace(a, b)
        ledger = json.loads(shot["ledger_json"] or "{}")
        assets = ledger.setdefault("assets", {})
        cur = set(assets.get("characters") or [])
        for cid, cname in chars:
            if cid not in cur and cname in desc:
                cur.add(cid)
                bound += 1
        if cur != set(assets.get("characters") or []):
            assets["characters"] = sorted(cur)
            conn.execute("UPDATE shots SET ledger_json=? WHERE id=?",
                         (json.dumps(ledger, ensure_ascii=False), shot["id"]))
    conn.commit()
    return bound


def enqueue_batch_redraw(db, data_dir, project_id) -> int:
    """「批量重绘分镜」：标记 pending_redraw → 逐镜入队 gpu_comfy（决策 11 逐镜
    job：温和停止可停/单镜失败不殃及）→ redraw_done=1。
    统一零-pending 规则（终审 2026-09-09）：当前没有任何镜带 pending 标记 →
    视为全新整批（全部非禁用镜重新标记+入队，不论 redraw_done——purge→重提取
    →重批量 与 换画风→整批重做 的正当入口，前端有确认框守护）；仍有镜带标记
    → 补漏（只入队带标记的镜，已完成镜不重复）。批次在飞中重发：在飞镜的
    标记未清，落补漏路径，只对未提交的镜补队，无整批重复。"""
    from .jobs import enqueue_job
    from .settings import ensure_comfy_configured
    import random
    require_redraw_project(db, project_id)
    ensure_comfy_configured(db)   # 409 门禁由路由转 HTTPException
    # F2b（2026-09-10 真机四修）：入口补跑变体归一补绑——真机根因①读图 VLM
    # 每页自由命名（黑发女子/白发女子…），全名精确匹配覆盖率仅两三成
    bind_redraw_characters(db, project_id)
    shots = [s for s in list_shots(db, project_id) if not s["disabled"]]

    def _pending(s) -> bool:
        try:
            return bool(json.loads(s["ledger_json"] or "{}").get("pending_redraw"))
        except (ValueError, TypeError):
            return False

    fresh = not any(_pending(s) for s in shots)   # 零 pending = 全新整批
    # F3：同批同 seed（根因②风格漂移）；**补漏/中断续跑复用在飞 seed**
    # （每点击一次随机新 seed 会让补漏镜与整批风格分裂——真机二轮发现）
    batch_seed = None
    row = db.connect().execute(
        "SELECT payload_json FROM jobs WHERE project_id=? AND type='redraw_kf' "
        "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1",
        (project_id,)).fetchone()
    if row:
        try:
            batch_seed = json.loads(row["payload_json"]).get("seed")
        except (ValueError, TypeError):
            batch_seed = None
    if batch_seed is None:
        batch_seed = random.randint(0, 2 ** 31 - 1)
    n = 0
    for s in shots:
        led = json.loads(s["ledger_json"] or "{}")
        if not led.get("pending_redraw"):
            if not fresh:
                continue   # 补漏：已完成（标记已清）的镜不再重绘
            led["pending_redraw"] = True
            update_shot(db, s["id"],
                        {"ledger_json": json.dumps(led, ensure_ascii=False)})
        enqueue_job(db, "redraw_kf", project_id=project_id, shot_id=s["id"],
                    resource="gpu_comfy",
                    payload={"shot_id": s["id"], "seed": batch_seed})
        n += 1
    conn = db.connect()
    conn.execute("UPDATE projects SET redraw_done=1 WHERE id=?", (project_id,))
    conn.commit()
    emit_log(db, "comfy", "info", f"批量重绘分镜：入队 {n} 镜（整页重绘）",
             project_id=project_id)
    return n


@register("redraw_kf")
def handle_redraw_kf(db, data_dir, job, comfy):
    payload = json.loads(job["payload_json"] or "{}")
    shot = get_shot(db, payload["shot_id"])
    if shot is None:
        raise ValueError("分镜已删除（redraw_kf 任务）")
    dest = redraw_page(db, data_dir, payload["shot_id"], comfy, job=job,
                       seed=payload.get("seed"))
    # 重读再清标记：redraw_page 经 _set_active 改过 ledger，勿拿旧行覆写
    fresh = get_shot(db, payload["shot_id"])
    led = json.loads(fresh["ledger_json"] or "{}")
    led.pop("pending_redraw", None)   # 完成清标记（重发不重复入队的判据）
    update_shot(db, payload["shot_id"],
                {"ledger_json": json.dumps(led, ensure_ascii=False)})
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 整页重绘完成：{dest.name}",
             project_id=job["project_id"], job_id=job["id"],
             data={"path": str(dest)})
    return dest
